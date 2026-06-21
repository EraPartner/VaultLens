#!/usr/bin/env python3
"""Self-contained tests for the scheduling dispatcher's pure decision logic.

Exercises the side-effect-free helpers (failure classification, account
failover selection, cooldown/backoff, step due-ness) without touching the
system. Run with:

    python3 tools/tests/test_schedule.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "schedule"))

import dispatch  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def fresh_ledger() -> dict:
    return {
        "jobs": {},
        "accounts": {a: {"limited_until": None, "last_error": None, "backoff": 0} for a in dispatch.ACCOUNTS},
    }


def main() -> int:
    now = datetime(2026, 6, 7, 3, 30).astimezone()  # a Sunday at 03:30

    print("classify_failure:")
    check("rc 0 -> ok", dispatch.classify_failure(0, "all good") == "ok")
    check("quota text -> quota", dispatch.classify_failure(1, "Premium request quota exceeded") == "quota")
    check("429 -> ratelimit", dispatch.classify_failure(1, "HTTP 429 Too Many Requests") == "ratelimit")
    check("other -> transient", dispatch.classify_failure(1, "connection reset") == "transient")

    print("backend availability / cooldown:")
    led = fresh_ledger()
    check("backend available when healthy", dispatch.backend_available(led, now) is True)
    dispatch.mark_limited(led, dispatch.ACCOUNTS[0], "ratelimit", now)
    check("limited_until set after ratelimit",
          led["accounts"][dispatch.ACCOUNTS[0]]["limited_until"] is not None)
    check("backend unavailable while limited", dispatch.backend_available(led, now) is False)
    dispatch.mark_limited(led, dispatch.ACCOUNTS[0], "quota", now)
    check("still unavailable after quota", dispatch.backend_available(led, now) is False)

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
    check("expired limit -> backend available again",
          dispatch.backend_available(led3, now) is True)

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
    check("daily step due when never run (in window)", dispatch.step_due(lint, led5, now) is True)
    led5["jobs"]["lint"] = {"last_ok": dispatch.iso(now)}
    check("daily step not due same day", dispatch.step_due(lint, led5, now) is False)
    tomorrow = now + timedelta(days=1)
    check("daily step due next day", dispatch.step_due(lint, led5, tomorrow) is True)

    morning = now.replace(hour=9)
    night_only = now.replace(hour=3)
    brief = steps["cos-brief"]
    check("cos-brief due in morning window", dispatch.step_due(brief, fresh_ledger(), morning) is True)
    check("cos-brief not due at 03:00", dispatch.step_due(brief, fresh_ledger(), night_only) is False)

    weekly = steps["contradict"]
    led6 = fresh_ledger()
    check("weekly due when never run", dispatch.step_due(weekly, led6, now) is True)
    led6["jobs"]["contradict"] = {"last_ok": dispatch.iso(now - timedelta(days=2))}
    check("weekly not due 2 days later", dispatch.step_due(weekly, led6, now) is False)
    led6["jobs"]["contradict"] = {"last_ok": dispatch.iso(now - timedelta(days=9))}
    weekday = now + timedelta(days=3)  # a Wednesday, age >= 8 -> catch up
    check("weekly catches up when overdue >8d", dispatch.step_due(weekly, led6, weekday) is True)

    print("ingest target selection:")
    check("slugify matches source-text convention",
          dispatch._slugify("Cryptology and Error Correction") == "cryptology-and-error-correction")
    # The bug: a wiki/sources page already cites the PDF -> never re-ingest it,
    # even when no literal-stem extracted text exists.
    check("PDF with a wiki source page is skipped",
          dispatch._select_ingest_pdfs(["Cryptology and Error Correction.pdf"], set(), {"Cryptology and Error Correction.pdf"}) == [])
    # Extracted text under the slugified name also counts as processed.
    check("slugified extracted text skips PDF",
          dispatch._select_ingest_pdfs(["Cryptology and Error Correction.pdf"], {"cryptology-and-error-correction"}, set()) == [])
    # ...as does extracted text under the literal stem (preprocess's naming).
    check("literal-stem extracted text skips PDF",
          dispatch._select_ingest_pdfs(["Foo Bar.pdf"], {"Foo Bar"}, set()) == [])
    # A genuinely new PDF (no page, no text) is still selected.
    check("new PDF is selected",
          dispatch._select_ingest_pdfs(["Brand New.pdf"], set(), set()) == ["Brand New.pdf"])

    print("scheduler status summary:")
    nowt = datetime(2026, 6, 20, 7, 0).astimezone()
    meta = [("lint", "daily"), ("enhance", "daily"), ("cos-brief", "daily"), ("contradict", "weekly")]
    healthy = {n: {"last_ok": dispatch.iso(nowt), "last_result": "ok"} for n, _ in meta}
    check("all-healthy verdict", "all scheduled jobs healthy" in dispatch.format_schedule_status(healthy, {}, meta, nowt))
    failed = dict(healthy)
    failed["cos-brief"] = {"last_ok": dispatch.iso(nowt - timedelta(days=4)), "last_result": "transient"}
    s_fail = dispatch.format_schedule_status(failed, {}, meta, nowt)
    check("failing job named in verdict", "cos-brief" in s_fail and "failing" in s_fail)
    never = {n: {} for n, _ in meta}  # never run -> stale, not failing
    s_stale = dispatch.format_schedule_status(never, {}, meta, nowt)
    check("never-run jobs read as stale", "stale" in s_stale and "failing" not in s_stale)
    accts = {"claude-plan": {"limited_until": dispatch.iso(nowt + timedelta(hours=2))}}
    s_lim = dispatch.format_schedule_status(healthy, accts, meta, nowt)
    check("backend cooldown surfaced", "Backend limited" in s_lim and "claude-plan" in s_lim)

    print("_record failure semantics:")
    led7 = fresh_ledger()
    dispatch._record(led7, "enhance", now, "ok")
    ok_ts = led7["jobs"]["enhance"]["last_ok"]
    dispatch._record(led7, "enhance", now + timedelta(hours=3), "transient")
    check("failure preserves last_ok", led7["jobs"]["enhance"]["last_ok"] == ok_ts)
    check("failure sets last_result", led7["jobs"]["enhance"]["last_result"] == "transient")
    check("attempt timestamp recorded", "last_attempt" in led7["jobs"]["enhance"])

    print("report retention:")
    names = ([f"scheduled-cos-brief-2026-06-{d:02d}.md" for d in range(1, 21)]
             + ["scheduled-contradict-2026-06-07.md", "scheduled-contradict-2026-06-14.md",
                "schedule-status.md", "lint-report.md", ".gitkeep"])
    prune = dispatch._reports_to_prune(names, 14)
    check("prunes oldest cos-briefs beyond 14/type", sum("cos-brief" in n for n in prune) == 6)
    check("deletes oldest, keeps newest",
          "scheduled-cos-brief-2026-06-01.md" in prune
          and "scheduled-cos-brief-2026-06-20.md" not in prune)
    check("keeps a type that is under the limit", not any("contradict" in n for n in prune))
    check("never touches schedule-status / non-scheduled files",
          not any(n in prune for n in ("schedule-status.md", "lint-report.md", ".gitkeep")))
    check("retention 0 prunes all matching", len(dispatch._reports_to_prune(names, 0)) == 22)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
