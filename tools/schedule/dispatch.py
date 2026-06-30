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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parents[2]  # the vault root
HOME = Path.home()
STATE_DIR = HOME / ".brain"  # outside iCloud (no sync conflicts)
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
# missed 01:30 batch (the ledger makes it run at most once/day either way).
NIGHTLY_WINDOW = (1, 11)
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

# The AGENDA.md format + recurrence engine (tools/agenda.py, stdlib-only). Imported
# so the project-runner builder can decide which projects are enabled-and-due
# without spending any LLM budget.
sys.path.insert(0, str(ROOT / "tools"))
import agenda  # noqa: E402

# Project-runner caps + snapshot store. projects/ is gitignored (apply-don't-commit
# has no git to revert from), so the dispatcher clones each project BEFORE the runner
# edits it; the morning roll-up points at the clone as the undo path.
MAX_PROJECTS_PER_NIGHT = 4
SNAPSHOT_DIR = STATE_DIR / "project-snapshots"
SNAPSHOT_RETENTION_DAYS = 14

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
        data["accounts"].setdefault(
            acct, {"limited_until": None, "last_error": None, "backoff": 0}
        )
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
    if any(
        s in t
        for s in (
            "quota",
            "premium request",
            "monthly limit",
            "upgrade your plan",
            "usage limit",
        )
    ):
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
    kind: str  # "host" (wiki.py, offline) | "llm" (brain-wiki claude)
    period: str  # "daily" | "weekly"
    window: tuple[int, int]
    gates: list[str]  # subset of {"ac","online","container","icloud","battery"}
    builder: Callable[
        [], list[list[str]]
    ]  # -> list of arg-vectors ([] = nothing to do)
    effort: str = "low"  # NOTE: currently inert — wiki-agent.py EFFORT_MAP maps every level to no claude CLI flag; kept to express intended depth for when the CLI gains a thinking-budget control.
    timeout: int = 1800
    report: bool = False  # capture stdout into wiki/reports/


def _slugify(text: str) -> str:
    """Lowercase-hyphen slug matching the wiki's source-page / source-text naming."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _select_ingest_pdfs(
    pdf_names: list[str], text_stems: set[str], ingested_pdf_names: set[str]
) -> list[str]:
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
        # Capture the PDF basename across the three link forms — wikilink
        # [[raw/sources/Foo.pdf]], markdown [..](raw/sources/Foo.pdf), and the
        # angle-bracket [..](<../../raw/sources/Foo.pdf>). The class excludes the
        # three close delimiters (] ) >) and newline but ALLOWS spaces, so
        # space-bearing names like "The Mad King.pdf" still match.
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
        targets += [
            p
            for p in sorted(inbox.iterdir())
            if p.is_file() and not p.name.startswith(".")
        ]
    srcs = ROOT / "raw" / "sources"
    textdir = ROOT / "raw" / "sources-text"
    if srcs.is_dir():
        pdf_names = [p.name for p in sorted(srcs.glob("*.pdf"))]
        text_stems = (
            {p.stem for p in textdir.glob("*.md")} if textdir.is_dir() else set()
        )
        selected = _select_ingest_pdfs(pdf_names, text_stems, _ingested_pdf_names())
        targets += [srcs / name for name in selected]
    return [["ingest", "--source", str(p)] for p in targets[:3]]  # cap per night


def _project_runner_targets() -> list[list[str]]:
    """Opted-in projects with a clear, due AGENDA task — one arg-vector each.

    Pure-python (no LLM): agenda.due_projects reads only the ≤N AGENDA.md files,
    skips dormant (enabled:false) and malformed ones, and returns slugs with work.
    Projects whose unreviewed edits have stacked up (is_paused_for_review) are held
    back until the operator runs `wiki.py project agenda ack <slug>`. Capped so a
    night with many due projects cannot blow the shared LLM budget; deferred
    projects stay due and are caught up on the next eligible night.
    """
    today = now_local().date()
    out: list[list[str]] = []
    for slug in agenda.due_projects(ROOT / "projects", today):
        if agenda.is_paused_for_review(slug):
            continue
        out.append(["project-run", "--project", slug])
    return out[:MAX_PROJECTS_PER_NIGHT]


def _runner_slug(args: list[str]) -> str | None:
    """Extract the project slug from a ["project-run", "--project", <slug>] vector."""
    if "--project" in args:
        i = args.index("--project")
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _parse_executed(out: str) -> int:
    """Read the runner's `Executed: <n>` line from its stdout report block."""
    m = re.search(r"^Executed:\s*(\d+)", out or "", re.MULTILINE)
    return int(m.group(1)) if m else 0


