#!/usr/bin/env python3
"""Deterministic wiki linting: structured checks, JSON output, safe auto-fixes.

This builds on the resolution/staleness helpers in `wiki.py` and adds the checks
needed for parity with a mature wiki linter: status-value validation, date
sanity, and present-but-blank required fields. Results are assembled into one
report so the same run can print human-readable output or `--json` (for agents /
CI), and `--fix` applies only unambiguous repairs (case-normalising enum and
status values).
"""

from __future__ import annotations

import datetime as dt
import json

from wiki import (
    ROOT,
    SPECIAL_LINK_TARGETS,
    Page,
    Project,
    _set_frontmatter_field,
    build_page_indexes,
    compute_inbound_links,
    list_content_pages,
    list_projects,
    normalize_link_target,
)

ALLOWED_STATUS = {"active", "superseded", "archived", "draft"}
SKIP_CATEGORIES = {"system", "root"}

# Required frontmatter. Every content page needs the base set; some categories
# add their own. `lint` reports pages missing or blank in these.
REQUIRED_FRONTMATTER_BASE = {"title", "type", "status", "created", "updated", "summary"}
REQUIRED_FRONTMATTER_BY_CATEGORY = {
    "sources": {"source_id", "source_type", "origin", "ingested_on"},
}
# project.md is load-bearing for search scoping, so wiki_refs/tags/domain are
# required (presence, not non-empty) alongside the base content fields.
PROJECT_REQUIRED_FIELDS = {
    "title",
    "type",
    "status",
    "created",
    "updated",
    "summary",
    "wiki_refs",
    "tags",
    "domain",
}

# Link/orphan handling: `system/` pages are excluded from link validation, and a
# couple of well-known landing pages are exempt from the orphan check.
LINK_VALIDATION_SKIP_CATEGORIES = {"system"}
ORPHAN_EXEMPT = {"home", "system/schema"}

# Trust/refresh enums validated by `check_field_enums`.
CONFIDENCE_VALUES = {"high", "medium", "low"}
VOLATILITY_VALUES = {"hot", "warm", "cold"}

# Volatility-aware staleness thresholds: hot pages go stale faster, cold ones
# slower. Pages without a `volatility` field fall back to the default warm cadence.
STALENESS_DAYS = 180
STALENESS_DAYS_BY_VOLATILITY = {"hot": 60, "warm": 180, "cold": 365}


def staleness_threshold(page: Page) -> int:
    """Days-until-stale for a page, based on its `volatility` (default warm)."""
    return STALENESS_DAYS_BY_VOLATILITY.get(page.volatility, STALENESS_DAYS)


def check_staleness(pages: list[Page]) -> list[str]:
    stale: list[str] = []
    today = dt.date.today()
    for page in pages:
        if page.category in {"system", "root", "inventory"} or page.is_archived:
            continue
        raw = page.updated.strip()
        if not raw:
            continue
        try:
            updated = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        age = (today - updated).days
        threshold = staleness_threshold(page)
        if age > threshold:
            vol = f", volatility {page.volatility}" if page.volatility else ""
            stale.append(
                f"{page.rel.as_posix()}: last updated {raw} "
                f"({age} days ago, threshold {threshold}{vol})"
            )
    return stale


def check_field_enums(pages: list[Page]) -> tuple[list[str], list[str]]:
    """Validate `confidence`/`volatility` values when present.

    Returns (invalid_values, low_confidence_pages). Invalid values are lint
    errors; low-confidence pages are surfaced informationally so the enhancer
    or source-verifier can be pointed at them.
    """
    invalid: list[str] = []
    low_confidence: list[str] = []
    for page in pages:
        if page.category in {"system", "root"}:
            continue
        conf = page.confidence
        if conf and conf not in CONFIDENCE_VALUES:
            invalid.append(
                f"{page.rel.as_posix()}: confidence '{conf}' not in "
                f"{sorted(CONFIDENCE_VALUES)}"
            )
        elif conf == "low":
            low_confidence.append(page.rel.as_posix())
        vol = page.volatility
        if vol and vol not in VOLATILITY_VALUES:
            invalid.append(
                f"{page.rel.as_posix()}: volatility '{vol}' not in "
                f"{sorted(VOLATILITY_VALUES)}"
            )
    return invalid, low_confidence


