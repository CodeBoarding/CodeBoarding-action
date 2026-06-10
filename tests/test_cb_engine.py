"""Smoke tests for scripts/cb_engine.py — verify it calls the engine API correctly,
using stub modules so no real engine venv is needed."""

import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cb_engine  # noqa: E402

_STUBBED = [
    "codeboarding_workflows", "codeboarding_workflows.analysis",
    "diagram_analysis", "diagram_analysis.exceptions",
    "health", "health.models", "health.runner",
    "static_analyzer", "static_analyzer.analysis_cache",
    "static_analyzer.cluster_helpers",
]


class _Rec:
    def __init__(self, ret="OUT", raises=None):
        self.calls = []
        self._ret, self._raises = ret, raises

    def __call__(self, *a, **k):
        self.calls.append(k)
        if self._raises:
            raise self._raises("boom")
        return self._ret


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


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
        return analysis, IncrementalCacheMissingError, BaselineUnavailableError

    def test_base_calls_run_full(self):
        rf = _Rec()
        self._install(run_full=rf)
        cb_engine.run_base("/repo", "/out", "myrepo", "rid-base", 2, "abc123")
        self.assertEqual(len(rf.calls), 1)
        k = rf.calls[0]
        self.assertEqual(k["repo_name"], "myrepo")
        self.assertEqual(str(k["repo_path"]), "/repo")
        self.assertEqual(k["depth_level"], 2)
        self.assertEqual(k["source_sha"], "abc123")

    def test_main_parses_depth_as_int(self):
        rf = _Rec()
        self._install(run_full=rf)
        cb_engine.main([
            "base",
            "--repo", "/repo",
            "--out", "/out",
            "--name", "myrepo",
            "--run-id", "rid-base",
            "--depth", "2",
            "--source-sha", "abc123",
        ])
        self.assertEqual(rf.calls[0]["depth_level"], 2)

    def test_main_sets_github_action_source(self):
        rf = _Rec()
        self._install(run_full=rf)
        with patch.dict(os.environ, {}, clear=True):
            cb_engine.main([
                "base",
                "--repo", "/repo",
                "--out", "/out",
                "--name", "myrepo",
                "--run-id", "rid-base",
                "--depth", "2",
                "--source-sha", "abc123",
            ])
            self.assertEqual(os.environ["CODEBOARDING_SOURCE"], "github_action")

    def test_main_rejects_invalid_depth(self):
        for depth in ("0", "4", "x"):
            with self.subTest(depth=depth):
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit):
                        cb_engine.main([
                            "base",
                            "--repo", "/repo",
                            "--out", "/out",
                            "--name", "myrepo",
                            "--run-id", "rid-base",
                            "--depth", depth,
                            "--source-sha", "abc123",
                        ])

    def test_head_uses_incremental(self):
        ri, rf = _Rec(), _Rec()
        self._install(run_full=rf, run_incremental=ri)
        cb_engine.run_head("/repo", "/out", "r", "rid", 1, "base", "head", "head")
        self.assertEqual(len(ri.calls), 1)
        self.assertEqual(len(rf.calls), 0)  # no fallback
        self.assertEqual(ri.calls[0]["base_ref"], "base")
        self.assertEqual(ri.calls[0]["target_ref"], "head")

    def test_head_falls_back_to_full_on_cache_miss(self):
        analysis, IncMiss, _ = self._install()  # install once so the exception class identity matches
        rf = _Rec()
        analysis.run_full = rf
        analysis.run_incremental = _Rec(raises=IncMiss)
        out = tempfile.mkdtemp()
        (Path(out) / "stale.json").write_text("{}")  # must be wiped before the full run
        (Path(out) / "health").mkdir()
        (Path(out) / "health" / "stale.json").write_text("{}")
        cb_engine.run_head("/repo", out, "r", "rid", 3, "base", "head", "head")
        self.assertEqual(len(rf.calls), 1)  # fell back to full
        self.assertEqual(rf.calls[0]["depth_level"], 3)
        self.assertFalse((Path(out) / "stale.json").exists())  # head dir wiped before full
        self.assertFalse((Path(out) / "health").exists())  # nested stale artifacts wiped too

    def test_head_falls_back_to_full_on_baseline_unavailable(self):
        analysis, _, BaseUnavail = self._install()  # the other warm-start failure must also fall back
        rf = _Rec()
        analysis.run_full = rf
        analysis.run_incremental = _Rec(raises=BaseUnavail)
        cb_engine.run_head("/repo", tempfile.mkdtemp(), "r", "rid", 1, "base", "head", "head")
        self.assertEqual(len(rf.calls), 1)  # BaselineUnavailableError also triggers the full re-run


class TestValidateBase(_Base):
    def test_validate_base_accepts_matching_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "abc123"}}), encoding="utf-8")

            ok, message = cb_engine.validate_base_analysis(path, "abc123")

            self.assertTrue(ok)
            self.assertIn("matches", message)

    def test_validate_base_rejects_mismatched_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "old"}}), encoding="utf-8")

            ok, message = cb_engine.validate_base_analysis(path, "new")

            self.assertFalse(ok)
            self.assertIn("old", message)
            self.assertIn("new", message)

    def test_validate_base_rejects_missing_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {}}), encoding="utf-8")

            ok, message = cb_engine.validate_base_analysis(path, "abc123")

            self.assertFalse(ok)
            self.assertIn("commit_hash", message)

    def test_main_validate_base_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps({"metadata": {"commit_hash": "abc123"}}), encoding="utf-8")

            self.assertEqual(
                cb_engine.main(["validate-base", "--analysis", str(path), "--expected-sha", "abc123"]),
                0,
            )
            self.assertEqual(
                cb_engine.main(["validate-base", "--analysis", str(path), "--expected-sha", "def456"]),
                1,
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
        sa.cluster_helpers = _mod("static_analyzer.cluster_helpers", build_all_cluster_results=build_all_cluster_results)
        sa.analysis_cache = _mod("static_analyzer.analysis_cache", StaticAnalysisCache=_Cache)
        return log, results

    def test_seed_analyzes_clusters_then_saves(self):
        log, results = self._install()
        cb_engine.run_seed("/repo", "/out", "abc123")
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
                    cb_engine.run_seed("/repo", "/out", "abc123")
                self.assertNotIn("save", [e[0] for e in log])
                self.tearDown()

    def test_main_seed_wires_args(self):
        log, _ = self._install()
        rc = cb_engine.main(["seed", "--repo", "/r", "--out", "/o", "--source-sha", "s1"])
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
        _mod("health", )
        _mod("static_analyzer.analysis_cache", StaticAnalysisCache=_Cache)
        _mod("static_analyzer", )
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
        self.assertEqual(cb_engine.run_health("/art", "/repo", "r"), 3)  # 2 warnings + 1 critical, info ignored

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
        self.assertEqual(cb_engine.run_health(str(artifact_dir), "/repo", "r"), 3)

    def test_malformed_health_report_falls_back(self):
        artifact_dir = Path(tempfile.mkdtemp())
        report_dir = artifact_dir / "health"
        report_dir.mkdir()
        (report_dir / "health_report.json").write_text("[]", encoding="utf-8")
        self.assertEqual(cb_engine.run_health(str(artifact_dir), "/repo", "r"), 0)

    def test_missing_module_yields_zero(self):
        # No health.* modules installed -> import fails -> 0, never raises.
        self.assertEqual(cb_engine.run_health("/art", "/repo", "r"), 0)


if __name__ == "__main__":
    unittest.main()
