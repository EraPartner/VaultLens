---
name: wiki-maintenance
description: Wiki health and upkeep — lint, validate, rebuild indexes, fix wikilink mirrors, archive/restore pages, manage the inventory, re-index search. Use when the user asks for a lint/health check, to archive or retire a page, to rebuild or fix indexes/links, to check staleness or coverage, or after bulk content changes.
---

# Maintenance command reference

```bash
# Lint / health (programmatic, fast)
python3 tools/wiki.py lint                       # links, metadata, status/date validity, staleness, confidence/volatility
python3 tools/wiki.py lint --strict              # + orphan pages
python3 tools/wiki.py lint --json                # machine-readable report (errors/warnings split)
python3 tools/wiki.py lint --fix                 # case-normalise confidence/volatility/status values
python3 tools/wiki.py validate-log               # check wiki/log.md format
python3 tools/tests/test_wiki.py                 # tooling test suite (golden + per-rule defect fixtures)

# Coverage / stats
python3 tools/wiki.py coverage                   # rank sparse / underlinked pages
python3 tools/wiki_extra.py stats                # wiki statistics

# Indexes & links (generated files — never hand-edit _index.md)
python3 tools/wiki.py index                      # report stale _index.md mirrors
python3 tools/wiki.py index --rebuild            # regenerate headless-readable _index.md files
python3 tools/wiki.py links                      # report wikilink dual-link coverage
python3 tools/wiki.py links --fix --write        # add portable markdown mirrors to wikilinks

# Archiving (retire without deleting; wikilinks keep resolving)
python3 tools/wiki.py archive page concepts/foo --reason "superseded by bar"
python3 tools/wiki.py archive restore concepts/foo
python3 tools/wiki.py archive list               # list archived pages (+ registry drift)
python3 tools/wiki.py search "term" --include-archived

# Inventory — tracked intentions (ingest-candidate/question/task/watch/corpus/artifact/item)
python3 tools/wiki.py inventory list             # filter: inventory list <kind> / --status X
python3 tools/wiki.py inventory new question how-x-works --priority p1 --summary "..."
python3 tools/wiki.py inventory show question/how-x-works

# Log
python3 tools/wiki.py append-log ...             # append-only; headings "## [YYYY-MM-DD] operation | title"

# Search index (qmd)
qmd update                                       # re-index after content changes
qmd embed                                        # refresh vector embeddings (host: Metal)
qmd status                                       # index health
```

Semantic (thorough) checks run through agents — `quality` / `contradict` / `verify`; see
`.claude/skills/wiki-agents/SKILL.md`. Write findings to `wiki/reports/`; fix highest-priority
issues first. Archived pages are excluded from staleness/orphan checks and from `search` by default.
