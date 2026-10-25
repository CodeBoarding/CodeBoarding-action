"""Every action that action.yml uses must be pinned to a full commit SHA.

Repositories can require actions to be pinned to a full-length commit SHA. GitHub enforces that
on the actions a composite action uses as well, so a single tag reference in action.yml makes
the whole action fail at startup for every adopter with that policy enabled, however they
reference this action themselves. The failure is a bare `startup_failure`: no job, no log, no
message naming the offending reference.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)", re.MULTILINE)
LOCAL = re.compile(r"^\./")
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")


class ActionPinningTests(unittest.TestCase):
    def test_action_uses_at_least_one_action(self) -> None:
        # Guards the pattern itself: were it to match nothing, the next test would pass vacuously.
        self.assertTrue(USES.search(ACTION))

    def test_every_used_action_is_pinned_to_a_full_commit_sha(self) -> None:
        unpinned = [
            match.group("ref")
            for match in USES.finditer(ACTION)
            if not LOCAL.match(match.group("ref")) and not PINNED.match(match.group("ref"))
        ]
        self.assertEqual([], unpinned, "pin these to a full 40-character commit SHA, with the version as a comment")


if __name__ == "__main__":
    unittest.main()
