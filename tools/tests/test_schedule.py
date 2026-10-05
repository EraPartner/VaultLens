#!/usr/bin/env python3
"""Self-contained tests for the scheduling dispatcher's pure decision logic.

Exercises the side-effect-free helpers (failure classification, account
failover selection, cooldown/backoff, step due-ness) without touching the
system. Run with:

    python3 tools/tests/test_schedule.py
"""

from __future__ import annotations

import os
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "schedule"))

import dispatch  # noqa: E402
from agent_profiles import AGENT_FILES  # noqa: E402
from llm_provider import load_config, load_profile_models  # noqa: E402

# One alias per private name the suite exercises (tests legitimately touch internals).
_env_flag = dispatch._env_flag  # pyright: ignore[reportPrivateUsage]  # tests exercise internals
_slugify = dispatch._slugify  # pyright: ignore[reportPrivateUsage]  # tests exercise internals
_select_ingest_pdfs = dispatch._select_ingest_pdfs  # pyright: ignore[reportPrivateUsage]  # tests exercise internals
_agent_output = dispatch._agent_output  # pyright: ignore[reportPrivateUsage]  # tests exercise internals
_record = dispatch._record  # pyright: ignore[reportPrivateUsage]  # tests exercise internals
_reports_to_prune = dispatch._reports_to_prune  # pyright: ignore[reportPrivateUsage]  # tests exercise internals

# Mutable counters in a dict so check() needs no module-level rebinding of constants.
_COUNTS = {"passed": 0, "failed": 0}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _COUNTS["passed"] += 1
        print(f"  PASS  {name}")
    else:
        _COUNTS["failed"] += 1
        print(f"  FAIL  {name}  {detail}")


def fresh_ledger() -> dispatch.Ledger:
    return {
        "jobs": {},
        "accounts": {
            a: {"limited_until": None, "last_error": None, "backoff": 0}
            for a in dispatch.ACCOUNTS
        },
    }


