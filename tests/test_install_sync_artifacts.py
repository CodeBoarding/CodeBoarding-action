"""Regression tests for selective sync-artifact installation."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install_sync_artifacts as isa


class InstallSyncArtifactsTests(unittest.TestCase):
    def test_preserves_user_config_while_replacing_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / ".codeboarding"
            health = output / "health"
            docs = root / "docs"
            analysis = root / "analysis"
            analysis_health = analysis / "health"
            for directory in (health, docs, analysis_health):
                directory.mkdir(parents=True)

            preserved = {
                output / ".codeboardingignore": "ignore me\n",
                output / "health_config.json": '{"root": true}\n',
                health / ".healthignore": "known issue\n",
                health / "health_config.json": '{"health": true}\n',
                output / "notes.txt": "user notes\n",
            }
            for path, content in preserved.items():
                path.write_text(content, encoding="utf-8")

            (output / "stale-component.md").write_text("stale\n", encoding="utf-8")
            (output / "codeboarding_version.json").write_text("stale\n", encoding="utf-8")
            (health / "health_report.json").write_text("old report\n", encoding="utf-8")
            (docs / "overview.md").write_text("# New overview\n", encoding="utf-8")
            analysis_path = analysis / "analysis.json"
            analysis_path.write_text('{"new": true}\n', encoding="utf-8")
            (analysis / "fingerprint.json").write_text('{"fingerprint": true}\n', encoding="utf-8")
            (analysis_health / "health_report.json").write_text("new report\n", encoding="utf-8")

            stage_paths = isa.install_sync_artifacts(
                output_dir=output,
                docs_dir=docs,
                analysis_path=analysis_path,
                analysis_dir=analysis,
            )

            for path, content in preserved.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertFalse((output / "stale-component.md").exists())
            self.assertFalse((output / "codeboarding_version.json").exists())
            self.assertEqual((output / "overview.md").read_text(encoding="utf-8"), "# New overview\n")
            self.assertEqual((output / "analysis.json").read_text(encoding="utf-8"), '{"new": true}\n')
            self.assertEqual((health / "health_report.json").read_text(encoding="utf-8"), "new report\n")

            staged = set(stage_paths)
            self.assertIn(output / "stale-component.md", staged)
            self.assertIn(output / "codeboarding_version.json", staged)
            self.assertIn(output / "overview.md", staged)
            self.assertIn(output / "analysis.json", staged)
            self.assertNotIn(output / ".codeboardingignore", staged)
            self.assertNotIn(health / ".healthignore", staged)
            self.assertNotIn(health / "health_config.json", staged)

    def test_rejects_empty_render_output_before_modifying_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / ".codeboarding"
            docs = root / "docs"
            analysis = root / "analysis"
            output.mkdir()
            docs.mkdir()
            analysis.mkdir()
            existing = output / "overview.md"
            existing.write_text("keep on failure\n", encoding="utf-8")
            analysis_path = analysis / "analysis.json"
            analysis_path.write_text("{}\n", encoding="utf-8")

            with self.assertRaises(isa.ArtifactInstallError):
                isa.install_sync_artifacts(
                    output_dir=output,
                    docs_dir=docs,
                    analysis_path=analysis_path,
                    analysis_dir=analysis,
                )

            self.assertEqual(existing.read_text(encoding="utf-8"), "keep on failure\n")


if __name__ == "__main__":
    unittest.main()
