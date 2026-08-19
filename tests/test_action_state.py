"""Tests for the stored-analysis reuse boundary owned by the action."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATE_NAMES = ROOT / "scripts" / "action" / "state-names.sh"
ANALYZE = ROOT / "scripts" / "action" / "analyze.sh"

ENGINE_STUB = '''#!/usr/bin/env python3
"""CodeBoarding CLI stand-in: records each call and writes a minimal analysis."""
import json, os, sys

argv = sys.argv[1:]
output = argv[argv.index("--output-dir") + 1]
os.makedirs(output, exist_ok=True)
with open(os.environ["CB_ENGINE_LOG"], "a") as log:
    log.write(json.dumps({
        "mode": argv[0],
        "checkout": argv[argv.index("--local") + 1],
        "depth": argv[argv.index("--depth-level") + 1] if "--depth-level" in argv else None,
    }) + "\\n")
analysis = os.path.join(output, "analysis.json")
with open(analysis, "w") as handle:
    json.dump({"metadata": {"depth_level": 2}, "components": [], "components_relations": []}, handle)
print(json.dumps({"requiresFullAnalysis": False, "analysis_path": analysis}))
'''


def _digest(path: Path) -> str:
    """Mirrors analysis_digest in analyze.sh: sha256 of the file, first 16 hex chars."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


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
            [str(STATE_NAMES)],
            env={
                "PATH": os.environ["PATH"],
                "GITHUB_OUTPUT": str(self.output),
                "CHECKOUT_DIR": str(self.checkout),
                "ENGINE_VERSION": "0.13.8",
                "MERGE_BASE_SHA": "mergebasesha",
                "PR_NUMBER": "42",
                "IS_FORK": "false",
                "LLM_PROVIDER": "openrouter",
                "MODEL": "",
                "AGENT_MODEL_INPUT": "",
                "PARSING_MODEL_INPUT": "",
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

    def test_names_pin_the_pull_request_and_the_merge_base(self) -> None:
        values = self._run()

        self.assertEqual(values["warmstart_name"], f"codeboarding-warmstart-{values['cfg_hash']}-pr42")
        self.assertEqual(values["base_name"], f"codeboarding-base-{values['cfg_hash']}-mergebasesha")

    def test_a_fork_never_gets_a_reusable_analysis(self) -> None:
        # Untrusted code must not shape a pickle a later run loads, and there is
        # no platform boundary here to lean on, so forks simply have no lineage.
        fork = self._run(IS_FORK="true")

        self.assertNotIn("warmstart_name", fork)
        self.assertIn("base_name", fork)

    def test_analysis_scope_and_engine_version_change_the_identity(self) -> None:
        baseline = self._run()
        self.assertEqual(baseline["cfg_hash"], self._run()["cfg_hash"])

        self.assertNotEqual(baseline["cfg_hash"], self._run(ENGINE_VERSION="0.14.0")["cfg_hash"])

        (self.checkout / ".codeboarding" / ".codeboardingignore").write_text("docs/\n", encoding="utf-8")
        self.assertNotEqual(baseline["cfg_hash"], self._run()["cfg_hash"])

    def test_model_selection_changes_the_identity(self) -> None:
        baseline = self._run()

        self.assertNotEqual(baseline["cfg_hash"], self._run(MODEL="gpt-5")["cfg_hash"])
        self.assertNotEqual(baseline["cfg_hash"], self._run(AGENT_MODEL_INPUT="gpt-5")["cfg_hash"])
        self.assertNotEqual(baseline["cfg_hash"], self._run(PARSING_MODEL_INPUT="gpt-5")["cfg_hash"])
        self.assertNotEqual(baseline["cfg_hash"], self._run(LLM_PROVIDER="anthropic")["cfg_hash"])

    def test_unresolvable_engine_version_disables_reuse_instead_of_failing(self) -> None:
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
        self.base_dir = self.root / "state" / "base"
        self.warmstart_dir = self.root / "state" / "warmstart"
        self.stage_dir = self.root / "state" / "out"

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
                "BASE_DIR": str(self.base_dir),
                "WARMSTART_DIR": str(self.warmstart_dir),
                "STAGE_DIR": str(self.stage_dir),
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

    def _bind(self, **origin: object) -> None:
        """Chain fixture bound to the base it was derived from."""
        _state(self.warmstart_dir, base_digest=_digest(self.base_dir / "analysis.json"), **origin)

    def test_a_stored_analysis_means_only_the_head_is_analyzed(self) -> None:
        _state(self.base_dir)
        self._bind(chain_depth=3, seed_source="pr-chain")

        values = self._analyze()

        self.assertEqual(values["seed_source"], "pr-chain")
        self.assertEqual(values["chain_depth"], "4")
        self.assertEqual(values["publish_base"], "false")
        calls = self._engine_calls()
        self.assertEqual(len(calls), 1, f"only the head should be analyzed, got {calls}")
        self.assertEqual(calls[0]["checkout"], str(self.checkout))

    def test_a_published_base_without_a_stored_head_seeds_from_the_base(self) -> None:
        _state(self.base_dir)

        values = self._analyze()

        self.assertEqual(values["seed_source"], "base")
        self.assertEqual(values["chain_depth"], "1")
        self.assertEqual(len(self._engine_calls()), 1)

    def test_refresh_ignores_the_stored_analysis(self) -> None:
        _state(self.base_dir)
        self._bind(chain_depth=3)

        values = self._analyze(SEED_MODE="refresh")

        self.assertEqual(values["seed_source"], "base")
        self.assertEqual(values["chain_depth"], "1")

    def test_depth_change_discards_the_stored_analysis(self) -> None:
        _state(self.base_dir, depth=2)
        _state(self.warmstart_dir, depth=1, chain_depth=3)

        values = self._analyze()

        self.assertEqual(values["seed_source"], "base")

    def test_a_run_that_stopped_short_of_its_cap_keeps_the_chain(self) -> None:
        # Core resolves incremental depth from depth_cap, so a realized
        # depth_level below the cap is not a scope change.
        _state(self.base_dir, depth=2, cap=2)
        _state(self.warmstart_dir, depth=1, cap=2, chain_depth=3, base_digest=_digest(self.base_dir / "analysis.json"))

        values = self._analyze()

        self.assertEqual(values["seed_source"], "pr-chain")

    def test_a_stored_analysis_from_a_different_base_is_discarded(self) -> None:
        # Two runs of the engine over the same commit need not name components
        # identically, so diffing a head grown from one against the other would
        # report changes nobody made.
        _state(self.base_dir)
        _state(self.warmstart_dir, chain_depth=3, base_digest="0000000000000000")

        values = self._analyze()

        self.assertEqual(values["seed_source"], "base")

    def test_a_stored_analysis_with_no_recorded_base_is_discarded(self) -> None:
        _state(self.base_dir)
        _state(self.warmstart_dir, chain_depth=3)

        values = self._analyze()

        self.assertEqual(values["seed_source"], "base")

    def test_a_forced_full_rebuilds_at_the_configured_cap(self) -> None:
        # The baseline stopped short of its cap. Rebuilding at the realized
        # depth would ratchet the configured depth down for good.
        _state(self.base_dir, depth=1, cap=2)

        self._analyze(SEED_MODE="full")

        calls = self._engine_calls()
        self.assertEqual(calls[-1]["mode"], "full")
        self.assertEqual(calls[-1]["depth"], "2")

    def test_analysis_is_staged_for_publication(self) -> None:
        _state(self.base_dir)
        self._bind()

        self._analyze()

        staged = self.stage_dir / "warmstart"
        self.assertTrue((staged / "analysis.json").is_file())
        origin = json.loads((staged / "origin.json").read_text(encoding="utf-8"))
        self.assertEqual(origin["merge_base_sha"], "merge-base-sha")
        self.assertEqual(origin["head_sha"], "head-sha")
        self.assertEqual(origin["engine_version"], "0.13.8")
        self.assertEqual(origin["base_digest"], _digest(self.base_dir / "analysis.json"))
        self.assertFalse((self.stage_dir / "base").exists(), "a published base needs no republishing")


class ReviewArtifactTests(unittest.TestCase):
    """The artifact is the only channel a reader outside the run can use: cache
    entries have no download API, so whatever the webview needs must ship here."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.head = self.root / "head.json"
        self.head.write_text('{"components": ["head"]}', encoding="utf-8")
        self.base = self.root / "base.json"
        self.base.write_text('{"components": ["base"]}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _build(self) -> subprocess.CompletedProcess:
        output = self.root / "github-output"
        result = subprocess.run(
            [str(ROOT / "scripts" / "action" / "build-review-artifact.sh")],
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(self.root),
                "GITHUB_OUTPUT": str(output),
                "ANALYSIS_PATH": str(self.head),
                "BASE_ARTIFACT_NAME": "codeboarding-base-cfg-mergebasesha",
                "ANALYSIS_MODE": "incremental",
                "BASE_SHA": "tip-sha",
                "MERGE_BASE_SHA": "merge-base-sha",
                "MERGE_BASE_RESOLVED": "true",
                "HEAD_SHA": "head-sha",
                "PR_NUMBER": "81",
                "SEED_SOURCE": "pr-chain",
                "CHAIN_DEPTH": "2",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def test_it_ships_both_graphs_and_the_commit_they_describe(self) -> None:
        self._build()

        artifact = self.root / "cb-review-artifact"
        self.assertEqual(json.loads((artifact / "analysis.json").read_text())["components"], ["head"])
        # The base graph is published separately, so the artifact names it
        # rather than carrying a copy per run.
        self.assertFalse((artifact / "base_analysis.json").exists())

        metadata = json.loads((artifact / "metadata.json").read_text(encoding="utf-8"))
        # base_sha stays the event tip for consumers keyed on it; the merge base
        # is what base_analysis.json actually describes.
        self.assertEqual(metadata["base_sha"], "tip-sha")
        self.assertEqual(metadata["merge_base_sha"], "merge-base-sha")
        self.assertEqual(metadata["base_artifact"], "codeboarding-base-cfg-mergebasesha")
        # The webview resolves base_commit_sha || pr_base_sha || base_sha, so the
        # merge base has to appear under a name it looks for or it silently uses
        # the branch tip.
        self.assertEqual(metadata["pr_base_sha"], "merge-base-sha")
        self.assertEqual(metadata["merge_base_resolved"], "true")
        self.assertEqual(metadata["seed_source"], "pr-chain")


class ReviewHealthArtifactTests(ReviewArtifactTests):
    """The engine writes a health report beside every analysis it produces."""

    def test_it_ships_the_health_report_when_the_engine_wrote_one(self) -> None:
        (self.root / "health").mkdir()
        (self.root / "health" / "health_report.json").write_text('{"overall_score": 0.9}', encoding="utf-8")

        self._build()

        report = self.root / "cb-review-artifact" / "health_report.json"
        self.assertEqual(report.read_text(encoding="utf-8"), '{"overall_score": 0.9}')

    def test_it_still_builds_when_no_health_report_was_written(self) -> None:
        self._build()

        artifact = self.root / "cb-review-artifact"
        self.assertTrue((artifact / "analysis.json").is_file())
        self.assertFalse((artifact / "health_report.json").exists())


class PublishedStateTests(unittest.TestCase):
    """Static checks on action.yml: what gets published, from where, by whom."""

    def _steps(self) -> list[dict[str, str]]:
        steps: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        for line in (ROOT / "action.yml").read_text(encoding="utf-8").splitlines():
            if line.startswith("    - name:"):
                current = {"name": line.split(":", 1)[1].strip()}
                steps.append(current)
            elif current is not None:
                stripped = line.strip()
                for field in ("uses", "path", "name", "if", "retention-days"):
                    if stripped.startswith(f"{field}:") and field not in ("name",):
                        current[field] = stripped.split(":", 1)[1].strip()
        return steps

    def _uploads(self) -> list[dict[str, str]]:
        return [s for s in self._steps() if s.get("uses", "").startswith("actions/upload-artifact")]

    def test_state_is_published_from_the_directory_the_analysis_stages(self) -> None:
        # analyze.sh writes STAGE_DIR/warmstart and STAGE_DIR/base. Publishing
        # from anywhere else uploads nothing and reports success.
        staged = {"warmstart", "base"}
        for step in self._uploads():
            path = step["path"]
            if "cb-state/out" not in path:
                continue
            self.assertIn(path.rsplit("/", 1)[-1], staged, f"{step['name']} publishes an unstaged path")

    def test_a_fork_publishes_nothing_another_run_would_read(self) -> None:
        # A base graph is named for a commit, so every pull request forking there
        # reads it; a fork's analysis must never be what they read.
        base_publish = [
            s
            for s in self._uploads()
            if "outputs.base_name" in s.get("path", "") + s.get("name", "") or "base_name" in s.get("if", "")
        ]
        published_by_review = [s for s in self._uploads() if "review_analyze.outputs.publish_base" in s.get("if", "")]
        self.assertTrue(published_by_review, "no base publication step found")
        for step in published_by_review:
            self.assertIn("is_fork != 'true'", step["if"], f"{step['name']} would let a fork publish a shared base")

    def test_the_reusable_analysis_honours_the_configured_retention(self) -> None:
        warmstart = [s for s in self._uploads() if "warmstart" in s.get("path", "")]
        self.assertTrue(warmstart, "no warm-start publication step found")
        for step in warmstart:
            self.assertEqual(step.get("retention-days"), "${{ inputs.warmstart_retention_days }}")


if __name__ == "__main__":
    unittest.main()
