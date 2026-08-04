"""Smoke tests for scripts/engine_adapter.py — verify it calls the engine API correctly,
using stub modules so no real engine venv is needed."""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


def _preload(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _InitialBaselineUnavailableError(Exception):
    pass


class _InitialIncrementalCacheMissingError(Exception):
    pass


class _InitialSeverity:
    WARNING, CRITICAL = "warning", "critical"


class _InitialStaticAnalysisCache:
    def __init__(self, *args, **kwargs):
        pass

    def get(self):
        return None

    def save(self, *args, **kwargs):
        pass


class _RunPaths:
    def __init__(self, repo_path=None, output_dir=None, project_name=None):
        self.repo_path, self.output_dir, self.project_name = repo_path, output_dir, project_name


class _RunContext:
    def __init__(self, run_id=None, log_path=None, repo_dir=None):
        self.run_id, self.log_path, self.repo_dir = run_id, log_path, repo_dir


class _InitialUnifiedAnalysisJson:
    def __init__(self, data):
        self.data = data

    @classmethod
    def model_validate(cls, data):
        return cls(data)

    def model_dump(self, **kwargs):
        return self.data


class _RejectingUnifiedAnalysisJson:
    @classmethod
    def model_validate(cls, data):
        raise ValueError("incompatible analysis schema")


class _LossyUnifiedAnalysisJson(_InitialUnifiedAnalysisJson):
    def model_dump(self, **kwargs):
        return {"normalized": True}


analysis = _preload(
    "codeboarding_workflows.analysis",
    run_full=lambda *a, **k: "OUT",
    run_incremental=lambda *a, **k: "OUT",
    BaselineUnavailableError=_InitialBaselineUnavailableError,
)
pkg = _preload("codeboarding_workflows")
pkg.analysis = analysis
exc = _preload("diagram_analysis.exceptions", IncrementalCacheMissingError=_InitialIncrementalCacheMissingError)
da = _preload("diagram_analysis", RunPaths=_RunPaths, RunContext=_RunContext)
da.exceptions = exc
_preload("diagram_analysis.analysis_json", UnifiedAnalysisJson=_InitialUnifiedAnalysisJson)
_preload("diagram_analysis.io_utils", write_fingerprint=lambda *a, **k: None)
_preload("logging_config", setup_logging=lambda **kwargs: None)
_preload("agents.content_hash", hash_repo_source_files=lambda *a, **k: {})
_preload("agents")
_preload("codeboarding_workflows.rendering", render_docs=lambda *args, **kwargs: None)
_preload("health.models", Severity=_InitialSeverity)
_preload("health.runner", run_health_checks=lambda *args, **kwargs: None)
_preload("health")
_preload("static_analyzer", get_static_analysis=lambda *args, **kwargs: {})
_preload("static_analyzer.analysis_cache", StaticAnalysisCache=_InitialStaticAnalysisCache)
_preload("static_analyzer.cluster_helpers", build_all_cluster_results=lambda *args, **kwargs: {})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import engine_adapter  # noqa: E402

_STUBBED = [
    "agents",
    "agents.content_hash",
    "codeboarding_workflows",
    "codeboarding_workflows.analysis",
    "diagram_analysis",
    "diagram_analysis.analysis_json",
    "diagram_analysis.exceptions",
    "diagram_analysis.io_utils",
    "logging_config",
    "health",
    "health.models",
    "health.runner",
    "static_analyzer",
    "static_analyzer.analysis_cache",
    "static_analyzer.cluster_helpers",
]


class _Rec:
    def __init__(self, ret="OUT", raises=None):
        self.calls = []  # kwargs of each call
        self.args = []  # positional args of each call
        self._ret, self._raises = ret, raises

    def __call__(self, *a, **k):
        self.calls.append(k)
        self.args.append(a)
        if self._raises:
            raise self._raises("boom")
        return self._ret


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _write_model_valid_analysis(output_dir, **metadata):
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "analysis.json").write_text(
        json.dumps({"metadata": metadata}),
        encoding="utf-8",
    )


