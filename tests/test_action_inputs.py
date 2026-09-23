"""action.yml must expose, and wire, exactly the contract the provider table describes.

The resolver only ever sees what action.yml hands it. A provider in the table with no
input declared is unreachable; an input declared but not wired reads as empty and is
refused as "you did not set your key" when the user did. Both are silent, so they are
checked here rather than discovered in a repository.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")
TABLE = json.loads((ROOT / "scripts" / "action" / "supported-providers.json").read_text(encoding="utf-8"))
DOGFOOD = (ROOT / ".github" / "workflows" / "codeboarding.yml").read_text(encoding="utf-8")


def declared_inputs() -> dict[str, str]:
    """Input name -> its declaration block, read from action.yml's inputs section."""
    section = ACTION[ACTION.index("\ninputs:\n") : ACTION.index("\noutputs:\n")]
    blocks = re.split(r"\n {2}(?=[a-z0-9_]+:\n)", section)
    found = {}
    for block in blocks:
        match = re.match(r"\s*([a-z0-9_]+):\n", block)
        if match:
            found[match.group(1)] = block
    return found


def table_inputs() -> set[str]:
    return {i for p in TABLE["providers"].values() for i in p["inputs"]}


class ActionInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = declared_inputs()

    def test_every_provider_input_is_declared(self) -> None:
        missing = sorted(table_inputs() - set(self.inputs))
        self.assertEqual(missing, [], f"provider inputs missing from action.yml: {missing}")

    def test_every_provider_input_is_wired_to_the_resolver(self) -> None:
        for name in sorted(table_inputs()):
            with self.subTest(input=name):
                self.assertIn(
                    f"CB_IN_{name.upper()}: ${{{{ inputs.{name} }}}}",
                    ACTION,
                    f"{name} is declared but never reaches the resolver",
                )

    def test_no_declared_provider_input_is_absent_from_the_table(self) -> None:
        """An input the table does not own can never be read, so it would mislead."""
        suffixes = ("_api_key", "_base_url", "_region")
        declared = {n for n in self.inputs if n.endswith(suffixes) and n not in {"license_key", "github_token"}}
        self.assertEqual(sorted(declared - table_inputs()), [])

    def test_llm_is_required_and_has_no_default(self) -> None:
        block = self.inputs["llm"]
        self.assertIn("required: true", block)
        self.assertNotIn("default:", block)

    def test_depth_is_wired_to_state_identity_and_both_analysis_modes(self) -> None:
        """The workflow's depth_cap is only a request: the preflight's answer, clamped to the
        payer's plan, is what the identity and both analyses run at (R4, R5)."""
        self.assertIn("default: '2'", self.inputs["depth_cap"])
        self.assertNotIn("depth_level", self.inputs)
        for identifier in ("id: state", "id: sync_analyze", "id: review_analyze"):
            start = ACTION.index(identifier)
            block = ACTION[start : ACTION.index("\n      run:", start)]
            self.assertIn("DEPTH_CAP: ${{ steps.preflight.outputs.depth_cap }}", block)
        preflight = ACTION[ACTION.index("id: preflight") :]
        self.assertIn("DEPTH_CAP: ${{ inputs.depth_cap }}", preflight[: preflight.index("\n      run:")])
        self.assertEqual(ACTION.count("${{ inputs.depth_cap }}"), 1, "only the preflight reads the input")
        review = ACTION[ACTION.index("id: review_analyze") :]
        self.assertIn(
            "FULL_ANALYSIS: ${{ steps.preflight.outputs.full_analysis }}", review[: review.index("\n      run:")]
        )

    def test_the_preflight_runs_before_the_engine_install_in_every_mode(self) -> None:
        start = ACTION.index("- name: Start the run with CodeBoarding")
        self.assertLess(ACTION.index("- name: Stop on LLM configuration failure"), start, "it reads the resolved tier")
        self.assertLess(ACTION.index("- name: Checkout analysis target"), start, "it reads the baseline's depth")
        self.assertLess(start, ACTION.index("- name: Install CodeBoarding"))
        condition = ACTION[start : ACTION.index("shell:", start)]
        self.assertIn("if: steps.guard.outputs.skip != 'true'\n", condition, "no credential mode is exempt")

    def test_a_refused_run_does_no_analysis_work(self) -> None:
        for step in (
            "Setup Java for CodeBoarding",
            "Install CodeBoarding",
            "Configure analysis authentication",
            "Resolve analysis identity",
            "Analyze baseline",
            "Deliver baseline",
            "Analyze pull request",
            "Render review diagram",
            "Post review comment",
        ):
            with self.subTest(step=step):
                start = ACTION.index(f"- name: {step}\n")
                condition = ACTION[start : ACTION.index("\n", ACTION.index("if:", start))]
                self.assertIn("steps.preflight.outputs.allowed != 'false'", condition)

    def test_the_finish_runs_last_on_every_outcome_and_never_fails_the_job(self) -> None:
        start = ACTION.index("- name: Finish the run with CodeBoarding")
        self.assertEqual(ACTION[start:].count("- name:"), 1, "the finish is the last step")
        block = ACTION[start:]
        self.assertIn("if: always() && steps.preflight.outputs.run_id != ''", block)
        self.assertIn("continue-on-error: true", block)
        self.assertIn("steps.review_analyze.outputs.analysis_path || steps.sync_analyze.outputs.analysis_path", block)
        self.assertIn("JOB_STATUS: ${{ job.status }}", block)

    def test_license_key_is_deprecated_but_still_wired(self) -> None:
        self.assertIn("Deprecated", self.inputs["license_key"])
        self.assertIn("CB_IN_LICENSE_KEY: ${{ inputs.license_key }}", ACTION)

    def test_default_workflow_reviews_drafts_on_open_and_new_commits(self) -> None:
        self.assertNotIn("github.event.pull_request.draft", DOGFOOD)
        start = DOGFOOD.index("types:")
        types = DOGFOOD[start : DOGFOOD.index("\n", start)]
        for event in ("opened", "reopened", "synchronize"):
            self.assertIn(event, types)
        for state_change in ("ready_for_review", "converted_to_draft"):
            self.assertNotIn(state_change, types)

    def test_the_inferred_credential_inputs_are_gone(self) -> None:
        """`llm_api_key`/`llm_provider` are what made a fallback expressible at all."""
        for stale in ("llm_api_key", "llm_provider"):
            self.assertNotIn(stale, self.inputs)
            self.assertNotIn(f"inputs.{stale}", ACTION)

    def test_credentials_resolve_before_the_checkout_and_the_engine_install(self) -> None:
        """Fail-fast is positional: preflight is worth little after a minute of setup."""
        preflight = ACTION.index("- name: Check LLM configuration")
        for later in ("- name: Checkout analysis target", "- name: Install CodeBoarding"):
            self.assertLess(preflight, ACTION.index(later), f"{later} runs before preflight")

    def test_the_review_reaches_the_job_summary_as_well_as_the_comment(self) -> None:
        """The comment is the product, but it is not always reachable: a manual dispatch has
        no pull request, and a token without `pull-requests: write` cannot write one. Job
        summaries are excluded from the artifact storage allowance, so the record is free."""
        start = ACTION.index("- name: Add the review to the job summary")
        block = ACTION[start : ACTION.index("- name: Post review failure", start)]
        self.assertIn("GITHUB_STEP_SUMMARY", block)
        self.assertIn("steps.review_body.outputs.path", block)
        # Never fail a good review because the summary write did not work.
        self.assertIn("continue-on-error: true", block)
        self.assertLess(ACTION.index("- name: Post review comment"), start, "comment first")

    def test_a_crashed_credential_check_still_stops_the_run(self) -> None:
        """The check is `continue-on-error` so a refusal can be reported before the job
        dies. That same flag would let a crash in it through: no `error` output written,
        so a condition keyed only on the code is false and the run reaches the checkout
        and the engine install. The stop must also watch the step's outcome."""
        start = ACTION.index("- name: Stop on LLM configuration failure")
        condition = ACTION[start : ACTION.index("run:", start)]
        self.assertIn("steps.llm.outputs.error != ''", condition)
        self.assertIn("steps.llm.outcome != 'success'", condition)

    def test_the_generic_failure_comment_never_buries_the_actionable_one(self) -> None:
        """Both write the same sticky comment, and the generic one runs on `failure()`.

        Without the guard, a run stopped for a missing secret posts the input and secret to
        fix, then immediately replaces it with "see the workflow logs" -- sending the reader
        to hunt for what they had just been told.
        """
        start = ACTION.index("- name: Post review failure")
        condition = ACTION[start : ACTION.index("message:", start)]
        self.assertIn("steps.llm.outputs.error == ''", condition)

    def test_python_is_available_before_the_credential_check_runs(self) -> None:
        """The check is a Python program, so a runner without a system python3 would fail a
        configuration that is perfectly valid."""
        self.assertLess(
            ACTION.index("- name: Setup Python"),
            ACTION.index("- name: Check LLM configuration"),
        )

    def test_a_refused_run_reports_and_then_fails(self) -> None:
        report = ACTION.index("- name: Report LLM configuration failure")
        stop = ACTION.index("- name: Stop on LLM configuration failure")
        self.assertLess(report, stop, "the run fails before it explains why")
        self.assertIn("continue-on-error: true", ACTION[ACTION.index("id: llm") : report])


if __name__ == "__main__":
    unittest.main()
