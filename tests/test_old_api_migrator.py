"""Deprecated inputs keep working, translated in one place, and never silently lose to the new ones."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATOR = ROOT / "scripts" / "action" / "old_api_migrator.sh"
DEFAULT_ANALYSIS_BRANCH = "codeboarding/baseline"


def migrate(**inputs: str) -> tuple[subprocess.CompletedProcess, str]:
    """Runs the migrator for a sync of main with action.yml's defaults, overridden by `inputs`."""
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "github-output"
        output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(MIGRATOR)],
            env={
                "PATH": os.environ["PATH"],
                "GITHUB_OUTPUT": str(output),
                "MODE": "sync",
                "REF_NAME": "main",
                "ANALYSIS_BRANCH": DEFAULT_ANALYSIS_BRANCH,
                "OLD_TARGET_BRANCH": "",
                "OLD_SYNC_STRATEGY": "",
                **inputs,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return result, values.get("analysis_branch", "<none>")


class OldApiMigratorTests(unittest.TestCase):
    """Each deprecated input becomes the codeboarding_analysis_branch a person would write, or fails."""

    def test_current_inputs_pass_through_without_warnings(self) -> None:
        result, branch = migrate(ANALYSIS_BRANCH="main")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(branch, "main")
        self.assertNotIn("::warning", result.stdout)

    def test_sync_strategy_push_names_the_synced_branch(self) -> None:
        result, branch = migrate(OLD_SYNC_STRATEGY="push")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(branch, "main")
        self.assertIn("codeboarding_analysis_branch: main", result.stdout)

    def test_sync_strategy_pull_request_keeps_the_default_analysis_branch(self) -> None:
        result, branch = migrate(OLD_SYNC_STRATEGY="pull_request")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(branch, DEFAULT_ANALYSIS_BRANCH)
        self.assertIn("codeboarding/sync", result.stdout)

    def test_target_branch_naming_the_synced_branch_changes_nothing(self) -> None:
        result, branch = migrate(OLD_TARGET_BRANCH="main")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(branch, DEFAULT_ANALYSIS_BRANCH)
        self.assertIn("::warning", result.stdout)

    def test_what_the_new_inputs_cannot_express_fails_with_the_way_to_migrate(self) -> None:
        for inputs in (
            {"OLD_TARGET_BRANCH": "develop"},
            {"OLD_SYNC_STRATEGY": "branch"},
            {"ANALYSIS_BRANCH": "main", "OLD_SYNC_STRATEGY": "push"},
            {"ANALYSIS_BRANCH": "main", "OLD_SYNC_STRATEGY": "pull_request"},
        ):
            with self.subTest(inputs=inputs):
                result, branch = migrate(**inputs)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(branch, "<none>")
                self.assertIn("#moving-an-existing-setup", result.stdout)

    def test_a_stale_target_branch_names_the_branch_the_run_is_on(self) -> None:
        # A repository whose branch is master, with target_branch: main left in its workflow:
        # "run sync on main" would send it to a branch that does not exist.
        result, _branch = migrate(REF_NAME="master", OLD_TARGET_BRANCH="main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("for this run is master", result.stdout)
        self.assertIn("Remove target_branch: main, and sync analyzes master.", result.stdout)
        self.assertNotIn("run sync on main", result.stdout)

    def test_an_empty_analysis_branch_fails(self) -> None:
        result, branch = migrate(ANALYSIS_BRANCH="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(branch, "<none>")
        self.assertIn("must name a branch", result.stdout)

    def test_review_never_reads_the_sync_inputs(self) -> None:
        result, branch = migrate(
            MODE="review", REF_NAME="42/merge", OLD_TARGET_BRANCH="develop", OLD_SYNC_STRATEGY="push"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(branch, DEFAULT_ANALYSIS_BRANCH)
        self.assertIn("replace it with codeboarding_analysis_branch", result.stdout)


if __name__ == "__main__":
    unittest.main()