class _Base(unittest.TestCase):
    def tearDown(self):
        for n in _STUBBED:
            sys.modules.pop(n, None)


class TestAnalysis(_Base):
    def _install(self, run_full=None, run_incremental=None):
        class BaselineUnavailableError(Exception):
            pass

        class IncrementalCacheMissingError(Exception):
            pass

        analysis = _mod(
            "codeboarding_workflows.analysis",
            run_full=run_full or _Rec(),
            run_incremental=run_incremental or _Rec(),
            BaselineUnavailableError=BaselineUnavailableError,
        )
        pkg = _mod("codeboarding_workflows")
        pkg.analysis = analysis
        exc = _mod("diagram_analysis.exceptions", IncrementalCacheMissingError=IncrementalCacheMissingError)
        da = _mod("diagram_analysis")
        da.exceptions = exc
        engine_adapter.run_full = analysis.run_full
        engine_adapter.run_incremental = analysis.run_incremental
        engine_adapter.BaselineUnavailableError = BaselineUnavailableError
        engine_adapter.IncrementalCacheMissingError = IncrementalCacheMissingError
        return analysis, IncrementalCacheMissingError, BaselineUnavailableError

    def test_base_calls_run_full(self):
        rf = _Rec()
        self._install(run_full=rf)
        engine_adapter.run_base("/repo", "/out", "myrepo", "rid-base", 2, "abc123")
        self.assertEqual(len(rf.calls), 1)
        run_paths, run_context = rf.args[0]
        self.assertEqual(run_paths.project_name, "myrepo")
        self.assertEqual(str(run_paths.repo_path), "/repo")
        self.assertEqual(str(run_paths.output_dir), "/out")
        self.assertEqual(run_context.run_id, "rid-base")
        self.assertEqual(rf.calls[0]["depth_level"], 2)
        self.assertEqual(rf.calls[0]["source_sha"], "abc123")

    def test_main_parses_depth_as_int(self):
        rf = _Rec()
        self._install(run_full=rf)
        engine_adapter.main(
            [
                "base",
                "--repo",
                "/repo",
                "--out",
                "/out",
                "--name",
                "myrepo",
                "--run-id",
                "rid-base",
                "--depth",
                "2",
                "--source-sha",
                "abc123",
            ]
        )
        self.assertEqual(rf.calls[0]["depth_level"], 2)

    def test_main_enables_engine_console_logging(self):
        self._install()
        setup_logging = _Rec()
        with (
            patch.object(engine_adapter, "setup_logging", setup_logging),
            patch.dict(os.environ, {"CODEBOARDING_LOG_LEVEL": "DEBUG"}),
        ):
            engine_adapter.main(
                [
                    "base",
                    "--repo",
                    "/repo",
                    "--out",
                    "/out",
                    "--name",
                    "myrepo",
                    "--run-id",
                    "rid-base",
                    "--depth",
                    "2",
                    "--source-sha",
                    "abc123",
                ]
            )

        self.assertEqual(setup_logging.calls, [{"default_level": "DEBUG"}])

    def test_main_sets_github_action_source(self):
        rf = _Rec()
        self._install(run_full=rf)
        with patch.dict(os.environ, {}, clear=True):
            engine_adapter.main(
                [
                    "base",
                    "--repo",
                    "/repo",
                    "--out",
                    "/out",
                    "--name",
                    "myrepo",
                    "--run-id",
                    "rid-base",
                    "--depth",
                    "2",
                    "--source-sha",
                    "abc123",
                ]
            )
            self.assertEqual(os.environ["CODEBOARDING_SOURCE"], "github_action")

    def test_main_rejects_invalid_depth(self):
        # argparse enforces the structural range 1-10; the per-tier cap (free=3)
        # is applied later by the action/resolver, not here.
        for depth in ("0", "11", "x"):
            with self.subTest(depth=depth):
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit):
                        engine_adapter.main(
                            [
                                "base",
                                "--repo",
                                "/repo",
                                "--out",
                                "/out",
                                "--name",
                                "myrepo",
                                "--run-id",
                                "rid-base",
                                "--depth",
                                depth,
                                "--source-sha",
                                "abc123",
                            ]
                        )

    def test_main_accepts_depth_four(self):
        # The action's accepted depth ceiling is 4 so a committed depth-4 baseline
        # is a first-class value review can inherit (the engine has no depth cap).
        rf = _Rec()
        self._install(run_full=rf)
        engine_adapter.main(
            [
                "base",
                "--repo",
                "/repo",
                "--out",
                "/out",
                "--name",
                "myrepo",
                "--run-id",
                "rid-base",
                "--depth",
                "4",
                "--source-sha",
                "abc123",
            ]
        )
        self.assertEqual(rf.calls[0]["depth_level"], 4)

    def test_head_uses_incremental(self):
        ri, rf = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()
        _write_model_valid_analysis(out, depth_level=1)
        buf = StringIO()
        with redirect_stdout(buf):
            engine_adapter.run_head("/repo", out, "r", "rid", 1, "head")
        self.assertEqual(len(ri.calls), 1)
        self.assertEqual(len(rf.calls), 0)  # no fallback
        # Git-free: no base/target ref — Core diffs the seeded fingerprint itself.
        run_paths, run_context = ri.args[0]
        self.assertEqual(str(run_paths.repo_path), "/repo")
        self.assertEqual(str(run_paths.output_dir), out)
        self.assertEqual(run_context.run_id, "rid")
        self.assertIn("head_analysis_mode=incremental", buf.getvalue())

    def test_head_force_full_skips_incremental(self):
        ri, rf = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()
        (Path(out) / "stale.json").write_text("{}")
        buf = StringIO()

        with redirect_stdout(buf):
            engine_adapter.run_head("/repo", out, "r", "rid", 2, "head", force_full=True)

        self.assertEqual(len(ri.calls), 0)
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(rf.calls[0]["depth_level"], 2)
        self.assertEqual(rf.calls[0]["source_sha"], "head")
        self.assertFalse((Path(out) / "stale.json").exists())
        self.assertIn("head_analysis_mode=full", buf.getvalue())

    def test_head_falls_back_to_full_on_cache_miss(self):
        analysis, IncMiss, _ = self._install()  # install once so the exception class identity matches
        rf = _Rec()
        analysis.run_full = rf
        analysis.run_incremental = _Rec(raises=IncMiss)
        engine_adapter.run_full = analysis.run_full
        engine_adapter.run_incremental = analysis.run_incremental
        out = tempfile.mkdtemp()
        _write_model_valid_analysis(out, depth_level=3)
        (Path(out) / "stale.json").write_text("{}")  # must be wiped before the full run
        (Path(out) / "health").mkdir()
        (Path(out) / "health" / "stale.json").write_text("{}")
        buf = StringIO()
        with redirect_stdout(buf):
            engine_adapter.run_head("/repo", out, "r", "rid", 3, "head")
        self.assertEqual(len(rf.calls), 1)  # fell back to full
        self.assertEqual(rf.calls[0]["depth_level"], 3)
        self.assertFalse((Path(out) / "stale.json").exists())  # head dir wiped before full
        self.assertFalse((Path(out) / "health").exists())  # nested stale artifacts wiped too
        self.assertIn("head_analysis_mode=full", buf.getvalue())

    def test_head_falls_back_to_full_on_baseline_unavailable(self):
        analysis, _, BaseUnavail = self._install()  # the other warm-start failure must also fall back
        rf = _Rec()
        analysis.run_full = rf
        analysis.run_incremental = _Rec(raises=BaseUnavail)
        engine_adapter.run_full = analysis.run_full
        engine_adapter.run_incremental = analysis.run_incremental
        out = tempfile.mkdtemp()
        _write_model_valid_analysis(out, depth_level=1)
        engine_adapter.run_head("/repo", out, "r", "rid", 1, "head")
        self.assertEqual(len(rf.calls), 1)  # BaselineUnavailableError also triggers the full re-run

    def test_head_rebuilds_analysis_rejected_by_core_model(self):
        ri, rf = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        (out / "analysis.json").write_text(
            json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 3}}),
            encoding="utf-8",
        )
        (out / "stale.json").write_text("{}", encoding="utf-8")
        buf = StringIO()

        with patch.object(engine_adapter, "UnifiedAnalysisJson", _RejectingUnifiedAnalysisJson):
            with redirect_stdout(buf):
                engine_adapter.run_head("/repo", str(out), "r", "rid", 1, "head")

        self.assertEqual(len(ri.calls), 0)
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(rf.calls[0]["depth_level"], 3)
        self.assertFalse((out / "stale.json").exists())
        self.assertIn("could not load baseline analysis.json", buf.getvalue())
        self.assertIn("head_analysis_mode=full", buf.getvalue())