def _snapshot_project(
    slug: str, now: datetime, log: Callable[[str], None]
) -> Path | None:
    """Clone projects/<slug>/ before the runner edits it (apply-don't-commit undo).

    Uses an APFS clonefile (`cp -c`) so it is instant and near-zero space; falls
    back to a plain recursive copy if clonefile is unavailable (e.g. across volumes).
    Idempotent per date — the first snapshot of the night wins, so it captures the
    pre-run state even if the step retries."""
    src = ROOT / "projects" / slug
    if not src.is_dir():
        return None
    dst = SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / slug
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["cp", "-c", "-R", str(src), str(dst)],
        ["cp", "-R", str(src), str(dst)],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            return dst
        except Exception:  # noqa: BLE001 - try the plain-copy fallback, then give up
            continue
    log(f"snapshot {slug} failed; no pre-run clone for tonight's edits")
    return None


def _prune_snapshots(retention_days: int = SNAPSHOT_RETENTION_DAYS) -> int:
    """Drop project-snapshot date-dirs older than the retention window."""
    if not SNAPSHOT_DIR.is_dir():
        return 0
    cutoff = now_local().date() - timedelta(days=retention_days)
    removed = 0
    for date_dir in SNAPSHOT_DIR.iterdir():
        try:
            d = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            shutil.rmtree(date_dir, ignore_errors=True)
            removed += 1
    return removed


def _project_runner_header(slugs: list[str], now: datetime) -> str:
    """Host-built preamble for the roll-up: the per-project restore command for the
    apply-don't-commit snapshots (projects/ is gitignored, so this is the undo)."""
    lines = [
        f"# Project runner roll-up — {now:%Y-%m-%d}",
        "",
        "Edits were applied to the working tree (not committed). To undo a project,",
        "restore it from tonight's pre-run snapshot:",
        "",
    ]
    for slug in slugs:
        snap = SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / slug
        lines.append(f"- `{slug}`: `cp -c -R {snap} {ROOT / 'projects' / slug}`")
    lines.append("")
    lines.append(
        "Once reviewed, resume a project's nightly runs with "
        "`python3 tools/wiki.py project agenda ack <slug>`."
    )
    return "\n".join(lines)


