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
import contextlib
import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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

# Resolve once per dispatcher process so a complete batch uses one provider.
sys.path.insert(0, str(ROOT / "tools"))
from llm_provider import (  # noqa: E402, F401
    BACKENDS,
    load_config,
    load_profile_models,
    resolve_provider,
)
from agent_profiles import AGENT_FILES, resolve_role_settings  # noqa: E402
from local_runtime import default_access_profile, runtime_available  # noqa: E402


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag (0/1, false/true, no/yes, off/on)")


def freeze_role_models(
    cli: str, *, root: Path = ROOT, environ: dict, config: dict, profile_models: dict
) -> dict[str, str]:
    """Resolve every role against one batch's provider configuration snapshot."""
    return {
        agent: resolve_role_settings(
            agent,
            cli=cli,
            root=root,
            environ=environ,
            config=config,
            profile_models=profile_models,
        )[0].model
        for agent in AGENT_FILES
    }


_PROVIDER_ERROR = ""
CLI, MODEL = "", ""
ROLE_MODELS = {}
BACKEND_HEALTH_HOST = BACKEND_IDENTITY = ""
ACCOUNTS = []
SCHEDULE_ENHANCE = False
try:
    _PROVIDER_ENV = dict(os.environ)
    _PROVIDER_CONFIG = load_config()
    _PROFILE_MODELS = load_profile_models()
    _PROVIDER = resolve_provider(
        environ=_PROVIDER_ENV,
        config=_PROVIDER_CONFIG,
        profile_models=_PROFILE_MODELS,
    )
    _ROLE_MODELS = freeze_role_models(
        _PROVIDER.cli,
        environ=_PROVIDER_ENV,
        config=_PROVIDER_CONFIG,
        profile_models=_PROFILE_MODELS,
    )
    _SCHEDULE_ENHANCE = _env_flag("VAULTLENS_SCHEDULE_ENHANCE", default=False)
except (ValueError, OSError, UnicodeError) as exc:
    # Invalid model policy blocks all model jobs without disabling host-side
    # diagnostics, maintenance or recovery. Never fall back to another provider.
    _PROVIDER_ERROR = str(exc)
else:
    CLI, MODEL = _PROVIDER.cli, _PROVIDER.model
    ROLE_MODELS = _ROLE_MODELS
    BACKEND_HEALTH_HOST = _PROVIDER.health_host
    BACKEND_IDENTITY = _PROVIDER.identity
    ACCOUNTS = [BACKEND_IDENTITY]
    SCHEDULE_ENHANCE = _SCHEDULE_ENHANCE
ENHANCE_ITERATIONS = 5

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
# pruned each tick so wiki/reports/agents/scheduled does not pile up. The CoS is read-only, so
# this hygiene runs host-side in the dispatcher that writes the reports.
REPORT_RETENTION = 14
# A daily brief is a current advisory surface, not a historical log. Keep only
# the newest generated brief; longer-term signal belongs in an explicit synthesis.
REPORT_RETENTION_BY_TYPE = {"cos-brief": 1}

# The AGENDA.md format + recurrence engine (tools/agenda.py, stdlib-only). Imported
# so the project-runner builder can decide which projects are enabled-and-due
# without spending any LLM budget.
sys.path.insert(0, str(ROOT / "tools"))
import agenda  # noqa: E402
from project_state import is_frozen_project  # noqa: E402

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
QMD = _tool("qmd", str(HOME / ".bun" / "bin" / "qmd"))
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
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if STATE_FILE.is_symlink():
            raise RuntimeError(
                "Schedule ledger is a broken link; refusing unattended work."
            )
        data = {}
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise RuntimeError(
            "Schedule ledger is unreadable or corrupt; refusing unattended work. Inspect and recover it explicitly."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            "Schedule ledger must be an object; refusing unattended work."
        )
    data.setdefault("jobs", {})
    data.setdefault("accounts", {})
    for key in ("jobs", "accounts"):
        if not isinstance(data[key], dict) or any(
            not isinstance(value, dict) for value in data[key].values()
        ):
            raise RuntimeError(
                f"Schedule ledger {key} is malformed; refusing unattended work."
            )
    for key in ("agent_in_flight", "cancellation_pending"):
        if key in data and not isinstance(data[key], dict):
            raise RuntimeError(
                f"Schedule ledger {key} is malformed; refusing unattended work."
            )
    if "cancellation_pending" in data and not data["cancellation_pending"].get(
        "detail"
    ):
        data["cancellation_pending"]["detail"] = (
            "An unresolved cancellation marker exists; operator recovery is required."
        )
    if "agent_in_flight" in data:
        data.setdefault(
            "cancellation_pending",
            {
                "since": data["agent_in_flight"].get("since", "unknown"),
                "detail": "A prior model invocation has no confirmed normal completion. Inner termination is unconfirmed; inspect agent logs and workload before acknowledging recovery.",
            },
        )
    for acct in ACCOUNTS:
        data["accounts"].setdefault(
            acct, {"limited_until": None, "last_error": None, "backoff": 0}
        )
    return data