class TestValidateBase(_Base):
    def test_validate_base_accepts_matching_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "abc123"}}), encoding="utf-8")

            ok, message = engine_adapter.validate_base_analysis(path, "abc123")

            self.assertTrue(ok)
            self.assertIn("matches", message)

    def test_validate_base_accepts_mismatched_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "old"}}), encoding="utf-8")

            ok, message = engine_adapter.validate_base_analysis(path, "new")

            self.assertTrue(ok)
            self.assertIn("old", message)
            self.assertIn("new", message)

    def test_validate_base_accepts_docs_only_bot_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.name", "Test")
            self._git(repo, "config", "user.email", "test@example.com")
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            self._git(repo, "add", "app.py")
            self._git(repo, "commit", "-m", "base")
            base_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            (repo / ".codeboarding").mkdir()
            (repo / ".codeboarding" / "analysis.json").write_text(
                json.dumps({"metadata": {"commit_hash": base_sha}}),
                encoding="utf-8",
            )
            (repo / ".codeboarding" / "overview.md").write_text("overview\n", encoding="utf-8")
            (repo / "docs" / "development").mkdir(parents=True)
            (repo / "docs" / "development" / "architecture.md").write_text("overview\n", encoding="utf-8")
            self._git(repo, "add", ".codeboarding", "docs/development/architecture.md")
            self._git(repo, "commit", "-m", "docs bot")
            docs_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            cwd = os.getcwd()
            try:
                os.chdir(repo)
                ok, message = engine_adapter.validate_base_analysis(repo / ".codeboarding" / "analysis.json", docs_sha)
            finally:
                os.chdir(cwd)

            self.assertTrue(ok)
            self.assertIn("Using committed baseline", message)

    def test_validate_base_accepts_committed_baseline_even_after_code_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.name", "Test")
            self._git(repo, "config", "user.email", "test@example.com")
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            self._git(repo, "add", "app.py")
            self._git(repo, "commit", "-m", "base")
            base_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / ".codeboarding").mkdir()
            analysis_path = repo / ".codeboarding" / "analysis.json"
            analysis_path.write_text(json.dumps({"metadata": {"commit_hash": base_sha}}), encoding="utf-8")
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            self._git(repo, "add", "app.py", ".codeboarding/analysis.json")
            self._git(repo, "commit", "-m", "code change")
            code_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            cwd = os.getcwd()
            try:
                os.chdir(repo)
                ok, message = engine_adapter.validate_base_analysis(analysis_path, code_sha)
            finally:
                os.chdir(cwd)

            self.assertTrue(ok)
            self.assertIn("Using committed baseline", message)

    def _git(self, repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_validate_base_accepts_missing_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {}}), encoding="utf-8")

            ok, message = engine_adapter.validate_base_analysis(path, "abc123")

            self.assertTrue(ok)
            self.assertIn("commit_hash", message)

    def test_validate_base_rejects_lossy_model_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(
                json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 2}}),
                encoding="utf-8",
            )

            with patch.object(engine_adapter, "UnifiedAnalysisJson", _LossyUnifiedAnalysisJson):
                ok, message = engine_adapter.validate_base_analysis(path, "abc123")

            self.assertFalse(ok)
            self.assertIn("could not load baseline analysis.json", message)
            self.assertIn("without schema changes", message)
            self.assertIn("full analysis", message)

    def test_main_validate_base_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "abc123"}}), encoding="utf-8")

            self.assertEqual(
                engine_adapter.main(["validate-base", "--analysis", str(path), "--expected-sha", "abc123"]),
                0,
            )
            self.assertEqual(
                engine_adapter.main(["validate-base", "--analysis", str(path), "--expected-sha", "def456"]),
                0,
            )

    def test_validate_base_accepts_matching_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(
                json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 2}}),
                encoding="utf-8",
            )

            ok, message = engine_adapter.validate_base_analysis(path, "abc123", expected_depth=2)

            self.assertTrue(ok)
            self.assertIn("matches", message)

    def test_validate_base_rejects_deeper_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(
                json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 3}}),
                encoding="utf-8",
            )

            ok, message = engine_adapter.validate_base_analysis(path, "abc123", expected_depth=1)

            self.assertFalse(ok)
            self.assertIn("3", message)  # baseline depth
            self.assertIn("1", message)  # expected depth

    def test_validate_base_accepts_shallower_baseline(self):
        # The engine records the depth REACHED, not requested: a depth-2 run on
        # a repo that never expands persists depth_level 1. Rejecting it would
        # regenerate (computing 1 again) on every PR without converging.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(
                json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 1}}),
                encoding="utf-8",
            )

            ok, _ = engine_adapter.validate_base_analysis(path, "abc123", expected_depth=3)

            self.assertTrue(ok)

    def test_validate_base_depth_checked_on_drift_path(self):
        # The deeper-baseline rejection must also apply when the commit matched
        # only via the docs-only-drift allowance, not just on exact SHA match.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.name", "Test")
            self._git(repo, "config", "user.email", "test@example.com")
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            self._git(repo, "add", "app.py")
            self._git(repo, "commit", "-m", "base")
            base_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            (repo / ".codeboarding").mkdir()
            analysis_path = repo / ".codeboarding" / "analysis.json"
            analysis_path.write_text(
                json.dumps({"metadata": {"commit_hash": base_sha, "depth_level": 3}}),
                encoding="utf-8",
            )
            self._git(repo, "add", ".codeboarding")
            self._git(repo, "commit", "-m", "docs bot")
            docs_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            cwd = os.getcwd()
            try:
                os.chdir(repo)
                ok_drift, _ = engine_adapter.validate_base_analysis(analysis_path, docs_sha)
                ok_depth, message = engine_adapter.validate_base_analysis(analysis_path, docs_sha, expected_depth=1)
            finally:
                os.chdir(cwd)

            self.assertTrue(ok_drift)  # drift alone is accepted...
            self.assertFalse(ok_depth)  # ...but the depth check still applies
            self.assertIn("deeper", message)

    def test_validate_base_accepts_legacy_baseline_without_depth(self):
        # Missing or unparseable depth_level remains acceptable when the
        # installed Core model accepts the document.
        for metadata in (
            {"commit_hash": "abc123"},
            {"commit_hash": "abc123", "depth_level": "not-a-number"},
        ):
            with self.subTest(metadata=metadata):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "analysis.json"
                    path.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

                    ok, _ = engine_adapter.validate_base_analysis(path, "abc123", expected_depth=2)

                    self.assertTrue(ok)

    def test_validate_base_without_expected_depth_ignores_depth(self):
        # No --expected-depth -> behavior unchanged even when depth_level disagrees.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(
                json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 3}}),
                encoding="utf-8",
            )

            ok, message = engine_adapter.validate_base_analysis(path, "abc123")

            self.assertTrue(ok)
            self.assertIn("matches", message)

    def test_validate_base_accepts_depth_four_baseline(self):
        # The core fix: review inherits the committed baseline's depth, so a
        # depth-4 baseline validated at --expected-depth 4 is accepted (reused,
        # not regenerated). Validated at a shallower expected depth it is still
        # rejected (an explicit shallower depth_level input).
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(
                json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 4}}),
                encoding="utf-8",
            )

            ok_same, _ = engine_adapter.validate_base_analysis(path, "abc123", expected_depth=4)
            ok_shallower, message = engine_adapter.validate_base_analysis(path, "abc123", expected_depth=2)

            self.assertTrue(ok_same)
            self.assertFalse(ok_shallower)
            self.assertIn("deeper", message)

    def test_main_validate_base_expected_depth_exit_codes(self):
        # patch.dict: main() setdefaults CODEBOARDING_SOURCE; don't leak it.
        with patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(
                json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 2}}),
                encoding="utf-8",
            )

            self.assertEqual(
                engine_adapter.main(
                    ["validate-base", "--analysis", str(path), "--expected-sha", "abc123", "--expected-depth", "2"]
                ),
                0,
            )
            self.assertEqual(
                engine_adapter.main(
                    ["validate-base", "--analysis", str(path), "--expected-sha", "abc123", "--expected-depth", "1"]
                ),
                1,
            )
            # depth 4 is now an accepted value (against a depth-2 baseline a
            # shallower-or-equal expected depth passes the depth check).
            self.assertEqual(
                engine_adapter.main(
                    ["validate-base", "--analysis", str(path), "--expected-sha", "abc123", "--expected-depth", "4"]
                ),
                0,
            )
            with redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):  # depth outside 1-10 rejected by argparse
                    engine_adapter.main(
                        ["validate-base", "--analysis", str(path), "--expected-sha", "abc123", "--expected-depth", "11"]
                    )


