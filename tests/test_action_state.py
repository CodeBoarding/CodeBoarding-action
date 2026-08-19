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
    (directory / "static_analysis.lock").write_text("", encoding="utf-8")
    (directory / "logs").mkdir(exist_ok=True)
    (directory / "logs" / "run.log").write_text("noise\n", encoding="utf-8")
    (directory / "static_analysis.lock").write_text("", encoding="utf-8")
    (directory / "logs").mkdir(exist_ok=True)
    (directory / "logs" / "run.log").write_text("noise\n", encoding="utf-8")
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

    def test_each_bundle_says_what_it_is(self) -> None:
        # A base bundle is otherwise an analysis.json and nothing else, which
        # unpacks exactly like a head artifact and would be rendered as one.
        _state(self.base_dir)
        self._analyze()

        warmstart = json.loads((self.stage_dir / "warmstart" / "metadata.json").read_text())
        self.assertEqual(warmstart["kind"], "warmstart")
        self.assertEqual(warmstart["merge_base_sha"], "merge-base-sha")
        # A warm-start bundle belongs to one pull request; a base does not.
        self.assertEqual(warmstart["pr_number"], "42")

    def test_a_base_is_not_labelled_with_the_run_that_computed_it(self) -> None:
        # One base serves every pull request forking from that commit, so the
        # run that happened to build it is not part of what the bundle is.
        _state(self.base_dir)
        self._analyze(RENEW_BASE="true")

        base = json.loads((self.stage_dir / "base" / "metadata.json").read_text())
        self.assertEqual(base["kind"], "base")
        self.assertEqual(base["merge_base_sha"], "merge-base-sha")
        self.assertNotIn("pr_number", base)
        self.assertNotIn("head_sha", base)

    def test_a_bundle_never_inherits_the_label_of_its_seed(self) -> None:
        # A fetched base bundle carries kind=base, and the head is seeded by
        # copying that directory. A marker that travelled with the files would
        # publish this pull request's analysis labelled as a base graph.
        _state(self.base_dir)
        (self.base_dir / "metadata.json").write_text(json.dumps({"kind": "base"}), encoding="utf-8")

        self._analyze()

        self.assertEqual(json.loads((self.stage_dir / "warmstart" / "metadata.json").read_text())["kind"], "warmstart")

    def test_scratch_files_are_not_published(self) -> None:
        # Run logs and lock files are the engine's working area. No reader
        # inflates them and every fetch pays for them.
        _state(self.base_dir)
        self._analyze()

        staged = self.stage_dir / "warmstart"
        self.assertTrue((staged / "analysis.json").is_file())
        self.assertFalse((staged / "logs").exists())
        self.assertEqual(list(staged.glob("*.lock")), [])

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

    def _build(self, **extra: str) -> subprocess.CompletedProcess:
        output = self.root / "github-output"
        result = subprocess.run(
            [str(ROOT / "scripts" / "action" / "build-review-artifact.sh")],
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(self.root),
                "GITHUB_OUTPUT": str(output),
                "ANALYSIS_PATH": str(self.head),
                "BASE_ARTIFACT_NAME": "codeboarding-base-cfg-mergebasesha",
                "BASE_ARTIFACT_ID": "4242",
                "BASE_ANALYSIS_PATH": str(self.base),
                "INLINE_BASE": "false",
                "ANALYSIS_MODE": "incremental",
                "BASE_SHA": "tip-sha",
                "MERGE_BASE_SHA": "merge-base-sha",
                "MERGE_BASE_RESOLVED": "true",
                "HEAD_SHA": "head-sha",
                "PR_NUMBER": "81",
                "SEED_SOURCE": "pr-chain",
                "CHAIN_DEPTH": "2",
                **extra,
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
        # The name is not enough: two artifacts can share it and disagree, since
        # the engine is not deterministic and sync publishes bases too.
        self.assertEqual(metadata["base_artifact_id"], "4242")
        # A reader can assert on one field instead of guessing from the payload.
        self.assertEqual(metadata["kind"], "review")
        # The webview resolves base_commit_sha || pr_base_sha || base_sha, so the
        # merge base has to appear under a name it looks for or it silently uses
        # the branch tip.
        self.assertEqual(metadata["pr_base_sha"], "merge-base-sha")
        # A JSON string, which "false" also is, is truthy in a consumer: this
        # has to be a real boolean or a caveat banner never fires.
        self.assertIs(metadata["merge_base_resolved"], True)
        self.assertEqual(metadata["seed_source"], "pr-chain")


class ReviewHealthArtifactTests(ReviewArtifactTests):
    """The engine writes a health report beside every analysis it produces."""

    def test_a_fork_review_carries_the_base_it_computed(self) -> None:
        # A fork run publishes nothing another run reads, so if it had to compute
        # the base itself, naming an artifact that does not exist would leave a
        # reader unable to reproduce the comparison.
        self._build(INLINE_BASE="true")

        artifact = self.root / "cb-review-artifact"
        self.assertEqual(json.loads((artifact / "base_analysis.json").read_text())["components"], ["base"])
        metadata = json.loads((artifact / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["base_artifact"], "", "it must not name a base it never published")

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
                for field in ("uses", "path", "name", "if", "retention-days", "run"):
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

    def test_the_base_is_inlined_whenever_no_artifact_holds_it(self) -> None:
        # Keying this on is_fork alone missed a same-repository review whose base
        # upload failed: the artifact then named a base that was never created.
        step = next(
            s
            for s in self._steps()
            if s.get("name", "").startswith("Build review artifact") or "build-review-artifact" in s.get("run", "")
        )
        inline = [l for l in (ROOT / "action.yml").read_text().splitlines() if "INLINE_BASE:" in l]
        self.assertEqual(len(inline), 1)
        self.assertIn("publish_base.outputs.artifact-id == ''", inline[0])
        self.assertIn("fetch_base.outputs.artifact_id == ''", inline[0])

    def test_a_base_outlives_the_reviews_that_reference_it(self) -> None:
        # A review points at a base by id for its whole life, so a base kept for
        # the same period is only ever good at the instant it is written: the
        # renewal check would then fire on every run and republish it every time,
        # which is exactly the duplication that splitting it out removed.
        review = next(s for s in self._uploads() if "review_artifact.outputs.artifact_dir" in s.get("path", ""))
        review_days = int(review["retention-days"])
        bases = [s for s in self._uploads() if "out/base" in s.get("path", "")]
        self.assertTrue(bases, "no base publication step found")
        for step in bases:
            self.assertGreater(
                int(step.get("retention-days", 0)),
                review_days,
                f"{step['name']} does not outlive the reviews that reference it",
            )

    def test_the_renewal_threshold_matches_the_review_retention(self) -> None:
        # A review references a base by id for its whole life, so the threshold
        # that triggers renewal has to be that same life. If the two drift apart,
        # a review can outlive the base it names and nothing catches it.
        text = (ROOT / "action.yml").read_text(encoding="utf-8")
        renew = next(l for l in text.splitlines() if "RENEW_WITHIN_DAYS:" in l)
        review = next(s for s in self._uploads() if "review_artifact.outputs.artifact_dir" in s.get("path", ""))
        self.assertIn(f"'{review['retention-days']}'", renew)

    def test_the_reusable_analysis_honours_the_configured_retention(self) -> None:
        warmstart = [s for s in self._uploads() if "warmstart" in s.get("path", "")]
        self.assertTrue(warmstart, "no warm-start publication step found")
        for step in warmstart:
            self.assertEqual(step.get("retention-days"), "${{ inputs.warmstart_retention_days }}")


if __name__ == "__main__":
    unittest.main()


class ArtifactProvenanceTests(unittest.TestCase):
    """These names are predictable, and a fork pull request can add a workflow
    that uploads one into this repository's artifact store. Loading it would
    hand a fork's bytes to a pickle loader in a privileged run."""

    FETCH = ROOT / "scripts" / "action" / "fetch-state.sh"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        # A real zip, so the download path is exercised rather than short-circuited.
        import zipfile

        self.bundle = self.root / "bundle.zip"
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr("analysis.json", '{"components": []}')

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, listing: dict) -> subprocess.CompletedProcess:
        gh = self.bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            f"  *artifacts?name=*) cat <<'JSON'\n{json.dumps(listing)}\nJSON\n    ;;\n"
            f'  *"/zip"*) cat "{self.bundle}" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        return subprocess.run(
            [str(self.FETCH)],
            env={
                "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(self.root),
                "REPOSITORY": "owner/repo",
                "GH_HOST": "https://github.com",
                "ARTIFACT_NAME": "codeboarding-warmstart-cfg-pr7",
                "DEST": str(self.root / "out"),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_paged(self, pages: dict) -> subprocess.CompletedProcess:
        gh = self.bin_dir / "gh"
        branches = "".join(
            # anchored: "page=1" would also match "per_page=100"
            f"  *\"&page={page}\") cat <<'JSON'\n{json.dumps(body)}\nJSON\n    ;;\n"
            for page, body in pages.items()
        )
        gh.write_text(
            "#!/usr/bin/env bash\n" 'case "$*" in\n' f'  *"/zip"*) cat "{self.bundle}" ;;\n' f"{branches}" "esac\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        return subprocess.run(
            [str(self.FETCH)],
            env={
                "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(self.root),
                "REPOSITORY": "owner/repo",
                "GH_HOST": "https://github.com",
                "ARTIFACT_NAME": "codeboarding-base-cfg-sha",
                "DEST": str(self.root / "out"),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _artifact(artifact_id: int, created: str, *, fork: bool) -> dict:
        return {
            "id": artifact_id,
            "expired": False,
            "created_at": created,
            "expires_at": "2027-01-01T00:00:00Z",
            "workflow_run": {
                "id": artifact_id,
                "repository_id": 1,
                "head_repository_id": 2 if fork else 1,
            },
        }

    def test_it_refuses_state_produced_by_a_run_on_forked_code(self) -> None:
        result = self._run({"artifacts": [self._artifact(1, "2026-08-19T10:00:00Z", fork=True)]})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("does not control", result.stdout)
        self.assertFalse((self.root / "out").exists(), "a fork's bundle was downloaded")

    def test_a_newer_fork_artifact_cannot_displace_a_trusted_one(self) -> None:
        # The attack is to upload a newer artifact under the same predictable
        # name, so picking "newest" without checking provenance is the bug.
        result = self._run(
            {
                "artifacts": [
                    self._artifact(1, "2026-08-19T10:00:00Z", fork=False),
                    self._artifact(2, "2026-08-19T11:00:00Z", fork=True),
                ]
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "out").exists(), "the trusted bundle should still be used")

    def test_it_clears_a_previous_invocation_before_looking_up(self) -> None:
        # The destinations are fixed, so a second use of the action in one job
        # would otherwise inherit the first one's files as its own state.
        stale = self.root / "out"
        stale.mkdir()
        (stale / "analysis.json").write_text('{"stale": true}', encoding="utf-8")

        result = self._run({"artifacts": []})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists(), "state from a previous invocation survived a miss")

    def test_a_flood_of_fork_artifacts_cannot_hide_a_trusted_one(self) -> None:
        # Rejected entries still occupy the page, so a fork uploading repeatedly
        # under the predictable name could push the trusted bundle out of reach.
        page_one = [self._artifact(i, f"2026-08-19T{i:02d}:00:00Z", fork=True) for i in range(10, 100)]
        page_one += [self._artifact(i, f"2026-08-18T{i - 100:02d}:00:00Z", fork=True) for i in range(100, 110)]
        pages = {
            "1": {"artifacts": page_one},
            "2": {"artifacts": [self._artifact(1, "2026-08-01T10:00:00Z", fork=False)]},
        }
        result = self._run_paged(pages)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "out").exists(), "the trusted bundle on page 2 was never reached")

    def test_it_uses_state_produced_by_this_repository(self) -> None:
        result = self._run({"artifacts": [self._artifact(1, "2026-08-19T10:00:00Z", fork=False)]})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "out").exists())
