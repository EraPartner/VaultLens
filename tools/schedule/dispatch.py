#!/usr/bin/env python3
"""Brain scheduled-agent dispatcher.

A host-side catch-up dispatcher fired by a launchd LaunchAgent at a few calendar
anchors spanning the run windows (see com.brain.schedule.plist). Each run it asks,
per step: is this due, am I in its window, and do its gates pass? If yes, run it;
record the result. Missed anchors are rerun by launchd on the next wake, so
sleep / offline / closed-lid become non-events. Full design: tools/schedule/SPEC.md.

stdlib only (matches the rest of tools/). Pure decision helpers (classify_failure,
backend_available, mark_limited, step_due) are kept side-effect-free so
tools/tests/test_schedule.py can exercise them without touching the system.

Usage:
    python3 tools/schedule/dispatch.py run        # one dispatcher tick (what launchd calls)
    python3 tools/schedule/dispatch.py run --dry-run
    python3 tools/schedule/dispatch.py status      # human-readable ledger view
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parents[2]          # the vault root
HOME = Path.home()
STATE_DIR = HOME / ".brain"                          # outside iCloud (no sync conflicts)
STATE_FILE = STATE_DIR / "schedule-state.json"
LOCK_FILE = STATE_DIR / "schedule.lock"
LOG_DIR = STATE_DIR / "logs"
REPORTS_DIR = ROOT / "wiki" / "reports"

# The LLM backend: the logged-in Claude CLI on the user's Claude-plan
# subscription. A single backend (was two failover-ed copilot GitHub accounts
# until 2026-06-02). A usage-limit error puts it on a cooldown (mark_limited) that
# defers the rest of the LLM batch; backend_available reports whether the cooldown
# has expired. ACCOUNTS stays a list as the seam for a future second identity.
ACCOUNTS = ["claude-plan"]

# Backend pinned per SPEC: Claude CLI on the Claude plan, model `sonnet`.
# (Was copilot/gpt-5.2 until 2026-06-02; copilot accounts are no longer usable.)
CLI = "claude"
MODEL = "sonnet"

# Windows are [start_hour, end_hour). Generous so a morning wake still catches a
# missed 03:00 batch (the ledger makes it run at most once/day either way).
NIGHTLY_WINDOW = (2, 11)
MORNING_WINDOW = (7, 12)
MIN_BATTERY_PCT = 20
# Notify once when a job has failed this many runs in a row. A deterministic
# config error (e.g. a misrouted agent exiting 2) otherwise retries every tick
# forever, surfacing only in schedule-status; this raises one alarm.
FAIL_STREAK_ALERT = 3
# Keep the latest N dated scheduled-<type> reports per type; older ones are
# pruned each tick so wiki/reports/ does not pile up. The CoS is read-only, so
# this hygiene runs host-side in the dispatcher that writes the reports.
REPORT_RETENTION = 14

# Backoff for short rate-limits (seconds): 30m -> 1h -> 2h (capped). Monthly quota
# uses a flat ~24h re-probe (we don't try to compute the exact reset).
RATELIMIT_BACKOFF_CAP = 2 * 3600
QUOTA_COOLDOWN = 24 * 3600

# Tool resolution (launchd runs with a minimal PATH; resolve defensively).
def _tool(name: str, *fallbacks: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for f in fallbacks:
        if Path(f).exists():
            return f
    return name


PYTHON = sys.executable or _tool("python3", "/opt/homebrew/bin/python3")
FISH = _tool("fish", "/opt/homebrew/bin/fish")
CONTAINER = _tool("container", "/usr/local/bin/container")
NC = _tool("nc", "/opt/homebrew/bin/nc", "/usr/bin/nc")
PMSET = _tool("pmset", "/usr/bin/pmset")
OSASCRIPT = _tool("osascript", "/usr/bin/osascript")
BRCTL = _tool("brctl", "/usr/bin/brctl")
IOREG = _tool("ioreg", "/usr/sbin/ioreg")
SUDO = _tool("sudo", "/usr/bin/sudo")


# --------------------------------------------------------------------------- #
# Time helpers (local, tz-aware so stored/compared values are consistent)
# --------------------------------------------------------------------------- #

def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def in_window(now: datetime, window: tuple[int, int]) -> bool:
    return window[0] <= now.hour < window[1]


# --------------------------------------------------------------------------- #
# Ledger (per-step last_ok + per-account cooldown)
# --------------------------------------------------------------------------- #

def load_ledger() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    data.setdefault("jobs", {})
    data.setdefault("accounts", {})
    for acct in ACCOUNTS:
        data["accounts"].setdefault(acct, {"limited_until": None, "last_error": None, "backoff": 0})
    return data


def save_ledger(ledger: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# Pure decision helpers (unit-tested)
# --------------------------------------------------------------------------- #

def classify_failure(returncode: int, text: str) -> str:
    """Map a CLI exit into one of: ok | quota | ratelimit | transient."""
    if returncode == 0:
        return "ok"
    t = text.lower()
    if any(s in t for s in ("quota", "premium request", "monthly limit", "upgrade your plan", "usage limit")):
        return "quota"
    if any(s in t for s in ("rate limit", "rate-limit", "429", "too many requests")):
        return "ratelimit"
    return "transient"


def backend_available(ledger: dict, now: datetime) -> bool:
    """True if the Claude backend is not currently in a usage-limit cooldown."""
    st = ledger["accounts"].get(ACCOUNTS[0], {})
    lu = st.get("limited_until")
    return not lu or parse(lu) <= now


def mark_limited(ledger: dict, acct: str, cls: str, now: datetime) -> None:
    """Record a rate-limit/quota hit and set the cooldown for this account."""
    st = ledger["accounts"].setdefault(acct, {})
    if cls == "quota":
        cooldown = QUOTA_COOLDOWN
    else:  # ratelimit -> exponential backoff
        prev = st.get("backoff") or 0
        cooldown = min(prev * 2 if prev else 1800, RATELIMIT_BACKOFF_CAP)
        st["backoff"] = cooldown
    st["limited_until"] = iso(now + timedelta(seconds=cooldown))
    st["last_error"] = cls


def clear_account(ledger: dict, acct: str) -> None:
    st = ledger["accounts"].setdefault(acct, {})
    st["backoff"] = 0
    st["last_error"] = None
    # a success means the backend is healthy again: clear the cooldown outright
    st["limited_until"] = None


def step_due(step: "Step", ledger: dict, now: datetime) -> bool:
    """Whether a step is due now (window + cadence vs last success)."""
    if not in_window(now, step.window):
        return False
    rec = ledger["jobs"].get(step.name, {})
    last = rec.get("last_ok")
    last_dt = parse(last) if last else None
    if step.period == "daily":
        return not (last_dt and last_dt.date() == now.date())
    if step.period == "weekly":
        if last_dt is None:
            return True  # first run on the first eligible nightly window
        age = (now - last_dt).days
        if age < 7:
            return False
        if now.weekday() == 6:  # prefer Sunday
            return True
        return age >= 8  # missed Sunday -> catch up the next eligible night
    return False


# --------------------------------------------------------------------------- #
# Step model
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    name: str
    kind: str                       # "host" (wiki.py, offline) | "llm" (brain-wiki claude)
    period: str                     # "daily" | "weekly"
    window: tuple[int, int]
    gates: list[str]                # subset of {"ac","online","container","icloud","battery"}
    builder: Callable[[], list[list[str]]]   # -> list of arg-vectors ([] = nothing to do)
    effort: str = "low"
    timeout: int = 1800
    report: bool = False            # capture stdout into wiki/reports/


def _slugify(text: str) -> str:
    """Lowercase-hyphen slug matching the wiki's source-page / source-text naming."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _select_ingest_pdfs(pdf_names: list[str], text_stems: set[str], ingested_pdf_names: set[str]) -> list[str]:
    """Pure core of _ingest_targets: which source PDFs still need ingesting.

    A PDF is skipped when either signal says it is already processed:
      * a wiki/sources page already cites it (authoritative: the page is the
        product of ingest, so its presence means ingest ran), or
      * extracted text exists under its literal stem OR its slug. raw/sources-text
        holds both naming conventions (preprocess writes the literal `<stem>.md`;
        the agent PDF pre-extraction writes a slugified name), so checking only one
        made the nightly batch re-ingest a fully-captured book every night.
    Side-effect-free so tools/tests/test_schedule.py can exercise it.
    """
    out: list[str] = []
    for name in pdf_names:
        if name in ingested_pdf_names:
            continue
        stem = name[:-4] if name.lower().endswith(".pdf") else name
        if stem in text_stems or _slugify(stem) in text_stems:
            continue
        out.append(name)
    return out


