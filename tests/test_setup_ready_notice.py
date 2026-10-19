"""The one new comment on a setup PR that makes GitHub email its author the map is ready."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "scripts" / "action" / "notify-setup-ready.sh"
MARKER = "<!-- codeboarding-setup-ready -->"


class SetupReadyNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.calls = self.root / "calls"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, existing: str = "", **extra: str) -> list[str]:
        gh = self.bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n----\\n" "$*" >> "{self.calls}"\n'
            f'case "$*" in *"/comments?per_page"*) printf "%s" "{existing}" ;; esac\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        env = {
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "REPOSITORY": "owner/repo",
            "PR_NUMBER": "418",
            "PR_AUTHOR": "maria-k",
            "HEAD_REF": "codeboarding/setup-action",
            "BASE_REF": "main",
            **extra,
        }
        subprocess.run([str(NOTIFY)], env=env, check=True, capture_output=True, text=True)
        if not self.calls.exists():
            return []
        return [c for c in self.calls.read_text().split("\n----\n") if c.startswith("api -X POST")]

    def test_a_setup_pr_gets_one_new_comment_that_mentions_its_author(self) -> None:
        (post,) = self._run()
        self.assertIn("repos/owner/repo/issues/418/comments", post)
        self.assertIn("@maria-k your CodeBoarding map of `main` is ready.", post)
        self.assertIn("https://app.codeboarding.org/owner/repo/pull/418?", post)
        self.assertNotIn("run=", post)
        self.assertIn(MARKER, post)

    def test_it_is_posted_once_per_pull_request(self) -> None:
        self.assertEqual(self._run(existing="991"), [])

    def test_other_pull_requests_get_nothing(self) -> None:
        self.assertEqual(self._run(HEAD_REF="feat/retry-webhooks"), [])
        self.assertFalse(self.calls.exists(), "no API call at all outside the setup branch")

    def test_a_bot_author_is_not_mentioned(self) -> None:
        (post,) = self._run(PR_AUTHOR="codeboarding-app[bot]")
        self.assertIn("Your CodeBoarding map of `main` is ready.", post)
        self.assertNotIn("@", post.split("body=", 1)[1].split("\n", 1)[0])

    def test_the_step_runs_only_after_a_posted_review_on_the_setup_branch(self) -> None:
        action = (ROOT / "action.yml").read_text(encoding="utf-8")
        start = action.index("    - name: Tell the setup PR's author the map is ready")
        step = action[start:action.index("\n    - name:", start + 1)]
        self.assertGreater(start, action.index("    - name: Post review comment"))
        condition = next(line for line in step.splitlines() if line.strip().startswith("if:"))
        self.assertIn("steps.review_comment.outcome == 'success'", condition)
        self.assertIn("github.event_name == 'pull_request'", condition)
        self.assertIn("github.head_ref == 'codeboarding/setup-action'", condition)
        self.assertIn("continue-on-error: true", step, "a notification must never fail the review")
        self.assertIn("notify-setup-ready.sh", step)


if __name__ == "__main__":
    unittest.main()