def save_ledger(ledger: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as output:
        output.write(json.dumps(ledger, indent=2, sort_keys=True))
        output.flush()
        os.fsync(output.fileno())
    tmp.replace(STATE_FILE)
    directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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
            "usage_limit",
            "usage-limit",
            "session limit",
            "weekly limit",
            "hit your limit",
            "spend limit",
            "spending limit",
            "credit balance is too low",
        )
    ):
        return "quota"
    if any(s in t for s in ("rate limit", "rate-limit", "429", "too many requests")):
        return "ratelimit"
    return "transient"


def backend_available(ledger: dict, now: datetime) -> bool:
    """True if the selected backend is not in a usage-limit cooldown."""
    if _PROVIDER_ERROR:
        return False
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
    kind: str  # "host" (wiki.py) | "qmd" (host index) | "llm" (native agent)
    period: str  # "daily" | "weekly"
    window: tuple[int, int]
    gates: list[str]  # subset of {"ac","online","runtime","icloud","battery"}
    builder: Callable[
        [], list[list[str]]
    ]  # -> list of arg-vectors ([] = nothing to do)
    effort: str = "low"  # Scheduled runs pass this override to either provider.
    timeout: int = 1800
    report: bool = False  # capture stdout into excluded wiki/reports/agents/scheduled


def _slugify(text: str) -> str:
    """Lowercase-hyphen slug matching the wiki's source-page / source-text naming."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _select_ingest_pdfs(
    pdf_names: list[str], ingested_pdf_names: set[str]
) -> list[str]:
    """Select PDFs without a wiki source page citing the original PDF.

    Preprocessed text is input to ingest, not evidence that ingest completed.
    """
    return [name for name in pdf_names if name not in ingested_pdf_names]


def _ingested_raw_references() -> set[str]:
    """Approved immutable inputs cited by a wiki source page.

    Sources may remain in raw/inbox after ingest. Match both wiki and Markdown
    links, including mirrors with relative prefixes and angle-bracket paths.
    """
    from urllib.parse import unquote

    references: set[str] = set()
    srcdir = ROOT / "wiki" / "sources"
    if srcdir.is_dir() and not srcdir.is_symlink():
        pat = re.compile(r"raw/(?:sources|inbox)/[^\]|>)\n]+")
        for page in srcdir.glob("*.md"):
            if page.is_symlink() or not page.is_file():
                continue
            try:
                text = page.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            references.update(
                unquote(match.group().strip().strip("'\"`"))
                for match in pat.finditer(text)
            )
    return references


def _ingested_pdf_names(references: set[str] | None = None) -> set[str]:
    """Preserve the source-PDF selection helper's basename interface."""
    references = _ingested_raw_references() if references is None else references
    return {
        Path(reference).name
        for reference in references
        if reference.startswith("raw/sources/") and reference.lower().endswith(".pdf")
    }


def _ingest_targets() -> list[list[str]]:
    """Unprocessed raw material: inbox files + not-yet-ingested source PDFs.

    See _select_ingest_pdfs for the (pure, tested) rule that decides when a source
    PDF still needs ingesting; this wrapper just supplies the filesystem facts.
    """
    targets: list[Path] = []
    ingested = _ingested_raw_references()
    inbox = ROOT / "raw" / "inbox"
    if inbox.is_dir() and not inbox.is_symlink():
        targets += [
            p
            for p in sorted(inbox.iterdir())
            if p.is_file()
            and not p.is_symlink()
            and not p.name.startswith(".")
            and f"raw/inbox/{p.name}" not in ingested
        ]
    srcs = ROOT / "raw" / "sources"
    if srcs.is_dir() and not srcs.is_symlink():
        pdf_names = [
            p.name
            for p in sorted(srcs.glob("*.pdf"))
            if p.is_file() and not p.is_symlink()
        ]
        selected = _select_ingest_pdfs(pdf_names, _ingested_pdf_names(ingested))
        targets += [srcs / name for name in selected]
    return [["ingest", "--source", str(p)] for p in targets[:3]]  # cap per night


