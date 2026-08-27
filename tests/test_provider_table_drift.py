"""The action's provider table must match the CodeBoarding release action.yml pins.

The table exists because credentials have to be validated before the engine is installed,
which rules out asking the engine at run time. That copy is only safe while something
fails when it stops matching -- otherwise bumping the pin silently makes providers
unreachable (core added one, the table did not) or accepted here and broken later (core
removed one). This test is that something. It runs in the `core-compatibility` CI job,
which installs the pinned release; without the engine present it skips.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE = json.loads((ROOT / "scripts" / "action" / "llm-providers.json").read_text(encoding="utf-8"))

try:  # pragma: no cover - availability is the point of the skip
    from agents.llm_config import LLM_PROVIDERS as CORE_PROVIDERS
except Exception:  # noqa: BLE001 - any import failure means the engine is not installed
    CORE_PROVIDERS = None


@unittest.skipIf(CORE_PROVIDERS is None, "the pinned CodeBoarding release is not installed")
class ProviderTableDriftTests(unittest.TestCase):
    def test_the_table_pins_the_release_action_yml_installs(self) -> None:
        pinned = re.search(r"'codeboarding==([^']+)'", (ROOT / "action.yml").read_text())
        self.assertIsNotNone(pinned, "action.yml no longer pins a CodeBoarding release")
        self.assertEqual(
            TABLE["engine"],
            pinned.group(1),
            "llm-providers.json records a different release than action.yml installs",
        )

    def test_the_same_providers_exist_on_both_sides(self) -> None:
        self.assertEqual(
            sorted(TABLE["providers"]),
            sorted(CORE_PROVIDERS),
            "the action and the pinned engine disagree about which providers exist",
        )

    def test_selection_variables_match_core_exactly(self) -> None:
        """Selection is the whole rule: 'configured' means core would select it."""
        for name, provider in TABLE["providers"].items():
            with self.subTest(provider=name):
                self.assertEqual(
                    sorted(provider["selection_envs"]),
                    sorted(CORE_PROVIDERS[name].selection_envs),
                    f"{name}'s selection variables drifted from the engine",
                )

    def test_every_provider_key_reaches_the_variable_core_reads(self) -> None:
        for name, provider in TABLE["providers"].items():
            api_key_env = CORE_PROVIDERS[name].api_key_env
            if api_key_env is None:
                continue  # e.g. aws, whose SDK reads its own bearer token variable
            with self.subTest(provider=name):
                self.assertIn(
                    api_key_env,
                    provider["inputs"].values(),
                    f"{name} has no input that sets {api_key_env}",
                )

    def test_no_selection_variable_escapes_the_table(self) -> None:
        """with-auth.sh strips foreign selectors using this table; a variable missing
        from it is one core could still be selected by."""
        core = {var for c in CORE_PROVIDERS.values() for var in c.selection_envs}
        known = {
            var for p in TABLE["providers"].values() for var in list(p["selection_envs"]) + list(p["inputs"].values())
        }
        self.assertEqual(sorted(core - known), [])

    def test_aliases_point_at_providers_that_exist(self) -> None:
        for alias, target in TABLE["aliases"].items():
            with self.subTest(alias=alias):
                self.assertIn(target, CORE_PROVIDERS)
                self.assertNotIn(alias, CORE_PROVIDERS, f"{alias} is a real provider, not an alias")

    def test_the_hosted_tier_names_a_real_provider(self) -> None:
        self.assertIn(TABLE["hosted_provider"], CORE_PROVIDERS)


if __name__ == "__main__":
    unittest.main()