class TestSeed(_Base):
    """run_seed must analyze, cluster, then save — in that order, same results object.

    The save-after-clustering order is the point of the subcommand: the engine
    persists a pkl on LSP teardown BEFORE clustering, and a pkl saved then has
    no cluster baseline, which is exactly the state that forces the head run
    into a full-analysis fallback.
    """

    def _install(self, fail_at=None):
        log = []
        results = object()

        def get_static_analysis(repo_path, cache_dir, skip_cache=False, source_sha=None):
            log.append(("analyze", str(repo_path), str(cache_dir), source_sha))
            if fail_at == "analyze":
                raise RuntimeError("boom")
            return results

        def build_all_cluster_results(res):
            log.append(("cluster", res))
            if fail_at == "cluster":
                raise RuntimeError("boom")
            return {"python": types.SimpleNamespace(clusters={1: {"a"}, 2: {"b"}})}

        class _Cache:
            def __init__(self, artifact_dir, repo_root):
                log.append(("cache_init", str(artifact_dir), str(repo_root)))

            def save(self, res, source_sha=None):
                log.append(("save", res, source_sha))

        sa = _mod("static_analyzer", get_static_analysis=get_static_analysis)
        sa.cluster_helpers = _mod(
            "static_analyzer.cluster_helpers", build_all_cluster_results=build_all_cluster_results
        )
        sa.analysis_cache = _mod("static_analyzer.analysis_cache", StaticAnalysisCache=_Cache)
        engine_adapter.get_static_analysis = get_static_analysis
        engine_adapter.build_all_cluster_results = build_all_cluster_results
        engine_adapter.StaticAnalysisCache = _Cache
        return log, results

    def test_seed_analyzes_clusters_then_saves(self):
        log, results = self._install()
        engine_adapter.run_seed("/repo", "/out", "abc123")
        self.assertEqual(
            log,
            [
                ("analyze", "/repo", "/out", "abc123"),
                ("cluster", results),
                ("cache_init", "/out", "/repo"),
                ("save", results, "abc123"),
            ],
        )

    def test_seed_propagates_engine_errors(self):
        # Fail-open lives in the action step; run_seed itself must not swallow.
        for stage in ("analyze", "cluster"):
            with self.subTest(stage=stage):
                log, _ = self._install(fail_at=stage)
                with self.assertRaises(RuntimeError):
                    engine_adapter.run_seed("/repo", "/out", "abc123")
                self.assertNotIn("save", [e[0] for e in log])
                self.tearDown()

    def test_main_seed_wires_args(self):
        log, _ = self._install()
        rc = engine_adapter.main(["seed", "--repo", "/r", "--out", "/o", "--source-sha", "s1"])
        self.assertEqual(rc, 0)
        self.assertEqual(log[0], ("analyze", "/r", "/o", "s1"))
        self.assertEqual(log[-1][0], "save")


