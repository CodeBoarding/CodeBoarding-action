"""Smoke tests for scripts/analyze_repository.py — JSON contract parsing and mode dispatch."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_repository as ar


class AnalyzeRepositoryTests(unittest.TestCase):
    def _analysis_json(self, base: Path) -> Path:
        path = base / "analysis.json"
        path.write_text(
            json.dumps({"metadata": {"commit_hash": "abc123", "depth_level": 2}}),
            encoding="utf-8",
        )
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

    def test_parse_cli_response_accepts_full_fallback_without_analysis_path(self) -> None:
        payload = json.dumps({"error": "baseline unavailable", "requiresFullAnalysis": True})
        requires_full, path, _ = ar._parse_cli_response(payload, "/tmp/output")
        self.assertTrue(requires_full)
        self.assertIsNone(path)

    def test_parse_cli_response_accepts_logs_before_json(self) -> None:
        raw = "Analyzing repository...\n" + json.dumps(
            {"error": "baseline unavailable", "requiresFullAnalysis": True}, indent=2
        )
        requires_full, path, _ = ar._parse_cli_response(raw, "/tmp/output")
        self.assertTrue(requires_full)
        self.assertIsNone(path)

    def test_run_command_streams_stdout_to_action_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            command = [
                sys.executable,
                "-c",
                "print('Analyzing repository...'); print('{\"requiresFullAnalysis\": true}')",
            ]

            with patch("sys.stderr", stderr):
                stdout = ar._run_command(command, Path(tmp) / "out")

            self.assertIn("Analyzing repository...", stderr.getvalue())
            self.assertIn('{"requiresFullAnalysis": true}', stderr.getvalue())
            self.assertEqual(stdout, 'Analyzing repository...\n{"requiresFullAnalysis": true}\n')

    def _fail_with(self, exit_code: int, outputs: Path) -> ar.AnalysisError:
        command = [sys.executable, "-c", f"import sys; sys.exit({exit_code})"]
        with patch.dict("os.environ", {"GITHUB_OUTPUT": str(outputs)}), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(ar.AnalysisError) as caught:
                ar._run_command(command, outputs.parent / "out")
        return caught.exception

    def test_run_command_names_an_exhausted_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outputs = Path(tmp) / "github_output"

            error = self._fail_with(3, outputs)

            self.assertIn("quota is exhausted", str(error))
            self.assertEqual(outputs.read_text(encoding="utf-8"), f"failure_reason={error}\n")

    def test_run_command_names_a_rejected_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            error = self._fail_with(2, Path(tmp) / "github_output")

            self.assertIn("rejected the API key", str(error))

    def test_run_command_leaves_other_failures_unnamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outputs = Path(tmp) / "github_output"

            error = self._fail_with(1, outputs)

            self.assertIn("exit code 1", str(error))
            self.assertFalse(outputs.exists())

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
                            "analysis_path": str(analysis_path.relative_to(out_dir.parent)),
                            "requiresFullAnalysis": False,
                        }
                    ),
                ) as _mock:
                    ar.main(
                        [
                            "incremental",
                            "--checkout",
                            str(checkout),
                            "--output-dir",
                            str(out_dir),
                        ]
                    )
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
                ar.main(
                    [
                        "full",
                        "--checkout",
                        str(checkout),
                        "--output-dir",
                        str(out_dir),
                    ]
                )

    def test_main_full_uses_generated_analysis_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            out_dir = root / "out"
            checkout.mkdir()
            out_dir.mkdir()
            stale_artifact = out_dir / "fingerprint.json"
            stale_artifact.write_text("stale", encoding="utf-8")

            def fake_run(_args, output_dir):
                self.assertIn("--depth-cap", _args)
                self.assertNotIn("--depth-level", _args)
                (output_dir / "analysis.json").write_text("ok", encoding="utf-8")
                return "human-readable CLI output"

            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch.object(ar, "_run_command", side_effect=fake_run):
                ar.main(
                    [
                        "full",
                        "--checkout",
                        str(checkout),
                        "--output-dir",
                        str(out_dir),
                        "--depth-cap",
                        "1",
                    ]
                )

            self.assertIn(f"analysis_path={out_dir / 'analysis.json'}", stdout.getvalue())
            self.assertFalse(stale_artifact.exists())

    def test_old_depth_input_is_rejected(self) -> None:
        with patch("sys.stderr", io.StringIO()), patch.object(ar, "_run_command") as command:
            with self.assertRaises(SystemExit) as raised:
                ar.main(["full", "--checkout", ".", "--output-dir", ".", "--depth-level", "3"])
        self.assertEqual(raised.exception.code, 2)
        command.assert_not_called()

    def test_main_rejects_bad_cli_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            out_dir = root / "out"
            checkout.mkdir()
            out_dir.mkdir()
            with self.assertRaises(ar.AnalysisError):
                with patch.object(ar, "_run_command", return_value="not-json"):
                    ar.main(
                        [
                            "incremental",
                            "--checkout",
                            str(checkout),
                            "--output-dir",
                            str(out_dir),
                        ]
                    )


if __name__ == "__main__":
    unittest.main()