def _project_runner_targets() -> list[list[str]]:
    """Opted-in projects with a clear, due AGENDA task — one arg-vector each.

    Pure-python (no LLM): only real project metadata and AGENDA.md files are read;
    dormant, frozen, linked and malformed projects are skipped.
    Projects whose unreviewed edits have stacked up (is_paused_for_review) are held
    back until the operator runs `wiki.py project agenda ack <slug>`. Capped so a
    night with many due projects cannot blow the shared LLM budget; deferred
    projects stay due and are caught up on the next eligible night.
    """
    today = now_local().date()
    out: list[list[str]] = []
    projects = ROOT / "projects"
    if not projects.is_dir() or projects.is_symlink():
        return out
    for project in sorted(projects.iterdir()):
        slug = project.name
        selected = resolve_proposal_dest(slug)
        if selected is None or agenda.is_paused_for_review(slug):
            continue
        try:
            if not agenda.project_is_due(selected, today):
                continue
        except (OSError, UnicodeError, ValueError):
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
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
        return None
    projects = ROOT / "projects"
    src = projects / slug
    if (
        projects.is_symlink()
        or not src.is_dir()
        or src.is_symlink()
        or src.resolve().parent != projects.resolve()
    ):
        return None
    dst = SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / slug
    completion = dst.parent / f".{slug}.complete.json"
    if dst.exists() or dst.is_symlink():
        try:
            if dst.is_symlink() or not dst.is_dir() or completion.is_symlink():
                raise ValueError(
                    "Snapshot or completion marker is not a regular target"
                )
            metadata = json.loads(completion.read_text(encoding="utf-8"))
            stat = dst.stat()
            expected = {
                "version": 1,
                "project": slug,
                "device": stat.st_dev,
                "inode": stat.st_ino,
            }
            if metadata != expected:
                raise ValueError("Completion marker does not match this snapshot")
        except (OSError, ValueError) as exc:
            log(
                f"defer project {slug}: existing snapshot has no valid completion marker ({exc}); preserved for operator review"
            )
            return None
        return dst
    if completion.exists() or completion.is_symlink():
        log(
            f"defer project {slug}: orphan completion marker preserved for operator review"
        )
        return None
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Only publish a complete copy. A failed cp can leave a partial tree,
        # which must never count as the next invocation's undo snapshot.
        with tempfile.TemporaryDirectory(
            dir=dst.parent, prefix=".snapshot-"
        ) as temporary:
            staged = Path(temporary) / "project"
            for clone in (True, False):
                cmd = ["cp", *(["-c"] if clone else []), "-R", str(src), str(staged)]
                try:
                    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
                except Exception:  # noqa: BLE001 - fall back without retaining partial copies
                    if staged.is_dir() and not staged.is_symlink():
                        shutil.rmtree(staged)
                    elif staged.exists() or staged.is_symlink():
                        staged.unlink()
                    continue
                stat = staged.stat()
                staged_marker = Path(temporary) / "complete.json"
                staged_marker.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "project": slug,
                            "device": stat.st_dev,
                            "inode": stat.st_ino,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                staged.rename(dst)
                # A marker failure leaves a preserved, unmarked tree. It cannot
                # authorize this writer or a later invocation after a restart.
                staged_marker.rename(completion)
                return dst
    except OSError as exc:
        log(f"snapshot {slug} failed: {exc}")
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
        "restore it from tonight's pre-run snapshot. The restore helper preserves",
        "the current project under projects/.restore-backups/ before replacing it:",
        "",
    ]
    for slug in slugs:
        snap = SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / slug
        destination = ROOT / "projects" / slug
        helper = ROOT / "tools" / "schedule" / "restore_project.py"
        lines.append(
            f"- `{slug}`: `python3 {_q(str(helper))} --snapshot {_q(str(snap))} "
            f"--project {_q(str(destination))}`"
        )
    lines.append("")
    lines.append(
        "Once reviewed, resume a project's nightly runs with "
        "`python3 tools/wiki.py project agenda ack <slug>`."
    )
    return "\n".join(lines)


