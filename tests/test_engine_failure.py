"""scripts/action/engine_failure.py: what a person reads when the engine refused to finish."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "action" / "engine_failure.py"

QUOTA = {
    "mode": "incremental",
    "error": "LLM quota exhausted",
    "kind": "llm_quota_exhausted",
    "statusCode": 402,
    "provider": "openai",
    "requiresFullAnalysis": False,
    "exitCode": 3,
}


class EngineFailureTests(unittest.TestCase):
    def _run(self, error: dict | None, **env: str) -> tuple[dict[str, str], str, str]:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        if error is not None:
            (tmp / "codeboarding-engine-error.json").write_text(json.dumps(error), encoding="utf-8")
        output, summary = tmp / "output", tmp / "summary"
        output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(tmp),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_RUN_ID": "99",
                "GITHUB_RUN_ATTEMPT": "1",
                "MODE": "review",
                "LLM": "hosted",
                "PR_NUMBER": "6",
                "HEAD_SHA": "abc123",
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        body = Path(outputs["body_path"]).read_text(encoding="utf-8") if "body_path" in outputs else ""
        return outputs, body, summary.read_text(encoding="utf-8") if summary.exists() else ""

    def test_hosted_quota_names_the_reset_and_both_ways_out(self) -> None:
        outputs, body, summary = self._run(QUOTA)

        self.assertEqual(outputs["reason"], "llm_quota_exhausted")
        self.assertTrue(body.startswith("### CodeBoarding review · stopped: LLM quota used up\n"))
        self.assertIn("CodeBoarding stopped instead of publishing a map without AI naming.", body)
        self.assertIn("token quota is used up", body)
        self.assertIn("per GitHub owner per week and resets Monday 00:00 UTC", body)
        self.assertIn("`anthropic_api_key`", body)
        self.assertIn("`llm: license` with `license_key`", body)
        self.assertEqual(summary, body, "the run page says what the pull request says")
        self.assertEqual(
            body.rstrip("\n").splitlines()[-1],
            "<!-- codeboarding: platform_url=https://app.codeboarding.org/owner/repo/pull/6 head=abc123"
            " failure=llm_quota_exhausted -->",
        )

    def test_quota_on_your_own_key_points_at_your_provider(self) -> None:
        _, body, _ = self._run(QUOTA, LLM="openai")
        self.assertIn("On `llm: openai` that is the quota of your own provider account", body)
        self.assertNotIn("Monday", body)

    def test_llm_input_is_case_insensitive_like_the_credential_check(self) -> None:
        _, body, _ = self._run(QUOTA, LLM=" Hosted ")
        self.assertIn("resets Monday 00:00 UTC", body)

    def test_quota_on_a_license_offers_your_own_key(self) -> None:
        _, body, _ = self._run(QUOTA, LLM="license")
        self.assertIn("your CodeBoarding plan's allowance", body)
        self.assertIn("`anthropic_api_key`", body)

    def test_sync_goes_to_the_summary_without_a_comment_marker(self) -> None:
        _, body, summary = self._run(QUOTA, MODE="sync", PR_NUMBER="")
        self.assertTrue(summary.startswith("### CodeBoarding sync · stopped: LLM quota used up\n"))
        self.assertIn("The baseline was not updated, and no base analysis was published.", summary)
        self.assertNotIn("<!-- codeboarding:", body)

    def test_rejected_credentials_have_their_own_heading(self) -> None:
        outputs, body, _ = self._run({**QUOTA, "kind": "llm_auth", "statusCode": 401}, LLM="anthropic")
        self.assertEqual(outputs["reason"], "llm_auth")
        self.assertIn("stopped: LLM credentials rejected", body)
        self.assertIn("`llm: anthropic`", body)

    def test_nothing_recorded_means_nothing_reported(self) -> None:
        """An empty `reason` is what lets the generic failure comment through."""
        for error in (None, {"kind": "something_else"}):
            with self.subTest(error=error):
                outputs, _, summary = self._run(error)
                self.assertEqual(outputs, {})
                self.assertEqual(summary, "")


if __name__ == "__main__":
    unittest.main()