def lint_projects(
    projects: list[Project],
    canonical: dict[str, Page],
    basename_map: dict[str, list[Page]],
) -> tuple[list[str], list[str]]:
    """Validate project metadata and wiki_refs. Returns (missing_fields, broken_refs)."""
    missing: list[str] = []
    broken: list[str] = []
    for project in projects:
        rel = project.path.relative_to(ROOT).as_posix()
        gaps = sorted(
            key for key in PROJECT_REQUIRED_FIELDS if key not in project.frontmatter
        )
        if gaps:
            missing.append(f"{rel}: missing {', '.join(gaps)}")
        for ref in project.wiki_refs:
            target = normalize_link_target(ref)
            if not target or target in canonical:
                continue
            if target in SPECIAL_LINK_TARGETS:
                continue
            if "/" not in target and len(basename_map.get(target, [])) == 1:
                continue
            broken.append(f"{rel}: wiki_refs [[{ref}]]")
    return missing, broken


def check_missing_fields(pages: list[Page]) -> list[str]:
    out: list[str] = []
    for page in pages:
        if page.category in SKIP_CATEGORIES:
            continue
        need = set(REQUIRED_FRONTMATTER_BASE)
        need.update(REQUIRED_FRONTMATTER_BY_CATEGORY.get(page.category, set()))
        missing = sorted(key for key in need if key not in page.frontmatter)
        if missing:
            out.append(f"{page.rel.as_posix()}: missing {', '.join(missing)}")
    return out


def check_empty_required(pages: list[Page]) -> list[str]:
    """Required base fields that are present but blank (half-filled templates)."""
    out: list[str] = []
    for page in pages:
        if page.category in SKIP_CATEGORIES:
            continue
        blank = sorted(
            key
            for key in REQUIRED_FRONTMATTER_BASE
            if key in page.frontmatter and not str(page.frontmatter[key]).strip()
        )
        if blank:
            out.append(f"{page.rel.as_posix()}: blank {', '.join(blank)}")
    return out


def check_status_values(pages: list[Page]) -> list[str]:
    out: list[str] = []
    for page in pages:
        if page.category in SKIP_CATEGORIES:
            continue
        st = page.status
        if st and st not in ALLOWED_STATUS:
            out.append(
                f"{page.rel.as_posix()}: status '{st}' not in {sorted(ALLOWED_STATUS)}"
            )
    return out


def check_dates(pages: list[Page]) -> tuple[list[str], list[str]]:
    """Return (malformed_dates, updated_before_created)."""
    malformed: list[str] = []
    reversed_dates: list[str] = []
    for page in pages:
        if page.category in SKIP_CATEGORIES:
            continue
        parsed: dict[str, dt.date] = {}
        for field in ("created", "updated"):
            raw = page._scalar(field).strip()
            if not raw:
                continue
            try:
                parsed[field] = dt.date.fromisoformat(raw)
            except ValueError:
                malformed.append(
                    f"{page.rel.as_posix()}: {field} '{raw}' is not YYYY-MM-DD"
                )
        if (
            "created" in parsed
            and "updated" in parsed
            and parsed["updated"] < parsed["created"]
        ):
            reversed_dates.append(
                f"{page.rel.as_posix()}: updated {parsed['updated']} < created {parsed['created']}"
            )
    return malformed, reversed_dates


def apply_fixes(pages: list[Page]) -> list[str]:
    """Case-normalise confidence/volatility/status when that makes them valid."""
    fixes: list[str] = []
    valid = {
        "confidence": CONFIDENCE_VALUES,
        "volatility": VOLATILITY_VALUES,
        "status": ALLOWED_STATUS,
    }
    for page in pages:
        if page.category in SKIP_CATEGORIES:
            continue
        text = page.path.read_text(encoding="utf-8")
        changed = False
        for field, allowed in valid.items():
            raw = page._scalar(field)
            normalized = raw.strip().lower()
            if raw and raw != normalized and normalized in allowed:
                text = _set_frontmatter_field(text, field, normalized)
                fixes.append(
                    f"{page.rel.as_posix()}: {field} '{raw}' -> '{normalized}'"
                )
                changed = True
        if changed:
            page.path.write_text(text, encoding="utf-8")
    return fixes


