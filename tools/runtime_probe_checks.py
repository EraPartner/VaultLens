"""Canonical required isolation evidence shared by the probe and its receipts.

This manifest has no runtime imports. A receipt must include every current check
exactly once; callers cannot substitute an easier or incomplete check set.
"""

import sys

CASE_NAMES = ("selected-read", "wiki-read", "wiki-write", "project-write")
CHECK_NAMES = (
    "active-boundary",
    "clean-provider-environment",
    "notes.python-read",
    "notes.shell-read",
    "notes.profile-write",
    "notes.shell-profile-write",
    "selection.other-note-read",
    "selection.other-note-shell-read",
    "scratch.python-write",
    "scratch.shell-write",
    "excluded.python-read",
    "excluded.shell-read",
    "excluded.python-write",
    "excluded.shell-write",
    "restricted.python-read",
    "restricted.shell-read",
    "restricted.python-write",
    "restricted.shell-write",
    "denied.future-child-read",
    "denied.future-child-write",
    "consent.python-read",
    "stale-index.python-read",
    "raw.profile-read",
    "raw.python-write",
    "raw.shell-write",
    "tools.python-write",
    "tools.shell-write",
    "instructions.python-write",
    "instructions.shell-write",
    "obsidian.python-write",
    "git.python-write",
    "wiki.profile-write",
    "project.profile-write",
    "sibling.python-read",
    "sibling.python-write",
    "sibling.shell-write",
    "project.denied-child-read",
    "project.denied-child-write",
    "project.future-denied-child-read",
    "project.future-denied-child-write",
    "symlink.python-read",
    "symlink.shell-read",
    "symlink.python-write",
    "symlink.shell-write",
    "hardlink.python-read",
    "hardlink.shell-read",
    "hardlink.python-write",
    "hardlink.shell-write",
    "symlink.new-escape",
    "hardlink.new-escape",
    "search.approved",
    "search.restricted",
    "search.stale-index",
    "search.selection",
    "search.cli",
    "search.mcp",
    "qmd-bridge.request-write",
    "qmd-bridge.response-python-write",
    "qmd-bridge.response-shell-write",
    "qmd-bridge.folder-write",
)
NETWORK_CHECKS = ("network.http", "network.direct-socket", "network.shell-http")
MACOS_GUARD_CHECKS = (
    "macos.audit-reassignment",
    "macos.foreign-session-port",
    "macos.security-session-create",
    "macos.system-audit-denied",
    "macos.job-creation-denied",
    "macos.securityd-lookup-denied",
    "macos.securityserver-lookup-denied",
    "macos.job-bootstrap-denied",
    "macos.session-unchanged",
    "macos.node-preload-removed",
    "macos.host-shared-memory-read",
    "macos.host-shared-memory-write",
    "macos.host-semaphore-open",
    "macos.job-absent",
)
LIFECYCLE_CASES = tuple(
    (boundary, kind, cancellation)
    for boundary in ("outer", "inner")
    for kind in ("attached", "detached")
    for cancellation in (False, True)
)
LIFECYCLE_CHECKS = tuple(
    f"lifecycle.{boundary}.{kind}.{'cancellation' if cancellation else 'normal-success'}"
    for boundary, kind, cancellation in LIFECYCLE_CASES
)


def case_check_names(case: str, *, platform: str | None = None) -> tuple[str, ...]:
    if case not in CASE_NAMES:
        raise ValueError(f"Unknown required isolation profile: {case!r}")
    return tuple(
        case + "." + name
        for name in (
            *CHECK_NAMES,
            *(NETWORK_CHECKS if case == "selected-read" else ()),
            *(
                MACOS_GUARD_CHECKS
                if case == "selected-read" and (platform or sys.platform) == "darwin"
                else ()
            ),
        )
    )


def expected_checks(*, platform: str | None = None) -> tuple[str, ...]:
    return (
        "runtime.prerequisite",
        *(
            name
            for case in CASE_NAMES
            for name in case_check_names(case, platform=platform)
        ),
        *(case + ".outside-content-unchanged" for case in CASE_NAMES),
        *LIFECYCLE_CHECKS,
    )
