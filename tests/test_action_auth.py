"""Tests for the composite action's provider credential boundary."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIGURE_AUTH = ROOT / "scripts" / "action" / "configure-auth.sh"
WITH_AUTH = ROOT / "scripts" / "action" / "with-auth.sh"


class ActionAuthTests(unittest.TestCase):
    def _configure(self, provider: str, key: str, **extra_env: str) -> tuple[subprocess.CompletedProcess, Path]:
        temp_dir = Path(self.temp_dir.name)
        runner_temp = temp_dir / "runner"
        runner_temp.mkdir()
        output = temp_dir / "output"
        github_env = temp_dir / "github-env"
        env = {
            "PATH": os.environ["PATH"],
            "ACTION_PATH": str(ROOT),
            "GITHUB_ENV": str(github_env),
            "GITHUB_OUTPUT": str(output),
            "LICENSE_KEY": "",
            "LLM_API_KEY": key,
            "LLM_PROVIDER": provider,
            "RUNNER_TEMP": str(runner_temp),
            **extra_env,
        }
        result = subprocess.run(
            [str(CONFIGURE_AUTH)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, runner_temp / "codeboarding-auth"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_maps_provider_keys_without_exporting_them_to_github_env(self) -> None:
        cases = {
            "openai": "OPENAI_API_KEY",
            "vercel": "VERCEL_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "aws": "AWS_BEARER_TOKEN_BEDROCK",
            "aws_bedrock": "AWS_BEARER_TOKEN_BEDROCK",
            "cerebras": "CEREBRAS_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "glm": "GLM_API_KEY",
            "kimi": "KIMI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        for provider, expected_env in cases.items():
            with self.subTest(provider=provider):
                self.temp_dir.cleanup()
                self.temp_dir = tempfile.TemporaryDirectory()
                result, auth_dir = self._configure(provider, "fake-key")
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual((auth_dir / "provider-env").read_text(), expected_env)
                self.assertEqual((auth_dir / "provider-key").read_text(), "fake-key")
                self.assertFalse((Path(self.temp_dir.name) / "github-env").exists())
                mode = stat.S_IMODE((auth_dir / "provider-key").stat().st_mode)
                self.assertEqual(mode & 0o077, 0)

    def test_maps_standard_custom_provider_names_without_an_action_allowlist(self) -> None:
        result, auth_dir = self._configure("acme-ai", "fake-key")

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual((auth_dir / "provider-env").read_text(), "ACME_AI_API_KEY")

    def test_ollama_and_litellm_keys_use_core_environment_names(self) -> None:
        for provider in ("ollama", "litellm"):
            with self.subTest(provider=provider):
                self.temp_dir.cleanup()
                self.temp_dir = tempfile.TemporaryDirectory()
                result, auth_dir = self._configure(provider, "fake-key")
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual((auth_dir / "provider-env").read_text(), f"{provider.upper()}_API_KEY")
                self.assertEqual((auth_dir / "provider-key").read_text(), "fake-key")

    def test_keyless_direct_provider_does_not_fall_back_to_hosted_openrouter(self) -> None:
        result, auth_dir = self._configure("ollama", "")

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual((auth_dir / "provider-env").read_text(), "OLLAMA_API_KEY")
        self.assertFalse((auth_dir / "provider-key").exists())

    def test_hosted_auth_relays_to_aws_proxy_instead_of_openrouter(self) -> None:
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

        result, auth_dir = self._configure(
            "openrouter",
            "",
            ACTIONS_ID_TOKEN_REQUEST_URL="https://oidc.example/token",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN="request-token",
            CAPTURED_ARGS=str(captured_args),
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        args = captured_args.read_text(encoding="utf-8").splitlines()
        upstream_index = args.index("--upstream-base-url") + 1
        self.assertEqual(
            args[upstream_index],
            "https://auduihjmm4b735zci7vyabuikq0hppqn.lambda-url.us-east-1.on.aws",
        )
        self.assertNotIn("https://openrouter.ai/api/v1", args)
        self.assertEqual((auth_dir / "provider-key").read_text(), "github-actions-oidc-relay")

    def test_with_auth_scopes_credentials_source_and_model_precedence(self) -> None:
        result, auth_dir = self._configure("anthropic", "fake=key")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        command = [
            str(WITH_AUTH),
            "bash",
            "-c",
            'test "$ANTHROPIC_API_KEY" = "fake=key" && '
            'test -z "${OPENAI_API_KEY:-}" && '
            'test -z "${OPENAI_BASE_URL:-}" && '
            'test -z "${OPENROUTER_API_KEY:-}" && '
            'test "$CODEBOARDING_SOURCE" = github_action && '
            'test "$AGENT_MODEL" = analysis-model && '
            'test "$PARSING_MODEL" = shared-model',
        ]
        env = {
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(auth_dir.parent),
            "MODEL": "shared-model",
            "AGENT_MODEL_INPUT": "analysis-model",
            "PARSING_MODEL_INPUT": "",
            "OPENAI_API_KEY": "inherited-key",
            "OPENAI_BASE_URL": "https://inherited.example",
        }
        scoped = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(scoped.returncode, 0, scoped.stderr or scoped.stdout)
        self.assertFalse(auth_dir.exists())

    def test_keyless_provider_requires_its_endpoint(self) -> None:
        result, auth_dir = self._configure("ollama", "")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        scoped = subprocess.run(
            [str(WITH_AUTH), "true"],
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(auth_dir.parent)},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(scoped.returncode, 0)
        self.assertIn("ollama requires OLLAMA_BASE_URL or OLLAMA_HOST", scoped.stderr)


if __name__ == "__main__":
    unittest.main()
