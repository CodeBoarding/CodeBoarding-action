"""Tests for the composite action's credential boundary, as the runner exercises it."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = ROOT / "scripts" / "action" / "preflight-llm.sh"
CONFIGURE_AUTH = ROOT / "scripts" / "action" / "configure-auth.sh"
WITH_AUTH = ROOT / "scripts" / "action" / "with-auth.sh"


class ActionAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _preflight(self, **inputs: str) -> tuple[subprocess.CompletedProcess, Path, dict[str, str]]:
        temp_dir = Path(self.temp_dir.name)
        runner_temp = temp_dir / "runner"
        runner_temp.mkdir(exist_ok=True)
        output = temp_dir / "github-output"
        output.write_text("", encoding="utf-8")
        env = {
            "PATH": os.environ["PATH"],
            "ACTION_PATH": str(ROOT),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(temp_dir / "summary.md"),
            "RUNNER_TEMP": str(runner_temp),
            **inputs,
        }
        result = subprocess.run([str(PREFLIGHT)], env=env, capture_output=True, text=True, check=False)
        return result, runner_temp / "codeboarding-auth", self._outputs(output)

    @staticmethod
    def _outputs(path: Path) -> dict[str, str]:
        """Parse the runner's key=value and key<<HEREDOC output file."""
        values: dict[str, str] = {}
        lines = path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            key, _, value = lines[index].partition("=")
            if value == "" and "<<" in lines[index]:
                key, _, delimiter = lines[index].partition("<<")
                body: list[str] = []
                index += 1
                while index < len(lines) and lines[index] != delimiter:
                    body.append(lines[index])
                    index += 1
                values[key] = "\n".join(body)
            else:
                values[key] = value
            index += 1
        return values

    def _with_auth(self, script: str, **extra_env: str) -> subprocess.CompletedProcess:
        runner_temp = Path(self.temp_dir.name) / "runner"
        env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner_temp), **extra_env}
        return subprocess.run(
            [str(WITH_AUTH), "bash", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    # -- the guarantee -----------------------------------------------------

    def test_a_named_provider_never_falls_back_to_codeboarding_credentials(self) -> None:
        """PROTECTED TEST -- a workflow that names a provider runs on that provider or
        not at all.

        This action used to treat an empty key as "no preference" and resolve it to
        CodeBoarding's hosted OpenRouter tier. A repository that asked for Anthropic and
        had not added its secret yet therefore went green while running on a different
        vendor, a different model, and CodeBoarding's money -- and nothing in the run
        said so. Falling back is never the right answer to an unanswered question here:
        the workflow named a provider, so the only honest outcomes are that provider or
        a failure that says what is missing.
        """
        result, auth_dir, outputs = self._preflight(
            CB_IN_LLM="anthropic",
            CB_IN_ANTHROPIC_API_KEY="",
            ACTIONS_ID_TOKEN_REQUEST_URL="https://oidc.example/token",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN="request-token",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs["error"], "missing_provider_key")
        self.assertFalse(auth_dir.exists(), "credentials were staged for a refused run")
        self.assertNotIn("openrouter", result.stdout.lower())
        self.assertNotIn("OPENROUTER_API_KEY", result.stdout)

        # And the analysis cannot proceed on whatever the environment happened to hold.
        scoped = self._with_auth("true", OPENROUTER_API_KEY="inherited")
        self.assertNotEqual(scoped.returncode, 0)

    # -- resolution --------------------------------------------------------

    def test_provider_key_is_staged_privately_and_exported_for_analysis(self) -> None:
        result, auth_dir, outputs = self._preflight(CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="fake=key")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(outputs["tier"], "byok")
        self.assertEqual(outputs["provider"], "anthropic")
        self.assertEqual(outputs["error"], "")

        key_file = auth_dir / "env" / "ANTHROPIC_API_KEY"
        self.assertEqual(key_file.read_text(encoding="utf-8"), "fake=key")
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode) & 0o077, 0)
        self.assertIn("::add-mask::fake=key", result.stdout)

        scoped = self._with_auth(
            'test "$ANTHROPIC_API_KEY" = "fake=key" && test "$CODEBOARDING_SOURCE" = github_action'
        )
        self.assertEqual(scoped.returncode, 0, scoped.stderr or scoped.stdout)
        self.assertFalse(auth_dir.exists(), "credentials outlived the analysis command")

    def test_selectors_for_other_providers_are_stripped_from_the_analysis(self) -> None:
        """Core picks a provider from its environment, so an inherited key must not vote."""
        result, _, _ = self._preflight(CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="k")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        scoped = self._with_auth(
            'test -z "${OPENAI_API_KEY:-}" && test -z "${OPENAI_BASE_URL:-}" && '
            'test -z "${OPENROUTER_API_KEY:-}" && test -z "${LITELLM_BASE_URL:-}" && '
            'test -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}"',
            OPENAI_API_KEY="inherited",
            OPENAI_BASE_URL="https://inherited.example",
            OPENROUTER_API_KEY="inherited",
            LITELLM_BASE_URL="https://inherited.example",
            ACTIONS_ID_TOKEN_REQUEST_URL="https://oidc.example/token",
        )
        self.assertEqual(scoped.returncode, 0, scoped.stderr or scoped.stdout)

    def test_an_inherited_key_cannot_supply_a_provider_selected_by_its_endpoint(self) -> None:
        """The same substitution as the protected test, one provider narrower.

        `llm: openai` with only an endpoint is a legitimate keyless configuration. Sparing
        every variable OpenAI *could* use, rather than the ones this run actually resolved,
        would leave a stray OPENAI_API_KEY from the job environment to credential the run.
        """
        result, auth_dir, _ = self._preflight(CB_IN_LLM="openai", CB_IN_OPENAI_BASE_URL="https://proxy.example/v1")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("OPENAI_API_KEY", (auth_dir / "foreign-envs").read_text(encoding="utf-8"))

        scoped = self._with_auth(
            'test -z "${OPENAI_API_KEY:-}" && test "$OPENAI_BASE_URL" = https://proxy.example/v1',
            OPENAI_API_KEY="inherited",
        )
        self.assertEqual(scoped.returncode, 0, scoped.stderr or scoped.stdout)

    def test_a_provider_input_left_empty_is_stripped_not_inherited(self) -> None:
        """`aws_region` unset means the engine's default, never whatever the job exported."""
        result, auth_dir, _ = self._preflight(CB_IN_LLM="aws", CB_IN_AWS_API_KEY="k")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("AWS_DEFAULT_REGION", (auth_dir / "foreign-envs").read_text(encoding="utf-8"))

    def test_model_inputs_keep_their_precedence(self) -> None:
        self._preflight(CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="k")
        scoped = self._with_auth(
            'test "$AGENT_MODEL" = analysis-model && test "$PARSING_MODEL" = shared-model',
            MODEL="shared-model",
            AGENT_MODEL_INPUT="analysis-model",
            PARSING_MODEL_INPUT="",
        )
        self.assertEqual(scoped.returncode, 0, scoped.stderr or scoped.stdout)

    def test_refusal_reports_a_code_and_an_actionable_message(self) -> None:
        result, _, outputs = self._preflight(CB_IN_LLM="hosted", CB_IN_OPENAI_API_KEY="k")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs["error"], "hosted_with_provider_key")
        self.assertIn("openai_api_key", outputs["message"])
        self.assertIn("::error title=CodeBoarding LLM configuration::", result.stdout)
        summary = (Path(self.temp_dir.name) / "summary.md").read_text(encoding="utf-8")
        self.assertIn("CodeBoarding could not start", summary)

    def test_successful_run_reports_its_tier_and_provider(self) -> None:
        """The webview reads this to show what a repository is actually running on."""
        result, _, outputs = self._preflight(
            CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="k", CB_IN_LICENSE_KEY="lic"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(outputs["tier"], "byok+license")
        summary = (Path(self.temp_dir.name) / "summary.md").read_text(encoding="utf-8")
        self.assertIn("byok+license", summary)
        self.assertIn("anthropic", summary)

    # -- hosted tiers ------------------------------------------------------

    def test_hosted_auth_relays_to_the_codeboarding_proxy(self) -> None:
        temp_dir = Path(self.temp_dir.name)
        fake_bin = temp_dir / "bin"
        fake_bin.mkdir()
        captured_args = temp_dir / "relay-args"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$CAPTURED_ARGS"
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--ready-file" ]; then
    printf '12345' > "$2"
    break
  fi
  shift
done
""",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

        result, auth_dir, outputs = self._preflight(
            CB_IN_LLM="license",
            CB_IN_LICENSE_KEY="a-license",
            ACTIONS_ID_TOKEN_REQUEST_URL="https://oidc.example/token",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN="request-token",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(outputs["tier"], "license")
        self.assertIn("::add-mask::a-license", result.stdout)

        configured = subprocess.run(
            [str(CONFIGURE_AUTH)],
            env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "ACTION_PATH": str(ROOT),
                "RUNNER_TEMP": str(temp_dir / "runner"),
                "CAPTURED_ARGS": str(captured_args),
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(configured.returncode, 0, configured.stderr or configured.stdout)
        args = captured_args.read_text(encoding="utf-8").splitlines()
        upstream = args[args.index("--upstream-base-url") + 1]
        self.assertEqual(upstream, "https://auduihjmm4b735zci7vyabuikq0hppqn.lambda-url.us-east-1.on.aws")
        self.assertIn("--license-file", args)
        self.assertEqual((auth_dir / "env" / "OPENROUTER_API_KEY").read_text(), "github-actions-oidc-relay")
        self.assertEqual((auth_dir / "env" / "OPENROUTER_BASE_URL").read_text(), "http://127.0.0.1:12345")

    def test_direct_provider_runs_start_no_relay(self) -> None:
        temp_dir = Path(self.temp_dir.name)
        fake_bin = temp_dir / "bin"
        fake_bin.mkdir()
        marker = temp_dir / "relay-started"
        (fake_bin / "python3").write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\n', encoding="utf-8")
        (fake_bin / "python3").chmod(0o755)

        self._preflight(CB_IN_LLM="anthropic", CB_IN_ANTHROPIC_API_KEY="k")
        configured = subprocess.run(
            [str(CONFIGURE_AUTH)],
            env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "ACTION_PATH": str(ROOT),
                "RUNNER_TEMP": str(temp_dir / "runner"),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(configured.returncode, 0, configured.stderr or configured.stdout)
        self.assertFalse(marker.exists(), "a direct-provider run contacted the hosted relay")

    def test_analysis_refuses_to_run_without_a_resolved_plan(self) -> None:
        scoped = self._with_auth("true")
        self.assertNotEqual(scoped.returncode, 0)
        self.assertIn("credentials are unavailable", scoped.stdout + scoped.stderr)


if __name__ == "__main__":
    unittest.main()