def _ingested_pdf_names() -> set[str]:
    """PDF basenames already cited by a wiki/sources page via raw/sources/<name>.pdf.

    Matches the wikilink ([[raw/sources/Foo.pdf]]) and markdown-link forms, anchoring
    on the `raw/sources/` segment so a relative-path mirror still resolves.
    """
    names: set[str] = set()
    srcdir = ROOT / "wiki" / "sources"
    if srcdir.is_dir():
        pat = re.compile(r"raw/sources/([^\]|>)\n]+\.pdf)", re.IGNORECASE)
        for page in srcdir.glob("*.md"):
            try:
                text = page.read_text(encoding="utf-8")
            except OSError:
                continue
            names.update(m.group(1).strip() for m in pat.finditer(text))
    return names


def _ingest_targets() -> list[list[str]]:
    """Unprocessed raw material: inbox files + not-yet-ingested source PDFs.

    See _select_ingest_pdfs for the (pure, tested) rule that decides when a source
    PDF still needs ingesting; this wrapper just supplies the filesystem facts.
    """
    targets: list[Path] = []
    inbox = ROOT / "raw" / "inbox"
    if inbox.is_dir():
        targets += [p for p in sorted(inbox.iterdir()) if p.is_file() and not p.name.startswith(".")]
    srcs = ROOT / "raw" / "sources"
    textdir = ROOT / "raw" / "sources-text"
    if srcs.is_dir():
        pdf_names = [p.name for p in sorted(srcs.glob("*.pdf"))]
        text_stems = {p.stem for p in textdir.glob("*.md")} if textdir.is_dir() else set()
        selected = _select_ingest_pdfs(pdf_names, text_stems, _ingested_pdf_names())
        targets += [srcs / name for name in selected]
    return [["ingest", "--source", str(p)] for p in targets[:3]]  # cap per night