def build_steps() -> list[Step]:
    """Ordered step list. The nightly batch is just the nightly-window steps run
    in this order; cos-brief is the lone morning step."""
    return [
        # 1. maintenance: offline, host-native, runs even on a battery night.
        Step(
            "lint", "host", "daily", NIGHTLY_WINDOW, [], lambda: [["lint"]], timeout=600
        ),
        Step(
            "index",
            "host",
            "daily",
            NIGHTLY_WINDOW,
            [],
            lambda: [["index", "--rebuild"]],
            timeout=600,
        ),
        # 2. ingest new raw material (only if any), before enhance.
        Step(
            "ingest",
            "llm",
            "daily",
            NIGHTLY_WINDOW,
            ["ac", "online", "container", "icloud"],
            _ingest_targets,
            effort="low",
            timeout=2400,
        ),
        # 3. weekly thinking digests (prefer Sunday); reports filed for you. They
        #    run BEFORE enhance so they analyse the night's pre-enhance wiki, and
        #    so the cheaper read-only digests claim the budget first on a contended
        #    night (a usage limit then defers only enhance, the biggest consumer).
        Step(
            "contradict",
            "llm",
            "weekly",
            NIGHTLY_WINDOW,
            ["ac", "online", "container", "icloud"],
            lambda: [["contradict"]],
            effort="high",
            timeout=2400,
            report=True,
        ),
        Step(
            "emerge",
            "llm",
            "weekly",
            NIGHTLY_WINDOW,
            ["ac", "online", "container", "icloud"],
            lambda: [["emerge"]],
            effort="high",
            timeout=2400,
            report=True,
        ),
        Step(
            "discover",
            "llm",
            "weekly",
            NIGHTLY_WINDOW,
            ["ac", "online", "container", "icloud"],
            lambda: [["discover"]],
            effort="high",
            timeout=2400,
            report=True,
        ),
        # 4. project runner: execute opted-in projects' due AGENDA tasks. Runs
        #    BEFORE enhance so this user-facing work claims the shared budget first
        #    (a usage limit then defers only enhance). Writes projects/ (not wiki/),
        #    so it never conflicts with enhance; report=True drives the roll-up.
        Step(
            "project-runner",
            "llm",
            "daily",
            NIGHTLY_WINDOW,
            ["ac", "online", "container", "icloud"],
            _project_runner_targets,
            effort="low",
            timeout=2400,
            report=True,
        ),
        # 5. enhance LAST, capped at 10 iterations/night (the biggest budget
        #    consumer); soaks up whatever time/quota is left after the digests.
        Step(
            "enhance",
            "llm",
            "daily",
            NIGHTLY_WINDOW,
            ["ac", "online", "container", "icloud"],
            lambda: [["enhance", "--iterations", "10", "--strategy", "alternate"]],
            effort="low",
            timeout=7200,
        ),
        # morning: daily chief-of-staff brief (battery OK, no AC gate).
        Step(
            "cos-brief",
            "llm",
            "daily",
            MORNING_WINDOW,
            ["online", "container", "icloud", "battery"],
            lambda: [["cos", "--mode", "brief"]],
            effort="low",
            timeout=1800,
            report=True,
        ),
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
            subprocess.run(
                [CONTAINER, "system", "start"], capture_output=True, timeout=60
            )
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
            subprocess.run(
                [BRCTL, "download", str(REPORTS_DIR)], capture_output=True, timeout=30
            )
        except Exception:
            pass
        return True

    # raw probes
    def _nc(self, host: str, port: int) -> bool:
        try:
            return (
                subprocess.run(
                    [NC, "-z", "-G", "5", host, str(port)],
                    capture_output=True,
                    timeout=10,
                ).returncode
                == 0
            )
        except Exception:
            return False

    def _container_up(self) -> bool:
        # apple/container runtime (the Docker-free sandbox launcher, bin/agent).
        try:
            return (
                subprocess.run(
                    [CONTAINER, "system", "status"], capture_output=True, timeout=30
                ).returncode
                == 0
            )
        except Exception:
            return False

    def _pmset_batt(self) -> str:
        try:
            return subprocess.run(
                [PMSET, "-g", "batt"], capture_output=True, text=True, timeout=10
            ).stdout
        except Exception:
            return ""


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def run_host(args: list[str], timeout: int) -> tuple[int, str]:
    cmd = [PYTHON, str(ROOT / "tools" / "wiki.py")] + args
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT)
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def exec_brain_wiki(
    args: list[str], acct: str, effort: str, timeout: int
) -> tuple[int, str]:
    inner_parts = [
        "brain-wiki",
        *args,
        "--cli",
        CLI,
        "--model",
        MODEL,
        "--effort",
        effort,
    ]
    inner = " ".join(_q(p) for p in inner_parts)
    env = dict(os.environ)
    # Keep the sandbox VM warm for the whole tick. bin/agent powers its container
    # down on exit BY DEFAULT (so a manual `brain-wiki` run cleans up after itself),
    # but one tick runs several steps back-to-back and should reuse a single warm
    # box; cmd_run's end-of-tick teardown stops it once the batch is done.
    env["BRAIN_KEEP_WARM"] = "1"
    # `acct` is the failover-ledger identity. The Claude CLI authenticates via
    # its own subscription login, so no per-exec env steering is needed; the
    # parameter stays for the multi-identity ledger interface.
    try:
        p = subprocess.run(
            [FISH, "-lc", inner],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)


