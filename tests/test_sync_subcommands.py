"""Smoke tests for the sync-mode subcommands of scripts/engine_adapter.py (analyze,
render, concat) with stubbed engine modules — ported from the standalone
docs-action's test_docs_engine.py. Seed tests are not ported: engine_adapter's seed
is byte-identical and already covered by tests/test_engine_adapter.py."""

import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


def _preload(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


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


analysis = _preload(
    "codeboarding_workflows.analysis",
    run_full=lambda **kwargs: "OUT",
    run_incremental=lambda **kwargs: "OUT",
    BaselineUnavailableError=_InitialBaselineUnavailableError,
)
pkg = _preload("codeboarding_workflows")
pkg.analysis = analysis
rendering = _preload("codeboarding_workflows.rendering", render_docs=lambda *args, **kwargs: None)
pkg.rendering = rendering
exc = _preload("diagram_analysis.exceptions", IncrementalCacheMissingError=_InitialIncrementalCacheMissingError)
da = _preload("diagram_analysis")
da.exceptions = exc
_preload("health.models", Severity=_InitialSeverity)
_preload("health.runner", run_health_checks=lambda *args, **kwargs: None)
_preload("health")
_preload("static_analyzer", get_static_analysis=lambda *args, **kwargs: {})
_preload("static_analyzer.analysis_cache", StaticAnalysisCache=_InitialStaticAnalysisCache)
_preload("static_analyzer.cluster_helpers", build_all_cluster_results=lambda *args, **kwargs: {})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import engine_adapter  # noqa: E402

_STUBBED = [
    "codeboarding_workflows",
    "codeboarding_workflows.analysis",
    "codeboarding_workflows.rendering",
    "diagram_analysis",
    "diagram_analysis.exceptions",
    "static_analyzer",
    "static_analyzer.analysis_cache",
    "static_analyzer.cluster_helpers",
]


class _Rec:
    def __init__(self, ret="OUT", raises=None):
        self.calls = []
        self._ret = ret
        self._raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises:
            raise self._raises("boom")
        return self._ret


def _mod(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _write_analysis(out, *, commit="base123", depth=2):
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    (path / "analysis.json").write_text(
        json.dumps({"metadata": {"commit_hash": commit, "depth_level": depth}}),
        encoding="utf-8",
    )


class _Base(unittest.TestCase):
    def tearDown(self):
        for name in _STUBBED:
            sys.modules.pop(name, None)


class TestAnalyze(_Base):
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

    def test_no_baseline_runs_full(self):
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()

        mode = engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 2)

        self.assertEqual(mode, "full")
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(len(ri.calls), 0)
        kwargs = rf.calls[0][1]
        self.assertEqual(kwargs["repo_name"], "myrepo")
        self.assertEqual(str(kwargs["repo_path"]), "/repo")
        self.assertEqual(kwargs["depth_level"], 2)
        self.assertEqual(kwargs["source_sha"], "head123")

    def test_incremental_uses_metadata_commit_as_base_ref(self):
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()
        _write_analysis(out, commit="metadata-base", depth=2)

        mode = engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 2)

        self.assertEqual(mode, "incremental")
        self.assertEqual(len(rf.calls), 0)
        self.assertEqual(len(ri.calls), 1)
        kwargs = ri.calls[0][1]
        self.assertEqual(kwargs["base_ref"], "metadata-base")
        self.assertEqual(kwargs["target_ref"], "head123")
        self.assertEqual(kwargs["source_sha"], "head123")

    def test_deeper_baseline_still_runs_incremental(self):
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        _write_analysis(out, commit="metadata-base", depth=3)
        (out / "stale.json").write_text("{}", encoding="utf-8")
        (out / "health").mkdir()
        (out / "health" / "stale.json").write_text("{}", encoding="utf-8")

        mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 2)

        self.assertEqual(mode, "incremental")
        self.assertEqual(len(rf.calls), 0)
        self.assertEqual(len(ri.calls), 1)
        self.assertTrue((out / "stale.json").exists())
        self.assertTrue((out / "health").exists())

    def test_shallower_baseline_runs_incremental(self):
        # The engine records the depth REACHED, not requested: a depth-2 push on
        # a repo that never expands keeps writing depth_level 1, so a strict !=
        # gate would run full on every push forever.
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()
        _write_analysis(out, commit="metadata-base", depth=1)

        mode = engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 2)

        self.assertEqual(mode, "incremental")
        self.assertEqual(len(rf.calls), 0)
        self.assertEqual(len(ri.calls), 1)

    def test_missing_depth_in_baseline_runs_full_at_default_depth(self):
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        out.joinpath("analysis.json").write_text(
            json.dumps({"metadata": {"commit_hash": "metadata-base"}}), encoding="utf-8"
        )

        mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 3)

        self.assertEqual(mode, "full")
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(len(ri.calls), 0)
        self.assertEqual(rf.calls[0][1]["depth_level"], 2)

    def test_missing_commit_in_baseline_runs_full_at_baseline_depth(self):
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        out.joinpath("analysis.json").write_text(json.dumps({"metadata": {"depth_level": 3}}), encoding="utf-8")

        mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 1)

        self.assertEqual(mode, "full")
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(len(ri.calls), 0)
        self.assertEqual(rf.calls[0][1]["depth_level"], 3)

    def test_falls_back_to_full_on_cache_miss(self):
        analysis, IncMiss, _ = self._install()
        rf = _Rec()
        analysis.run_full = rf
        analysis.run_incremental = _Rec(raises=IncMiss)
        engine_adapter.run_full = analysis.run_full
        engine_adapter.run_incremental = analysis.run_incremental
        out = Path(tempfile.mkdtemp())
        _write_analysis(out, commit="metadata-base", depth=3)
        (out / "stale.json").write_text("{}", encoding="utf-8")

        mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 1)

        self.assertEqual(mode, "full")
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(rf.calls[0][1]["depth_level"], 3)
        self.assertFalse((out / "stale.json").exists())

    def test_falls_back_to_full_on_baseline_unavailable(self):
        analysis, _, BaseUnavailable = self._install()
        rf = _Rec()
        analysis.run_full = rf
        analysis.run_incremental = _Rec(raises=BaseUnavailable)
        engine_adapter.run_full = analysis.run_full
        engine_adapter.run_incremental = analysis.run_incremental
        out = tempfile.mkdtemp()
        _write_analysis(out, commit="metadata-base", depth=2)

        mode = engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 1)

        self.assertEqual(mode, "full")
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(rf.calls[0][1]["depth_level"], 2)

    def _markers(self, buf):
        return [line for line in buf.getvalue().splitlines() if line.startswith("analysis_mode=")]

    def test_stdout_marker_full_printed_exactly_once(self):
        # The action reads the mode from stdout (tee + sed 's/^analysis_mode=//p');
        # main() discards run_analyze's return value, so the print IS the interface.
        self._install()
        buf = StringIO()
        with redirect_stdout(buf):
            engine_adapter.run_analyze("/repo", tempfile.mkdtemp(), "myrepo", "rid", "head123", 2)
        self.assertEqual(self._markers(buf), ["analysis_mode=full"])

    def test_stdout_marker_incremental_printed_exactly_once(self):
        self._install()
        out = tempfile.mkdtemp()
        _write_analysis(out, commit="metadata-base", depth=2)
        buf = StringIO()
        with redirect_stdout(buf):
            engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 2)
        self.assertEqual(self._markers(buf), ["analysis_mode=incremental"])

    def test_stdout_marker_fallback_prints_full_exactly_once(self):
        analysis, IncMiss, _ = self._install()
        analysis.run_full = _Rec()
        analysis.run_incremental = _Rec(raises=IncMiss)
        engine_adapter.run_full = analysis.run_full
        engine_adapter.run_incremental = analysis.run_incremental
        out = tempfile.mkdtemp()
        _write_analysis(out, commit="metadata-base", depth=2)
        buf = StringIO()
        with redirect_stdout(buf):
            engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 2)
        self.assertEqual(self._markers(buf), ["analysis_mode=full"])

    def test_force_full_ignores_valid_baseline(self):
        # force_full must run a full analysis even when a reusable baseline is
        # present (the escape hatch that replaces refresh-baseline.yml).
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()
        _write_analysis(out, commit="metadata-base", depth=2)  # a perfectly reusable baseline
        buf = StringIO()
        with redirect_stdout(buf):
            mode = engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 2, force_full=True)
        self.assertEqual(mode, "full")
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(len(ri.calls), 0)  # baseline never consulted
        self.assertEqual(self._markers(buf), ["analysis_mode=full"])

    def test_main_force_full_flag_wires_through(self):
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()
        _write_analysis(out, commit="metadata-base", depth=2)
        with patch.dict(os.environ, {}, clear=True):
            engine_adapter.main(
                [
                    "analyze",
                    "--repo",
                    "/r",
                    "--out",
                    out,
                    "--name",
                    "n",
                    "--run-id",
                    "rid",
                    "--source-sha",
                    "head123",
                    "--depth",
                    "2",
                    "--force-full",
                ]
            )
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(len(ri.calls), 0)

    def test_main_parses_depth_as_int_and_sets_sync_source(self):
        rf = _Rec()
        self._install(run_full=rf)
        with patch.dict(os.environ, {}, clear=True):
            engine_adapter.main(
                [
                    "analyze",
                    "--repo",
                    "/repo",
                    "--out",
                    tempfile.mkdtemp(),
                    "--name",
                    "myrepo",
                    "--run-id",
                    "rid",
                    "--source-sha",
                    "head123",
                    "--depth",
                    "2",
                ]
            )
            self.assertEqual(rf.calls[0][1]["depth_level"], 2)
            self.assertEqual(os.environ["CODEBOARDING_SOURCE"], "sync")

    def test_main_rejects_invalid_depth(self):
        for depth in ("0", "4", "x"):
            with self.subTest(depth=depth):
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit):
                        engine_adapter.main(
                            [
                                "analyze",
                                "--repo",
                                "/repo",
                                "--out",
                                "/out",
                                "--name",
                                "myrepo",
                                "--run-id",
                                "rid",
                                "--source-sha",
                                "head123",
                                "--depth",
                                depth,
                            ]
                        )


