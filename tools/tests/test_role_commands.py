#!/usr/bin/env python3
"""Role shell examples must be runnable under the role's headless Claude grants.

Headless Claude runs use `--permission-mode dontAsk`, so a command outside the
granted `Bash(...)` rules is denied and the agent loses a turn. This suite
checks every fenced bash example in `.agents/roles/*.md` against the rules that
`agent_capabilities.claude_tools` grants for the role's permission profile.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from agent_capabilities import claude_tools, profile_capabilities  # noqa: E402

ROLES = TOOLS.parent / ".agents" / "roles"
FENCE = re.compile(r"^[ \t]*```(?:bash|sh)[ \t]*\n(.*?)^[ \t]*```", re.MULTILINE | re.DOTALL)
PROFILE = re.compile(r"^permission_profile:\s*(\S+)\s*$", re.MULTILINE)
SEPARATORS = {"|", "||", "&&", ";"}


def _commands(block: str) -> list[str]:
    """Split each example line into the simple commands Claude checks separately."""
    commands: list[str] = []
    for line in block.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        current: list[str] = []
        for token in lexer:
            if token in SEPARATORS:
                if current:
                    commands.append(" ".join(current))
                current = []
            else:
                current.append(token)
        if current:
            commands.append(" ".join(current))
    return commands


def _allowed(command: str, rules: list[str]) -> bool:
    for rule in rules:
        if fnmatch.fnmatchcase(command, rule):
            return True
        # Claude treats "cmd *" as also matching the bare command.
        if rule.endswith(" *") and command == rule[:-2]:
            return True
    return False


class RoleCommandTests(unittest.TestCase):
    def test_fenced_shell_examples_fit_headless_grants(self) -> None:
        roles = sorted(ROLES.glob("*.md"))
        self.assertTrue(roles)
        denied: list[str] = []
        for role in roles:
            text = role.read_text(encoding="utf-8")
            match = PROFILE.search(text)
            self.assertIsNotNone(match, f"{role.name} has no permission_profile")
            assert match is not None
            tools = claude_tools(profile_capabilities(match.group(1)))
            rules = [tool[len("Bash(") : -1] for tool in tools if tool.startswith("Bash(")]
            for block in FENCE.findall(text):
                for command in _commands(block):
                    if not _allowed(command, rules):
                        denied.append(f"{role.name} ({match.group(1)}): {command}")
        self.assertEqual(denied, [], "\n".join(denied))

    def test_checker_rejects_ungranted_commands(self) -> None:
        rules = [
            tool[len("Bash(") : -1]
            for tool in claude_tools(profile_capabilities("wiki-write"))
            if tool.startswith("Bash(")
        ]
        self.assertTrue(_allowed("python3 tools/wiki.py sample concept", rules))
        self.assertTrue(_allowed("wc -l wiki/concepts/a.md", rules))
        self.assertFalse(_allowed("python3 -c print(1)", rules))
        self.assertFalse(_allowed("find wiki -name x", rules))
        read_rules = [
            tool[len("Bash(") : -1]
            for tool in claude_tools(profile_capabilities("read-shell"))
            if tool.startswith("Bash(")
        ]
        self.assertTrue(_allowed("python3 tools/wiki.py lint --json --strict", read_rules))
        self.assertFalse(_allowed("python3 tools/wiki.py lint --fix", read_rules))
        self.assertEqual(
            _commands('ls wiki/ | grep -iE "a|b"  # note'),
            ["ls wiki/", "grep -iE a|b"],
        )


if __name__ == "__main__":
    unittest.main()
