"""Tests for the cached-analysis reuse boundary owned by the action."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CACHE_KEYS = ROOT / "scripts" / "action" / "cache-keys.sh"
ANALYZE = ROOT / "scripts" / "action" / "analyze.sh"

ENGINE_STUB = '''#!/usr/bin/env python3
"""CodeBoarding CLI stand-in: records each call and writes a minimal analysis."""
import json, os, sys

argv = sys.argv[1:]
output = argv[argv.index("--output-dir") + 1]
os.makedirs(output, exist_ok=True)
with open(os.environ["CB_ENGINE_LOG"], "a") as log:
    log.write(json.dumps({"mode": argv[0], "checkout": argv[argv.index("--local") + 1]}) + "\\n")
analysis = os.path.join(output, "analysis.json")
with open(analysis, "w") as handle:
    json.dump({"metadata": {"depth_level": 2}, "components": [], "components_relations": []}, handle)
print(json.dumps({"requiresFullAnalysis": False, "analysis_path": analysis}))
'''


def _state(directory: Path, depth: int = 2, cap: int | None = None, **origin: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, int] = {"depth_level": depth}
    if cap is not None:
        metadata["depth_cap"] = cap
    (directory / "analysis.json").write_text(
        json.dumps({"metadata": metadata, "components": [], "components_relations": []}),
        encoding="utf-8",
    )
    (directory / "static_analysis.pkl").write_text("pickle", encoding="utf-8")
    if origin:
        (directory / "origin.json").write_text(json.dumps(origin), encoding="utf-8")
    return directory


class CacheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.checkout = self.root / "checkout"
        (self.checkout / ".codeboarding").mkdir(parents=True)
        self.output = self.root / "github-output"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, **extra: str) -> dict[str, str]:
        self.output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(CACHE_KEYS)],
            env={
                "PATH": os.environ["PATH"],
                "GITHUB_OUTPUT": str(self.output),
                "CHECKOUT_DIR": str(self.checkout),
                "ENGINE_VERSION": "0.13.8",
                "MERGE_BASE_SHA": "mergebasesha",
                "PR_NUMBER": "42",
                "HEAD_SHA": "head-sha",
                "HEAD_REPO": "owner/repo",
                "IS_FORK": "false",
                **extra,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        values: dict[str, str] = {}
        for line in self.output.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return values

    def test_chain_key_pins_the_pull_request_and_its_merge_base(self) -> None:
        values = self._run()

        self.assertTrue(values["chain_key"].startswith("cb-head-v1-"))
        self.assertIn("-pr42-mbmergebasesha-", values["chain_key"])
        self.assertTrue(values["chain_key"].endswith("head-sha"))
        self.assertEqual(values["chain_key"][: -len("head-sha")], values["chain_restore_keys"])
        self.assertEqual(values["base_key"], values["base_key_prefix"] + "mergebasesha")

    def test_fork_state_cannot_be_restored_by_a_trusted_run(self) -> None:
        trusted = self._run()
        fork = self._run(IS_FORK="true", HEAD_REPO="contributor/repo")

        self.assertTrue(fork["chain_key"].startswith("cb-fork-v1-"))
        self.assertIn("contributor-repo", fork["chain_key"])
        self.assertFalse(fork["chain_key"].startswith(trusted["chain_restore_keys"]))
        self.assertFalse(trusted["chain_key"].startswith(fork["chain_restore_keys"]))

    def test_analysis_scope_and_engine_version_change_the_identity(self) -> None:
        baseline = self._run()
        self.assertEqual(baseline["cfg_hash"], self._run()["cfg_hash"])

        self.assertNotEqual(baseline["cfg_hash"], self._run(ENGINE_VERSION="0.14.0")["cfg_hash"])

        (self.checkout / ".codeboarding" / ".codeboardingignore").write_text("docs/\n", encoding="utf-8")
        self.assertNotEqual(baseline["cfg_hash"], self._run()["cfg_hash"])

    def test_unresolvable_engine_version_disables_caching_instead_of_failing(self) -> None:
        stub_bin = self.root / "bin"
        stub_bin.mkdir()
        python_stub = stub_bin / "python3"
        python_stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        python_stub.chmod(0o755)

        values = self._run(ENGINE_VERSION="", PATH=f"{stub_bin}:{os.environ['PATH']}")

        self.assertEqual(values, {})


class ReviewChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        stub = self.bin_dir / "codeboarding"
        stub.write_text(ENGINE_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.engine_log = self.root / "engine.log"
        self.engine_log.write_text("", encoding="utf-8")
        self.output = self.root / "github-output"
        self.runner_temp = self.root / "runner"
        self.runner_temp.mkdir()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.cache_base = self.root / "cache" / "base"
        self.cache_chain = self.root / "cache" / "chain"
        self.cache_out = self.root / "cache" / "out"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _analyze(self, **extra: str) -> dict[str, str]:
        self.output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(ANALYZE)],
            env={
                "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
                "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.runner_temp),
                "CB_ENGINE_LOG": str(self.engine_log),
                "ACTION_PATH": str(ROOT),
                "ANALYSIS_KIND": "review",
                "CHECKOUT_DIR": str(self.checkout),
                "REVIEW_BASE_SHA": "merge-base-sha",
                "REVIEW_HEAD_SHA": "head-sha",
                "REVIEW_BASE_REPO": "owner/repo",
                "GITHUB_SERVER_URL": "https://github.com",
                "PR_NUMBER": "42",
                "ENGINE_VERSION": "0.13.8",
                "CFG_HASH": "cfg",
                "SEED_MODE": "chain",
                "CACHE_BASE_DIR": str(self.cache_base),
                "CACHE_BASE_HIT": "true",
                "CACHE_CHAIN_DIR": str(self.cache_chain),
                "CACHE_OUT_DIR": str(self.cache_out),
                **extra,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        values: dict[str, str] = {}
        for line in self.output.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return values

    def _engine_calls(self) -> list[dict[str, str]]:
        return [json.loads(line) for line in self.engine_log.read_text(encoding="utf-8").splitlines()]

    def test_warm_chain_analyzes_only_the_head(self) -> None:
        _state(self.cache_base)
        _state(self.cache_chain, chain_depth=3, seed_source="pr-chain")

        values = self._analyze()

        self.assertEqual(values["seed_source"], "pr-chain")
        self.assertEqual(values["chain_depth"], "4")
        self.assertEqual(values["save_base"], "false")
        calls = self._engine_calls()
        self.assertEqual(len(calls), 1, f"only the head should be analyzed, got {calls}")
        self.assertEqual(calls[0]["checkout"], str(self.checkout))

    def test_cached_base_without_a_chain_seeds_from_the_base(self) -> None:
        _state(self.cache_base)

        values = self._analyze()

        self.assertEqual(values["seed_source"], "base")
        self.assertEqual(values["chain_depth"], "1")
        self.assertEqual(len(self._engine_calls()), 1)

    def test_refresh_ignores_the_pull_request_chain(self) -> None:
        _state(self.cache_base)
        _state(self.cache_chain, chain_depth=3)

        values = self._analyze(SEED_MODE="refresh")

        self.assertEqual(values["seed_source"], "base")
        self.assertEqual(values["chain_depth"], "1")

    def test_depth_change_discards_the_chain(self) -> None:
        _state(self.cache_base, depth=2)
        _state(self.cache_chain, depth=1, chain_depth=3)

        values = self._analyze()

        self.assertEqual(values["seed_source"], "base")

    def test_a_run_that_stopped_short_of_its_cap_keeps_the_chain(self) -> None:
        # Core resolves incremental depth from depth_cap, so a realized
        # depth_level below the cap is not a scope change.
        _state(self.cache_base, depth=2, cap=2)
        _state(self.cache_chain, depth=1, cap=2, chain_depth=3)

        values = self._analyze()

        self.assertEqual(values["seed_source"], "pr-chain")

    def test_analysis_is_staged_for_the_cache(self) -> None:
        _state(self.cache_base)
        _state(self.cache_chain)

        self._analyze()

        staged = self.cache_out / "chain"
        self.assertTrue((staged / "analysis.json").is_file())
        origin = json.loads((staged / "origin.json").read_text(encoding="utf-8"))
        self.assertEqual(origin["merge_base_sha"], "merge-base-sha")
        self.assertEqual(origin["head_sha"], "head-sha")
        self.assertEqual(origin["engine_version"], "0.13.8")
        self.assertFalse((self.cache_out / "base").exists(), "a cached base needs no re-save")


if __name__ == "__main__":
    unittest.main()
