"""Tests for the sync artifact boundary owned by Core."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL_SYNC = ROOT / "scripts" / "action" / "install-sync.sh"
GUARD = ROOT / "scripts" / "action" / "guard.sh"


class ActionSyncTests(unittest.TestCase):
    def test_review_guard_rejects_fork_before_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            result = subprocess.run(
                [str(GUARD)],
                env={
                    "PATH": os.environ["PATH"],
                    "GITHUB_OUTPUT": str(output),
                    "MODE": "review",
                    "EVENT": "pull_request",
                    "EVENT_PR_NUMBER": "42",
                    "PULL_BASE_SHA": "base-sha",
                    "PULL_HEAD_SHA": "head-sha",
                    "PULL_BASE_REPO": "owner/repo",
                    "PULL_HEAD_REPO": "contributor/repo",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            values = output.read_text(encoding="utf-8")
            self.assertTrue(values.endswith("skip=true\n"))
            self.assertNotIn("checkout_ref=", values)

    def test_installs_core_manifest_and_preserves_user_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            output = checkout / ".codeboarding"
            analysis = root / "analysis"
            fake_core = root / "core"
            static_analyzer = fake_core / "static_analyzer"
            for directory in (output / "health", analysis, static_analyzer, checkout / "docs" / "development"):
                directory.mkdir(parents=True)

            (fake_core / "utils.py").write_text(
                "ANALYSIS_FILENAME = 'analysis.json'\nFINGERPRINT_FILENAME = 'fingerprint.json'\n",
                encoding="utf-8",
            )
            (static_analyzer / "__init__.py").touch()
            (static_analyzer / "analysis_cache.py").write_text(
                "STATIC_ANALYSIS_PKL = 'static_analysis.pkl'\nSTATIC_ANALYSIS_SHA = 'static_analysis.sha'\n",
                encoding="utf-8",
            )
            produced = ("analysis.json", "fingerprint.json", "static_analysis.pkl")
            for name in produced:
                (analysis / name).write_text(f"new {name}\n", encoding="utf-8")
            missing_optional = ("static_analysis.sha", "codeboarding_version.json")
            for name in missing_optional:
                (output / name).write_text(f"stale {name}\n", encoding="utf-8")

            preserved = {
                output / ".codeboardingignore": "ignore me\n",
                output / "health_config.json": "{}\n",
                output / "health" / ".healthignore": "known issue\n",
            }
            for path, content in preserved.items():
                path.write_text(content, encoding="utf-8")
            legacy = (
                output / "overview.md",
                output / "health" / "health_report.json",
                checkout / "docs" / "development" / "architecture.md",
            )
            for path in legacy:
                path.write_text("old generated content\n", encoding="utf-8")

            github_output = root / "github-output"
            result = subprocess.run(
                [str(INSTALL_SYNC)],
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(fake_core),
                    "ANALYSIS_DIR": str(analysis),
                    "CHECKOUT_DIR": str(checkout),
                    "GITHUB_OUTPUT": str(github_output),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            for name in produced:
                self.assertEqual((output / name).read_text(encoding="utf-8"), f"new {name}\n")
            for name in missing_optional:
                self.assertFalse((output / name).exists())
            for path, content in preserved.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)
            for path in legacy:
                self.assertFalse(path.exists())
            self.assertEqual(github_output.read_text(encoding="utf-8"), "installed=3\n")


if __name__ == "__main__":
    unittest.main()