def main() -> int:
    now = datetime(2026, 6, 7, 3, 30).astimezone()  # a Sunday at 03:30

    print("command interface:")
    original_cmd_status = dispatch.cmd_status
    try:
        dispatch.cmd_status = lambda: 37
        check(
            "status is a subcommand",
            dispatch.main(["status"]) == 37,
        )
    finally:
        dispatch.cmd_status = original_cmd_status

    print("environment flags:")
    flag_name = "VAULTLENS_TEST_BOOLEAN_FLAG"
    original_flag = os.environ.get(flag_name)
    try:
        os.environ[flag_name] = "yes"
        check("true environment flag", _env_flag(flag_name) is True)
        os.environ[flag_name] = "off"
        check("false environment flag", _env_flag(flag_name) is False)
        os.environ[flag_name] = "invalid"
        try:
            _env_flag(flag_name)
        except ValueError:
            invalid_rejected = True
        else:
            invalid_rejected = False
        check("invalid environment flag rejected", invalid_rejected)
    finally:
        if original_flag is None:
            os.environ.pop(flag_name, None)
        else:
            os.environ[flag_name] = original_flag

    print("classify_failure:")
    check("rc 0 -> ok", dispatch.classify_failure(0, "all good") == "ok")
    check(
        "quota text -> quota",
        dispatch.classify_failure(1, "Premium request quota exceeded") == "quota",
    )
    check(
        "429 -> ratelimit",
        dispatch.classify_failure(1, "HTTP 429 Too Many Requests") == "ratelimit",
    )
    check(
        "other -> transient",
        dispatch.classify_failure(1, "connection reset") == "transient",
    )
    for response in (
        "You've hit your session limit. Try again later.",
        "You've hit your limit · resets 6pm (Europe/Helsinki)",
        "You’ve hit your weekly limit",
        "Monthly spend limit reached",
        "Extra usage spending limit reached",
        "Credit balance is too low",
        '{"error":{"type":"usage_limit","message":"Try again later"}}',
    ):
        check(
            f"Claude limit -> quota: {response}",
            dispatch.classify_failure(1, response) == "quota",
        )

    print("backend availability / cooldown:")
    led = fresh_ledger()
    check(
        "backend available when healthy", dispatch.backend_available(led, now) is True
    )
    dispatch.mark_limited(led, dispatch.ACCOUNTS[0], "ratelimit", now)
    check(
        "limited_until set after ratelimit",
        led["accounts"][dispatch.ACCOUNTS[0]]["limited_until"] is not None,
    )
    check(
        "backend unavailable while limited",
        dispatch.backend_available(led, now) is False,
    )
    dispatch.mark_limited(led, dispatch.ACCOUNTS[0], "quota", now)
    check(
        "still unavailable after quota", dispatch.backend_available(led, now) is False
    )

    print("cooldown semantics:")
    led2 = fresh_ledger()
    dispatch.mark_limited(led2, dispatch.ACCOUNTS[0], "ratelimit", now)
    first = dispatch.parse(led2["accounts"][dispatch.ACCOUNTS[0]]["limited_until"])
    check("ratelimit cooldown ~30m", abs((first - now).total_seconds() - 1800) < 5)
    dispatch.mark_limited(led2, dispatch.ACCOUNTS[0], "ratelimit", now)
    second = dispatch.parse(led2["accounts"][dispatch.ACCOUNTS[0]]["limited_until"])
    check("backoff doubles to ~1h", abs((second - now).total_seconds() - 3600) < 5)
    dispatch.mark_limited(led2, dispatch.ACCOUNTS[0], "quota", now)
    q = dispatch.parse(led2["accounts"][dispatch.ACCOUNTS[0]]["limited_until"])
    check("quota cooldown ~24h", abs((q - now).total_seconds() - 24 * 3600) < 5)

    print("expired cooldown frees the backend:")
    led3 = fresh_ledger()
    past = now - timedelta(hours=1)
    led3["accounts"][dispatch.ACCOUNTS[0]]["limited_until"] = dispatch.iso(past)
    check(
        "expired limit -> backend available again",
        dispatch.backend_available(led3, now) is True,
    )

    print("clear_account on success:")
    led4 = fresh_ledger()
    dispatch.mark_limited(led4, dispatch.ACCOUNTS[0], "ratelimit", now)
    dispatch.clear_account(led4, dispatch.ACCOUNTS[0])
    st = led4["accounts"][dispatch.ACCOUNTS[0]]
    check("cleared limit + backoff", st["limited_until"] is None and st["backoff"] == 0)

    print("step_due:")
    steps = {s.name: s for s in dispatch.build_steps()}
    led5 = fresh_ledger()
    lint = steps["lint"]
    check(
        "daily step due when never run (in window)",
        dispatch.step_due(lint, led5, now) is True,
    )
    led5["jobs"]["lint"] = {"last_ok": dispatch.iso(now)}
    check("daily step not due same day", dispatch.step_due(lint, led5, now) is False)
    tomorrow = now + timedelta(days=1)
    check("daily step due next day", dispatch.step_due(lint, led5, tomorrow) is True)

    morning = now.replace(hour=9)
    night_only = now.replace(hour=3)
    brief = steps["cos-brief"]
    check(
        "cos-brief due in morning window",
        dispatch.step_due(brief, fresh_ledger(), morning) is True,
    )
    check(
        "cos-brief not due at 03:00",
        dispatch.step_due(brief, fresh_ledger(), night_only) is False,
    )

    weekly = steps["contradict"]
    led6 = fresh_ledger()
    check("weekly due when never run", dispatch.step_due(weekly, led6, now) is True)
    led6["jobs"]["contradict"] = {"last_ok": dispatch.iso(now - timedelta(days=2))}
    check("weekly not due 2 days later", dispatch.step_due(weekly, led6, now) is False)
    led6["jobs"]["contradict"] = {"last_ok": dispatch.iso(now - timedelta(days=9))}
    weekday = now + timedelta(days=3)  # a Wednesday, age >= 8 -> catch up
    check(
        "weekly catches up when overdue >8d",
        dispatch.step_due(weekly, led6, weekday) is True,
    )

    print("ingest target selection:")
    check(
        "slugify matches source-text convention",
        _slugify("Cryptology and Error Correction")
        == "cryptology-and-error-correction",
    )
    check(
        "PDF with a wiki source page is skipped",
        _select_ingest_pdfs(
            ["Cryptology and Error Correction.pdf"],
            {"Cryptology and Error Correction.pdf"},
        )
        == [],
    )
    check(
        "new PDF is selected",
        _select_ingest_pdfs(["Brand New.pdf"], set()) == ["Brand New.pdf"],
    )

    print("scheduler status summary:")
    nowt = datetime(2026, 6, 20, 7, 0).astimezone()
    meta = [
        ("lint", "daily"),
        ("enhance", "daily"),
        ("cos-brief", "daily"),
        ("contradict", "weekly"),
    ]
    healthy = {n: {"last_ok": dispatch.iso(nowt), "last_result": "ok"} for n, _ in meta}
    check(
        "all-healthy verdict",
        "all scheduled jobs healthy"
        in dispatch.format_schedule_status(healthy, {}, meta, nowt),
    )
    failed = dict(healthy)
    failed["cos-brief"] = {
        "last_ok": dispatch.iso(nowt - timedelta(days=4)),
        "last_result": "transient",
    }
    s_fail = dispatch.format_schedule_status(failed, {}, meta, nowt)
    check("failing job named in verdict", "cos-brief" in s_fail and "failing" in s_fail)
    never: dict[str, dict[str, object]] = {n: {} for n, _ in meta}  # never run -> stale, not failing
    s_stale = dispatch.format_schedule_status(never, {}, meta, nowt)
    check(
        "never-run jobs read as stale", "stale" in s_stale and "failing" not in s_stale
    )
    accts = {
        dispatch.BACKEND_IDENTITY: {
            "limited_until": dispatch.iso(nowt + timedelta(hours=2))
        }
    }
    s_lim = dispatch.format_schedule_status(healthy, accts, meta, nowt)
    check(
        "backend cooldown surfaced",
        "Backend limited" in s_lim and "1 backend(s)" in s_lim,
    )

    print("backend command selection:")
    native_prefix = [
        dispatch.PYTHON,
        str(dispatch.ROOT / "tools" / "agents" / "wiki-agent.py"),
    ]
    original_cli, original_model = dispatch.CLI, dispatch.MODEL
    original_role_models = dispatch.ROLE_MODELS
    try:
        dispatch.CLI, dispatch.MODEL = "claude", "sonnet"
        claude_parts = dispatch.build_brain_wiki_args(["search"], "high")
        check("native Python entrypoint selected", claude_parts[:2] == native_prefix)
        check("Claude backend selected", claude_parts[-6:-4] == ["--cli", "claude"])
        check("Claude model pinned", claude_parts[-2:] == ["--model", "sonnet"])

        dispatch.CLI, dispatch.MODEL = "codex", ""
        dispatch.ROLE_MODELS = {"search": ""}
        codex_parts = dispatch.build_brain_wiki_args(["search"], "medium")
        check("Codex backend selected", "codex" in codex_parts)
        check(
            "Codex default model frozen for the batch",
            codex_parts[-2:] == ["--model", ""],
        )
        dispatch.ROLE_MODELS = {"search": "custom-standard", "enhance": "custom-deep"}
        check(
            "scheduled role model selected",
            dispatch.build_brain_wiki_args(["enhance"], "low")[-2:]
            == ["--model", "custom-deep"],
        )
        check(
            "scheduled effort overrides role effort",
            dispatch.build_brain_wiki_args(["enhance"], "low")[-4:-2]
            == ["--effort", "low"],
        )
        for model_args in (
            ["--model", "caller-model"],
            ["--model=caller-model"],
            ["--model", ""],
            ["--model="],
        ):
            caller_args = ["search", *model_args]
            parts = dispatch.build_brain_wiki_args(caller_args, "low")
            check(
                f"caller model override retained {model_args!r}",
                parts
                == [
                    *native_prefix,
                    *caller_args,
                    "--access-profile",
                    "wiki-read",
                    "--cli",
                    "codex",
                    "--effort",
                    "low",
                ],
            )
        for effort_args in (["--effort", "high"], ["--effort=xhigh"]):
            caller_args = ["search", *effort_args]
            parts = dispatch.build_brain_wiki_args(caller_args, "low")
            check(
                f"caller effort override retained {effort_args!r}",
                parts
                == [
                    *native_prefix,
                    *caller_args,
                    "--access-profile",
                    "wiki-read",
                    "--cli",
                    "codex",
                    "--model",
                    "custom-standard",
                ],
            )
        for cli_args in (["--cli", "codex"], ["--cli=codex"]):
            caller_args = ["search", *cli_args]
            check(
                f"matching caller provider retained {cli_args!r}",
                dispatch.build_brain_wiki_args(caller_args, "low")
                == [
                    *native_prefix,
                    *caller_args,
                    "--access-profile",
                    "wiki-read",
                    "--effort",
                    "low",
                    "--model",
                    "custom-standard",
                ],
            )
        for cli_args in (["--cli", "claude"], ["--cli=claude"]):
            try:
                dispatch.build_brain_wiki_args(["search", *cli_args], "low")
            except ValueError:
                mismatch_rejected = True
            else:
                mismatch_rejected = False
            check(
                "caller cannot mix a provider with the batch's model mapping",
                mismatch_rejected,
            )
        for role, access in (
            ("cos", "cos-read"),
            ("contradict", "wiki-read"),
            ("emerge", "wiki-read"),
            ("discover", "wiki-read"),
            ("ingest", "wiki-write"),
            ("enhance", "wiki-write"),
            ("project-run", "project-write"),
        ):
            parts = dispatch.build_brain_wiki_args([role], "low")
            check(
                f"scheduled {role} access profile is explicit",
                parts[parts.index("--access-profile") + 1] == access,
            )
        explicit_access = dispatch.build_brain_wiki_args(
            ["search", "--access-profile", "custom-report"], "low"
        )
        check(
            "explicit caller access profile retained",
            explicit_access.count("--access-profile") == 1
            and explicit_access[explicit_access.index("--access-profile") + 1]
            == "custom-report",
        )
        source_parts = dispatch.build_brain_wiki_args(
            ["ingest", "--source", "raw/inbox/approved source.pdf"], "low"
        )
        check(
            "scheduled ingest grants its exact selected source",
            source_parts[source_parts.index("--read-path") + 1]
            == "raw/inbox/approved source.pdf",
        )
    finally:
        dispatch.CLI, dispatch.MODEL = original_cli, original_model
        dispatch.ROLE_MODELS = original_role_models

    print("scheduled model configuration snapshots:")
    with tempfile.TemporaryDirectory() as temporary:
        fixture_root = Path(temporary)
        role_dir = fixture_root / ".agents" / "roles"
        role_dir.mkdir(parents=True)
        for filename in AGENT_FILES.values():
            source = dispatch.ROOT / ".agents" / "roles" / filename
            (role_dir / filename).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
        config_path = fixture_root / "llm.local.json"
        profiles_path = fixture_root / "model-profiles.json"
        profile_models = {
            provider: {"standard": f"{provider}-standard", "deep": f"{provider}-deep"}
            for provider in ("claude", "codex")
        }
        profiles_path.write_text(json.dumps(profile_models), encoding="utf-8")
        config_path.write_text(
            json.dumps({"profiles": {"claude": {"standard": "local-standard"}}}),
            encoding="utf-8",
        )
        config_snapshot = load_config(config_path)
        profiles_snapshot = load_profile_models(profiles_path)
        env_snapshot: dict[str, str] = {}
        for provider in ("claude", "codex"):
            frozen = dispatch.freeze_role_models(
                provider,
                root=fixture_root,
                environ=env_snapshot,
                config=config_snapshot,
                profile_models=profiles_snapshot,
            )
            check(
                f"{provider} standard and deep mappings selected",
                frozen["search"]
                == ("local-standard" if provider == "claude" else "codex-standard")
                and frozen["enhance"] == f"{provider}-deep",
            )
        config_path.write_text(
            json.dumps({"models": {"claude": "changed-global"}}), encoding="utf-8"
        )
        profiles_path.write_text(
            json.dumps(
                {
                    provider: {"standard": "changed-standard", "deep": "changed-deep"}
                    for provider in ("claude", "codex")
                }
            ),
            encoding="utf-8",
        )
        original_env_model = os.environ.get("VAULTLENS_LLM_MODEL")
        try:
            os.environ["VAULTLENS_LLM_MODEL"] = "changed-environment"
            frozen_again = dispatch.freeze_role_models(
                "claude",
                root=fixture_root,
                environ=env_snapshot,
                config=config_snapshot,
                profile_models=profiles_snapshot,
            )
            check(
                "batch snapshots ignore later config and environment changes",
                frozen_again["search"] == "local-standard"
                and frozen_again["enhance"] == "claude-deep",
            )
        finally:
            if original_env_model is None:
                os.environ.pop("VAULTLENS_LLM_MODEL", None)
            else:
                os.environ["VAULTLENS_LLM_MODEL"] = original_env_model

    print("agent report stream selection:")
    check(
        "successful agent report keeps stdout only",
        _agent_output(0, "final answer\n", "runtime trace\n")
        == "final answer\n",
    )
    check(
        "failed agent result keeps diagnostics",
        _agent_output(1, "partial\n", "failure detail\n")
        == "partial\nfailure detail\n",
    )
    noisy_report = (
        "[post-start] Ready. Work on the tooling: qmd.\n"
        "[cos] Gathering live context (mode=brief)...\n"
        "## Chief of Staff Brief — 2026-06-07\n\n"
        "Keep this [post-start] text because it is not a diagnostic line.\n"
        "Agent: Alice\n"
        "Invoking cos agent with codex\n"
        "Effort: low\n"
        "Agent: wiki-cos.md\n"
    )
    cleaned_report = dispatch.clean_scheduled_report(noisy_report)
    check(
        "scheduled report strips known launcher diagnostics",
        cleaned_report.startswith("## Chief of Staff Brief")
        and "Invoking cos agent" not in cleaned_report
        and "Effort: low" not in cleaned_report,
    )
    check(
        "scheduled report keeps non-diagnostic content",
        "Keep this [post-start] text" in cleaned_report
        and "Agent: Alice" in cleaned_report,
    )

    print("scheduled qmd maintenance:")
    original_schedule_enhance = dispatch.SCHEDULE_ENHANCE
    try:
        dispatch.SCHEDULE_ENHANCE = False
        paused_names = [step.name for step in dispatch.build_steps()]
        check("nightly enhancement is paused by default", "enhance" not in paused_names)
        dispatch.SCHEDULE_ENHANCE = True
        opted_in_steps = dispatch.build_steps()
        opted_in_names = [step.name for step in opted_in_steps]
        enhance = next(step for step in opted_in_steps if step.name == "enhance")
        check(
            "nightly enhancement requires opt-in and runs before brief",
            "enhance" in opted_in_names
            and opted_in_names.index("enhance") < opted_in_names.index("cos-brief"),
        )
        check(
            "nightly enhancement runs five global alternating iterations",
            enhance.builder()
            == [["enhance", "--iterations", "5", "--strategy", "alternate"]],
        )
    finally:
        dispatch.SCHEDULE_ENHANCE = original_schedule_enhance

    scheduled = dispatch.build_steps()
    scheduled_names = [step.name for step in scheduled]
    qmd_update = next(step for step in scheduled if step.name == "qmd-update")
    qmd_cleanup = next(step for step in scheduled if step.name == "qmd-cleanup")
    check(
        "qmd update follows markdown index",
        scheduled_names.index("qmd-update") == scheduled_names.index("index") + 1,
    )
    check("qmd update uses host qmd runner", qmd_update.kind == "qmd")
    check(
        "qmd cleanup is weekly, host-side, and AC gated",
        qmd_cleanup.kind == "qmd"
        and qmd_cleanup.period == "weekly"
        and qmd_cleanup.gates == ["ac"]
        and qmd_cleanup.builder() == [["cleanup"]],
    )
    check(
        "qmd cleanup follows update",
        scheduled_names.index("qmd-update") < scheduled_names.index("qmd-cleanup"),
    )
    check(
        "qmd embedding remains manual",
        "qmd-embed" not in scheduled_names,
    )
    check(
        "every model step gates on native runtime availability",
        all("runtime" in step.gates for step in scheduled if step.kind == "llm"),
    )

    print("_record failure semantics:")
    led7 = fresh_ledger()
    _record(led7, "enhance", now, "ok")
    ok_ts = led7["jobs"]["enhance"]["last_ok"]
    _record(led7, "enhance", now + timedelta(hours=3), "transient")
    check("failure preserves last_ok", led7["jobs"]["enhance"]["last_ok"] == ok_ts)
    check(
        "failure sets last_result",
        led7["jobs"]["enhance"]["last_result"] == "transient",
    )
    check("attempt timestamp recorded", "last_attempt" in led7["jobs"]["enhance"])

    print("report retention:")
    names = [f"scheduled-cos-brief-2026-06-{d:02d}.md" for d in range(1, 21)] + [
        "scheduled-contradict-2026-06-07.md",
        "scheduled-contradict-2026-06-14.md",
        "schedule-status.md",
        "lint-report.md",
        ".gitkeep",
    ]
    prune = _reports_to_prune(names, 14, dispatch.REPORT_RETENTION_BY_TYPE)
    check(
        "daily cos briefs retain only the latest generated report",
        sum("cos-brief" in n for n in prune) == 19,
    )
    check(
        "deletes oldest, keeps newest",
        "scheduled-cos-brief-2026-06-01.md" in prune
        and "scheduled-cos-brief-2026-06-20.md" not in prune,
    )
    check(
        "keeps a type that is under the limit",
        not any("contradict" in n for n in prune),
    )
    check(
        "never touches schedule-status / non-scheduled files",
        not any(
            n in prune for n in ("schedule-status.md", "lint-report.md", ".gitkeep")
        ),
    )
    check(
        "retention 0 prunes all matching",
        len(_reports_to_prune(names, 0)) == 22,
    )

    print("cos proposal parsing (CoS→AGENDA seam):")
    brief = (
        "## Chief of Staff Brief — 2026-06-29 (Monday)\n"
        "### Today's focus\n- do the thing\n\n"
        "## Proposals\n"
        "proposal:: vision | Triage the 3 failed CSV imports | time-sensitive\n"
        "proposal::assistant|Draft reply to supervisor|overdue commitment\n"
        "proposal:: alpha | only two fields\n"  # one pipe -> malformed -> skipped
        "garbage line, not a proposal\n"
        "proposal:: | | \n"  # empty target/task/why -> skipped
    )
    props = dispatch.parse_cos_proposals(brief)
    check("parses only the 2 well-formed proposals", len(props) == 2)
    check(
        "first proposal target+task",
        props[0]["target"] == "vision" and props[0]["task"].startswith("Triage"),
    )
    check(
        "pipes without surrounding spaces still parse",
        props[1]["target"] == "assistant"
        and props[1]["task"] == "Draft reply to supervisor",
    )
    check(
        "malformed one-pipe line skipped",
        all("only two fields" not in p["task"] for p in props),
    )
    check(
        "brief with no block => []", dispatch.parse_cos_proposals("no block here") == []
    )
    stripped = dispatch.strip_cos_proposals(brief)
    check(
        "stored brief omits legacy proposal block",
        "## Proposals" not in stripped and "Today's focus" in stripped,
    )
    check(
        "format_work_item: from-tag + why",
        dispatch.format_work_item("cos", {"task": "Do X", "why": "because"})
        == "[from:cos] Do X — because",
    )
    check(
        "format_work_item: empty why omitted, source preserved",
        dispatch.format_work_item("fleet-health", {"task": "Do Y", "why": ""})
        == "[from:fleet-health] Do Y",
    )

    print("handoff parsing:")
    handoff_text = (
        "## Project run: fleet-health — 2026-06-29\n"
        "Handoffs: 2\n"
        "handoff:: vision | Bump the lodash dep flagged in the sweep | projects/fleet-health/notes/sweep-x.md\n"
        "handoff::watchman|Patch the exposed port|notes/y.md\n"
        "proposal:: vision | this is a proposal not a handoff | x\n"
    )
    hos = dispatch.parse_handoffs(handoff_text)
    check("parses only handoff:: lines (2)", len(hos) == 2)
    check("ignores proposal:: lines", all("proposal" not in h["task"] for h in hos))
    check(
        "handoff target+task parsed",
        hos[0]["target"] == "vision" and hos[0]["task"].startswith("Bump"),
    )

    print("routing guard (anti-loop / cap):")
    g = dispatch.RoutingGuard(cap=2)
    check(
        "self-handoff blocked",
        dispatch.RoutingGuard().allow("vision", "vision")[0] is False,
    )
    ok1, _ = g.allow("cos", "vision")
    check("first edge allowed", ok1 is True)
    g.record("cos", "vision")
    check(
        "reciprocal edge blocked (cycle)",
        g.allow("vision", "cos")[0] is False,
    )
    g.record("cos", "watchman")  # now routed == 2 == cap
    check("per-tick cap enforced", g.allow("cos", "alpha")[0] is False)

    print("cos proposal routing destination:")
    # Self-contained: resolve_proposal_dest takes an explicit projects_dir and its
    # only reads scoped metadata, so build a throwaway `projects/` tree
    # rather than reaching into the developer's real vault (gitignored, so absent in
    # CI). The subdir is literally named `projects` to keep the suffix assertion true.
    with tempfile.TemporaryDirectory() as tmp:
        projects_dir = Path(tmp) / "projects"
        (projects_dir / "fleet-health").mkdir(parents=True)
        (projects_dir / "fleet-health" / "AGENDA.md").write_text(
            "---\nenabled: true\n---\n# fleet-health AGENDA\n", encoding="utf-8"
        )
        (projects_dir / "fleet-health" / "project.md").write_text(
            "---\nstatus: active\n---\n", encoding="utf-8"
        )
        check(
            "real project slug resolves to its AGENDA",
            str(
                dispatch.resolve_proposal_dest("fleet-health", projects_dir) or ""
            ).endswith("projects/fleet-health/AGENDA.md"),
        )
        check(
            "unknown slug => None (left advisory)",
            dispatch.resolve_proposal_dest("definitely-not-a-project-zzz", projects_dir)
            is None,
        )
        check(
            "empty target => None",
            dispatch.resolve_proposal_dest("", projects_dir) is None,
        )
        (projects_dir / "fleet-health" / "project.md").write_text(
            "---\nstatus: frozen\n---\n", encoding="utf-8"
        )
        check(
            "frozen project => None (cannot receive routed work)",
            dispatch.resolve_proposal_dest("fleet-health", projects_dir) is None,
        )
        # A project not present in the tree (e.g. the retired `assistant`) resolves to
        # None, so the proposal is left advisory rather than force-filed.
        check(
            "retired assistant project => None",
            dispatch.resolve_proposal_dest("assistant", projects_dir) is None,
        )

    print(f"\n{_COUNTS['passed']} passed, {_COUNTS['failed']} failed")
    return 1 if _COUNTS["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