def build_steps() -> list[Step]:
    """Ordered step list. The nightly batch is just the nightly-window steps run
    in this order; cos-brief is the lone morning step."""
    return [
        # 1. maintenance: offline, host-native, runs even on a battery night.
        Step("lint", "host", "daily", NIGHTLY_WINDOW, [],
             lambda: [["lint"]], timeout=600),
        Step("index", "host", "daily", NIGHTLY_WINDOW, [],
             lambda: [["index", "--rebuild"]], timeout=600),
        # 2. ingest new raw material (only if any), before enhance.
        Step("ingest", "llm", "daily", NIGHTLY_WINDOW, ["ac", "online", "container", "icloud"],
             _ingest_targets, effort="low", timeout=2400),
        # 3. enhance, capped at 10 iterations/night (the biggest budget consumer).
        Step("enhance", "llm", "daily", NIGHTLY_WINDOW, ["ac", "online", "container", "icloud"],
             lambda: [["enhance", "--iterations", "10", "--strategy", "alternate"]], effort="low", timeout=7200),
        # 4. weekly thinking digests (prefer Sunday); reports filed for you.
        Step("contradict", "llm", "weekly", NIGHTLY_WINDOW, ["ac", "online", "container", "icloud"],
             lambda: [["contradict"]], effort="high", timeout=2400, report=True),
        Step("emerge", "llm", "weekly", NIGHTLY_WINDOW, ["ac", "online", "container", "icloud"],
             lambda: [["emerge"]], effort="high", timeout=2400, report=True),
        Step("discover", "llm", "weekly", NIGHTLY_WINDOW, ["ac", "online", "container", "icloud"],
             lambda: [["discover"]], effort="high", timeout=2400, report=True),
        # morning: daily chief-of-staff brief (battery OK, no AC gate).
        Step("cos-brief", "llm", "daily", MORNING_WINDOW, ["online", "container", "icloud"],
             lambda: [["cos", "--mode", "brief"]], effort="low", timeout=1800, report=True),
    ]


