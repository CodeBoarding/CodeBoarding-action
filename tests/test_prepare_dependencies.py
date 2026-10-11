"""The dependency step turns Core's install on, except for a fork, and keys the cache on a lockfile."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "action" / "prepare-dependencies.sh"


def prepare(files: dict[str, str], install: str = "true", is_fork: str = "false") -> tuple[dict, dict]:
    """Runs the step on a checkout holding *files*; returns its outputs and its exported env."""
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / "checkout"
        checkout.mkdir()
        for name, text in files.items():
            (checkout / name).write_text(text, encoding="utf-8")
        output, env_file = Path(tmp) / "output", Path(tmp) / "env"
        output.write_text("", encoding="utf-8")
        env_file.write_text("", encoding="utf-8")
        subprocess.run(
            [str(SCRIPT)],
            env={
                "PATH": os.environ["PATH"],
                "GITHUB_OUTPUT": str(output),
                "GITHUB_ENV": str(env_file),
                "CHECKOUT_DIR": str(checkout),
                "STORE_DIR": str(Path(tmp) / "store"),
                "INSTALL_DEPENDENCIES": install,
                "IS_FORK": is_fork,
            },
            check=True,
            capture_output=True,
        )
        outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        exported = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines())
        return outputs, exported


class PrepareDependenciesTests(unittest.TestCase):
    def test_core_is_asked_to_install_and_every_cache_points_at_the_store(self) -> None:
        outputs, env = prepare({"package.json": "{}", "pnpm-lock.yaml": ""})
        self.assertEqual(outputs, {"lockfile": "true"})
        self.assertEqual(env["CODEBOARDING_INSTALL_DEPENDENCIES"], "1")
        store = env["npm_config_cache"].rsplit("/", 1)[0]
        for variable in ("npm_config_store_dir", "BUN_INSTALL_CACHE_DIR", "YARN_CACHE_FOLDER", "YARN_GLOBAL_FOLDER"):
            self.assertTrue(env[variable].startswith(store), variable)

    def test_without_a_lockfile_core_still_checks_the_merge_base(self) -> None:
        outputs, env = prepare({"main.py": ""})
        self.assertEqual(outputs, {})
        self.assertEqual(env["CODEBOARDING_INSTALL_DEPENDENCIES"], "1")

    def test_a_forks_pull_request_installs_nothing(self) -> None:
        self.assertEqual(prepare({"bun.lock": ""}, is_fork="true"), ({}, {}))

    def test_turning_it_off_installs_nothing(self) -> None:
        self.assertEqual(prepare({"bun.lock": ""}, install="false"), ({}, {}))


class ActionWiringTests(unittest.TestCase):
    """The steps around the script: the store is restored for every run and saved only by sync."""

    def setUp(self) -> None:
        self.action = (ROOT / "action.yml").read_text(encoding="utf-8")

    def _step(self, name: str) -> str:
        return self.action.split(f"- name: {name}", 1)[1].split("- name:", 1)[0]

    def test_the_input_defaults_to_installing(self) -> None:
        block = re.search(r"^  install_dependencies:\n((?:    .*\n)+)", self.action, re.MULTILINE)
        assert block is not None
        self.assertIn("default: 'true'", block.group(1))

    def test_the_step_knows_a_fork(self) -> None:
        self.assertIn("IS_FORK: ${{ steps.guard.outputs.is_fork }}", self._step("Prepare dependency install"))

    def test_only_sync_saves_the_store(self) -> None:
        save = self._step("Save dependency cache")
        self.assertIn("steps.guard.outputs.mode == 'sync'", save)
        self.assertIn("actions/cache/save@", save)

    def test_every_lockfile_keys_the_cache(self) -> None:
        key = self._step("Restore dependency cache")
        for pattern in ("*.lock", "*.lockb", "*-lock.yaml", "*-lock.json", "npm-shrinkwrap.json"):
            self.assertIn(f"'.codeboarding-target/{pattern}'", key)

    def test_an_install_is_part_of_the_analysis_identity(self) -> None:
        """A fork's review installs nothing, so it must not reuse a base analyzed with an install."""
        self.assertIn(
            "INSTALL_DEPENDENCIES: ${{ steps.deps.outputs.lockfile == 'true' }}",
            self._step("Resolve analysis cache keys"),
        )

    def test_the_store_is_ready_before_the_engine_runs(self) -> None:
        for step in ("Generate sync baseline", "Generate review baseline"):
            self.assertLess(self.action.index("- name: Restore dependency cache"), self.action.index(f"- name: {step}"))


if __name__ == "__main__":
    unittest.main()
