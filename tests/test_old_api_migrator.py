"""Deprecated inputs keep working, translated in one place, and never silently lose to the new ones."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATOR = ROOT / "scripts" / "action" / "old_api_migrator.sh"


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
                "ANALYSIS_LOCATION": "codeboarding_branch",
                "OLD_TARGET_BRANCH": "",
                "OLD_SYNC_STRATEGY": "",
                **inputs,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return result, values.get("analysis_location", "<none>")


class OldApiMigratorTests(unittest.TestCase):
    """Each deprecated input becomes the codeboarding_analysis_location a person would write, or fails."""

    def test_current_inputs_pass_through_without_warnings(self) -> None:
        for location in ("codeboarding_branch", "in_place"):
            with self.subTest(location=location):
                result, value = migrate(ANALYSIS_LOCATION=location)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(value, location)
                self.assertNotIn("::warning", result.stdout)

    def test_a_location_that_is_not_one_of_the_two_fails(self) -> None:
        for location in ("", "main", "codeboarding/baseline"):
            with self.subTest(location=location):
                result, value = migrate(ANALYSIS_LOCATION=location)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(value, "<none>")
                self.assertIn("must be codeboarding_branch or in_place", result.stdout)

    def test_sync_strategy_maps_onto_the_location_in_both_modes(self) -> None:
        # Review reads where sync wrote, so a shared with-block keeps both in step.
        for mode in ("sync", "review"):
            for old, new in (("push", "in_place"), ("pull_request", "codeboarding_branch")):
                with self.subTest(mode=mode, old=old):
                    result, value = migrate(MODE=mode, OLD_SYNC_STRATEGY=old)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(value, new)
                    self.assertIn(f"codeboarding_analysis_location: {new}", result.stdout)

    def test_sync_strategy_pull_request_says_to_close_the_rolling_pull_request(self) -> None:
        result, _value = migrate(OLD_SYNC_STRATEGY="pull_request")
        self.assertIn("codeboarding/sync", result.stdout)

    def test_target_branch_naming_the_branch_the_run_is_on_changes_nothing(self) -> None:
        result, value = migrate(OLD_TARGET_BRANCH="main")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(value, "codeboarding_branch")
        self.assertIn("target_branch is deprecated", result.stdout)

    def test_a_target_branch_elsewhere_points_at_the_push_trigger(self) -> None:
        # It must not tell anyone to analyze a branch they did not choose.
        result, _value = migrate(REF_NAME="master", OLD_TARGET_BRANCH="develop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this run is on master. List develop under on: push: branches:", result.stdout)

    def test_what_the_new_input_cannot_express_fails_with_the_way_to_migrate(self) -> None:
        for inputs in (
            {"OLD_TARGET_BRANCH": "develop"},
            {"OLD_SYNC_STRATEGY": "branch"},
            {"ANALYSIS_LOCATION": "in_place", "OLD_SYNC_STRATEGY": "push"},
            {"ANALYSIS_LOCATION": "in_place", "OLD_SYNC_STRATEGY": "pull_request"},
        ):
            with self.subTest(inputs=inputs):
                result, value = migrate(**inputs)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(value, "<none>")
                self.assertIn("#moving-an-existing-setup", result.stdout)

    def test_review_never_fails_on_target_branch(self) -> None:
        result, value = migrate(MODE="review", REF_NAME="42/merge", OLD_TARGET_BRANCH="develop")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(value, "codeboarding_branch")


if __name__ == "__main__":
    unittest.main()
