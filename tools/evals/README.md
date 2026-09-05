# Live context fixture evaluation

Run with Python 3.12 (the CI version):

```sh
python3.12 tools/context_evaluation.py --check
python3.12 tools/tests/test_agent_safety.py
```

`context-baseline.json` compares the existing default Chief of Staff collector against the
experimental bounded collector on deterministic temporary fixtures. `--write` refreshes this
reviewable baseline after an intentional change. The runner does not read the real vault and does
not call a model or service. It measures Unicode characters and UTF-8 bytes of live context only,
not tokens, prompt overhead, latency, cost, or answer quality. The small fixture gets larger due
to source labels and omission accounting; no universal savings are claimed.

The bounded mode remains **opt-in** using `VAULTLENS_COS_CONTEXT_CHARS=<positive integer>` in the
headless launcher's environment. Leave it unset until a representative model evaluation confirms
that briefs preserve priorities, consent boundaries, unresolved problems and required follow-up
reads. The configured limit covers the live-context string before JSON encoding; fixed role/task
instructions and JSON escaping are outside that limit. It does not change the model or effort.

Selection scans every open TODO item, including entries after the old 40-item prefix. It orders
each project's candidates by due date/priority markers, then gives each source a round-robin turn.
It retains complete lines, source paths, included/omitted counts, full operator profile, consent
instructions, all selected project/desk overviews and review-inbox names. Mandatory overflow is
an error; it never silently drops operator constraints. A large item may be omitted to allow
smaller items to fit. Omission means detail remains unknown, and a model must inspect the cited
source if the task needs it. Review-inbox content is never read by either collector.

The scheduler overview counts attention items across the full source, and failure details are
selected first. Footers distinguish lines omitted before selection from lines omitted by the
budget, with original line references. Omitted urgent tasks and failure details require retrieval.
Inbox previews reject links at both directory and file boundaries, including in the legacy
collector; linked files remain available only through a separately authorized read.

The safety tests also check task/system data separation for both providers and use local
synthetic processes to test timeout cancellation, signal death, and retained partial logs.
Interruption/restart fixtures verify the persistent in-flight marker and corrupt-ledger refusal.
The adversarial document
fixture checks transport separation only. Resistance to prompt injection by an actual model is
**not evaluated**. Runtime container termination cannot be inferred from this local child test.
