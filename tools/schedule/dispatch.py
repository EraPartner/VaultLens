#!/usr/bin/env python3
"""Brain scheduled-agent dispatcher.

A host-side catch-up dispatcher fired by a launchd LaunchAgent at a few calendar
anchors spanning the run windows (see com.brain.schedule.plist). Each run it asks,
per step: is this due, am I in its window, and do its gates pass? If yes, run it;
record the result. Missed anchors are rerun by launchd on the next wake, so
sleep / offline / closed-lid become non-events. Full design: tools/schedule/SPEC.md.

stdlib only (matches the rest of tools/). Pure decision helpers (classify_failure,
choose_account, mark_limited, step_due) are kept side-effect-free so
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

ROOT = Path(__file__).resolve().parents[2]          # the vault root
HOME = Path.home()
STATE_DIR = HOME / ".brain"                          # outside iCloud (no sync conflicts)
STATE_FILE = STATE_DIR / "schedule-state.json"
LOCK_FILE = STATE_DIR / "schedule.lock"
LOG_DIR = STATE_DIR / "logs"
REPORTS_DIR = ROOT / "wiki" / "reports"

# Backend auth identities, priority order. Historically two copilot GitHub
# accounts with failover; now a single Claude-plan subscription (the logged-in
# `claude` CLI), so this is one sentinel entry. The failover machinery below
# (cooldown ledger, classify_failure, choose_account) still applies: a Claude
# usage-limit error marks this identity limited_until and defers the rest of the
# LLM batch. Re-add entries here only if a second backend identity exists.
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
DOCKER = _tool("docker", str(HOME / ".docker/bin/docker"), "/usr/local/bin/docker")
NC = _tool("nc", "/opt/homebrew/bin/nc", "/usr/bin/nc")
PMSET = _tool("pmset", "/usr/bin/pmset")
OSASCRIPT = _tool("osascript", "/usr/bin/osascript")
BRCTL = _tool("brctl", "/usr/bin/brctl")
OPEN = _tool("open", "/usr/bin/open")
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


def choose_account(ledger: dict, now: datetime) -> str | None:
    """First account in priority order whose cooldown has expired, else None."""
    for acct in ACCOUNTS:
        st = ledger["accounts"].get(acct, {})
        lu = st.get("limited_until")
        if not lu or parse(lu) <= now:
            return acct
    return None


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
    # leave limited_until in place only if still in the future; a success means healthy
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
    gates: list[str]                # subset of {"ac","online","docker","icloud","battery"}
    builder: Callable[[], list[list[str]]]   # -> list of arg-vectors ([] = nothing to do)
    effort: str = "low"
    timeout: int = 1800
    report: bool = False            # capture stdout into wiki/reports/


def _ingest_targets() -> list[list[str]]:
    """Unprocessed raw material: inbox files + source PDFs lacking extracted text."""
    targets: list[Path] = []
    inbox = ROOT / "raw" / "inbox"
    if inbox.is_dir():
        targets += [p for p in sorted(inbox.iterdir()) if p.is_file() and not p.name.startswith(".")]
    srcs = ROOT / "raw" / "sources"
    textdir = ROOT / "raw" / "sources-text"
    if srcs.is_dir():
        for p in sorted(srcs.glob("*.pdf")):
            if not (textdir / (p.stem + ".md")).exists():
                targets.append(p)
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
        Step("ingest", "llm", "daily", NIGHTLY_WINDOW, ["ac", "online", "docker", "icloud"],
             _ingest_targets, effort="low", timeout=2400),
        # 3. enhance, capped at 5 iterations/night (the biggest budget consumer).
        Step("enhance", "llm", "daily", NIGHTLY_WINDOW, ["ac", "online", "docker", "icloud"],
             lambda: [["enhance", "--iterations", "5"]], effort="low", timeout=5400),
        # 4. weekly thinking digests (prefer Sunday); reports filed for you.
        Step("contradict", "llm", "weekly", NIGHTLY_WINDOW, ["ac", "online", "docker", "icloud"],
             lambda: [["contradict"]], effort="high", timeout=2400, report=True),
        Step("emerge", "llm", "weekly", NIGHTLY_WINDOW, ["ac", "online", "docker", "icloud"],
             lambda: [["emerge"]], effort="high", timeout=2400, report=True),
        Step("discover", "llm", "weekly", NIGHTLY_WINDOW, ["ac", "online", "docker", "icloud"],
             lambda: [["discover"]], effort="high", timeout=2400, report=True),
        # morning: daily chief-of-staff brief (battery OK, no AC gate).
        Step("cos-brief", "llm", "daily", MORNING_WINDOW, ["online", "docker", "icloud"],
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
        # LLM jobs run on the Claude CLI; probe its API endpoint (github.com as
        # a generic-connectivity fallback for non-LLM online needs).
        return self._nc("api.anthropic.com", 443) or self._nc("github.com", 443)

    def _g_docker(self) -> bool:
        if self._docker_up():
            return True
        self.log("docker not running; launching Docker Desktop")
        try:
            subprocess.run([OPEN, "-a", "Docker"], capture_output=True, timeout=20)
        except Exception:
            return False
        for _ in range(18):  # ~90s
            time.sleep(5)
            if self._docker_up():
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

    def _docker_up(self) -> bool:
        try:
            return subprocess.run([DOCKER, "info"], capture_output=True, timeout=30).returncode == 0
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
    """Run one LLM invocation with account failover.

    Returns (status, account_or_reason, output). status in
    {ok, transient, deferred}. On a rate-limit/quota hit the current account is
    marked limited and the next healthy account is tried; if none remain the job
    is deferred.
    """
    last_out = ""
    for acct in ACCOUNTS:
        st = ledger["accounts"].get(acct, {})
        lu = st.get("limited_until")
        if lu and parse(lu) > now:
            continue  # skip an account still in cooldown
        log(f"    -> account {acct}")
        rc, out = exec_brain_wiki(args, acct, effort, timeout)
        last_out = out
        cls = classify_failure(rc, out)
        if cls == "ok":
            clear_account(ledger, acct)
            return "ok", acct, out
        if cls == "transient":
            log(f"    transient failure on {acct} (rc={rc})")
            return "transient", acct, out
        log(f"    {cls} on {acct}; switching account")
        mark_limited(ledger, acct, cls, now)
    return "deferred", "all-accounts-limited", last_out


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
    llm_blocked = False  # set once both accounts are limited; skip remaining LLM steps
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
        for args in invocations:
            log(f"run {step.name}: {' '.join(args)}")
            if step.kind == "host":
                rc, out = run_host(args, step.timeout)
                if rc == 124:
                    all_ok = False
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
                    llm_blocked = True
                    log(f"  {step.name} deferred: {who}")
                    notify("Brain schedule", "Claude usage limited; LLM jobs deferred")
                    break  # shared quota -> stop the LLM batch this tick
                else:  # transient
                    all_ok = False
                    log(f"  {step.name} transient failure; will retry next tick")

        if all_ok:
            _record(ledger, step.name, now, "ok")


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
        # only if LLM work is actually due and runnable (online + docker).
        on_ac = gates.get("ac")
        lid = lid_closed()
        any_llm_due = any(step_due(s, ledger, now) for s in steps if s.kind == "llm")
        if dry_run:
            log(f"keep-awake check: on_ac={on_ac} lid_closed={lid} llm_due={any_llm_due}")
        engaged = bool(
            not dry_run and on_ac and lid and any_llm_due
            and gates.get("online") and gates.get("docker") and keepawake_on(log)
        )

        try:
            _run_steps(steps, ledger, gates, now, dry_run, log)
            save_ledger(ledger)
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
    ledger["jobs"][name] = {"last_ok": iso(now), "last_result": result}


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