# --------------------------------------------------------------------------- #
# Gates (system probes, cached per tick)
# --------------------------------------------------------------------------- #

class Gates:
    def __init__(self, log: Callable[[str], None]):
        self.log = log
        self._cache: dict[str, bool] = {}

    def get(self, name: str) -> bool:
        if name not in self._cache:
            self._cache[name] = getattr(self, f"_g_{name}")()
        return self._cache[name]

    def check(self, names: list[str]) -> tuple[bool, str]:
        for n in names:
            if not self.get(n):
                return False, n
        return True, ""

    def _g_online(self) -> bool:
        # Every online-gated step is an LLM job on the Claude CLI, so require the
        # Anthropic endpoint itself: github-up / anthropic-down must not pass.
        return self._nc("api.anthropic.com", 443)

    def _g_container(self) -> bool:
        if self._container_up():
            return True
        self.log("apple/container system not running; starting it")
        try:
            subprocess.run([CONTAINER, "system", "start"], capture_output=True, timeout=60)
        except Exception:
            return False
        for _ in range(12):  # ~60s
            time.sleep(5)
            if self._container_up():
                return True
        return False

    def _g_ac(self) -> bool:
        out = self._pmset_batt()
        return "AC Power" in out

    def _g_battery(self) -> bool:
        out = self._pmset_batt()
        for tok in out.replace(";", " ").split():
            if tok.endswith("%"):
                try:
                    return int(tok[:-1]) >= MIN_BATTERY_PCT
                except ValueError:
                    pass
        return True  # desktop / unknown -> don't block

    def _g_icloud(self) -> bool:
        wiki = ROOT / "wiki"
        if not wiki.is_dir():
            return False
        # Best-effort: ask iCloud to materialise the dirs we touch.
        try:
            subprocess.run([BRCTL, "download", str(REPORTS_DIR)], capture_output=True, timeout=30)
        except Exception:
            pass
        return True

    # raw probes
    def _nc(self, host: str, port: int) -> bool:
        try:
            return subprocess.run([NC, "-z", "-G", "5", host, str(port)],
                                  capture_output=True, timeout=10).returncode == 0
        except Exception:
            return False

    def _container_up(self) -> bool:
        # apple/container runtime (the Docker-free sandbox launcher, bin/agent).
        try:
            return subprocess.run([CONTAINER, "system", "status"],
                                  capture_output=True, timeout=30).returncode == 0
        except Exception:
            return False

    def _pmset_batt(self) -> str:
        try:
            return subprocess.run([PMSET, "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return ""


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def run_host(args: list[str], timeout: int) -> tuple[int, str]:
    cmd = [PYTHON, str(ROOT / "tools" / "wiki.py")] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def exec_brain_wiki(args: list[str], acct: str, effort: str, timeout: int) -> tuple[int, str]:
    inner_parts = ["brain-wiki", *args, "--cli", CLI, "--model", MODEL, "--effort", effort]
    inner = " ".join(_q(p) for p in inner_parts)
    env = dict(os.environ)
    # `acct` is the failover-ledger identity. The Claude CLI authenticates via
    # its own subscription login, so no per-exec env steering is needed; the
    # parameter stays for the multi-identity ledger interface.
    try:
        p = subprocess.run([FISH, "-lc", inner], capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _q(s: str) -> str:
    import shlex
    return shlex.quote(s)


def run_llm(args: list[str], effort: str, timeout: int, ledger: dict, now: datetime,
            log: Callable[[str], None]) -> tuple[str, str, str]:
    """Run one LLM invocation on the single Claude backend.

    Returns (status, backend, output), status in {ok, transient, deferred}. A
    usage-limit (quota/ratelimit) hit, or a cooldown still in effect from an
    earlier hit, defers the job; the cooldown also defers the rest of the LLM
    batch this tick (see _run_steps).
    """
    acct = ACCOUNTS[0]
    if not backend_available(ledger, now):
        return "deferred", acct, ""  # cooldown from an earlier usage-limit
    log(f"    -> backend {acct}")
    rc, out = exec_brain_wiki(args, acct, effort, timeout)
    cls = classify_failure(rc, out)
    if cls == "ok":
        clear_account(ledger, acct)
        return "ok", acct, out
    if cls == "transient":
        log(f"    transient failure on {acct} (rc={rc})")
        return "transient", acct, out
    log(f"    {cls} on {acct}; backend usage-limited, deferring LLM batch")
    mark_limited(ledger, acct, cls, now)
    return "deferred", acct, out


# --------------------------------------------------------------------------- #
# Reports & notifications
# --------------------------------------------------------------------------- #

def write_report(name: str, text: str, now: datetime) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    f = REPORTS_DIR / f"scheduled-{name}-{now:%Y-%m-%d}.md"
    header = (
        "---\n"
        "type: report\n"
        f"title: Scheduled {name} {now:%Y-%m-%d}\n"
        f"created: {now:%Y-%m-%d}\n"
        f"tags: [scheduled, {name}]\n"
        "---\n\n"
    )
    f.write_text(header + text.strip() + "\n")
    return f


STATUS_OK = ("ok", "noop")
# A job is "stale" when its last success predates this many days, per cadence.
_STALE_DAYS = {"daily": 2, "weekly": 9}


def format_schedule_status(
    jobs: dict, accounts: dict, step_meta: list[tuple[str, str]], now: datetime
) -> str:
    """Render a compact scheduler-health summary as markdown. Pure / no I/O.

    `jobs` is the ledger's per-step record, `step_meta` is [(name, period), ...]
    in run order, `accounts` is the cooldown ledger. A job is flagged failing when
    its most recent attempt did not succeed, or stale when its last success is
    older than the period threshold. Tested directly in test_schedule.py.
    """
    failing: list[str] = []
    stale: list[str] = []
    rows: list[str] = []
    for name, period in step_meta:
        rec = jobs.get(name, {})
        last_ok = rec.get("last_ok")
        result = rec.get("last_result")
        ok_dt = parse(last_ok) if last_ok else None
        last_ok_s = ok_dt.strftime("%Y-%m-%d %H:%M") if ok_dt else "never"
        is_fail = result is not None and result not in STATUS_OK
        is_stale = ok_dt is None or (now - ok_dt).days > _STALE_DAYS.get(period, 2)
        streak = rec.get("fail_streak", 0)
        if is_fail:
            failing.append(name)
            health = f"FAIL ({result} x{streak})" if streak > 1 else f"FAIL ({result})"
        elif is_stale:
            stale.append(name)
            health = "stale"
        else:
            health = "ok"
        rows.append(f"| {name} | {period} | {last_ok_s} | {result or 'none'} | {health} |")

    limited = [
        a for a, st in accounts.items()
        if st.get("limited_until") and parse(st["limited_until"]) > now
    ]

    if failing:
        verdict = f"WARNING: {len(failing)} job(s) failing: {', '.join(failing)}"
    elif stale:
        verdict = f"WARNING: {len(stale)} job(s) stale: {', '.join(stale)}"
    else:
        verdict = "OK: all scheduled jobs healthy"

    lines = [
        verdict,
        f"_as of {now:%Y-%m-%d %H:%M}_",
        "",
        "| job | cadence | last success | last result | health |",
        "|---|---|---|---|---|",
        *rows,
    ]
    if limited:
        lines += ["", f"Backend limited (LLM batch deferred): {', '.join(limited)}"]
    return "\n".join(lines)


def write_schedule_status(ledger: dict, steps: list["Step"], now: datetime) -> Path:
    """Mirror the run ledger into the vault as a compact health page.

    The live ledger lives at ~/.brain (outside the vault and the agent sandbox),
    so the CoS cannot read it directly; this rolling page in wiki/reports lets the
    morning brief surface nightly-batch failures.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    body = format_schedule_status(
        ledger.get("jobs", {}),
        ledger.get("accounts", {}),
        [(s.name, s.period) for s in steps],
        now,
    )
    header = (
        "---\n"
        "type: report\n"
        "title: Scheduler status\n"
        "status: active\n"
        f"created: {now:%Y-%m-%d}\n"
        f"updated: {now:%Y-%m-%d}\n"
        "summary: Live health of the nightly scheduled-agent batch: last success and last result per job.\n"
        "tags: [scheduled, status, health]\n"
        "---\n\n"
    )
    f = REPORTS_DIR / "schedule-status.md"
    f.write_text(header + body.strip() + "\n")
    return f


def _reports_to_prune(names: list[str], retention: int) -> list[str]:
    """Pure: of `scheduled-<type>-<date>.md` names, those to delete to keep only
    the latest `retention` per type. Any other filename is ignored, so
    schedule-status.md and hand-written reports are never touched. Tested directly."""
    pat = re.compile(r"^scheduled-(.+)-(\d{4}-\d{2}-\d{2})\.md$")
    groups: dict[str, list[tuple[str, str]]] = {}
    for n in names:
        m = pat.match(n)
        if m:
            groups.setdefault(m.group(1), []).append((m.group(2), n))
    out: list[str] = []
    for items in groups.values():
        items.sort()  # date strings sort chronologically; oldest first
        stale = items[:-retention] if retention > 0 else items
        out.extend(n for _d, n in stale)
    return out


def prune_reports(retention: int = REPORT_RETENTION) -> list[str]:
    """Delete dated scheduled reports beyond the retention window so wiki/reports/
    does not grow without bound. Touches only scheduled-<type>-<date>.md (the
    dispatcher's own outputs), never schedule-status.md or other files. The CoS is
    read-only, so report hygiene lives here, on the host side that writes them."""
    if not REPORTS_DIR.is_dir():
        return []
    names = [f.name for f in REPORTS_DIR.glob("scheduled-*.md")]
    removed: list[str] = []
    for n in _reports_to_prune(names, retention):
        try:
            (REPORTS_DIR / n).unlink()
            removed.append(n)
        except OSError:
            pass
    return removed


def notify(title: str, msg: str) -> None:
    try:
        subprocess.run([OSASCRIPT, "-e",
                        f"display notification {json.dumps(msg)} with title {json.dumps(title)}"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Lid state + AC-gated keep-awake (lid-close override)
# --------------------------------------------------------------------------- #
#
# To run the nightly batch with the lid CLOSED we must override macOS lid-close
# sleep, which only `pmset disablesleep` can do (caffeinate cannot). We engage it
# ONLY when on AC, so the "laptop overheating in a closed bag" case (which is
# always on battery) cannot occur by construction -- AC-gating makes the flag
# safe and makes clamshell mode irrelevant.
#
# The three privileged calls below are the ENTIRE root surface; they map 1:1 to
# the least-privilege sudoers rule in tools/schedule/brain-schedule.sudoers:
#     /usr/bin/pmset -a disablesleep 1
#     /usr/bin/pmset -a disablesleep 0
#     /usr/bin/pmset sleepnow
# `sudo -n` never prompts: if the rule is absent these fail fast and we fall back
# to "won't run reliably lid-closed" rather than hanging.

def lid_closed() -> bool:
    try:
        out = subprocess.run([IOREG, "-r", "-k", "AppleClamshellState"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if "AppleClamshellState" in line:
                return "Yes" in line
    except Exception:
        pass
    return False


def _sudo_pmset(args: list[str]) -> bool:
    try:
        return subprocess.run([SUDO, "-n", PMSET, *args],
                              capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def keepawake_on(log: Callable[[str], None]) -> bool:
    ok = _sudo_pmset(["-a", "disablesleep", "1"])
    log("disablesleep 1 (lid-close override ON)" if ok
        else "WARN: could not set disablesleep -- sudoers rule missing? (won't hold lid-closed)")
    return ok


def keepawake_off(log: Callable[[str], None]) -> None:
    _sudo_pmset(["-a", "disablesleep", "0"])


def sleep_now(log: Callable[[str], None]) -> None:
    log("returning to sleep (pmset sleepnow)")
    _sudo_pmset(["sleepnow"])


# --------------------------------------------------------------------------- #
# Logging & lock
# --------------------------------------------------------------------------- #

def make_logger() -> Callable[[str], None]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logf = LOG_DIR / f"schedule-{now_local():%Y-%m-%d}.log"

    def log(msg: str) -> None:
        line = f"{now_local():%H:%M:%S} {msg}"
        print(line, flush=True)
        try:
            with open(logf, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    return log


def acquire_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _run_steps(steps: list[Step], ledger: dict, gates: "Gates", now: datetime,
               dry_run: bool, log: Callable[[str], None]) -> None:
    """Run every due step in order, honoring gates, account failover, and reports."""
    llm_blocked = False  # set once the backend is usage-limited; skip remaining LLM steps
    for step in steps:
        if not step_due(step, ledger, now):
            continue
        if step.kind == "llm" and llm_blocked:
            log(f"skip {step.name}: LLM jobs blocked this tick (accounts limited)")
            continue
        ok, missing = gates.check(step.gates)
        if not ok:
            log(f"skip {step.name}: gate '{missing}' not satisfied")
            continue
        invocations = step.builder()
        if not invocations:
            log(f"{step.name}: nothing to do; marking done")
            _record(ledger, step.name, now, "noop")
            continue
        if dry_run:
            log(f"WOULD RUN {step.name} ({len(invocations)} invocation(s))")
            continue

        all_ok = True
        outcome = "ok"
        for args in invocations:
            log(f"run {step.name}: {' '.join(args)}")
            if step.kind == "host":
                rc, out = run_host(args, step.timeout)
                if rc == 124:
                    all_ok = False
                    outcome = "timeout"
                    log(f"  {step.name} timed out; will retry next tick")
                else:
                    # rc != 0 from a host tool means "issues found" (e.g. lint
                    # errors), not a dispatcher failure: it ran, so record it
                    # (no overnight retry-spam) but surface the finding once.
                    if rc != 0:
                        log(f"  {step.name} reported issues (rc={rc})")
                        notify("Brain schedule", f"{step.name}: issues found (rc={rc})")
                    if step.report:
                        write_report(step.name, out, now)
            else:  # llm
                status, who, out = run_llm(args, step.effort, step.timeout, ledger, now, log)
                if status == "ok":
                    if step.report:
                        f = write_report(step.name, out, now)
                        notify("Brain schedule", f"{step.name} ready: {f.name}")
                elif status == "deferred":
                    all_ok = False
                    outcome = "deferred"
                    llm_blocked = True
                    log(f"  {step.name} deferred: {who}")
                    notify("Brain schedule", "Claude usage limited; LLM jobs deferred")
                    break  # shared quota -> stop the LLM batch this tick
                else:  # transient
                    all_ok = False
                    outcome = "transient"
                    log(f"  {step.name} transient failure; will retry next tick")

        if all_ok:
            _record(ledger, step.name, now, "ok")
        else:
            _record(ledger, step.name, now, outcome)
            if ledger["jobs"][step.name].get("fail_streak", 0) == FAIL_STREAK_ALERT:
                notify("Brain schedule",
                       f"{step.name}: failed {FAIL_STREAK_ALERT} runs in a row "
                       f"({outcome}); see wiki/reports/schedule-status.md")


def cmd_run(dry_run: bool = False) -> int:
    log = make_logger()
    lock = acquire_lock()
    if lock is None:
        log("another dispatcher run holds the lock; exiting")
        return 0
    try:
        ledger = load_ledger()
        now = now_local()
        gates = Gates(log)
        steps = build_steps()
        log(f"tick {now:%Y-%m-%d %H:%M} (dry-run={dry_run})")

        # Self-heal: clear a lid-close override left stuck by a hard-killed prior
        # run (kill -9 / power loss bypass the finally below). No-op if already 0.
        if not dry_run:
            keepawake_off(lambda _m: None)

        # AC-gated lid-close keep-awake: override sleep with the lid CLOSED only
        # when on AC (battery -> never, so a closed bag can't overheat). Engage
        # only if LLM work is actually due and runnable (online + container).
        on_ac = gates.get("ac")
        lid = lid_closed()
        any_llm_due = any(step_due(s, ledger, now) for s in steps if s.kind == "llm")
        if dry_run:
            log(f"keep-awake check: on_ac={on_ac} lid_closed={lid} llm_due={any_llm_due}")
        engaged = bool(
            not dry_run and on_ac and lid and any_llm_due
            and gates.get("online") and gates.get("container") and keepawake_on(log)
        )

        try:
            _run_steps(steps, ledger, gates, now, dry_run, log)
            save_ledger(ledger)
            if not dry_run:
                write_schedule_status(ledger, steps, now)
                pruned = prune_reports()
                if pruned:
                    log(f"pruned {len(pruned)} old report(s) (keep latest "
                        f"{REPORT_RETENTION}/type)")
        finally:
            if engaged:
                keepawake_off(log)
                if lid_closed():  # only return to sleep if it woke headless for the job
                    sleep_now(log)
        return 0
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


def _record(ledger: dict, name: str, now: datetime, result: str) -> None:
    """Record a step outcome. `last_ok` advances only on success-equivalent
    results (ok/noop) and still drives step_due; every attempt also updates
    `last_attempt`/`last_result`, so failures stay visible to the status summary
    and the Chief of Staff instead of being log-only."""
    rec = ledger["jobs"].setdefault(name, {})
    rec["last_attempt"] = iso(now)
    rec["last_result"] = result
    if result in ("ok", "noop"):
        rec["last_ok"] = iso(now)
        rec["fail_streak"] = 0
    else:
        rec["fail_streak"] = rec.get("fail_streak", 0) + 1


def cmd_status() -> int:
    ledger = load_ledger()
    now = now_local()
    steps = build_steps()
    print(f"Brain schedule status  ({now:%Y-%m-%d %H:%M %Z})")
    print(f"ledger: {STATE_FILE}\n")
    print(f"{'step':12} {'period':7} {'due':4} {'last run':16} result")
    print("-" * 60)
    for step in steps:
        rec = ledger["jobs"].get(step.name, {})
        last = rec.get("last_ok")
        last_s = parse(last).strftime("%m-%d %H:%M") if last else "never"
        due = "yes" if step_due(step, ledger, now) else "-"
        print(f"{step.name:12} {step.period:7} {due:4} {last_s:16} {rec.get('last_result', '')}")
    print("\naccounts:")
    for acct in ACCOUNTS:
        st = ledger["accounts"].get(acct, {})
        lu = st.get("limited_until")
        if lu and parse(lu) > now:
            state = f"LIMITED until {parse(lu):%m-%d %H:%M} ({st.get('last_error')})"
        else:
            state = "healthy"
        print(f"  {acct:28} {state}")
    # scheduled wakes
    try:
        sched = subprocess.run([PMSET, "-g", "sched"], capture_output=True, text=True, timeout=10).stdout.strip()
        print(f"\npmset scheduled wakes:\n{sched or '  (none)'}")
    except Exception:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Brain scheduled-agent dispatcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one dispatcher tick")
    run.add_argument("--dry-run", action="store_true", help="evaluate gates/due-ness, run nothing")
    sub.add_parser("status", help="human-readable ledger view")
    args = p.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(dry_run=args.dry_run)
    if args.cmd == "status":
        return cmd_status()
    return 1


if __name__ == "__main__":
    sys.exit(main())
