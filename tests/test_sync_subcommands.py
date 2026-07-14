"""Smoke tests for the sync-mode subcommands of scripts/engine_adapter.py (analyze,
render, concat) with stubbed engine modules — ported from the standalone
docs-action's test_docs_engine.py. Seed tests are not ported: engine_adapter's seed
is byte-identical and already covered by tests/test_engine_adapter.py."""

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
rendering = _preload("codeboarding_workflows.rendering", render_docs=lambda *args, **kwargs: None)
pkg.rendering = rendering
exc = _preload("diagram_analysis.exceptions", IncrementalCacheMissingError=_InitialIncrementalCacheMissingError)
da = _preload("diagram_analysis", RunPaths=_RunPaths, RunContext=_RunContext)
da.exceptions = exc
_preload("diagram_analysis.analysis_json", UnifiedAnalysisJson=_InitialUnifiedAnalysisJson)
_preload("diagram_analysis.io_utils", write_fingerprint=lambda *a, **k: None)
_preload("agents.content_hash", hash_repo_source_files=lambda *a, **k: {})
_preload("agents")
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
    "codeboarding_workflows.rendering",
    "diagram_analysis",
    "diagram_analysis.analysis_json",
    "diagram_analysis.exceptions",
    "diagram_analysis.io_utils",
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
        run_paths, run_context = rf.calls[0][0]
        self.assertEqual(run_paths.project_name, "myrepo")
        self.assertEqual(str(run_paths.repo_path), "/repo")
        self.assertEqual(rf.calls[0][1]["depth_level"], 2)
        self.assertEqual(rf.calls[0][1]["source_sha"], "head123")

    def test_committed_baseline_runs_incremental(self):
        # Git-free: a committed analysis.json is the baseline; incremental runs
        # (Core diffs the committed fingerprint itself), with no commit_hash gate.
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = tempfile.mkdtemp()
        _write_analysis(out, depth=2)

        mode = engine_adapter.run_analyze("/repo", out, "myrepo", "rid", "head123", 2)

        self.assertEqual(mode, "incremental")
        self.assertEqual(len(rf.calls), 0)
        self.assertEqual(len(ri.calls), 1)
        run_paths, run_context = ri.calls[0][0]
        self.assertEqual(str(run_paths.repo_path), "/repo")
        self.assertEqual(run_context.run_id, "rid")

    def test_incompatible_baseline_runs_full_at_baseline_depth(self):
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        _write_analysis(out, depth=3)
        (out / "stale.json").write_text("{}", encoding="utf-8")
        buf = StringIO()

        with patch.object(engine_adapter, "UnifiedAnalysisJson", _LossyUnifiedAnalysisJson):
            with redirect_stdout(buf):
                mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 1)

        self.assertEqual(mode, "full")
        self.assertEqual(len(ri.calls), 0)
        self.assertEqual(len(rf.calls), 1)
        self.assertEqual(rf.calls[0][1]["depth_level"], 3)
        self.assertFalse((out / "stale.json").exists())
        self.assertIn("could not load baseline analysis.json", buf.getvalue())
        self.assertEqual(self._markers(buf), ["analysis_mode=full"])

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

    def test_deep_baseline_runs_incremental_regardless_of_tier(self):
        # A committed depth-7 baseline still runs incremental (the depth value
        # doesn't gate incremental — baseline presence does); on the free tier the
        # depth is clamped for any eventual run, but incremental is unaffected.
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        _write_analysis(out, commit="metadata-base", depth=7)

        mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 2)

        self.assertEqual(mode, "incremental")
        self.assertEqual(len(rf.calls), 0)
        self.assertEqual(len(ri.calls), 1)

    def test_over_cap_depth_clamped_on_forced_full(self):
        # When a full run happens (here: force_full), the requested depth is
        # clamped to the tier ceiling: free clamps 7 -> 3, licensed keeps 7.
        for licensed, expected in ((False, 3), (True, 7)):
            with self.subTest(licensed=licensed):
                rf, ri = _Rec(), _Rec()
                self._install(run_full=rf, run_incremental=ri)
                out = Path(tempfile.mkdtemp())
                _write_analysis(out, depth=2)

                mode = engine_adapter.run_analyze(
                    "/repo", str(out), "myrepo", "rid", "head123", 7, force_full=True, licensed=licensed
                )

                self.assertEqual(mode, "full")
                self.assertEqual(rf.calls[0][1]["depth_level"], expected)

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

    def test_missing_depth_still_runs_incremental(self):
        # A missing/unparseable depth_level is not a reason to force a full: the
        # baseline (analysis.json) is present, so incremental runs; the depth
        # resolves to a default and Core falls back to full itself if the cache
        # is actually absent.
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        out.joinpath("analysis.json").write_text(json.dumps({"metadata": {}}), encoding="utf-8")

        mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 3)

        self.assertEqual(mode, "incremental")
        self.assertEqual(len(rf.calls), 0)
        self.assertEqual(len(ri.calls), 1)

    def test_baseline_without_commit_still_runs_incremental(self):
        # commit_hash is gone from #401 metadata, so its absence no longer forces
        # a full rebuild — a present analysis.json runs incremental git-free.
        rf, ri = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        out = Path(tempfile.mkdtemp())
        out.joinpath("analysis.json").write_text(json.dumps({"metadata": {"depth_level": 3}}), encoding="utf-8")

        mode = engine_adapter.run_analyze("/repo", str(out), "myrepo", "rid", "head123", 1)

        self.assertEqual(mode, "incremental")
        self.assertEqual(len(ri.calls), 1)
        self.assertEqual(len(rf.calls), 0)

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
        # argparse enforces the structural range 1-10; the per-tier cap is applied
        # later by the action/resolver, not here.
        for depth in ("0", "11", "x"):
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


