"""Credentials last exactly as long as the analysis steps that need them."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WITH_AUTH = ROOT / "scripts" / "action" / "with-auth.sh"
ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")


class WithAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner_temp = Path(self.temp_dir.name)
        self.auth = self.runner_temp / "codeboarding-auth"
        self.auth.mkdir()
        (self.auth / "tier").write_text("byok", encoding="utf-8")
        (self.auth / "provider-name").write_text("openai", encoding="utf-8")

    def _run(self, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(WITH_AUTH), "true"],
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(self.runner_temp), **extra},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_credentials_are_removed_after_the_command(self) -> None:
        self.assertEqual(self._run().returncode, 0)
        self.assertFalse(self.auth.exists())

    def test_a_step_that_keeps_them_leaves_them_for_the_next(self) -> None:
        self.assertEqual(self._run(KEEP_AUTH="true").returncode, 0)
        self.assertTrue((self.auth / "tier").exists())
        # The next step analyzes, then removes them.
        self.assertEqual(self._run().returncode, 0, "the second analysis step found no credentials")
        self.assertFalse(self.auth.exists())


class ActionWiringTests(unittest.TestCase):
    def _steps(self) -> list[str]:
        runs = ACTION[ACTION.index("\nruns:\n") :]
        return re.split(r"\n    - name: ", runs)[1:]

    def test_every_step_but_the_last_of_a_mode_keeps_credentials(self) -> None:
        """with-auth.sh consumes credentials; a second analysis step in one run needs them kept."""
        for mode in ("review", "sync"):
            steps = [s for s in self._steps() if "with-auth.sh" in s and f"mode == '{mode}'" in s]
            self.assertTrue(steps, mode)
            for step in steps[:-1]:
                self.assertIn("KEEP_AUTH: 'true'", step, f"{mode}: {step.splitlines()[0]} consumes the credentials")
            self.assertNotIn("KEEP_AUTH", steps[-1], f"{mode}: the last analysis step must remove them")

    def test_kept_credentials_are_always_removed(self) -> None:
        names = [s.splitlines()[0] for s in self._steps()]
        cleanup = self._steps()[names.index("Remove analysis credentials")]
        self.assertIn("clear-auth.sh", cleanup)
        self.assertIn("always()", cleanup)
        self.assertGreater(names.index("Remove analysis credentials"), names.index("Analyze pull request"))


if __name__ == "__main__":
    unittest.main()
