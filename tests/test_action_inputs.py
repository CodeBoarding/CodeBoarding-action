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
TABLE = json.loads((ROOT / "scripts" / "action" / "llm-providers.json").read_text(encoding="utf-8"))


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

    def test_a_refused_run_reports_and_then_fails(self) -> None:
        report = ACTION.index("- name: Report LLM configuration failure")
        stop = ACTION.index("- name: Stop on LLM configuration failure")
        self.assertLess(report, stop, "the run fails before it explains why")
        self.assertIn("continue-on-error: true", ACTION[ACTION.index("id: llm") : report])


if __name__ == "__main__":
    unittest.main()