def build_report(pages: list[Page], strict: bool) -> dict:
    canonical, basename_map = build_page_indexes(pages)
    inbound, broken_links, ambiguous_links = compute_inbound_links(
        pages, canonical, basename_map, skip_categories=LINK_VALIDATION_SKIP_CATEGORIES
    )

    orphan_pages = [
        page.rel.as_posix()
        for page in pages
        if page.rel.with_suffix("").as_posix() not in ORPHAN_EXEMPT
        and not page.is_archived
        and page.category != "inventory"
        and inbound.get(page.rel.with_suffix("").as_posix(), 0) == 0
    ]

    invalid_enums, low_confidence = check_field_enums(pages)
    malformed_dates, reversed_dates = check_dates(pages)
    projects = list_projects()
    project_missing, project_broken_refs = lint_projects(
        projects, canonical, basename_map
    )

    errors = {
        "missing_fields": check_missing_fields(pages),
        "broken_links": broken_links,
        "ambiguous_links": ambiguous_links,
        "invalid_enums": invalid_enums,
        "invalid_status": check_status_values(pages),
        "malformed_dates": malformed_dates,
        "project_missing": project_missing,
        "project_broken_refs": project_broken_refs,
    }
    if strict:
        errors["orphans"] = orphan_pages

    warnings = {
        "stale_pages": check_staleness(pages),
        "low_confidence": low_confidence,
        "empty_required": check_empty_required(pages),
        "updated_before_created": reversed_dates,
    }
    if not strict:
        warnings["orphans"] = orphan_pages

    return {
        "pages_checked": len(pages),
        "projects_checked": len(projects),
        "errors": errors,
        "warnings": warnings,
        "error_count": sum(len(v) for v in errors.values()),
        "warning_count": sum(len(v) for v in warnings.values()),
    }


def _print_section(label: str, rows: list[str]) -> None:
    if rows:
        print(f"\n{label}:")
        for row in rows:
            print(f"- {row}")


def run_lint(strict: bool, as_json: bool, fix: bool) -> int:
    pages = list_content_pages()
    fixes: list[str] = []
    if fix:
        fixes = apply_fixes(pages)
        pages = list_content_pages()  # reload so checks see fixed values

    report = build_report(pages, strict)
    report["fixes_applied"] = fixes

    if as_json:
        print(json.dumps(report, indent=2))
        return 1 if report["error_count"] else 0

    print(f"Pages checked: {report['pages_checked']}")
    print(f"Projects checked: {report['projects_checked']}")
    print(f"Errors: {report['error_count']}  Warnings: {report['warning_count']}")
    if fixes:
        print(f"Auto-fixes applied: {len(fixes)}")

    labels = {
        "missing_fields": "Missing fields",
        "broken_links": "Broken links",
        "ambiguous_links": "Ambiguous links",
        "invalid_enums": "Invalid confidence/volatility values",
        "invalid_status": "Invalid status values",
        "malformed_dates": "Malformed dates",
        "project_missing": "Project missing fields",
        "project_broken_refs": "Project broken wiki_refs",
        "orphans": "Orphan pages",
        "stale_pages": "Stale pages",
        "low_confidence": "Low-confidence pages (consider wiki-source-verifier / wiki-enhancer)",
        "empty_required": "Blank required fields",
        "updated_before_created": "updated before created",
    }
    for key, rows in {**report["errors"], **report["warnings"]}.items():
        _print_section(labels.get(key, key), rows)
    _print_section("Auto-fixes applied", fixes)

    print("\nNote: contradiction and semantic quality checks require agent review.")
    print("Run: python3 tools/agents/wiki-agent.py contradict")
    return 1 if report["error_count"] else 0