def run_llm(
    args: list[str],
    effort: str,
    timeout: int,
    ledger: dict,
    now: datetime,
    log: Callable[[str], None],
) -> tuple[str, str, str]:
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
        "status: active\n"
        f"title: Scheduled {name} {now:%Y-%m-%d}\n"
        f"created: {now:%Y-%m-%d}\n"
        f"updated: {now:%Y-%m-%d}\n"
        f"summary: Scheduled {name} run output for {now:%Y-%m-%d}.\n"
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
        rows.append(
            f"| {name} | {period} | {last_ok_s} | {result or 'none'} | {health} |"
        )

    limited = [
        a
        for a, st in accounts.items()
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
        subprocess.run(
            [
                OSASCRIPT,
                "-e",
                f"display notification {json.dumps(msg)} with title {json.dumps(title)}",
            ],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Chief-of-Staff proposals → per-project inboxes (the CoS→AGENDA routing seam)
# --------------------------------------------------------------------------- #
#
# The CoS is read-only by design (SPEC decision 4): it emits a machine-readable
# `## Proposals` block in its brief but writes nothing. The dispatcher — which
# already captures the brief's stdout into a report — also parses that block and
# appends each proposal to the `## Inbox` of the PROJECT it names. The existing
# project-runner then grooms + (when that project is enabled) actions them, so
# there is ONE executor per project, not a second write-capable agent. Keeps the
# orthogonal split: CoS decides what should happen (and where), the dispatcher
# wires, each project's runner acts within its own scope. A proposal whose target
# is not a real project is left advisory (logged + still in the brief/report), not
# force-filed somewhere — so there is no catch-all "assistant" project to maintain.


# The shared routed-work-item grammar for BOTH CoS proposals and inter-role
# handoffs: `<keyword>:: <target-project> | <imperative task> | <why-or-ref>`.
def _routed_re(keyword: str):
    return re.compile(
        rf"^\s*{keyword}::\s*(?P<target>[^|]+?)\s*\|\s*(?P<task>[^|]+?)\s*\|\s*(?P<why>.+?)\s*$"
    )


_PROPOSAL_RE = _routed_re("proposal")  # CoS → project
_HANDOFF_RE = _routed_re("handoff")  # any producer role → another desk


def _parse_routed(text: str, pattern) -> list[dict]:
    """Extract routed work-item lines matching `pattern`. Pure / side-effect-free
    (tested in test_schedule.py). Malformed lines (wrong pipe count, empty
    target/task) are skipped, so a sloppy producer degrades to "nothing routed"
    rather than corrupting an inbox."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        m = pattern.match(line)
        if not m:
            continue
        target, task, why = (
            m.group("target").strip(),
            m.group("task").strip(),
            m.group("why").strip(),
        )
        if task and target:
            out.append({"target": target, "task": task, "why": why})
    return out


def parse_cos_proposals(text: str) -> list[dict]:
    """CoS `proposal:: <project> | <task> | <why>` lines from a brief."""
    return _parse_routed(text, _PROPOSAL_RE)


def parse_handoffs(text: str) -> list[dict]:
    """Inter-role `handoff:: <to-project> | <ask> | <deliverable-ref>` lines from a
    producer agent's output."""
    return _parse_routed(text, _HANDOFF_RE)


def resolve_proposal_dest(target: str, projects_dir: Path | None = None) -> Path | None:
    """The AGENDA.md of the project a proposal names, or None if it names no real
    project. Routing the work to the owning project is safe because that project's
    runner is scoped to its own dir; an empty / unknown / typo target resolves to
    None and the proposal is left advisory (never force-filed). Pure (only a
    `.exists()` check) so it is testable against a temp projects dir."""
    base = Path(projects_dir) if projects_dir is not None else (ROOT / "projects")
    slug = (target or "").strip()
    if not slug:
        return None
    cand = base / slug / "AGENDA.md"
    return cand if cand.exists() else None


def format_work_item(source: str, item: dict) -> str:
    """One inbox bullet body for a routed work-item. Provenance `[from:<source>]`
    (no date) so an identical item re-routed on a later day dedupes against the
    existing line, and so every hop is visible/auditable in the receiving inbox."""
    why = f" — {item['why']}" if item.get("why") else ""
    return f"[from:{source}] {item['task']}{why}"


MAX_ROUTED_PER_TICK = (
    12  # backstop: bound how many items routing can append per dispatcher run
)


@dataclass
class RoutingGuard:
    """Anti-loop / anti-runaway guard for the handoff bus. One instance is shared by
    every routing call within a SINGLE dispatcher tick, so its cap and cycle-blocks span
    all producers that route in that tick — within the nightly batch that means every
    project-runner handoff across projects; a cos-brief that routes in the same tick
    shares it too, but a cos-brief running in a separate morning tick gets its own guard.
    Blocks self-handoffs, direct reciprocal edges (A→B when B→A was already routed this
    tick), and a hard per-tick cap. Longer cycles are bounded by the cap plus the facts
    that desks default `enabled: false` and the operator reviews the brief daily; precise
    multi-hop cycle detection (hop propagation through agents) is deferred.

    `record` counts routing *decisions*, not post-dedup writes: an item that
    `append_inbox_items` later dedups to a no-op still consumes cap budget and records its
    edge. Deliberate — the cap then also bounds a producer that spams duplicates, and
    cycle-blocking stays independent of whether the item was already in the inbox."""

    cap: int = MAX_ROUTED_PER_TICK
    routed: int = 0
    edges: set = field(default_factory=set)

    def allow(self, source: str, dest_slug: str) -> tuple[bool, str]:
        if source == dest_slug:
            return False, "self-handoff"
        if self.routed >= self.cap:
            return False, f"per-tick routing cap ({self.cap}) reached"
        if f"{dest_slug}>{source}" in self.edges:
            return (
                False,
                f"reciprocal edge {dest_slug}->{source} already routed (cycle)",
            )
        return True, ""

    def record(self, source: str, dest_slug: str) -> None:
        self.edges.add(f"{source}>{dest_slug}")
        self.routed += 1


def _route_work_items(
    items: list[dict],
    source: str,
    now: datetime,
    log: Callable[[str], None],
    guard: "RoutingGuard",
    projects_dir: Path | None = None,
) -> int:
    """Append each work-item to the `## Inbox` of the project it names, honoring the
    guard. Items whose target is not a real project are left advisory (logged). Groups
    by destination so each file is written once. Returns new items appended."""
    buckets: dict[Path, list[str]] = {}
    for it in items:
        dest = resolve_proposal_dest(it["target"], projects_dir)
        if dest is None:
            log(
                f"routing: '{it['target']}' is not a project; '{it['task'][:50]}' left advisory"
            )
            continue
        dest_slug = dest.parent.name
        ok, why = guard.allow(source, dest_slug)
        if not ok:
            log(f"routing: dropped {source}->{dest_slug} ({why})")
            continue
        guard.record(source, dest_slug)
        buckets.setdefault(dest, []).append(format_work_item(source, it))
    total = 0
    for dest, lines in buckets.items():
        n = agenda.append_inbox_items(dest, lines, now.date())
        if n:
            total += n
            log(
                f"routing: {n} new item(s) {source} -> projects/{dest.parent.name} inbox"
            )
    return total


def route_cos_proposals(
    out: str,
    now: datetime,
    log: Callable[[str], None],
    guard: "RoutingGuard | None" = None,
    projects_dir: Path | None = None,
) -> int:
    """Route a CoS brief's `## Proposals` into the named projects' inboxes. Best-effort:
    catches everything so a routing problem can never abort the morning tick."""
    try:
        props = parse_cos_proposals(out)
        if not props:
            return 0
        total = _route_work_items(
            props, "cos", now, log, guard or RoutingGuard(), projects_dir
        )
        if total:
            notify(
                "Brain schedule", f"CoS routed {total} proposal(s) to project inboxes"
            )
        return total
    except Exception as e:  # noqa: BLE001 - routing must never break the tick
        log(f"cos proposals: routing failed ({e}); brief unaffected")
        return 0


def route_handoffs(
    out: str,
    source: str,
    now: datetime,
    log: Callable[[str], None],
    guard: "RoutingGuard | None" = None,
    projects_dir: Path | None = None,
) -> int:
    """Route a producer agent's `handoff::` lines to other desks' inboxes — the same
    guarded path as CoS proposals (sharing one guard means caps and cycle-blocks span
    both). Best-effort: never raises into the tick."""
    try:
        items = parse_handoffs(out)
        if not items:
            return 0
        total = _route_work_items(
            items, source, now, log, guard or RoutingGuard(), projects_dir
        )
        if total:
            notify(
                "Brain schedule", f"{source} handed off {total} item(s) to other desks"
            )
        return total
    except Exception as e:  # noqa: BLE001 - routing must never break the tick
        log(f"handoffs from {source}: routing failed ({e}); run unaffected")
        return 0


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
        out = subprocess.run(
            [IOREG, "-r", "-k", "AppleClamshellState"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        for line in out.splitlines():
            if "AppleClamshellState" in line:
                return "Yes" in line
    except Exception:
        pass
    return False


def _sudo_pmset(args: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                [SUDO, "-n", PMSET, *args], capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except Exception:
        return False


def keepawake_on(log: Callable[[str], None]) -> bool:
    ok = _sudo_pmset(["-a", "disablesleep", "1"])
    log(
        "disablesleep 1 (lid-close override ON)"
        if ok
        else "WARN: could not set disablesleep -- sudoers rule missing? (won't hold lid-closed)"
    )
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


def _run_steps(
    steps: list[Step],
    ledger: dict,
    gates: "Gates",
    now: datetime,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """Run every due step in order, honoring gates, account failover, and reports."""
    llm_blocked = (
        False  # set once the backend is usage-limited; skip remaining LLM steps
    )
    # One routing guard per tick: its cap + cycle-blocks span every item routed during
    # this dispatcher run — CoS proposals and/or project-runner handoffs, whichever steps
    # run this tick (a step that runs in a different tick gets a fresh guard).
    routing_guard = RoutingGuard()
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
        report_chunks: list[str] = []  # collected across invocations, written once
        ran_slugs: list[str] = []  # project-runner: projects actually executed
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
                        report_chunks.append(out)
            else:  # llm
                # apply-don't-commit undo: clone the project before the runner edits
                # it (projects/ is gitignored, so this snapshot is the only revert).
                slug = _runner_slug(args) if step.name == "project-runner" else None
                if slug:
                    _snapshot_project(slug, now, log)
                status, who, out = run_llm(
                    args, step.effort, step.timeout, ledger, now, log
                )
                if status == "ok":
                    if slug:
                        # Only edit-producing passes advance the stacking guard.
                        agenda.record_run(slug, _parse_executed(out), iso(now))
                        ran_slugs.append(slug)
                    if step.report:
                        report_chunks.append(out)
                    # Routing seam: the CoS brief's proposals, and any project-runner
                    # pass's `handoff::` lines, are filed into the named projects'
                    # inboxes by the dispatcher (the agents are read-only / dir-scoped;
                    # the dispatcher does the guarded cross-project write).
                    if step.name == "cos-brief":
                        route_cos_proposals(out, now, log, routing_guard)
                    elif slug:
                        route_handoffs(out, slug, now, log, routing_guard)
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

        # Write the step's report ONCE, after all invocations, so a multi-invocation
        # step (project-runner) yields a single aggregated roll-up rather than each
        # invocation overwriting the last. Single-invocation report steps are
        # unaffected (one chunk -> identical output).
        if step.report and report_chunks:
            body = "\n\n---\n\n".join(c.strip() for c in report_chunks)
            if step.name == "project-runner":
                body = _project_runner_header(ran_slugs, now) + "\n\n" + body
            f = write_report(step.name, body, now)
            notify("Brain schedule", f"{step.name} ready: {f.name}")

        if all_ok:
            _record(ledger, step.name, now, "ok")
        else:
            _record(ledger, step.name, now, outcome)
            if ledger["jobs"][step.name].get("fail_streak", 0) == FAIL_STREAK_ALERT:
                notify(
                    "Brain schedule",
                    f"{step.name}: failed {FAIL_STREAK_ALERT} runs in a row "
                    f"({outcome}); see wiki/reports/schedule-status.md",
                )


def _running_brain_containers(log) -> set[str]:
    """Names of currently-running `brain-*` sandbox containers (apple/container).

    bin/agent names each VM `brain-<root-hash>-<profile>` (e.g. -reader/-author),
    so the `brain-` prefix selects exactly the sandboxes this vault launches and
    never the build shim or another project's container."""
    try:
        out = subprocess.run(
            [CONTAINER, "ls", "-q"], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception as e:  # noqa: BLE001 - teardown bookkeeping must never abort a tick
        log(f"container ls failed ({e}); skipping container teardown")
        return set()
    return {n for n in out.split() if n.startswith("brain-")}


def _stop_new_brain_containers(pre_running: set[str], log) -> None:
    """Power down the sandbox VMs THIS tick started.

    bin/agent launches each container detached (`container run -d`, `--init` as PID
    1) and reuses it across steps but never stops it, so a finished 6 GB job would
    otherwise stay "running" and pin its full -m allocation until reboot. We stop
    only the set that appeared during the tick (now-running minus pre-running), which
    preserves warm reuse WITHIN a tick and never tears down an interactive
    brain-claude/brain-shell session that predated the tick. `container stop` is
    graceful (PID 1 traps SIGTERM) and a no-op if already stopped."""
    started = sorted(_running_brain_containers(log) - pre_running)
    for name in started:
        try:
            subprocess.run(
                [CONTAINER, "stop", name], capture_output=True, text=True, timeout=60
            )
            log(f"powered down sandbox container {name} (tick teardown)")
        except Exception as e:  # noqa: BLE001
            log(f"failed to stop {name} ({e}); stop it manually to free RAM")


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
            log(
                f"keep-awake check: on_ac={on_ac} lid_closed={lid} llm_due={any_llm_due}"
            )
        engaged = bool(
            not dry_run
            and on_ac
            and lid
            and any_llm_due
            and gates.get("online")
            and gates.get("container")
            and keepawake_on(log)
        )

        # Snapshot the brain sandbox VMs already running BEFORE this tick, so the
        # post-tick teardown stops only the ones THIS tick starts and never an
        # interactive brain-claude/brain-shell session that predates it.
        pre_running = set() if dry_run else _running_brain_containers(log)

        try:
            _run_steps(steps, ledger, gates, now, dry_run, log)
            save_ledger(ledger)
            if not dry_run:
                write_schedule_status(ledger, steps, now)
                pruned = prune_reports()
                if pruned:
                    log(
                        f"pruned {len(pruned)} old report(s) (keep latest "
                        f"{REPORT_RETENTION}/type)"
                    )
                snaps = _prune_snapshots()
                if snaps:
                    log(
                        f"pruned {snaps} old project-snapshot day(s) (keep "
                        f"{SNAPSHOT_RETENTION_DAYS}d)"
                    )
        finally:
            if engaged:
                keepawake_off(log)
                if lid_closed():  # only return to sleep if it woke headless for the job
                    sleep_now(log)
            # Power down the sandbox VMs this tick started so a finished 6 GB job
            # stops pinning RAM (bin/agent launches them detached and never stops
            # them). Only the newly-started set is stopped -> warm reuse within the
            # tick is preserved; the cross-tick RAM leak is closed.
            if not dry_run:
                _stop_new_brain_containers(pre_running, log)
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
        print(
            f"{step.name:12} {step.period:7} {due:4} {last_s:16} {rec.get('last_result', '')}"
        )
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
        sched = subprocess.run(
            [PMSET, "-g", "sched"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        print(f"\npmset scheduled wakes:\n{sched or '  (none)'}")
    except Exception:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Brain scheduled-agent dispatcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one dispatcher tick")
    run.add_argument(
        "--dry-run", action="store_true", help="evaluate gates/due-ness, run nothing"
    )
    sub.add_parser("status", help="human-readable ledger view")
    args = p.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(dry_run=args.dry_run)
    if args.cmd == "status":
        return cmd_status()
    return 1


if __name__ == "__main__":
    sys.exit(main())
