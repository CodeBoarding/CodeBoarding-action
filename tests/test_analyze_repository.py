"""Smoke tests for scripts/analyze_repository.py — JSON contract parsing and mode dispatch."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analyze_repository as ar


class AnalyzeRepositoryTests(unittest.TestCase):
    def _analysis_json(self, base: Path) -> Path:
        path = base / "analysis.json"
        path.write_text(json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 2}}, encoding="utf-8") )
        return path

    def test_parse_cli_response_accepts_contract_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "analysis.json"
            out.write_text("ok", encoding="utf-8")
            payload = json.dumps({"analysis_path": "analysis.json", "requiresFullAnalysis": True})
            requires_full, path, _ = ar._parse_cli_response(payload, str(root))
            self.assertTrue(requires_full)
            self.assertEqual(path, out)

    def test_parse_cli_response_rejects_invalid_bool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"analysis_path": "analysis.json", "requiresFullAnalysis": "maybe"})
            root = Path(tmp)
            (root / "analysis.json").write_text("x", encoding="utf-8")
            with self.assertRaises(ar.AnalysisError):
                ar._parse_cli_response(payload, str(root))

    def test_parse_main_incremental_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            out_dir = root / "out"
            checkout.mkdir()
            out_dir.mkdir()
            analysis_path = out_dir / "analysis.json"
            analysis_path.write_text("ok", encoding="utf-8")

            stdout = io.StringIO()
            with unittest.mock.patch("sys.stdout", stdout):
                with patch.object(
                    ar,
                    "_run_command",
                    return_value=json.dumps(
                        {
                            "analysis_path": str(analysis_path.relative_to(root)),
                            "requiresFullAnalysis": False,
                        }
                    ),
                ) as _mock:
                    ar.main(["incremental", "--checkout", str(checkout), "--output-dir", str(out_dir)])
            lines = dict(line.split("=", 1) for line in stdout.getvalue().splitlines() if "=" in line)
            self.assertEqual(lines.get("analysis_mode"), "incremental")
            self.assertEqual(lines.get("requires_full_analysis"), "false")
            self.assertEqual(lines.get("analysis_path"), str(analysis_path))

    def test_main_full_fails_without_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            out_dir = root / "out"
            checkout.mkdir()
            out_dir.mkdir()
            with self.assertRaises(SystemExit):
                ar.main(["full", "--checkout", str(checkout), "--output-dir", str(out_dir)])

    def test_main_rejects_bad_cli_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            out_dir = root / "out"
            checkout.mkdir()
            out_dir.mkdir()
            with self.assertRaises(SystemExit):
                with patch.object(ar, "_run_command", return_value="not-json"):
                    ar.main(["incremental", "--checkout", str(checkout), "--output-dir", str(out_dir)])


if __name__ == "__main__":
    unittest.main()
