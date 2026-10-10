"""Deprecated inputs keep working, translated in one place, and never silently lose to the new ones."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATOR = ROOT / "scripts" / "action" / "old_api_migrator.sh"


def migrate(**inputs: str) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    """Runs the migrator with action.yml's defaults, overridden by `inputs`."""
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "github-output"
        output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(MIGRATOR)],
            env={
                "PATH": os.environ["PATH"],
                "GITHUB_OUTPUT": str(output),
                "SYNCED_BRANCH": "",
                "SAVE_BASELINE_TO": "synced_branch",
                "OLD_TARGET_BRANCH": "",
                "OLD_SYNC_STRATEGY": "",
                **inputs,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return result, values


class OldApiMigratorTests(unittest.TestCase):
    def test_current_inputs_pass_through_without_warnings(self) -> None:
        result, values = migrate(SYNCED_BRANCH="main", SAVE_BASELINE_TO="baseline_branch")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(values, {"synced_branch": "main", "save_baseline_to": "baseline_branch"})
        self.assertNotIn("::warning::", result.stdout)

    def test_target_branch_becomes_synced_branch(self) -> None:
        result, values = migrate(OLD_TARGET_BRANCH="develop")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(values["synced_branch"], "develop")
        self.assertIn("::warning::target_branch is deprecated", result.stdout)

    def test_sync_strategy_values_map_onto_save_baseline_to(self) -> None:
        for old, new in (("push", "synced_branch"), ("pull_request", "pull_request")):
            with self.subTest(old=old):
                result, values = migrate(OLD_SYNC_STRATEGY=old)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(values["save_baseline_to"], new)
                self.assertIn("::warning::sync_strategy is deprecated", result.stdout)

    def test_a_value_sync_strategy_never_accepted_fails(self) -> None:
        result, _values = migrate(OLD_SYNC_STRATEGY="branch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("use save_baseline_to", result.stdout)

    def test_setting_an_input_under_both_names_fails(self) -> None:
        for inputs in (
            {"SYNCED_BRANCH": "main", "OLD_TARGET_BRANCH": "main"},
            {"SAVE_BASELINE_TO": "baseline_branch", "OLD_SYNC_STRATEGY": "push"},
        ):
            with self.subTest(inputs=inputs):
                result, _values = migrate(**inputs)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("deprecated name", result.stdout)


if __name__ == "__main__":
    unittest.main()