def build_steps() -> list[Step]:
    """Ordered step list. The nightly batch is just the nightly-window steps run
    in this order; cos-brief is the lone morning step."""
    steps = [
        # 1. maintenance: offline, host-native, runs even on a battery night.
        Step(
            "lint",
            "host",
            "daily",
            NIGHTLY_WINDOW,
            [],
            lambda: [["lint", "--json"]],
            timeout=600,
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
        Step(
            "qmd-update",
            "qmd",
            "daily",
            NIGHTLY_WINDOW,
            [],
            lambda: [["update"]],
            timeout=600,
        ),
        Step(
            "qmd-cleanup",
            "qmd",
            "weekly",
            NIGHTLY_WINDOW,
            ["ac"],
            lambda: [["cleanup"]],
            timeout=900,
        ),
        # 2. ingest new raw material (only if any), before optional enhancement.
        Step(
            "ingest",
            "llm",
            "daily",
            NIGHTLY_WINDOW,
            ["ac", "online", "runtime", "icloud"],
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
            ["ac", "online", "runtime", "icloud"],
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
            ["ac", "online", "runtime", "icloud"],
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
            ["ac", "online", "runtime", "icloud"],
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
            ["ac", "online", "runtime", "icloud"],
            _project_runner_targets,
            effort="low",
            timeout=2400,
            report=True,
        ),
    ]
    # 5. Optional wiki enhancement. This is a broad writer and the biggest
    #    budget consumer, so unattended runs require explicit operator opt-in.
    if SCHEDULE_ENHANCE:
        steps.append(
            Step(
                "enhance",
                "llm",
                "daily",
                NIGHTLY_WINDOW,
                ["ac", "online", "runtime", "icloud"],
                lambda: [
                    [
                        "enhance",
                        "--iterations",
                        str(ENHANCE_ITERATIONS),
                        "--strategy",
                        "alternate",
                    ]
                ],
                effort="low",
                timeout=7200,
            )
        )
    # Morning: daily chief-of-staff brief (battery OK, no AC gate).
    steps.append(
        Step(
            "cos-brief",
            "llm",
            "daily",
            MORNING_WINDOW,
            ["online", "runtime", "icloud", "battery"],
            lambda: [["cos", "--mode", "brief"]],
            effort="low",
            timeout=1800,
            report=True,
        )
    )
    return steps


# --------------------------------------------------------------------------- #
# Gates (system probes, cached per tick)
# --------------------------------------------------------------------------- #


class Gates:
    def __init__(self, log: Callable[[str], None], *, read_only: bool = False):
        self.log = log
        self.read_only = read_only
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
        # Require the selected provider endpoint, not merely generic internet.
        return self._nc(BACKEND_HEALTH_HOST, 443)

    def _g_runtime(self) -> bool:
        """Probe the installed native sandbox; never install or start a service."""
        try:
            return runtime_available(root=ROOT, cli=CLI)
        except (OSError, ValueError) as exc:
            self.log(f"native sandbox unavailable: {exc}")
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
        if self.read_only:
            return REPORTS_DIR.is_dir()
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
    except OSError as error:
        return 127, str(error)


def run_qmd(args: list[str], timeout: int) -> tuple[int, str]:
    """Run qmd on the host so it can use Metal and its persistent host index."""
    try:
        p = subprocess.run(
            [QMD, *args], capture_output=True, text=True, timeout=timeout, cwd=str(ROOT)
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as error:
        return 127, str(error)


def exec_brain_wiki(
    args: list[str], acct: str, effort: str, timeout: int
) -> tuple[int, str]:
    command = build_brain_wiki_args(args, effort)
    env = dict(os.environ)
    # Drop the obsolete shell-launcher switch during migration. Native runs
    # own their subprocess tree and never reuse a global runtime session.
    env.pop("BRAIN_KEEP_WARM", None)
    # The explicit script path and cwd bind the run to this deployment. Caller
    # shell configuration and unrelated vault fallbacks cannot reroute it.
    # `acct` is the cooldown-ledger identity. Each CLI authenticates through its
    # own login, so no secret or per-exec credential steering happens here.
    return _run_agent_process(command, timeout, env, LOG_DIR, cwd=ROOT)


def _run_agent_process(
    command: list[str],
    timeout: float,
    env: dict,
    log_dir: Path,
    *,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """Cancel the wrapper process group and retain output without pipe deadlocks.

    The native launcher and its provider run in this owned process group.
    Return 125 on timeout or signal death so callers retain the conservative
    recovery latch until the operator verifies all descendants have stopped.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.NamedTemporaryFile(
            mode="w+",
            encoding="utf-8",
            prefix="agent-",
            suffix=".stdout.log",
            dir=log_dir,
            delete=False,
        ) as stdout,
        tempfile.NamedTemporaryFile(
            mode="w+",
            encoding="utf-8",
            prefix="agent-",
            suffix=".stderr.log",
            dir=log_dir,
            delete=False,
        ) as stderr,
    ):
        process = subprocess.Popen(
            command,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=True,
            cwd=str(cwd) if cwd is not None else None,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            cancellation = _cancel_agent_group(process)
            return 125, (
                f"Agent timed out; {cancellation}. "
                "Agent descendant termination is UNCONFIRMED; unattended LLM work is blocked. "
                f"Partial stdout: {stdout.name}; stderr: {stderr.name}."
            )
        except BaseException as exc:
            try:
                cancellation = _cancel_agent_group(process)
            except BaseException as cleanup_error:
                cancellation = (
                    f"Local cleanup also failed: {type(cleanup_error).__name__}"
                )
            exc.add_note(
                f"{cancellation}. Partial stdout: {stdout.name}; stderr: {stderr.name}. Inner termination is unconfirmed."
            )
            raise
        if process.returncode < 0:
            cancellation = _cancel_agent_group(process)
            return 125, (
                f"Agent wrapper terminated by signal {-process.returncode}; {cancellation}. "
                "Agent descendant termination is UNCONFIRMED; unattended LLM work is blocked. "
                f"Partial stdout: {stdout.name}; stderr: {stderr.name}."
            )
        stdout.seek(0)
        stderr.seek(0)
        output = _agent_output(process.returncode, stdout.read(), stderr.read())
        if process.returncode == 0:
            Path(stdout.name).unlink()
            Path(stderr.name).unlink()
        return process.returncode, output


def _cancel_agent_group(process) -> str:
    """Best-effort local cleanup; do not claim anything about detached work."""
    issues = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except OSError as exc:
            issues.append(f"{sig.name}: {exc}")
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        issues.append("local leader did not exit after cancellation")
    return "host process group cancellation attempted" + (
        f" ({'; '.join(issues)})" if issues else ""
    )


def _agent_output(returncode: int, stdout: str | None, stderr: str | None) -> str:
    """Keep successful reports clean while retaining failure diagnostics.

    The native agent writes its final answer to stdout and launcher/runtime
    traces to stderr. A successful run therefore reports stdout only. On failure,
    both streams remain available to failure classification and the operator.
    """
    if returncode == 0:
        return stdout or ""
    return (stdout or "") + (stderr or "")


_REPORT_DIAGNOSTIC_PATTERNS = (
    re.compile(r"^\[post-start\] (?:Ready\.|Refreshing qmd index snapshot).*$"),
    re.compile(r"^\[cos\] (?:Gathering live context|Inbox mode:).*$"),
    re.compile(r"^Invoking [a-z-]+ agent with (?:claude|codex)(?: \(.+\))?$"),
    re.compile(r"^Effort: (?:low|medium|high|xhigh)$"),
    re.compile(r"^Agent: wiki-[a-z-]+\.md$"),
)


def clean_scheduled_report(text: str) -> str:
    """Remove known launcher progress lines from a successful report body.

    Security warnings and arbitrary tool output are retained. Producers now send
    progress to stderr; this fallback also handles older saved wrapper output.
    """
    lines = [
        line
        for line in text.splitlines()
        if not any(pattern.match(line) for pattern in _REPORT_DIAGNOSTIC_PATTERNS)
    ]
    return "\n".join(lines).strip()


def build_brain_wiki_args(args: list[str], effort: str) -> list[str]:
    """Build a native command with role selection frozen for the scheduled batch.

    The historical helper name remains a compatibility seam for recovery tests
    and callers. No shell launcher is involved in the returned argument vector.
    """
    if _PROVIDER_ERROR:
        raise ValueError(f"LLM configuration invalid: {_PROVIDER_ERROR}")
    parts = [PYTHON, str(ROOT / "tools" / "agents" / "wiki-agent.py"), *args]

    def has_option(name: str) -> bool:
        return any(arg == name or arg.startswith(name + "=") for arg in args)

    if not has_option("--access-profile"):
        parts.extend(
            [
                "--access-profile",
                default_access_profile(args[0] if args else "", root=ROOT),
            ]
        )
    # A scheduled ingest grants exactly the approved source it selected. The
    # runtime validates the path before exposing it to the provider.
    if args and args[0] == "ingest":
        for index, arg in enumerate(args):
            if arg == "--source" and index + 1 < len(args):
                parts.extend(["--read-path", args[index + 1]])
            elif arg.startswith("--source="):
                parts.extend(["--read-path", arg.partition("=")[2]])
    for index, arg in enumerate(args):
        if arg == "--cli":
            if index + 1 >= len(args) or args[index + 1] != CLI:
                raise ValueError("Scheduled jobs must use the batch's frozen provider")
        elif arg.startswith("--cli=") and arg.partition("=")[2] != CLI:
            raise ValueError("Scheduled jobs must use the batch's frozen provider")
    if not has_option("--cli"):
        parts.extend(["--cli", CLI])
    if not has_option("--effort"):
        parts.extend(["--effort", effort])
    if not has_option("--model"):
        role = args[0] if args else ""
        model = MODEL or ROLE_MODELS.get(role, "")
        # Even an empty model is explicit: the launcher must not resolve the
        # role again from configuration that changed after this batch started.
        parts.extend(["--model", model])
    return parts


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
    """Run one LLM invocation on the selected backend.

    Returns (status, backend, output), status in {ok, transient, deferred}. A
    usage-limit (quota/ratelimit) hit, or a cooldown still in effect from an
    earlier hit, defers the job; the cooldown also defers the rest of the LLM
    batch this tick (see _run_steps).
    """
    if _PROVIDER_ERROR:
        return "transient", "", f"LLM configuration invalid: {_PROVIDER_ERROR}"
    acct = ACCOUNTS[0]
    if ledger.get("cancellation_pending") or "agent_in_flight" in ledger:
        return (
            "cancelled-unconfirmed",
            acct,
            "Prior cancellation needs operator acknowledgement.",
        )
    if not backend_available(ledger, now):
        return "deferred", acct, ""  # cooldown from an earlier usage-limit
    log(f"    -> backend {acct}")
    ledger["agent_in_flight"] = {
        "since": iso(now),
        "backend": acct,
        "task": args[0] if args else "unknown",
    }
    # Persist before launch. A hard kill can bypass every exception/finally path.
    save_ledger(ledger)
    try:
        rc, out = exec_brain_wiki(args, acct, effort, timeout)
    except BaseException as exc:
        detail = (
            f"Model invocation interrupted ({type(exc).__name__}); inner termination unconfirmed. "
            + " ".join(getattr(exc, "__notes__", []))
        )
        ledger["cancellation_pending"] = {"since": iso(now), "detail": detail}
        try:
            save_ledger(ledger)
        except Exception as save_error:
            exc.add_note(
                f"Could not persist cancellation detail: {save_error}; the pre-launch in-flight marker remains the recovery boundary."
            )
        raise
    if rc == 125:
        ledger["cancellation_pending"] = {"since": iso(now), "detail": out}
        # Persist before another step or a later exception can lose this latch.
        save_ledger(ledger)
        log(out)
        return "cancelled-unconfirmed", acct, out
    ledger.pop("agent_in_flight", None)
    save_ledger(ledger)
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
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError("Scheduled report name must be a simple job name")
    filename = f"scheduled-{name}-{now:%Y-%m-%d}.md"
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
    return _write_report_text(filename, header + text.strip() + "\n", private=True)


@contextlib.contextmanager
def _report_directory(*, private: bool, create: bool = True):
    """Open every directory without following links; retain the final descriptor."""
    if REPORTS_DIR != ROOT / "wiki" / "reports":
        raise ValueError(
            "Report destination must be this vault's wiki/reports directory"
        )
    parts = (
        ("wiki", "reports", "agents", "scheduled") if private else ("wiki", "reports")
    )
    descriptors = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent = os.open(ROOT, flags)
        descriptors.append(parent)
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            parent = os.open(part, flags, dir_fd=parent)
            descriptors.append(parent)
        destination = ROOT.joinpath(*parts)
    except FileNotFoundError:
        if not create:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            yield None
            return
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise ValueError("Report destination is unavailable") from None
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise ValueError(
            "Report destination must use real directories without links"
        ) from exc
    try:
        yield destination, parent
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_report_text(filename: str, text: str, *, private: bool) -> Path:
    """Atomically replace a host-owned report using only its directory descriptor."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.md", filename):
        raise ValueError("Report filename must be a simple Markdown filename")
    with _report_directory(private=private) as (destination, directory):
        try:
            metadata = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"Report target is a link: {filename}")
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Report target must be a regular file without aliases")
        temporary = f".schedule-report-{secrets.token_hex(12)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            # Keep the descriptor for all mutations, and refuse to publish if
            # a directory was renamed or replaced while the report was staged.
            with _report_directory(private=private, create=False) as current:
                if current is None or not os.path.samestat(
                    os.fstat(current[1]), os.fstat(directory)
                ):
                    raise ValueError("Report destination changed during the write")
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        return destination / filename


STATUS_OK = ("ok", "noop")
_STATUS_RESULTS = {
    *STATUS_OK,
    "timeout",
    "execution-failed",
    "transient",
    "deferred",
    "snapshot-failed",
    "cancelled-unconfirmed",
}
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
        value = rec.get("last_result")
        result = (
            value
            if isinstance(value, str) and value in _STATUS_RESULTS
            else ("unknown" if value is not None else None)
        )
        ok_dt = parse(last_ok) if last_ok else None
        last_ok_s = ok_dt.strftime("%Y-%m-%d %H:%M") if ok_dt else "never"
        is_fail = result is not None and result not in STATUS_OK
        is_stale = ok_dt is None or (now - ok_dt).days > _STALE_DAYS.get(period, 2)
        streak = rec.get("fail_streak", 0)
        if type(streak) is not int or streak < 0:
            streak = 0
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
        lines += [
            "",
            f"Backend limited (LLM batch deferred): {len(limited)} backend(s)",
        ]
    return "\n".join(lines)


def write_schedule_status(ledger: dict, steps: list["Step"], now: datetime) -> Path:
    """Mirror the run ledger into the vault as a compact health page.

    The live ledger lives at ~/.brain (outside the vault and the agent sandbox),
    so the CoS cannot read it directly; this rolling page in wiki/reports lets the
    morning brief surface nightly-batch failures.
    """
    body = format_schedule_status(
        ledger.get("jobs", {}),
        ledger.get("accounts", {}),
        [(s.name, s.period) for s in steps],
        now,
    )
    if _PROVIDER_ERROR:
        body = (
            "WARNING: LLM configuration invalid; model jobs blocked. Inspect host diagnostics.\n\n"
            + body
        )
    if ledger.get("cancellation_pending") or "agent_in_flight" in ledger:
        body = (
            "WARNING: unattended LLM work blocked; inner termination unconfirmed. Inspect host diagnostics before recovery.\n\n"
            + body
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
    return _write_report_text(
        "schedule-status.md", header + body.strip() + "\n", private=False
    )


def _reports_to_prune(
    names: list[str],
    retention: int,
    retention_by_type: dict[str, int] | None = None,
) -> list[str]:
    """Pure: of `scheduled-<type>-<date>.md` names, those to delete to keep only
    the latest `retention` per type. Any other filename is ignored, so
    schedule-status.md and hand-written reports are never touched. Tested directly."""
    pat = re.compile(r"scheduled-([a-z0-9][a-z0-9-]*)-(\d{4}-\d{2}-\d{2})\.md")
    groups: dict[str, list[tuple[str, str]]] = {}
    for n in names:
        m = pat.fullmatch(n)
        if m:
            groups.setdefault(m.group(1), []).append((m.group(2), n))
    out: list[str] = []
    per_type = retention_by_type or {}
    for report_type, items in groups.items():
        items.sort()  # date strings sort chronologically; oldest first
        keep = per_type.get(report_type, retention)
        stale = items[:-keep] if keep > 0 else items
        out.extend(n for _d, n in stale)
    return out


def prune_reports(retention: int = REPORT_RETENTION) -> list[str]:
    """Prune only the excluded wiki/reports/agents/scheduled content directory.
    Touches only scheduled-<type>-<date>.md (the
    dispatcher's own outputs), never schedule-status.md or other files. The CoS is
    read-only, so report hygiene lives here, on the host side that writes them."""
    removed: list[str] = []
    with _report_directory(private=True, create=False) as opened:
        if opened is None:
            return []
        _destination, directory = opened
        names = os.listdir(directory)
        for name in _reports_to_prune(names, retention, REPORT_RETENTION_BY_TYPE):
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    os.unlink(name, dir_fd=directory)
                    removed.append(name)
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


def strip_cos_proposals(text: str) -> str:
    """Remove the legacy final Proposals block from a Chief of Staff brief.

    Chief of Staff output is advisory. The scheduler neither stores nor routes
    this machine-readable tail.
    """
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Proposals":
            return "\n".join(lines[:i]).rstrip()
    return text or ""


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
    opted-in project. Model output cannot authorize a path or write to a dormant
    project. Traversal, links, frozen projects and malformed agendas fail closed.
    """
    base = Path(projects_dir) if projects_dir is not None else (ROOT / "projects")
    slug = (target or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
        return None
    project = base / slug
    cand = project / "AGENDA.md"
    metadata = project / "project.md"
    if (
        base.is_symlink()
        or project.is_symlink()
        or cand.is_symlink()
        or metadata.is_symlink()
        or not cand.is_file()
        or not metadata.is_file()
        or project.resolve().parent != base.resolve()
    ):
        return None
    try:
        frontmatter = agenda.parse_frontmatter(cand.read_text(encoding="utf-8"))
        if not agenda.is_enabled(frontmatter) or is_frozen_project(project):
            return None
    except (OSError, UnicodeError, ValueError):
        return None
    return cand


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
                f"routing: '{it['target']}' is not an active project; "
                f"'{it['task'][:50]}' left advisory"
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


def _lint_findings(step: Step, args: list[str], rc: int, output: str) -> bool:
    """Recognize a completed lint report, never an arbitrary process failure."""
    if step.kind != "host" or args != ["lint", "--json"] or rc != 1:
        return False
    try:
        report = json.loads(output)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(report, dict)
        and type(report.get("error_count")) is int
        and report["error_count"] > 0
        and isinstance(report.get("errors"), dict)
        and type(report.get("pages_checked")) is int
    )


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
        bool(ledger.get("cancellation_pending")) or "agent_in_flight" in ledger
    )
    # One routing guard per tick: its cap + cycle-blocks span project-runner
    # handoffs produced during this dispatcher run.
    routing_guard = RoutingGuard()
    for step in steps:
        if not step_due(step, ledger, now):
            continue
        if step.kind == "llm" and _PROVIDER_ERROR:
            log(f"skip {step.name}: LLM configuration invalid ({_PROVIDER_ERROR})")
            continue
        if step.kind == "llm" and llm_blocked:
            log(
                f"skip {step.name}: LLM jobs blocked (quota or unresolved cancellation)"
            )
            continue
        ok, missing = gates.check(step.gates)
        if not ok:
            log(f"skip {step.name}: gate '{missing}' not satisfied")
            continue
        invocations = step.builder()
        if dry_run:
            log(f"WOULD RUN {step.name} ({len(invocations)} invocation(s))")
            continue
        if not invocations:
            log(f"{step.name}: nothing to do; marking done")
            _record(ledger, step.name, now, "noop")
            save_ledger(ledger)
            continue

        all_ok = True
        outcome = "ok"
        report_chunks: list[str] = []  # collected across invocations, written once
        ran_slugs: list[str] = []  # project-runner: projects actually executed
        for args in invocations:
            log(f"run {step.name}: {' '.join(args)}")
            if step.kind in {"host", "qmd"}:
                runner = run_host if step.kind == "host" else run_qmd
                rc, out = runner(args, step.timeout)
                if rc == 124:
                    all_ok = False
                    outcome = "timeout"
                    log(f"  {step.name} timed out; will retry next tick")
                elif rc != 0 and not _lint_findings(step, args, rc, out):
                    all_ok = False
                    outcome = "execution-failed"
                    log(f"  {step.name} failed (rc={rc}); will retry next tick")
                else:
                    if rc != 0:
                        log(f"  {step.name} reported lint findings (rc={rc})")
                        notify("Brain schedule", f"{step.name}: issues found (rc={rc})")
                    if step.report:
                        report_chunks.append(out)
            else:  # llm
                # apply-don't-commit undo: clone the project before the runner edits
                # it (projects/ is gitignored, so this snapshot is the only revert).
                slug = _runner_slug(args) if step.name == "project-runner" else None
                if slug:
                    if _snapshot_project(slug, now, log) is None:
                        all_ok = False
                        outcome = "snapshot-failed"
                        log(
                            f"skip project {slug}: no complete undo snapshot; will retry next tick"
                        )
                        continue
                status, who, out = run_llm(
                    args, step.effort, step.timeout, ledger, now, log
                )
                if status == "ok":
                    if slug:
                        # Only edit-producing passes advance the stacking guard.
                        agenda.record_run(slug, _parse_executed(out), iso(now))
                        ran_slugs.append(slug)
                    if step.report:
                        clean_out = clean_scheduled_report(out)
                        report_chunks.append(
                            strip_cos_proposals(clean_out)
                            if step.name == "cos-brief"
                            else clean_out
                        )
                    # Project-runner `handoff::` lines remain the guarded
                    # cross-project bus. Chief of Staff advice stays advisory.
                    if slug:
                        route_handoffs(out, slug, now, log, routing_guard)
                elif status == "cancelled-unconfirmed":
                    all_ok = False
                    outcome = "cancelled-unconfirmed"
                    llm_blocked = True
                    log(
                        f"  {step.name}: inner termination unconfirmed; aborting LLM batch"
                    )
                    break
                elif status == "deferred":
                    all_ok = False
                    outcome = "deferred"
                    llm_blocked = True
                    log(f"  {step.name} deferred: {who}")
                    notify("Brain schedule", "LLM backend usage limited; jobs deferred")
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
            notify(
                "Brain schedule", f"{step.name} ready: {f.relative_to(ROOT).as_posix()}"
            )

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
        save_ledger(ledger)


def cmd_run(dry_run: bool = False) -> int:
    log = print if dry_run else make_logger()
    lock = None if dry_run else acquire_lock()
    if lock is None and not dry_run:
        log("another dispatcher run holds the lock; exiting")
        return 0
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupted(_signum, _frame):
        raise InterruptedError("Dispatcher interrupted by SIGTERM")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        # Recovery must still run when model configuration or the ledger fails.
        if not dry_run:
            keepawake_off(lambda _m: None)
        ledger = load_ledger()
        now = now_local()
        gates = Gates(log, read_only=dry_run)
        steps = build_steps()
        log(f"tick {now:%Y-%m-%d %H:%M} (dry-run={dry_run})")
        if _PROVIDER_ERROR:
            log(f"LLM configuration invalid; model jobs blocked: {_PROVIDER_ERROR}")

        # AC-gated lid-close keep-awake: override sleep with the lid CLOSED only
        # when on AC (battery -> never, so a closed bag can't overheat). Engage
        # only if LLM work is actually due and runnable (online + runtime).
        on_ac = gates.get("ac")
        lid = lid_closed()
        any_llm_due = (
            not _PROVIDER_ERROR
            and not ledger.get("cancellation_pending")
            and any(step_due(s, ledger, now) for s in steps if s.kind == "llm")
        )
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
            and gates.get("runtime")
            and keepawake_on(log)
        )

        try:
            _run_steps(steps, ledger, gates, now, dry_run, log)
            if not dry_run:
                save_ledger(ledger)
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
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        try:
            if lock is not None:
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
    if _PROVIDER_ERROR:
        print(f"LLM CONFIGURATION INVALID: {_PROVIDER_ERROR}")
        print("Model jobs blocked; host maintenance and recovery remain available.\n")
    if ledger.get("cancellation_pending"):
        print("UNATTENDED LLM WORK BLOCKED: inner cancellation is unconfirmed.")
        print(ledger["cancellation_pending"]["detail"])
    print(
        "nightly wiki enhancement: "
        + (
            "enabled"
            if SCHEDULE_ENHANCE
            else "paused (opt in with VAULTLENS_SCHEDULE_ENHANCE=1)"
        )
        + "\n"
    )
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
    for acct in sorted(set(ACCOUNTS) | set(ledger["accounts"])):
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


def acknowledge_cancellation() -> int:
    """Operator-only recovery after verifying the timed-out inner work stopped."""
    lock = acquire_lock()
    if lock is None:
        print(
            "Dispatcher is running; cancellation cannot be acknowledged.",
            file=sys.stderr,
        )
        return 1
    try:
        ledger = load_ledger()
        ledger.pop("cancellation_pending", None)
        ledger.pop("agent_in_flight", None)
        save_ledger(ledger)
        print("Cancellation acknowledged. Due LLM work may resume on the next tick.")
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Brain scheduled-agent dispatcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one dispatcher tick")
    run.add_argument(
        "--dry-run", action="store_true", help="evaluate gates/due-ness, run nothing"
    )
    sub.add_parser("status", help="human-readable ledger view")
    acknowledge = sub.add_parser(
        "acknowledge-cancellation",
        help="clear timeout latch only after verifying inner agent work has stopped",
    )
    acknowledge.add_argument(
        "--confirmed-inner-stopped",
        action="store_true",
        required=True,
        help="operator attestation that the timed-out inner workload has stopped",
    )
    args = p.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(dry_run=args.dry_run)
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "acknowledge-cancellation":
        return acknowledge_cancellation()
    return 1


if __name__ == "__main__":
    sys.exit(main())