class TestHealth(_Base):
    def _install_health(self, report):
        class Severity:
            WARNING, CRITICAL = "warning", "critical"

        class _Cache:
            def __init__(self, artifact_dir, repo_root):
                pass

            def get(self):
                return object()  # non-None static analysis

        _mod("health.models", Severity=Severity)
        _mod("health.runner", run_health_checks=lambda sa, repo_name, repo_path: report)
        _mod(
            "health",
        )
        _mod("static_analyzer.analysis_cache", StaticAnalysisCache=_Cache)
        _mod(
            "static_analyzer",
        )
        engine_adapter.Severity = Severity
        engine_adapter.run_health_checks = lambda sa, repo_name, repo_path: report
        engine_adapter.StaticAnalysisCache = _Cache
        return Severity

    def test_counts_warning_and_critical(self):
        Sev = self._install_health(report=None)

        class FG:
            def __init__(self, sev, n):
                self.severity, self.entities = sev, list(range(n))

        class CS:
            finding_groups = [FG(Sev.WARNING, 2), FG(Sev.CRITICAL, 1), FG("info", 5)]

        report = types.SimpleNamespace(check_summaries=[CS()])
        self._install_health(report=report)
        self.assertEqual(engine_adapter.run_health("/art", "/repo", "r"), 3)  # 2 warnings + 1 critical, info ignored

    def test_prefers_written_health_report(self):
        artifact_dir = Path(tempfile.mkdtemp())
        report_dir = artifact_dir / "health"
        report_dir.mkdir()
        (report_dir / "health_report.json").write_text(
            """
            {
              "check_summaries": [
                {"finding_groups": [
                  {"severity": "warning", "entities": [{}, {}]},
                  {"severity": "critical", "entities": [{}]},
                  {"severity": "info", "entities": [{}, {}, {}, {}, {}]}
                ]}
              ]
            }
            """,
            encoding="utf-8",
        )
        self.assertEqual(engine_adapter.run_health(str(artifact_dir), "/repo", "r"), 3)

    def test_malformed_health_report_falls_back(self):
        self._install_health(report=None)
        artifact_dir = Path(tempfile.mkdtemp())
        report_dir = artifact_dir / "health"
        report_dir.mkdir()
        (report_dir / "health_report.json").write_text("[]", encoding="utf-8")
        self.assertEqual(engine_adapter.run_health(str(artifact_dir), "/repo", "r"), 0)

    def test_missing_module_yields_zero(self):
        # Health failures are best-effort: return 0, never raise.
        class _BrokenCache:
            def __init__(self, *args, **kwargs):
                raise ImportError("missing health dependency")

        old_cache = engine_adapter.StaticAnalysisCache
        engine_adapter.StaticAnalysisCache = _BrokenCache
        try:
            self.assertEqual(engine_adapter.run_health("/art", "/repo", "r"), 0)
        finally:
            engine_adapter.StaticAnalysisCache = old_cache


