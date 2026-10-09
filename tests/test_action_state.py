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
        "depth": argv[argv.index("--depth-cap") + 1] if "--depth-cap" in argv else None,
    }) + "\\n")
analysis = os.path.join(output, "analysis.json")
metadata = json.load(open(analysis))["metadata"] if os.path.isfile(analysis) else {}
if argv[0] == "incremental" and os.environ.get("CB_REQUIRE_FULL") == "true":
    print(json.dumps({"requiresFullAnalysis": True}))
    sys.exit(0)
if argv[0] == "full":
    metadata = {"depth_cap": int(argv[argv.index("--depth-cap") + 1])}
with open(analysis, "w") as handle:
    json.dump({"metadata": metadata, "components": [], "components_relations": []}, handle)
print(json.dumps({"requiresFullAnalysis": False, "analysis_path": analysis}))
'''


def _digest(path: Path) -> str:
    """Mirrors analysis_digest in analyze.sh: sha256 of the file, first 16 hex chars."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _state(directory: Path, depth: int = 2, cap: int | None = 2, **origin: object) -> Path:
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

        self.assertNotEqual(baseline["cfg_hash"], self._run(DEPTH_CAP="4")["cfg_hash"])
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
        _state(self.warmstart_dir, depth=1, cap=1, chain_depth=3)

        values = self._analyze()

        self.assertEqual(values["seed_source"], "base")

    def _commit_base(self, cap: int | None = None, legacy: bool = False) -> str:
        if cap is not None or legacy:
            _state(self.checkout / ".codeboarding", depth=1, cap=cap)
        (self.checkout / "code.py").write_text("pass\n")
        for args in (
            ("init",),
            ("add", "."),
            (
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "test: base",
            ),
        ):
            subprocess.run(["git", "-C", str(self.checkout), *args], check=True, capture_output=True)
        return subprocess.check_output(["git", "-C", str(self.checkout), "rev-parse", "HEAD"], text=True).strip()

    def test_missing_baseline_runs_full_then_incremental_at_configured_depth(self) -> None:
        sha = self._commit_base()
        values = self._analyze(REVIEW_BASE_SHA=sha, DEPTH_CAP="4")
        calls = self._engine_calls()
        self.assertEqual([c["mode"] for c in calls], ["full", "incremental"])
        self.assertEqual(calls[0]["depth"], "4")
        self.assertEqual(calls[1]["checkout"], str(self.checkout))
        self.assertEqual(values["publish_base"], "true")
        for kind in ("base", "warmstart"):
            analysis = json.loads((self.stage_dir / kind / "analysis.json").read_text())
            self.assertEqual(analysis["metadata"]["depth_cap"], 4)

    def test_compatible_committed_baseline_runs_incrementally(self) -> None:
        sha = self._commit_base(cap=4)
        self._analyze(REVIEW_BASE_SHA=sha, DEPTH_CAP="4")
        self.assertEqual([c["mode"] for c in self._engine_calls()], ["incremental", "incremental"])

    def test_legacy_committed_depth_is_not_inherited(self) -> None:
        sha = self._commit_base(legacy=True)
        self._analyze(REVIEW_BASE_SHA=sha, DEPTH_CAP="4")
        self.assertEqual([c["mode"] for c in self._engine_calls()], ["full", "incremental"])
        self.assertEqual(self._engine_calls()[0]["depth"], "4")

    def test_changed_configuration_rebuilds_the_base(self) -> None:
        sha = self._commit_base(cap=2)
        _state(self.base_dir, cap=2)
        self._analyze(REVIEW_BASE_SHA=sha, DEPTH_CAP="4")
        self.assertEqual([c["mode"] for c in self._engine_calls()], ["full", "incremental"])
        self.assertEqual(self._engine_calls()[0]["depth"], "4")

    def test_head_full_fallback_uses_configured_cap(self) -> None:
        _state(self.base_dir, depth=1, cap=4)
        self._analyze(DEPTH_CAP="4", CB_REQUIRE_FULL="true")
        self.assertEqual([c["mode"] for c in self._engine_calls()], ["incremental", "full"])
        self.assertEqual(self._engine_calls()[1]["depth"], "4")

    def test_base_and_head_fallbacks_keep_configured_depth(self) -> None:
        sha = self._commit_base(cap=4)
        self._analyze(REVIEW_BASE_SHA=sha, DEPTH_CAP="4", CB_REQUIRE_FULL="true")
        calls = self._engine_calls()
        self.assertEqual([c["mode"] for c in calls], ["incremental", "full", "incremental", "full"])
        self.assertEqual([c["depth"] for c in calls if c["mode"] == "full"], ["4", "4"])

    # How the base was obtained is reported, not just used: the review comment and
    # the webview explain a slow run by it.

    def _git(self, *args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.checkout),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _commit(self, message: str, files: dict[str, str], *, bot: bool = False) -> str:
        for name, content in files.items():
            (self.checkout / name).parent.mkdir(parents=True, exist_ok=True)
            (self.checkout / name).write_text(content, encoding="utf-8")
        self._git("add", "-A")
        committer = ("-c", "user.email=codeboarding-review[bot]@users.noreply.github.com") if bot else ()
        self._git(*committer, "commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _sync_history(self) -> tuple[str, str]:
        """An analysed commit with a sync commit on top that writes only .codeboarding/."""
        self._git("init", "-q", "-b", "main")
        analysed = self._commit("feat: code", {"code.py": "pass\n"})
        _state(self.checkout / ".codeboarding", cap=2)
        sync = self._commit("chore(codeboarding): sync analysis baseline", {}, bot=True)
        return analysed, sync

    def _merge(self, branch: str, files: dict[str, str], *, bot: bool = False) -> str:
        """A commit on `branch` merged into main with --no-ff."""
        self._git("checkout", "-q", "-b", branch)
        self._commit(f"work on {branch}", files, bot=bot)
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        return self._git("rev-parse", "HEAD")

    def _provenance(self, values: dict[str, str]) -> dict[str, str]:
        keys = ("base_analysis_method", "base_analysis_reason")
        for key in ("base_seconds", "head_seconds"):
            self.assertRegex(values[key], r"^[0-9]+$", key)
        return {key: values[key] for key in keys}

    def test_a_saved_base_is_reused(self) -> None:
        _state(self.base_dir)

        values = self._analyze(BASE_FETCH_SECONDS="7")

        self.assertEqual(
            self._provenance(values),
            {"base_analysis_method": "reused", "base_analysis_reason": "merge-b already has a saved analysis"},
        )
        # The download happened in the step before; it is still time spent on the base.
        self.assertGreaterEqual(int(values["base_seconds"]), 7)

    def test_a_base_with_nothing_to_seed_it_is_a_full_analysis(self) -> None:
        sha = self._commit_base()

        values = self._analyze(REVIEW_BASE_SHA=sha)

        self.assertEqual(
            self._provenance(values),
            {"base_analysis_method": "full", "base_analysis_reason": "no usable analysis was available"},
        )

    def _assert_incompatible(self, values: dict[str, str]) -> None:
        self.assertEqual(
            self._provenance(values),
            {
                "base_analysis_method": "full",
                "base_analysis_reason": "the existing analysis was incompatible or could not be updated incrementally",
            },
        )

    def test_a_committed_baseline_with_another_depth_is_incompatible(self) -> None:
        sha = self._commit_base(legacy=True)

        self._assert_incompatible(self._analyze(REVIEW_BASE_SHA=sha))

    def test_a_saved_base_with_another_depth_is_incompatible(self) -> None:
        sha = self._commit_base()
        _state(self.base_dir, cap=1)

        self._assert_incompatible(self._analyze(REVIEW_BASE_SHA=sha))

    def test_an_engine_that_demands_a_full_run_makes_the_baseline_incompatible(self) -> None:
        sha = self._commit_base(cap=2)

        self._assert_incompatible(self._analyze(REVIEW_BASE_SHA=sha, CB_REQUIRE_FULL="true"))

    def test_a_baseline_committed_at_the_merge_base_is_reused(self) -> None:
        # The sync commit changes only .codeboarding/, which the analysis ignores,
        # so a merge base on it is exactly the commit the baseline describes.
        _analysed, sync = self._sync_history()

        values = self._analyze(REVIEW_BASE_SHA=sync)

        self.assertEqual(
            self._provenance(values),
            {"base_analysis_method": "reused", "base_analysis_reason": f"{sync[:7]} already has a saved analysis"},
        )
        # Not under the merge base's name yet, so this run publishes it.
        self.assertEqual(values["publish_base"], "true")

    def test_a_committed_baseline_counts_the_commits_it_caught_up(self) -> None:
        analysed, _sync = self._sync_history()
        self._commit("feat: more", {"more.py": "pass\n"})
        merge_base = self._commit("feat: again", {"code.py": "print()\n"})

        values = self._analyze(REVIEW_BASE_SHA=merge_base)

        self.assertEqual(
            self._provenance(values),
            {
                "base_analysis_method": "incremental",
                "base_analysis_reason": f"updated the analysis of {analysed[:7]} to {merge_base[:7]}, 2 commits caught up",
            },
        )

    def test_merged_pull_requests_count_as_commits_to_catch_up(self) -> None:
        # Merged with --no-ff, the first-parent chain is merge commits only, and
        # each one brings code in even though it changes nothing against itself.
        analysed, _sync = self._sync_history()
        self._merge("feature-a", {"a.py": "pass\n"})
        merge_base = self._merge("feature-b", {"b.py": "pass\n"})

        values = self._analyze(REVIEW_BASE_SHA=merge_base)

        self.assertEqual(
            values["base_analysis_reason"],
            f"updated the analysis of {analysed[:7]} to {merge_base[:7]}, 2 commits caught up",
        )

    def test_a_merged_sync_pull_request_describes_its_own_parent(self) -> None:
        # sync_strategy: pull_request. The sync commit sits on codeboarding/sync on
        # top of the analysed commit; main moved on before the merge.
        self._git("init", "-q", "-b", "main")
        analysed = self._commit("feat: code", {"code.py": "pass\n"})
        self._git("checkout", "-q", "-b", "codeboarding/sync")
        _state(self.checkout / ".codeboarding", cap=2)
        self._commit("chore(codeboarding): sync analysis baseline", {}, bot=True)
        self._git("checkout", "-q", "main")
        self._commit("feat: meanwhile", {"later.py": "pass\n"})
        self._git("merge", "-q", "--no-ff", "-m", "Merge codeboarding/sync", "codeboarding/sync")
        merge_base = self._git("rev-parse", "HEAD")

        values = self._analyze(REVIEW_BASE_SHA=merge_base)

        self.assertEqual(
            self._provenance(values),
            {
                "base_analysis_method": "incremental",
                "base_analysis_reason": f"updated the analysis of {analysed[:7]} to {merge_base[:7]}, 1 commit caught up",
            },
        )

    def test_a_baseline_sync_did_not_write_is_of_unknown_origin(self) -> None:
        # A squash or a hand edit: its parent is not known to be what was analysed,
        # so the reason names no starting commit and claims nothing was exact.
        self._git("init", "-q", "-b", "main")
        self._commit("feat: code", {"code.py": "pass\n"})
        _state(self.checkout / ".codeboarding", cap=2)
        merge_base = self._commit("chore(codeboarding): sync analysis baseline (#7)", {})

        values = self._analyze(REVIEW_BASE_SHA=merge_base)

        self.assertEqual(
            self._provenance(values),
            {
                "base_analysis_method": "incremental",
                "base_analysis_reason": f"updated an existing analysis to {merge_base[:7]}",
            },
        )

    def test_an_attributes_line_is_not_code_to_catch_up(self) -> None:
        _analysed, _sync = self._sync_history()
        merge_base = self._commit(
            "chore: attributes", {".gitattributes": ".codeboarding/analysis.json linguist-generated=true\n"}
        )

        values = self._analyze(REVIEW_BASE_SHA=merge_base)

        self.assertEqual(values["base_analysis_method"], "reused")

    def _progress_stub(self) -> Path:
        calls = self.root / "gh-calls"
        gh = self.bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n----\\n" "$*" >> "{calls}"\n'
            'case "$*" in *"/comments?per_page"*) echo 77 ;; esac\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        return calls

    def test_building_the_base_from_scratch_rewrites_the_progress_comment(self) -> None:
        calls = self._progress_stub()
        sha = self._commit_base()

        self._analyze(
            REVIEW_BASE_SHA=sha,
            PROGRESS_HEADER="codeboarding-review",
            REPOSITORY="owner/repo",
            BASE_REF="develop",
            GIT_TOKEN="token",
        )

        patches = [call for call in calls.read_text().split("\n----\n") if "PATCH" in call]
        self.assertGreaterEqual(len(patches), 2, calls.read_text())
        self.assertIn("repos/owner/repo/issues/comments/77", patches[0])
        self.assertIn(f"Building the diagram of `develop` @{sha[:7]} from scratch", patches[0])
        self.assertIn("`develop` has no saved diagram yet", patches[0])
        self.assertIn("2. ⏳ Analysing this PR's changes", patches[-1])
        # The sticky-comment action finds its comment by this line on the final write.
        self.assertIn("<!-- Sticky Pull Request Commentcodeboarding-review -->", patches[-1])

    def test_a_saved_base_leaves_the_progress_comment_alone(self) -> None:
        calls = self._progress_stub()
        _state(self.base_dir)

        self._analyze(PROGRESS_HEADER="codeboarding-review", REPOSITORY="owner/repo", GIT_TOKEN="token")

        self.assertFalse(calls.exists())

    def test_invalid_depth_fails_before_analysis(self) -> None:
        result = subprocess.run(
            [str(ANALYZE)],
            env={"PATH": os.environ["PATH"], "DEPTH_CAP": "-1"},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depth_cap must be a positive integer", result.stdout)
        self.assertEqual(self._engine_calls(), [])

    def test_sync_without_baseline_uses_configured_depth_directly(self) -> None:
        self._analyze(ANALYSIS_KIND="sync", FORCE_FULL="false", DEPTH_CAP="4")
        self.assertEqual([c["mode"] for c in self._engine_calls()], ["full"])
        self.assertEqual(self._engine_calls()[0]["depth"], "4")

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


GH_STUB = """#!/usr/bin/env python3
\"\"\"gh stand-in: serves an artifact listing and one zip from a JSON config.\"\"\"
import json, os, sys
from urllib.parse import parse_qs, urlparse

config = json.load(open(os.environ["CB_GH_CONFIG"]))
with open(os.environ["CB_GH_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
path = next(a for a in sys.argv[1:] if a.startswith("repos/"))
url = urlparse(path)
query = parse_qs(url.query)
if url.path.endswith("/zip"):
    sys.stdout.buffer.write(open(config["zip"], "rb").read())
elif url.path.endswith("/actions/artifacts"):
    artifacts = config["artifacts"]
    if "name" in query:
        artifacts = [a for a in artifacts if a["name"] == query["name"][0]]
    page = int(query.get("page", ["1"])[0])
    if "name" not in query and "pages" in config:
        pages = config["pages"]
        print(json.dumps({"artifacts": pages[page - 1] if page <= len(pages) else []}))
    else:
        print(json.dumps({"artifacts": artifacts if page == 1 else []}))
"""


class AncestorSeedTests(unittest.TestCase):
    """With no saved base for the merge base and nothing committed there, a review
    catches up from the nearest saved ancestor on the base branch's first-parent
    history instead of analyzing the merge base from scratch."""

    def setUp(self) -> None:
        import zipfile

        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        for name, body in (("codeboarding", ENGINE_STUB), ("gh", GH_STUB)):
            (self.bin_dir / name).write_text(body, encoding="utf-8")
            (self.bin_dir / name).chmod(0o755)
        self.engine_log = self.root / "engine.log"
        self.engine_log.write_text("", encoding="utf-8")
        self.gh_log = self.root / "gh.log"
        self.gh_log.write_text("", encoding="utf-8")
        self.gh_config = self.root / "gh.json"
        self.output = self.root / "github-output"
        self.runner_temp = self.root / "runner"
        self.runner_temp.mkdir()
        self.stage_dir = self.root / "state" / "out"
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.bundle = self.root / "bundle.zip"
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr("analysis.json", json.dumps({"metadata": {"depth_cap": 2}, "components": []}))
            archive.writestr("static_analysis.pkl", "pickle")
            archive.writestr("metadata.json", json.dumps({"kind": "base", "merge_base_sha": "ancestor"}))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _history(self, commits: int) -> list[str]:
        """A base branch of `commits` code commits, oldest first, in origin/ (the checkout)."""
        git = ["git", "-C", str(self.origin), "-c", "user.name=T", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "init", "-q"], check=True)
        shas = []
        for index in range(commits):
            (self.origin / f"file{index}.py").write_text("pass\n", encoding="utf-8")
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"c{index}"], check=True)
            shas.append(subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip())
        return shas

    @staticmethod
    def _artifact(name: str, *, fork: bool = False) -> dict:
        return {
            "id": abs(hash(name)) % 100000,
            "name": name,
            "expired": False,
            "created_at": "2026-10-01T00:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
            "workflow_run": {"id": 1, "repository_id": 1, "head_repository_id": 2 if fork else 1},
        }

    def _serve(self, artifacts: list[dict], pages: list | None = None) -> None:
        config = {"artifacts": artifacts, "zip": str(self.bundle)}
        if pages is not None:
            config["pages"] = pages
        self.gh_config.write_text(json.dumps(config), encoding="utf-8")

    def _analyze(self, checkout: Path, **extra: str) -> dict[str, str]:
        self.output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(ANALYZE)],
            env={
                "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
                "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.runner_temp),
                "CB_ENGINE_LOG": str(self.engine_log),
                "CB_GH_CONFIG": str(self.gh_config),
                "CB_GH_LOG": str(self.gh_log),
                "ACTION_PATH": str(ROOT),
                "ANALYSIS_KIND": "review",
                "CHECKOUT_DIR": str(checkout),
                "REVIEW_HEAD_SHA": "head-sha",
                "REVIEW_BASE_REPO": "origin",
                "REPOSITORY": "owner/repo",
                "GITHUB_SERVER_URL": f"file://{self.root}",
                "PR_NUMBER": "42",
                "ENGINE_VERSION": "0.14.5",
                "CFG_HASH": "cfg",
                "ANCESTOR_LOOKUP": "true",
                "BASE_DIR": str(self.root / "state" / "base"),
                "WARMSTART_DIR": str(self.root / "state" / "warmstart"),
                "STAGE_DIR": str(self.stage_dir),
                "DEPTH_CAP": "2",
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

    def _modes(self) -> list[str]:
        return [json.loads(line)["mode"] for line in self.engine_log.read_text().splitlines()]

    def _assert_full(self, values: dict[str, str], reason: str = "no usable analysis was available") -> None:
        self.assertEqual(values["base_analysis_method"], "full")
        self.assertEqual(values["base_analysis_reason"], reason)

    def test_it_catches_up_from_the_nearest_saved_ancestor(self) -> None:
        shas = self._history(5)
        merge_base = shas[-1]
        # Two saved ancestors: the nearer one wins.
        self._serve(
            [self._artifact(f"codeboarding-base-cfg-{shas[0]}"), self._artifact(f"codeboarding-base-cfg-{shas[2]}")]
        )

        values = self._analyze(self.origin, REVIEW_BASE_SHA=merge_base)

        self.assertEqual(values["base_analysis_method"], "incremental")
        self.assertEqual(
            values["base_analysis_reason"],
            f"updated the analysis of {shas[2][:7]} to {merge_base[:7]}, 2 commits caught up",
        )
        self.assertEqual(self._modes(), ["incremental", "incremental"])
        # Published under the merge base's own name, so the next review hits it exactly.
        self.assertEqual(values["publish_base"], "true")
        staged = json.loads((self.stage_dir / "base" / "metadata.json").read_text())
        self.assertEqual(staged["merge_base_sha"], merge_base)

    def test_an_ancestor_beyond_the_bound_is_not_used(self) -> None:
        shas = self._history(5)
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[0]}")])

        values = self._analyze(self.origin, REVIEW_BASE_SHA=shas[-1], CATCHUP_BOUND="2")

        self._assert_full(values)
        self.assertEqual(self._modes(), ["full", "incremental"])
        self.assertNotIn("/zip", self.gh_log.read_text())

    def test_a_saved_analysis_off_this_history_is_not_used(self) -> None:
        shas = self._history(2)
        self._serve([self._artifact("codeboarding-base-cfg-" + "e" * 40)])

        values = self._analyze(self.origin, REVIEW_BASE_SHA=shas[-1])

        self._assert_full(values)

    def test_a_busy_artifact_store_is_paged_until_a_saved_ancestor_appears(self) -> None:
        shas = self._history(3)
        noise = [self._artifact(f"codeboarding-review-{i}-1") for i in range(100)]
        found = self._artifact(f"codeboarding-base-cfg-{shas[0]}")
        later = [self._artifact(f"codeboarding-warmstart-cfg-pr{i}") for i in range(100)]
        self._serve([found], pages=[noise] * 11 + [noise[:99] + [found], later])

        values = self._analyze(self.origin, REVIEW_BASE_SHA=shas[-1])

        self.assertEqual(values["base_analysis_method"], "incremental")
        self.assertIn(f"updated the analysis of {shas[0][:7]} ", values["base_analysis_reason"])
        listings = [
            line
            for line in self.gh_log.read_text().splitlines()
            if "per_page=100&page=" in line and "name=" not in line
        ]
        self.assertEqual(len(listings), 12, "paging stops at the page holding the ancestor")

    def test_a_failed_deepen_falls_back_to_a_full_analysis(self) -> None:
        # The deepen fails, so the shallow checkout walks no ancestor at all.
        shas = self._history(4)
        bare = self.root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.origin), str(bare)], check=True)
        checkout = self.root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth=1", f"file://{bare}", str(checkout)], check=True)
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[0]}")])

        values = self._analyze(checkout, REVIEW_BASE_SHA=shas[-1], GITHUB_SERVER_URL=f"file://{self.root}/missing")

        self._assert_full(values)

    def test_the_merge_bases_own_configuration_survives_the_seed(self) -> None:
        shas = self._history(2)
        (self.origin / ".codeboarding").mkdir()
        (self.origin / ".codeboarding" / ".codeboardingignore").write_text("docs/\n", encoding="utf-8")
        git = ["git", "-C", str(self.origin), "-c", "user.name=T", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "ignore docs"], check=True)
        merge_base = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        import zipfile

        with zipfile.ZipFile(self.bundle, "a") as archive:
            archive.writestr(".codeboardingignore", "stale/\n")
            archive.writestr("health/health_config.json", "{}")
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[0]}")])

        values = self._analyze(self.origin, REVIEW_BASE_SHA=merge_base)

        self.assertEqual(values["base_analysis_method"], "incremental")
        staged = self.stage_dir / "base"
        self.assertEqual((staged / ".codeboardingignore").read_text(), "docs/\n")
        self.assertFalse((staged / "health" / "health_config.json").exists(), "the merge base has none")

    def test_an_ancestor_saved_by_a_run_on_forked_code_is_never_read(self) -> None:
        shas = self._history(3)
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[0]}", fork=True)])

        values = self._analyze(self.origin, REVIEW_BASE_SHA=shas[-1])

        self._assert_full(values)
        self.assertNotIn("/zip", self.gh_log.read_text(), "a fork's bundle was downloaded")

    def test_another_configuration_saved_at_the_merge_base_is_incompatible(self) -> None:
        shas = self._history(2)
        self._serve([self._artifact(f"codeboarding-base-othercfg-{shas[-1]}")])

        values = self._analyze(self.origin, REVIEW_BASE_SHA=shas[-1])

        self._assert_full(values, "the existing analysis was incompatible or could not be updated incrementally")

    def test_a_shallow_checkout_is_deepened_to_find_the_ancestor(self) -> None:
        shas = self._history(4)
        bare = self.root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.origin), str(bare)], check=True)
        subprocess.run(["git", "-C", str(bare), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
        checkout = self.root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth=1", f"file://{bare}", str(checkout)], check=True)
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[0]}")])

        values = self._analyze(checkout, REVIEW_BASE_SHA=shas[-1])

        self.assertEqual(
            values["base_analysis_reason"],
            f"updated the analysis of {shas[0][:7]} to {shas[-1][:7]}, 3 commits caught up",
        )

    def test_without_the_lookup_nothing_is_listed(self) -> None:
        # GHES has no artifact store, so the action turns the lookup off there.
        shas = self._history(2)
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[0]}")])

        values = self._analyze(self.origin, REVIEW_BASE_SHA=shas[-1], ANCESTOR_LOOKUP="false")

        self.assertEqual(values["base_analysis_method"], "full")
        self.assertEqual(self.gh_log.read_text(), "")

    def test_a_first_sync_catches_up_from_the_setup_reviews_base(self) -> None:
        # Merging the setup pull request leaves no committed baseline, but its
        # preview review saved the base at its merge base, the new tip's parent.
        shas = self._history(3)
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[1]}")])

        self._analyze(self.origin, ANALYSIS_KIND="sync", FORCE_FULL="false")

        self.assertEqual(self._modes(), ["incremental"])

    def test_a_forced_sync_never_seeds(self) -> None:
        shas = self._history(2)
        self._serve([self._artifact(f"codeboarding-base-cfg-{shas[0]}")])

        self._analyze(self.origin, ANALYSIS_KIND="sync", FORCE_FULL="True")

        self.assertEqual(self._modes(), ["full"])
        self.assertEqual(self.gh_log.read_text(), "")


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


class UnreadableArtifactsTests(unittest.TestCase):
    """Listing needs actions: read; uploading does not. Without it a repository
    publishes on every run and reads on none, which looks like a permanent miss."""

    def test_it_says_why_when_the_listing_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # denied
            gh.chmod(0o755)

            result = subprocess.run(
                [str(ROOT / "scripts" / "action" / "fetch-state.sh")],
                env={
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "RUNNER_TEMP": str(root),
                    "REPOSITORY": "owner/repo",
                    "GH_HOST": "https://github.com",
                    "ARTIFACT_NAME": "codeboarding-base-cfg-sha",
                    "DEST": str(root / "out"),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("actions: read", result.stdout)
            self.assertFalse((root / "out").exists())
