"""sync_strategy: branch saves the analysis to an orphan branch, and reviews read their base from it."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_action_state import ANALYZE, ENGINE_STUB

ROOT = Path(__file__).resolve().parent.parent
DELIVER = ROOT / "scripts" / "action" / "deliver-sync.sh"
BRANCH = "codeboarding/analysis"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class AnalysisBranchDeliveryTests(unittest.TestCase):
    """deliver-sync.sh against a real local remote."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.remote = self.root / "owner" / "repo.git"
        self.remote.mkdir(parents=True)
        git(self.remote, "init", "-q", "--bare", "-b", "main")
        self.checkout = self.root / "checkout"
        git(self.root, "clone", "-q", str(self.remote), str(self.checkout))
        (self.checkout / "app.py").write_text("print('hi')\n", encoding="utf-8")
        self._push_code("initial")
        self.analysis = self.root / "analysis"
        self.analysis.mkdir()
        self._analysis("first")
        self.core = self.root / "core"
        (self.core / "static_analyzer").mkdir(parents=True)
        (self.core / "utils.py").write_text(
            "ANALYSIS_FILENAME = 'analysis.json'\nFINGERPRINT_FILENAME = 'fingerprint.json'\n", encoding="utf-8"
        )
        (self.core / "static_analyzer" / "__init__.py").touch()
        (self.core / "static_analyzer" / "analysis_cache.py").write_text(
            "STATIC_ANALYSIS_PKL = 'static_analysis.pkl'\nSTATIC_ANALYSIS_SHA = 'static_analysis.sha'\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _push_code(self, message: str) -> str:
        (self.checkout / f"{message}.py").write_text("pass\n", encoding="utf-8")
        git(self.checkout, "add", "-A")
        git(self.checkout, "commit", "-q", "-m", message)
        git(self.checkout, "push", "-q", "origin", "HEAD:main")
        return git(self.checkout, "rev-parse", "HEAD")

    def _analysis(self, content: str) -> None:
        for name in ("analysis.json", "fingerprint.json", "static_analysis.pkl"):
            (self.analysis / name).write_text(f"{content} {name}\n", encoding="utf-8")

    def _deliver(self, expect_ok: bool = True) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
        output = self.root / "github-output"
        output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(DELIVER)],
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(self.core),
                "ACTION_PATH": str(ROOT),
                "ANALYSIS_DIR": str(self.analysis),
                "CHECKOUT_DIR": str(self.checkout),
                "GITHUB_OUTPUT": str(output),
                "RUNNER_TEMP": str(self.root),
                "GITHUB_SERVER_URL": str(self.root),
                "GITHUB_TOKEN": "unused",
                "GH_HOST": "github.com",
                "REPOSITORY": "owner/repo",
                "TARGET_BRANCH": "main",
                "SYNC_STRATEGY": "branch",
                "ANALYSIS_BRANCH": BRANCH,
                "ENGINE_VERSION": "0.14.5",
                "CFG_HASH": "cfg",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
        return result, values

    def _branch_log(self) -> list[str]:
        return git(self.remote, "log", "--format=%H %P", BRANCH).splitlines()

    def test_the_first_sync_creates_an_orphan_branch_with_its_provenance(self) -> None:
        main_before = git(self.remote, "rev-parse", "main")

        _result, values = self._deliver()

        (only,) = self._branch_log()
        self.assertEqual(only.split(), [values["analysis_branch_sha"]], "the branch must have no parent")
        self.assertEqual(values["committed"], "true")
        self.assertEqual(values["baseline_sha"], main_before, "artifacts are named for the analysed commit")
        files = set(git(self.remote, "ls-tree", "-r", "--name-only", BRANCH).splitlines())
        self.assertEqual(
            files,
            {
                ".codeboarding/analysis.json",
                ".codeboarding/fingerprint.json",
                ".codeboarding/static_analysis.pkl",
                ".codeboarding/source.json",
            },
        )
        source = json.loads(git(self.remote, "show", f"{BRANCH}:.codeboarding/source.json"))
        self.assertEqual(source["schema"], 1)
        self.assertEqual(source["source_branch"], "main")
        self.assertEqual(source["source_sha"], main_before)
        self.assertEqual(source["engine_version"], "0.14.5")
        self.assertEqual(source["config"], "cfg")
        self.assertRegex(source["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        message = git(self.remote, "log", "-1", "--format=%B", BRANCH)
        self.assertEqual(
            message,
            f"chore(codeboarding): diagram of main @{main_before[:7]}\n\n"
            f"CodeBoarding-Source: {main_before}\nCodeBoarding-Config: cfg",
        )
        # The default branch is never written in this strategy.
        self.assertEqual(git(self.remote, "rev-parse", "main"), main_before)

    def test_the_next_sync_appends_a_fast_forward_commit(self) -> None:
        self._deliver()
        first = git(self.remote, "rev-parse", BRANCH)
        new_main = self._push_code("feature")
        self._analysis("second")

        _result, values = self._deliver()

        log = self._branch_log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0].split(), [values["analysis_branch_sha"], first])
        self.assertIn(f"CodeBoarding-Source: {new_main}", git(self.remote, "log", "-1", "--format=%B", BRANCH))

    def test_a_rerun_on_the_same_commit_adds_nothing(self) -> None:
        self._deliver()

        _result, values = self._deliver()

        self.assertEqual(values["committed"], "false")
        self.assertEqual(len(self._branch_log()), 1)

    def test_a_push_refused_by_a_branch_rule_says_how_to_fix_it(self) -> None:
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\nwhile read old new ref; do\n"
            f'  [ "$ref" != refs/heads/{BRANCH} ] || {{ echo "GH013: Repository rule violations found"; exit 1; }}\n'
            "done\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        result, _values = self._deliver(expect_ok=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"GitHub refused the push to {BRANCH}", result.stdout)
        self.assertIn("bypass actor", result.stdout)
        self.assertIn("sync_strategy: push", result.stdout)

    def test_a_deleted_branch_is_recreated_as_a_new_orphan(self) -> None:
        self._deliver()
        git(self.remote, "branch", "-D", BRANCH)
        self._push_code("later")

        self._deliver()

        (only,) = self._branch_log()
        self.assertEqual(len(only.split()), 1, "a recreated branch starts a new history")

    def test_an_existing_branch_that_is_not_an_analysis_branch_is_never_written(self) -> None:
        # analysis_branch pointed at a code branch: building on it would leave it
        # holding nothing but .codeboarding/.
        git(self.checkout, "push", "-q", "origin", f"main:refs/heads/{BRANCH}")
        before = git(self.remote, "rev-parse", BRANCH)

        result, values = self._deliver(expect_ok=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{BRANCH} already exists and is not a CodeBoarding analysis branch", result.stdout)
        self.assertEqual(git(self.remote, "rev-parse", BRANCH), before)
        self.assertNotIn("analysis_branch_sha", values)

    def test_a_target_that_moved_during_analysis_keeps_the_branch_unchanged(self) -> None:
        self._deliver()
        before = git(self.remote, "rev-parse", BRANCH)
        other = self.root / "other"
        git(self.root, "clone", "-q", str(self.remote), str(other))
        (other / "x.py").write_text("pass\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "x")
        git(other, "push", "-q", "origin", "HEAD:main")
        self._analysis("stale")

        _result, values = self._deliver()

        self.assertEqual(values["committed"], "false")
        self.assertEqual(git(self.remote, "rev-parse", BRANCH), before)


class AnalysisBranchReadTests(unittest.TestCase):
    """Reviews and sync read the branch: commits c0..c4 on main, branch entries for c1 and c3
    made under configuration `cfg`."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        work = self.root / "work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        self.shas = []
        for index in range(5):
            (work / f"f{index}.py").write_text("pass\n", encoding="utf-8")
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", f"c{index}")
            self.shas.append(git(work, "rev-parse", "HEAD"))
        git(work, "checkout", "-q", "--orphan", BRANCH)
        git(work, "rm", "-rq", "--cached", ".")
        for path in work.glob("f*.py"):
            path.unlink()
        board = work / ".codeboarding"
        board.mkdir()
        for source in (self.shas[1], self.shas[3]):
            (board / "analysis.json").write_text(
                json.dumps({"metadata": {"depth_cap": 2}, "components": [source]}), encoding="utf-8"
            )
            (board / "static_analysis.pkl").write_text("pickle", encoding="utf-8")
            (board / "source.json").write_text(json.dumps({"schema": 1, "source_sha": source}), encoding="utf-8")
            git(work, "add", "-A")
            git(
                work,
                "commit",
                "-q",
                "-m",
                f"chore(codeboarding): diagram of main @{source[:7]}",
                "-m",
                f"CodeBoarding-Source: {source}\nCodeBoarding-Config: cfg",
            )
        git(work, "checkout", "-q", "main")
        bare = self.root / "origin.git"
        git(self.root, "clone", "-q", "--bare", str(work), str(bare))
        git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
        git(bare, "config", "uploadpack.allowFilter", "true")
        self.checkout = self.root / "checkout"
        git(self.root, "clone", "-q", "--depth=1", "--branch", "main", f"file://{bare}", str(self.checkout))

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "codeboarding").write_text(ENGINE_STUB, encoding="utf-8")
        (self.bin_dir / "codeboarding").chmod(0o755)
        self.engine_log = self.root / "engine.log"
        self.engine_log.write_text("", encoding="utf-8")
        self.runner = self.root / "runner"
        self.runner.mkdir()
        self.output = self.root / "github-output"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _analyze(self, **extra: str) -> dict[str, str]:
        self.output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(ANALYZE)],
            env={
                "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
                "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.runner),
                "CB_ENGINE_LOG": str(self.engine_log),
                "ACTION_PATH": str(ROOT),
                "ANALYSIS_KIND": "review",
                "CHECKOUT_DIR": str(self.checkout),
                "REVIEW_HEAD_SHA": "head-sha",
                "REVIEW_BASE_REPO": "origin",
                "REPOSITORY": "origin",
                "GITHUB_SERVER_URL": f"file://{self.root}",
                "PR_NUMBER": "42",
                "ENGINE_VERSION": "0.14.5",
                "CFG_HASH": "cfg",
                "ANALYSIS_BRANCH": BRANCH,
                "BASE_DIR": str(self.root / "state" / "base"),
                "WARMSTART_DIR": str(self.root / "state" / "warmstart"),
                "STAGE_DIR": str(self.root / "state" / "out"),
                "DEPTH_CAP": "2",
                **extra,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return dict(line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines() if "=" in line)

    def _modes(self) -> list[str]:
        return [json.loads(line)["mode"] for line in self.engine_log.read_text().splitlines()]

    def test_a_branch_entry_for_the_merge_base_is_reused(self) -> None:
        values = self._analyze(REVIEW_BASE_SHA=self.shas[3])

        self.assertEqual(values["base_analysis_method"], "reused")
        self.assertEqual(values["base_analysis_reason"], f"{self.shas[3][:7]} already has a saved analysis")
        self.assertEqual(self._modes(), ["incremental"], "only the head is analyzed")
        base = json.loads(Path(values["base_analysis_path"]).read_text())
        self.assertEqual(base["components"], [self.shas[3]])
        self.assertFalse(Path(values["base_analysis_path"]).with_name("source.json").exists())
        # No artifact holds it yet, so it is published under the merge base's name.
        self.assertEqual(values["publish_base"], "true")

    def test_the_nearest_branch_entry_below_the_merge_base_is_caught_up(self) -> None:
        values = self._analyze(REVIEW_BASE_SHA=self.shas[4])

        self.assertEqual(values["base_analysis_method"], "incremental")
        self.assertEqual(
            values["base_analysis_reason"],
            f"updated the analysis of {self.shas[3][:7]} to {self.shas[4][:7]}, 1 commit caught up",
        )
        self.assertEqual(self._modes(), ["incremental", "incremental"])

    def test_an_entry_made_under_another_configuration_is_never_reused(self) -> None:
        # Another engine version or model: reusing it as is would diff an old
        # configuration's base against a new head.
        values = self._analyze(REVIEW_BASE_SHA=self.shas[3], CFG_HASH="othercfg")

        self.assertEqual(values["base_analysis_method"], "full")
        self.assertEqual(
            values["base_analysis_reason"],
            "the existing analysis was incompatible or could not be updated incrementally",
        )
        self.assertEqual(self._modes(), ["full", "incremental"])

    def test_without_a_configuration_hash_no_entry_is_trusted(self) -> None:
        values = self._analyze(REVIEW_BASE_SHA=self.shas[3], CFG_HASH="")

        self.assertEqual(values["base_analysis_method"], "full")

    def test_without_the_branch_the_base_is_a_full_analysis(self) -> None:
        values = self._analyze(REVIEW_BASE_SHA=self.shas[4], ANALYSIS_BRANCH="codeboarding/none")

        self.assertEqual(values["base_analysis_method"], "full")
        self.assertEqual(values["base_analysis_reason"], "no usable analysis was available")
        self.assertEqual(self._modes(), ["full", "incremental"])

    def test_sync_continues_from_the_branch_tip(self) -> None:
        self._analyze(ANALYSIS_KIND="sync", SYNC_STRATEGY="branch", FORCE_FULL="false")

        self.assertEqual(self._modes(), ["incremental"])

    def test_sync_replaces_the_committed_state_with_the_branch_tip(self) -> None:
        # Switching from push: the checkout still holds the old committed baseline.
        # Only its user configuration may survive; generated files come from the tip.
        board = self.checkout / ".codeboarding"
        board.mkdir()
        (board / "analysis.json").write_text(json.dumps({"metadata": {"depth_cap": 2}, "old": True}))
        (board / "static_analysis.pkl").write_text("old pickle")
        (board / "static_analysis.sha").write_text("stale\n")
        (board / ".codeboardingignore").write_text("docs/\n")

        values = self._analyze(ANALYSIS_KIND="sync", SYNC_STRATEGY="branch", FORCE_FULL="false")

        state = Path(values["analysis_dir"])
        self.assertEqual((state / "static_analysis.pkl").read_text(), "pickle", "the tip's engine state")
        self.assertFalse((state / "static_analysis.sha").exists(), "a generated file from the old baseline")
        self.assertFalse((state / "source.json").exists())
        self.assertEqual((state / ".codeboardingignore").read_text(), "docs/\n")

    def test_sync_does_not_continue_from_a_tip_made_under_another_configuration(self) -> None:
        self._analyze(ANALYSIS_KIND="sync", SYNC_STRATEGY="branch", FORCE_FULL="false", CFG_HASH="othercfg")

        self.assertEqual(self._modes(), ["full"])

    def test_sync_without_the_branch_analyzes_from_scratch(self) -> None:
        self._analyze(
            ANALYSIS_KIND="sync", SYNC_STRATEGY="branch", FORCE_FULL="false", ANALYSIS_BRANCH="codeboarding/none"
        )

        self.assertEqual(self._modes(), ["full"])


class AnalysisBranchGuardTests(unittest.TestCase):
    def _guard(self, **extra: str) -> tuple[subprocess.CompletedProcess, str]:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            output.write_text("", encoding="utf-8")
            result = subprocess.run(
                [str(ROOT / "scripts" / "action" / "guard.sh")],
                env={
                    "PATH": os.environ["PATH"],
                    "GITHUB_OUTPUT": str(output),
                    "MODE": "sync",
                    "EVENT": "push",
                    "REF_NAME": "main",
                    "REF_TYPE": "branch",
                    "HEAD_AUTHOR_EMAIL": "dev@example.com",
                    "SYNC_STRATEGY": "branch",
                    "ANALYSIS_BRANCH": BRANCH,
                    "REPOSITORY": "owner/repo",
                    **extra,
                },
                capture_output=True,
                text=True,
                check=False,
            )
            return result, output.read_text(encoding="utf-8")

    def test_the_branch_strategy_is_accepted(self) -> None:
        result, values = self._guard()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("target_branch=main", values)

    def test_a_push_to_the_analysis_branch_itself_is_ignored(self) -> None:
        result, values = self._guard(REF_NAME=BRANCH)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("skip=true", values)

    def test_a_branch_name_git_cannot_use_fails_before_anything_runs(self) -> None:
        for mode in ("sync", "review"):
            for name in ("codeboarding/a..b", "codeboarding/with space", "codeboarding/trailing."):
                result, _values = self._guard(MODE=mode, ANALYSIS_BRANCH=name)
                self.assertNotEqual(result.returncode, 0, (mode, name))
                self.assertIn("is not a valid branch name", result.stdout)

    def test_target_branch_must_differ_from_the_analysis_branch(self) -> None:
        result, _values = self._guard(TARGET_BRANCH_INPUT=BRANCH)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target_branch must differ from analysis_branch", result.stdout)


if __name__ == "__main__":
    unittest.main()
