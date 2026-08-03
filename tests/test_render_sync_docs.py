"""Smoke tests for scripts/render_sync_docs.py."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

stub_pkg = ModuleType("codeboarding_workflows")
stub_rendering = ModuleType("codeboarding_workflows.rendering")
stub_pkg.rendering = stub_rendering
sys.modules["codeboarding_workflows"] = stub_pkg
sys.modules["codeboarding_workflows.rendering"] = stub_rendering

import render_sync_docs as rsd  # noqa: E402


class RenderSyncDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.render_calls = []

    def _make_fake_render(self, with_overview: bool = True):
        def _render(analysis, repo_name, repo_ref, temp_dir, format=".md", root_name="overview"):
            self.render_calls.append((analysis, repo_name, repo_ref, temp_dir, format, root_name))
            out = Path(temp_dir)
            out.mkdir(parents=True, exist_ok=True)
            if with_overview:
                (out / "overview.md").write_text("# Overview\n", encoding="utf-8")
            (out / "api.md").write_text("# API\n", encoding="utf-8")
            (out / "zeta.md").write_text("# Zeta\n", encoding="utf-8")
        return _render

    def test_concat_prefers_overview_first_and_appends_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = root / "analysis.json"
            analysis.write_text("{}", encoding="utf-8")
            output = root / "docs"
            architecture = root / "architecture.md"
            with patch.object(rsd, "render_docs", new=self._make_fake_render(True)):
                rsd.main([
                    "--analysis",
                    str(analysis),
                    "--output-dir",
                    str(output),
                    "--repo-name",
                    "org/repo",
                    "--repo-ref",
                    "abc123",
                    "--format",
                    ".md",
                    "--architecture-file",
                    str(architecture),
                ])
            result = architecture.read_text(encoding="utf-8")
            self.assertIn("# Overview", result)
            self.assertIn("# API", result)
            self.assertIn("# Zeta", result)
            self.assertLess(result.index("# Overview"), result.index("# API"))
            self.assertLess(result.index("# API"), result.index("# Zeta"))
            self.assertEqual(len(self.render_calls), 1)

    def test_missing_overview_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = root / "analysis.json"
            analysis.write_text("{}", encoding="utf-8")
            output = root / "docs"
            architecture = root / "architecture.md"
            with patch.object(rsd, "render_docs", new=self._make_fake_render(False)):
                with self.assertRaises(SystemExit):
                    rsd.main([
                        "--analysis",
                        str(analysis),
                        "--output-dir",
                        str(output),
                        "--repo-name",
                        "org/repo",
                        "--repo-ref",
                        "abc123",
                        "--format",
                        ".md",
                        "--architecture-file",
                        str(architecture),
                    ])


if __name__ == "__main__":
    unittest.main()