class TestBaselineDepth(_Base):
    """baseline-depth lets review inherit the committed baseline's depth_level
    (clamped to the tier ceiling) so the PR head is analyzed at the same depth as
    the base it is diffed against. It returns a usable number for any present
    baseline, and None only when there is no baseline at all (cold start)."""

    def _write(self, metadata):
        out = Path(tempfile.mkdtemp())
        (out / "analysis.json").write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
        return out / "analysis.json"

    def test_in_range_passes_through(self):
        for depth in (1, 2, 3):  # within the free cap
            with self.subTest(depth=depth):
                self.assertEqual(engine_adapter.baseline_depth(self._write({"depth_level": depth}), False), depth)

    def test_clamps_over_cap_per_tier(self):
        # depth 4-10 exceed the free cap (3) -> clamp to 3; licensed cap is 10.
        self.assertEqual(engine_adapter.baseline_depth(self._write({"depth_level": 7}), False), 3)
        self.assertEqual(engine_adapter.baseline_depth(self._write({"depth_level": 7}), True), 7)
        self.assertEqual(engine_adapter.baseline_depth(self._write({"depth_level": 4}), False), 3)
        self.assertEqual(engine_adapter.baseline_depth(self._write({"depth_level": 4}), True), 4)
        self.assertEqual(engine_adapter.baseline_depth(self._write({"depth_level": 99}), True), 10)

    def test_invalid_depth_uses_default(self):
        # Every spec violation that isn't an over-cap clamp falls back to the
        # default depth (2): a non-positive depth, an unparseable value, or a
        # missing depth_level are all handled the same way.
        for metadata in (
            {"depth_level": 0},
            {"depth_level": -3},
            {"depth_level": "x"},
            {"commit_hash": "deadbeef1234"},
        ):
            with self.subTest(metadata=metadata):
                self.assertEqual(engine_adapter.baseline_depth(self._write(metadata), False), 2)

    def test_none_only_when_no_baseline(self):
        # No file, or an empty/no-metadata object -> cold start (caller defaults).
        self.assertIsNone(engine_adapter.baseline_depth(Path(tempfile.mkdtemp()) / "absent.json", False))
        self.assertIsNone(engine_adapter.baseline_depth(self._write({}), False))

    def test_main_prints_depth_line(self):
        path = self._write({"depth_level": 3})
        buf = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            rc = engine_adapter.main(["baseline-depth", "--analysis", str(path)])
        self.assertEqual(rc, 0)
        self.assertIn("depth_level=3", buf.getvalue())

    def test_main_licensed_raises_ceiling(self):
        path = self._write({"depth_level": 7})
        free, lic = StringIO(), StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(free):
            engine_adapter.main(["baseline-depth", "--analysis", str(path)])
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(lic):
            engine_adapter.main(["baseline-depth", "--analysis", str(path), "--licensed"])
        self.assertIn("depth_level=3", free.getvalue())  # clamped
        self.assertIn("depth_level=7", lic.getvalue())  # within licensed cap

    def test_main_prints_empty_for_no_baseline(self):
        buf = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            engine_adapter.main(["baseline-depth", "--analysis", str(Path(tempfile.mkdtemp()) / "absent.json")])
        self.assertIn("depth_level=", buf.getvalue())
        self.assertNotIn("depth_level=None", buf.getvalue())

    def test_diagnostics_go_to_stderr_not_stdout(self):
        # Clamp/default messages must not pollute the machine-readable stdout line.
        path = self._write({"depth_level": 7})
        adapter = Path(__file__).resolve().parent.parent / "scripts" / "engine_adapter.py"
        result = subprocess.run(
            [sys.executable, str(adapter), "baseline-depth", "--analysis", str(path)],
            capture_output=True,
            text=True,
            cwd=tempfile.mkdtemp(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "depth_level=3")  # stdout is JUST the value
        self.assertIn("clamping to 3", result.stderr)  # the log is on stderr

    def test_runs_without_engine_installed(self):
        # The action calls baseline-depth BEFORE the engine package is installed,
        # so it must work as a subprocess with no engine modules on sys.path.
        path = self._write({"depth_level": 3})
        adapter = Path(__file__).resolve().parent.parent / "scripts" / "engine_adapter.py"
        result = subprocess.run(
            [sys.executable, str(adapter), "baseline-depth", "--analysis", str(path)],
            capture_output=True,
            text=True,
            cwd=tempfile.mkdtemp(),  # not the repo: no stub engine modules importable
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("depth_level=3", result.stdout)


if __name__ == "__main__":
    unittest.main()
