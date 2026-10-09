"""The progress comment while a base is built from scratch: two steps, elapsed time only."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POST_PROGRESS = ROOT / "scripts" / "action" / "post-progress.sh"


class ProgressCommentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.calls = self.root / "calls"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _post(self, step: str, elapsed: int, comment_id: str = "77", **extra: str) -> list[str]:
        gh = self.bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n----\\n" "$*" >> "{self.calls}"\n'
            f'case "$*" in *"/comments?per_page"*) echo "{comment_id}" ;; esac\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        subprocess.run(
            [str(POST_PROGRESS), step, str(elapsed)],
            env={
                "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(self.root),
                "PROGRESS_HEADER": "codeboarding-review",
                "PR_NUMBER": "9",
                "REPOSITORY": "owner/repo",
                "BASE_REF": "main",
                "REVIEW_BASE_SHA": "a1b2c3d4e5f6",
                "GITHUB_RUN_ID": "55",
                **extra,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        return [c for c in self.calls.read_text().split("\n----\n") if "PATCH" in c] if self.calls.exists() else []

    def test_the_base_step_shows_elapsed_minutes_and_the_reason(self) -> None:
        (patch,) = self._post("base", 125, FULL_CAUSE="no_baseline")
        self.assertIn("1. ⏳ Building the diagram of `main` @a1b2c3d from scratch · running for 2 min\n", patch)
        self.assertIn("   `main` has no saved diagram yet, so this review builds one first.", patch)
        self.assertIn("\n2. Analysing this PR's changes\n", patch)
        self.assertNotIn("estimate", patch.lower())

    def test_the_first_minute_does_not_read_as_zero(self) -> None:
        (patch,) = self._post("base", 0)
        self.assertIn("running for less than a minute", patch)

    def test_an_incompatible_base_says_why(self) -> None:
        (patch,) = self._post("base", 60, FULL_CAUSE="incompatible")
        self.assertIn("The saved diagram of `main` was made by a different engine version or settings", patch)

    def test_the_head_step_reports_the_measured_base_time(self) -> None:
        (patch,) = self._post("head", 534)
        self.assertIn("1. ✅ Built the diagram of `main` @a1b2c3d from scratch in 8 m 54 s\n", patch)
        self.assertIn("2. ⏳ Analysing this PR's changes", patch)
        self.assertTrue(patch.rstrip().endswith("<!-- Sticky Pull Request Commentcodeboarding-review -->"))

    def test_the_comment_is_looked_up_once(self) -> None:
        self._post("base", 0)
        self._post("base", 60)
        lookups = [c for c in self.calls.read_text().split("\n----\n") if "/comments?per_page" in c]
        self.assertEqual(len(lookups), 1)

    def test_a_stopped_ticker_never_edits_after_the_next_step(self) -> None:
        stop = self.root / "stop"
        stop.touch()
        self.assertEqual(self._post("base", 120, PROGRESS_STOP_FILE=str(stop)), [])
        # The step-2 edit is the one that follows the stop, so it still lands.
        self.assertEqual(len(self._post("head", 120, PROGRESS_STOP_FILE=str(stop))), 1)

    def test_no_progress_comment_means_no_edit(self) -> None:
        self.assertEqual(self._post("base", 0, comment_id=""), [])


if __name__ == "__main__":
    unittest.main()
