"""The progress comment while a base is built from scratch: two steps, base then head."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE_PROGRESS = ROOT / "scripts" / "action" / "update-review-progress.sh"


class ProgressCommentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.calls = self.root / "calls"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _post(self, step: str, comment_id: str = "77", **extra: str) -> list[str]:
        gh = self.bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n----\\n" "$*" >> "{self.calls}"\n'
            f'case "$*" in *"/comments?per_page"*) echo "{comment_id}" ;; esac\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        subprocess.run(
            [str(UPDATE_PROGRESS), step],
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

    def test_the_base_step_says_the_base_is_being_built_and_why(self) -> None:
        (patch,) = self._post("base")
        self.assertIn("1. ⏳ Building the diagram of `main` @a1b2c3d from scratch\n", patch)
        self.assertIn("   `main` has no saved diagram this review can start from, so it builds one first.", patch)
        self.assertIn("\n2. Analysing this PR's changes\n", patch)

    def test_the_head_step_marks_the_base_done(self) -> None:
        (patch,) = self._post("head")
        self.assertIn("1. ✅ Built the diagram of `main` @a1b2c3d from scratch\n", patch)
        self.assertIn("2. ⏳ Analysing this PR's changes", patch)
        self.assertTrue(patch.rstrip().endswith("<!-- Sticky Pull Request Commentcodeboarding-review -->"))

    def test_the_comment_is_looked_up_once(self) -> None:
        self._post("base")
        self._post("head")
        lookups = [c for c in self.calls.read_text().split("\n----\n") if "/comments?per_page" in c]
        self.assertEqual(len(lookups), 1)

    def test_no_progress_comment_means_no_edit(self) -> None:
        self.assertEqual(self._post("base", comment_id=""), [])


if __name__ == "__main__":
    unittest.main()