class TestRenderAndConcat(_Base):
    def _install_rendering(self, render_docs=None):
        rec = render_docs or _Rec()
        rendering = _mod("codeboarding_workflows.rendering", render_docs=rec)
        pkg = _mod("codeboarding_workflows")
        pkg.rendering = rendering
        engine_adapter.render_docs = rec
        return rec

    def test_render_calls_engine_with_overview_root(self):
        rec = self._install_rendering()

        engine_adapter.run_render(
            "/tmp/analysis.json", "/tmp/docs", "repo", "https://example/repo/.codeboarding", ".md"
        )

        args, kwargs = rec.calls[0]
        self.assertEqual(str(args[0]), "/tmp/analysis.json")
        self.assertEqual(kwargs["repo_name"], "repo")
        self.assertEqual(kwargs["repo_ref"], "https://example/repo/.codeboarding")
        self.assertEqual(str(kwargs["temp_dir"]), "/tmp/docs")
        self.assertEqual(kwargs["format"], ".md")
        self.assertEqual(kwargs["root_name"], "overview")

    def test_concat_orders_overview_first_then_sorted_markdown(self):
        docs_dir = Path(tempfile.mkdtemp())
        (docs_dir / "z_component.md").write_text("z", encoding="utf-8")
        (docs_dir / "overview.md").write_text("overview", encoding="utf-8")
        (docs_dir / "a_component.md").write_text("a", encoding="utf-8")
        (docs_dir / "notes.txt").write_text("ignored", encoding="utf-8")
        out = Path(tempfile.mkdtemp()) / "docs" / "development" / "architecture.md"

        engine_adapter.run_concat(str(docs_dir), str(out))

        self.assertEqual(out.read_text(encoding="utf-8"), "overview\n\na\n\nz\n")