class TestQuotaExhausted(_Base):
    def test_detects_402_status_attr(self):
        class APIErr(Exception):
            status_code = 402

        self.assertTrue(engine_adapter._is_quota_exhausted(APIErr("nope")))

    def test_detects_status_attr(self):
        class FunctionUrlErr(Exception):
            status = 402

        self.assertTrue(engine_adapter._is_quota_exhausted(FunctionUrlErr("nope")))

    def test_detects_marker_string(self):
        exc = RuntimeError("upstream said: Resource exhausted: token limit reached")
        self.assertTrue(engine_adapter._is_quota_exhausted(exc))

    def test_detects_in_cause_chain(self):
        inner = RuntimeError("Resource exhausted: token limit reached")
        try:
            raise ValueError("wrapped") from inner
        except ValueError as e:
            self.assertTrue(engine_adapter._is_quota_exhausted(e))

    def test_other_errors_not_flagged(self):
        self.assertFalse(engine_adapter._is_quota_exhausted(RuntimeError("disk full")))

        class OtherStatus(Exception):
            status_code = 500

        self.assertFalse(engine_adapter._is_quota_exhausted(OtherStatus("boom")))

    def _install_raising(self, exc):
        analysis = _mod(
            "codeboarding_workflows.analysis",
            run_full=_Rec(raises=exc),
            run_incremental=_Rec(),
            BaselineUnavailableError=type("BaselineUnavailableError", (Exception,), {}),
        )
        pkg = _mod("codeboarding_workflows")
        pkg.analysis = analysis
        excmod = _mod(
            "diagram_analysis.exceptions",
            IncrementalCacheMissingError=type("IncrementalCacheMissingError", (Exception,), {}),
        )
        da = _mod("diagram_analysis")
        da.exceptions = excmod
        engine_adapter.run_full = analysis.run_full
        engine_adapter.run_incremental = analysis.run_incremental
        engine_adapter.BaselineUnavailableError = analysis.BaselineUnavailableError
        engine_adapter.IncrementalCacheMissingError = excmod.IncrementalCacheMissingError

    def _run_base(self):
        return engine_adapter.main(
            [
                "base",
                "--repo",
                "/r",
                "--out",
                "/o",
                "--name",
                "n",
                "--run-id",
                "rid",
                "--depth",
                "2",
                "--source-sha",
                "abc123",
            ]
        )

    def test_main_drops_sentinel_on_quota_error(self):
        class APIErr(Exception):
            status_code = 402

        self._install_raising(APIErr)
        sentinel = Path(tempfile.mkdtemp()) / "cb-quota-exhausted"
        with patch.dict(os.environ, {"CB_QUOTA_SENTINEL": str(sentinel)}):
            with redirect_stderr(StringIO()):
                with self.assertRaises(APIErr):  # re-raised so the step still fails
                    self._run_base()
        self.assertTrue(sentinel.exists(), "quota sentinel should be written")

    def test_main_no_sentinel_on_other_error(self):
        self._install_raising(RuntimeError)
        sentinel = Path(tempfile.mkdtemp()) / "cb-quota-exhausted"
        with patch.dict(os.environ, {"CB_QUOTA_SENTINEL": str(sentinel)}):
            with redirect_stderr(StringIO()):
                with self.assertRaises(RuntimeError):
                    self._run_base()
        self.assertFalse(sentinel.exists(), "non-quota errors must not write the sentinel")


