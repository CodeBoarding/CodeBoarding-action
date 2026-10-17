"""Exercise core's subprocess refusal contract through the posted comment body."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import analyze_repository as ar

ROOT = Path(__file__).resolve().parent.parent
PRICING = "https://codeboarding.org/pricing"


class QuotaCommentTests(unittest.TestCase):
    def _failure_body(self, report: object, tier: str = "hosted", mode: str = "review") -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "analysis-outputs"
            program = (
                "import os,sys; from pathlib import Path; "
                f"Path(os.environ['CODEBOARDING_ERROR_FILE']).write_text({json.dumps(json.dumps(report))}); "
                "sys.exit(3)"
            )
            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs), "CODEBOARDING_LLM_TIER": tier}),
                self.assertRaises(ar.AnalysisError),
            ):
                ar._run_command([sys.executable, "-c", program], root / "out")

            reason = outputs.read_text().removeprefix("failure_reason=").strip()
            summary = root / "summary.md"
            script = "build-review-failure.sh" if mode == "review" else "sync-summary.sh"
            result = subprocess.run(
                [str(ROOT / "scripts" / "action" / script)],
                env={
                    "PATH": os.environ["PATH"],
                    "RUNNER_TEMP": tmp,
                    "GITHUB_OUTPUT": str(root / "comment-outputs"),
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_REPOSITORY": "octocat/hello",
                    "GITHUB_RUN_ID": "123",
                    "FAILURE_REASON": reason,
                },
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stderr, "")
            text = summary.read_text()
            if mode == "review":
                self.assertEqual(text, (root / "review-failure.md").read_text())
                self.assertIn(f"path={root / 'review-failure.md'}", (root / "comment-outputs").read_text())
            return text, reason

    def test_free_credits_comment_links_to_purchase_and_explains_setup(self):
        body, reason = self._failure_body({"version": 1, "code": "free_quota_exhausted"})
        self.assertIn("### CodeBoarding review · failed", body)
        self.assertIn("free CodeBoarding credits for this week are exhausted", body)
        self.assertIn(f"[Buy a CodeBoarding license]({PRICING})", body)
        self.assertIn("shared across its repositories", body)
        self.assertIn("`CODEBOARDING_LICENSE`", body)
        self.assertIn("`llm: license`", body)
        self.assertIn("`license_key: ${{ secrets.CODEBOARDING_LICENSE }}`", body)
        self.assertIn("https://github.com/octocat/hello/actions/runs/123", body)
        self.assertNotIn("Traceback", body)
        self.assertIn(reason, body)

    def test_sync_reports_the_same_purchase_instructions(self):
        body, reason = self._failure_body({"version": 1, "code": "free_quota_exhausted"}, mode="sync")
        self.assertIn(f"[Buy a CodeBoarding license]({PRICING})", body)
        self.assertIn(reason, body)

    def test_supplied_license_is_checked_before_suggesting_another_purchase(self):
        for code in ("license_not_accepted", "free_quota_exhausted"):
            with self.subTest(code=code):
                body, _ = self._failure_body({"version": 1, "code": code}, tier="license")
                self.assertIn("Check that the license is active", body)
                self.assertIn(f"]({PRICING})", body)
                self.assertNotIn("After purchasing", body)

    def test_provider_billing_does_not_sell_a_codeboarding_license(self):
        for code in ("provider_quota_exhausted", "hosted_provider_quota_exhausted"):
            with self.subTest(code=code):
                body, _ = self._failure_body({"version": 1, "code": code})
                self.assertNotIn(PRICING, body)
                self.assertNotIn("Buy", body)

    def test_unknown_or_invalid_report_keeps_the_legacy_message(self):
        for report in (
            [],
            {"version": 2, "code": "free_quota_exhausted"},
            {"version": 1, "code": []},
            {"version": 1, "code": "unknown"},
        ):
            with self.subTest(report=report):
                body, _ = self._failure_body(report)
                self.assertIn("LLM provider's token or credit quota is exhausted", body)
                self.assertNotIn(PRICING, body)

    def test_workflow_posts_the_tested_failure_file(self):
        action = (ROOT / "action.yml").read_text()
        steps = re.split(r"\n    - name: ", action[action.index("\nruns:\n") :])[1:]
        build = next(step for step in steps if step.startswith("Build review failure\n"))
        post = next(step for step in steps if step.startswith("Post review failure\n"))
        self.assertIn("failure()", build)
        self.assertIn(
            "steps.review_baseline.outputs.failure_reason || steps.review_analyze.outputs.failure_reason", build
        )
        self.assertIn("build-review-failure.sh", build)
        self.assertIn("marocchino/sticky-pull-request-comment@v2", post)
        self.assertIn("path: ${{ steps.review_failure.outputs.path }}", post)
        self.assertIn("header: ${{ steps.guard.outputs.comment_id }}", post)


if __name__ == "__main__":
    unittest.main()