class TestSourceDispatch(_Base):
    """CODEBOARDING_SOURCE is setdefault'ed after argparse: sync for
    analyze/render/concat, github_action for everything else (base/seed/head/
    health/validate-base — base is asserted in test_engine_adapter.py)."""

    def test_main_render_sets_sync_source(self):
        rec = _Rec()
        rendering = _mod("codeboarding_workflows.rendering", render_docs=rec)
        pkg = _mod("codeboarding_workflows")
        pkg.rendering = rendering
        engine_adapter.render_docs = rec
        with patch.dict(os.environ, {}, clear=True):
            rc = engine_adapter.main(
                [
                    "render",
                    "--analysis",
                    "/tmp/analysis.json",
                    "--out",
                    tempfile.mkdtemp(),
                    "--repo-name",
                    "repo",
                    "--repo-ref",
                    "ref",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(rec.calls[0][1]["format"], ".md")  # default --format
            self.assertEqual(os.environ["CODEBOARDING_SOURCE"], "sync")

    def test_main_concat_sets_sync_source(self):
        docs_dir = Path(tempfile.mkdtemp())
        (docs_dir / "overview.md").write_text("overview", encoding="utf-8")
        out = Path(tempfile.mkdtemp()) / "architecture.md"
        with patch.dict(os.environ, {}, clear=True):
            rc = engine_adapter.main(["concat", "--docs-dir", str(docs_dir), "--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual(os.environ["CODEBOARDING_SOURCE"], "sync")

    def test_main_validate_base_keeps_github_action_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "abc123"}}), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                engine_adapter.main(["validate-base", "--analysis", str(path), "--expected-sha", "abc123"])
                self.assertEqual(os.environ["CODEBOARDING_SOURCE"], "github_action")

    def test_main_does_not_override_existing_source(self):
        docs_dir = Path(tempfile.mkdtemp())
        (docs_dir / "overview.md").write_text("overview", encoding="utf-8")
        out = Path(tempfile.mkdtemp()) / "architecture.md"
        with patch.dict(os.environ, {"CODEBOARDING_SOURCE": "custom"}, clear=True):
            engine_adapter.main(["concat", "--docs-dir", str(docs_dir), "--out", str(out)])
            self.assertEqual(os.environ["CODEBOARDING_SOURCE"], "custom")


class TestBaselineInfo(_Base):
    """baseline-info replaces the sync_seed step's inline heredoc: it returns the
    committed baseline's commit_hash only when present and SHA-shaped."""

    def _write(self, metadata):
        out = Path(tempfile.mkdtemp())
        (out / "analysis.json").write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
        return out / "analysis.json"

    def test_returns_sha_shaped_commit(self):
        path = self._write({"commit_hash": "a1b2c3d4e5f6"})
        self.assertEqual(engine_adapter.baseline_info(path), "a1b2c3d4e5f6")

    def test_rejects_non_sha_commit(self):
        # A non-SHA value must not flow into GITHUB_OUTPUT / cache keys / git.
        for bad in ("not-a-sha", "abc\ncb_dir=/evil", "ABC123", "", "12345"):  # too short / wrong charset / injection
            with self.subTest(commit=bad):
                self.assertEqual(engine_adapter.baseline_info(self._write({"commit_hash": bad})), "")

    def test_missing_metadata_or_file(self):
        self.assertEqual(engine_adapter.baseline_info(self._write({})), "")
        self.assertEqual(engine_adapter.baseline_info(Path(tempfile.mkdtemp()) / "absent.json"), "")

    def test_main_prints_commit_hash_line(self):
        path = self._write({"commit_hash": "deadbeef1234"})
        buf = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            rc = engine_adapter.main(["baseline-info", "--analysis", str(path)])
        self.assertEqual(rc, 0)
        self.assertIn("commit_hash=deadbeef1234", buf.getvalue())

    def test_main_prints_empty_for_bad_baseline(self):
        path = self._write({"commit_hash": "nope"})
        buf = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            engine_adapter.main(["baseline-info", "--analysis", str(path)])
        self.assertIn("commit_hash=", buf.getvalue())
        self.assertNotIn("nope", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
