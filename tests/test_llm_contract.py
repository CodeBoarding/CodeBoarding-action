"""The credential contract: exactly one explicitly named source, or a named failure.

Every case here is a workflow someone could plausibly write. The point of the table is
that each one either resolves to precisely what it asked for, or is refused with a code
and a message naming the input to fix -- never quietly resolved to something else.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("credential_check", ROOT / "scripts" / "action" / "credential_check.py")
credential_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(credential_check)

# Both, since the relay needs both and the check now says so.
OIDC = {
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
}


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = credential_check.load_table()

    def resolve(self, **environ: str) -> dict:
        return credential_check.resolve(self.table, environ)

    def refuse(self, **environ: str) -> credential_check.ConfigError:
        with self.assertRaises(credential_check.ConfigError) as caught:
            credential_check.resolve(self.table, environ)
        return caught.exception

    # -- accepted shapes ---------------------------------------------------

    def test_every_provider_resolves_from_its_own_inputs(self) -> None:
        """The table is the contract: each provider must be reachable through it."""
        for name, provider in self.table["providers"].items():
            with self.subTest(provider=name):
                inputs = {
                    f"CB_IN_{i.upper()}": "value"
                    for i, var in provider["inputs"].items()
                    if var in provider["selection_envs"]
                }
                plan = self.resolve(CB_IN_LLM=name, **inputs)
                self.assertEqual(plan["provider"], name)
                self.assertEqual(plan["tier"], "byok")
                self.assertTrue(
                    any(plan["env"].get(var) for var in provider["selection_envs"]),
                    f"{name} resolved without a variable core selects it by",
                )

    def test_hosted_and_license_are_named_not_inferred(self) -> None:
        hosted = self.resolve(CB_IN_LLM="hosted", **OIDC)
        self.assertEqual(hosted["tier"], "hosted")
        self.assertEqual(hosted["env"], {})

        licensed = self.resolve(CB_IN_LLM="license", CB_IN_LICENSE_KEY="lic", **OIDC)
        self.assertEqual(licensed["tier"], "license")
        self.assertEqual(licensed["license"], "lic")

    def test_licence_alongside_a_provider_key_is_recorded_not_refused(self) -> None:
        """A CodeBoarding plan and your own tokens are two different questions."""
        plan = self.resolve(CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="k", CB_IN_LICENSE_KEY="lic")
        self.assertEqual(plan["tier"], "byok+license")
        self.assertEqual(plan["provider"], "anthropic")

    def test_one_spelling_per_provider_and_nothing_else(self) -> None:
        """Casing and surrounding space are forgiven; a second spelling is not.

        Aliases look free and are not: each one is another value to document, test and keep
        in step with the picker, and the refusal already lists what is accepted.
        """
        for value in ("aws_bedrock", "AWS_BEDROCK", "  aws_bedrock  "):
            with self.subTest(value=value):
                plan = self.resolve(CB_IN_LLM=value, CB_IN_AWS_BEDROCK_API_KEY="k")
                self.assertEqual(plan["provider"], "aws_bedrock")
        for value in ("aws", "bedrock", "aws-bedrock", "gemini"):
            with self.subTest(rejected=value):
                error = self.refuse(CB_IN_LLM=value, CB_IN_AWS_BEDROCK_API_KEY="k")
                self.assertEqual(error.code, "unknown_llm")
                self.assertIn("aws_bedrock", error.message, "the refusal must name what to use")

    def test_each_providers_inputs_are_named_after_the_value_that_selects_it(self) -> None:
        """`llm: X` always pairs with `X_api_key`, so the pairing never has to be looked up."""
        for name, provider in self.table["providers"].items():
            for input_name in provider["inputs"]:
                with self.subTest(provider=name, input=input_name):
                    self.assertTrue(input_name.startswith(f"{name}_"), input_name)

    def test_the_table_carries_no_alias_map(self) -> None:
        self.assertNotIn("aliases", self.table)

    def test_pasted_key_wrappers_are_stripped(self) -> None:
        plan = self.resolve(CB_IN_LLM="openrouter", CB_IN_OPENROUTER_API_KEY="  'OPENROUTER_API_KEY=\"sk-x\"'  \n")
        self.assertEqual(plan["env"]["OPENROUTER_API_KEY"], "sk-x")

    def test_endpoint_only_providers_resolve_without_a_key(self) -> None:
        for name, endpoint in (("ollama", "OLLAMA_BASE_URL"), ("litellm", "LITELLM_BASE_URL")):
            with self.subTest(provider=name):
                plan = self.resolve(CB_IN_LLM=name, **{f"CB_IN_{name.upper()}_BASE_URL": "http://host:1234"})
                self.assertEqual(plan["env"][endpoint], "http://host:1234")

    # -- refused shapes ----------------------------------------------------

    def test_an_undeclared_workflow_is_refused_rather_than_defaulted(self) -> None:
        error = self.refuse()
        self.assertEqual(error.code, "missing_llm")
        self.assertIn("required", error.message)

    def test_a_named_provider_without_its_key_names_the_input_and_the_secret(self) -> None:
        error = self.refuse(CB_IN_LLM="anthropic")
        self.assertEqual(error.code, "missing_provider_key")
        self.assertIn("anthropic_api_key", error.message)
        self.assertIn("ANTHROPIC_API_KEY", error.message)

    def test_a_key_alone_does_not_configure_an_endpoint_selected_provider(self) -> None:
        """Core selects ollama by its endpoint, so a key alone leaves it unusable."""
        for name in ("ollama", "litellm"):
            with self.subTest(provider=name):
                error = self.refuse(CB_IN_LLM=name, **{f"CB_IN_{name.upper()}_API_KEY": "k"})
                self.assertEqual(error.code, "missing_provider_key")
                self.assertIn(f"{name}_base_url", error.message)
                self.assertNotIn("repository secret", error.message)

    def test_a_second_providers_key_is_refused_rather_than_ignored(self) -> None:
        error = self.refuse(CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="k", CB_IN_OPENAI_API_KEY="other")
        self.assertEqual(error.code, "foreign_provider_key")
        self.assertIn("openai_api_key", error.message)

    def test_hosted_refuses_to_share_a_workflow_with_a_provider_key(self) -> None:
        error = self.refuse(CB_IN_LLM="hosted", CB_IN_ANTHROPIC_API_KEY="k", **OIDC)
        self.assertEqual(error.code, "hosted_with_provider_key")
        self.assertIn("llm: anthropic", error.message)

    def test_license_refuses_to_share_a_workflow_with_a_provider_key(self) -> None:
        """The mirror of the hosted case, and it shipped untested: `llm: license` runs on
        CodeBoarding's credentials, so a provider key beside it asks for two things at once."""
        error = self.refuse(CB_IN_LLM="license", CB_IN_LICENSE_KEY="lic", CB_IN_ANTHROPIC_API_KEY="k", **OIDC)
        self.assertEqual(error.code, "license_with_provider_key")
        self.assertIn("anthropic_api_key", error.message)
        self.assertIn("llm: anthropic", error.message)

    def test_hosted_refuses_a_licence_it_would_not_spend(self) -> None:
        error = self.refuse(CB_IN_LLM="hosted", CB_IN_LICENSE_KEY="lic", **OIDC)
        self.assertEqual(error.code, "hosted_with_license")
        self.assertIn("llm: license", error.message)

    def test_license_without_a_licence_key_is_refused(self) -> None:
        error = self.refuse(CB_IN_LLM="license", **OIDC)
        self.assertEqual(error.code, "missing_license_key")
        self.assertIn("CODEBOARDING_LICENSE", error.message)

    def test_hosted_tiers_require_both_oidc_variables(self) -> None:
        """The relay refuses to start without either, and a runner can expose one alone.

        Checking only the URL let that through preflight and turned a detectable
        configuration error into a generic failure after the engine install.
        """
        for extra in ({}, {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token"}):
            with self.subTest(present=sorted(extra)):
                error = self.refuse(CB_IN_LLM="hosted", **extra)
                self.assertEqual(error.code, "missing_id_token")
        # Both present is the only accepted shape.
        plan = self.resolve(
            CB_IN_LLM="hosted",
            ACTIONS_ID_TOKEN_REQUEST_URL="https://oidc.example/token",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN="request-token",
        )
        self.assertEqual(plan["tier"], "hosted")

    def test_a_hosted_run_does_not_name_the_upstream_it_is_routed_to(self) -> None:
        """Which provider sits behind CodeBoarding's proxy is our routing decision.

        Reporting it invites "why does my plan say openrouter?", and implies a commitment
        we have not made: the proxy's upstream can change without notice. The plan keeps
        it, because the analysis still has to be pointed somewhere; only the reporting
        withholds it.
        """
        for environ in (
            {"CB_IN_LLM": "hosted", **OIDC},
            {"CB_IN_LLM": "license", "CB_IN_LICENSE_KEY": "lic", **OIDC},
        ):
            plan = self.resolve(**environ)
            with self.subTest(tier=plan["tier"]):
                self.assertEqual(credential_check.reported_provider(plan), "")
                rendered = dict(credential_check.plan_summary(self.table, plan))
                self.assertNotIn("Provider", rendered)
                self.assertNotIn("openrouter", credential_check.plan_headline(self.table, plan))
                # Still resolved internally: the run has to be pointed somewhere.
                self.assertEqual(plan["provider"], "openrouter")

    def test_your_own_provider_is_always_named(self) -> None:
        """It is your configuration, and it is the thing you would check first."""
        plan = self.resolve(CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="k")
        self.assertEqual(credential_check.reported_provider(plan), "anthropic")
        self.assertEqual(dict(credential_check.plan_summary(self.table, plan))["Provider"], "`anthropic`")

    def test_an_endpoint_only_run_is_not_described_as_using_a_key(self) -> None:
        """`llm: openai` with only a base URL resolves no key, and with-auth.sh strips any
        inherited one, so naming a key would name a credential that is not there."""
        table = self.table
        keyless = self.resolve(CB_IN_LLM="openai", CB_IN_OPENAI_BASE_URL="https://gw.example/v1")
        self.assertIn("no API key", credential_check.plan_headline(table, keyless))
        self.assertNotIn("key, called directly", credential_check.plan_headline(table, keyless))

        keyed = self.resolve(CB_IN_LLM="openai", CB_IN_OPENAI_API_KEY="sk-x")
        self.assertIn("your own OpenAI key, called directly", credential_check.plan_headline(table, keyed))

    def test_the_headline_and_the_summary_cannot_disagree(self) -> None:
        """Both render from the same phrase; they were separately written and drifted."""
        plan = self.resolve(CB_IN_LLM="ollama", CB_IN_OLLAMA_BASE_URL="http://localhost:11434")
        rows = dict(credential_check.plan_summary(self.table, plan))
        self.assertIn(rows["Credentials"], credential_check.plan_headline(self.table, plan))

    def test_hosted_tiers_require_the_oidc_permission(self) -> None:
        for value, extra in (("hosted", {}), ("license", {"CB_IN_LICENSE_KEY": "lic"})):
            with self.subTest(llm=value):
                error = self.refuse(CB_IN_LLM=value, **extra)
                self.assertEqual(error.code, "missing_id_token")
                self.assertIn("id-token: write", error.message)

    def test_an_unknown_provider_lists_the_ones_that_exist(self) -> None:
        error = self.refuse(CB_IN_LLM="claude")
        self.assertEqual(error.code, "unknown_llm")
        self.assertIn("anthropic", error.message)

    def test_a_remedy_links_the_secrets_page_and_shows_the_line_to_add(self) -> None:
        """ "Add the secret and wire it" is a description of the fix, not the fix.

        The reader is someone who has never edited a workflow. They get the page to click
        and the exact YAML, in the file it belongs in, indented as it will sit there.
        """
        runner = {
            "GITHUB_REPOSITORY": "acme/widgets",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_WORKFLOW_REF": "acme/widgets/.github/workflows/codeboarding.yml@refs/pull/7/merge",
        }
        error = self.refuse(CB_IN_LLM="anthropic", **runner)
        self.assertIn("https://github.com/acme/widgets/settings/secrets/actions/new", error.details)
        self.assertIn(".github/workflows/codeboarding.yml", error.details)
        self.assertIn("```yaml", error.details)
        self.assertIn("anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}", error.details)
        self.assertIn("llm: anthropic", error.details)

    def test_a_remedy_degrades_to_prose_off_a_runner(self) -> None:
        """No GITHUB_REPOSITORY means no link to build, so say it without one."""
        error = self.refuse(CB_IN_LLM="anthropic")
        self.assertNotIn("](", error.details, "a half-built link is worse than none")
        self.assertIn("Add a repository secret", error.details)
        self.assertIn("your CodeBoarding workflow", error.details)

    def test_the_licence_and_permission_remedies_are_copyable_too(self) -> None:
        licence = self.refuse(CB_IN_LLM="license", GITHUB_REPOSITORY="acme/widgets", **OIDC)
        self.assertIn("secrets/actions/new", licence.details)
        self.assertIn("license_key: ${{ secrets.CODEBOARDING_LICENSE }}", licence.details)

        oidc = self.refuse(CB_IN_LLM="hosted")
        self.assertIn("permissions:", oidc.details)
        self.assertIn("id-token: write", oidc.details)
        self.assertIn("```yaml", oidc.details)

    def test_the_annotation_stays_one_line_however_rich_the_remedy(self) -> None:
        """`::error::` cannot carry newlines, so the two renderings must stay separate."""
        for environ in (
            {"CB_IN_LLM": "anthropic", "GITHUB_REPOSITORY": "acme/widgets"},
            {"CB_IN_LLM": "license", **OIDC},
            {"CB_IN_LLM": "hosted"},
            {},
        ):
            with self.subTest(environ=environ):
                error = self.refuse(**environ)
                self.assertNotIn("\n", error.message)

    def test_every_declared_refusal_is_exercised_by_this_file(self) -> None:
        """No refusal ships untested. Walks one configuration per code and asserts the set
        it produces is exactly the set the module declares it can raise, so adding a code
        without a case here fails rather than going unnoticed."""
        cases = [
            {},
            {"CB_IN_LLM": "not_a_provider"},
            {"CB_IN_LLM": "anthropic"},
            {"CB_IN_LLM": "license", **OIDC},
            {"CB_IN_LLM": "hosted"},
            {"CB_IN_LLM": "hosted", "CB_IN_ANTHROPIC_API_KEY": "k", **OIDC},
            {"CB_IN_LLM": "hosted", "CB_IN_LICENSE_KEY": "lic", **OIDC},
            {"CB_IN_LLM": "license", "CB_IN_LICENSE_KEY": "lic", "CB_IN_ANTHROPIC_API_KEY": "k", **OIDC},
            {"CB_IN_LLM": "anthropic", "CB_IN_ANTHROPIC_API_KEY": "k", "CB_IN_OPENAI_API_KEY": "o"},
        ]
        seen = {self.refuse(**case).code for case in cases}
        self.assertEqual(seen, set(credential_check.ERROR_CODES))

    def test_every_refusal_carries_a_code_and_an_actionable_message(self) -> None:
        cases = [
            {},
            {"CB_IN_LLM": "nope"},
            {"CB_IN_LLM": "anthropic"},
            {"CB_IN_LLM": "hosted", "CB_IN_LICENSE_KEY": "l", **OIDC},
            {"CB_IN_LLM": "license", **OIDC},
            {"CB_IN_LLM": "hosted"},
        ]
        for environ in cases:
            with self.subTest(environ=environ):
                error = self.refuse(**environ)
                self.assertTrue(error.code and error.code.islower())
                self.assertTrue(error.message.endswith("."))
                self.assertGreater(len(error.message), 40)


if __name__ == "__main__":
    unittest.main()
