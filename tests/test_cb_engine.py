"""Smoke tests for scripts/cb_engine.py — verify it calls the engine API correctly,
using stub modules so no real engine venv is needed."""

import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cb_engine  # noqa: E402

_STUBBED = [
    "codeboarding_workflows", "codeboarding_workflows.analysis",
    "diagram_analysis", "diagram_analysis.exceptions",
    "health", "health.models", "health.runner",
    "static_analyzer", "static_analyzer.analysis_cache",
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