class TestEngineRequired(_Base):
    """A missing/too-old engine (RunPaths imported as None) fails the analysis
    subcommands with a clear message, while metadata-only subcommands still run."""

    def _argv(self, cmd):
        run = ["--repo", "/r", "--out", "/o", "--name", "n", "--run-id", "id", "--source-sha", "s"]
        return {
            "base": [cmd, *run, "--depth", "2"],
            "seed": [cmd, "--repo", "/r", "--out", "/o", "--source-sha", "s"],
            "head": [cmd, *run, "--depth", "2"],
            "validate-base": [cmd, "--analysis", "/a.json", "--expected-sha", "abc123"],
            "analyze": [cmd, *run, "--depth", "2"],
            "render": [cmd, "--analysis", "/a.json", "--out", "/o", "--repo-name", "n", "--repo-ref", "r"],
        }[cmd]

    def test_engine_commands_fail_clearly_when_engine_missing(self):
        for cmd in engine_adapter._ENGINE_COMMANDS:
            with (
                self.subTest(cmd=cmd),
                patch.object(engine_adapter, "RunPaths", None),
                patch.object(engine_adapter, "UnifiedAnalysisJson", None),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    engine_adapter.main(self._argv(cmd))
                msg = str(ctx.exception)
                self.assertIn(cmd, msg)
                self.assertIn("too old", msg)
                self.assertIn("codeboarding_version", msg)

    def test_metadata_command_runs_without_engine(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "abc1234"}}))
            with patch.object(engine_adapter, "RunPaths", None), redirect_stdout(StringIO()):
                rc = engine_adapter.main(["baseline-info", "--analysis", str(path)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
