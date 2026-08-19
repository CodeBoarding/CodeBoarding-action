"""Tests for the sync artifact boundary owned by Core."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL_SYNC = ROOT / "scripts" / "action" / "install-sync.sh"
RENDER_REVIEW = ROOT / "scripts" / "action" / "render-review.sh"
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

    def test_review_guard_rejects_fork_pull_request_target_before_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            result = subprocess.run(
                [str(GUARD)],
                env={
                    "PATH": os.environ["PATH"],
                    "GITHUB_OUTPUT": str(output),
                    "MODE": "review",
                    "EVENT": "pull_request_target",
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

    def test_review_guard_ignores_a_distance_it_cannot_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            gh = fake_bin / "gh"
            # A comparison with no common ancestor still reports behind_by.
            gh.write_text("#!/usr/bin/env bash\nprintf '\\t3\\n'\n", encoding="utf-8")
            gh.chmod(0o755)
            output = root / "github-output"
            result = subprocess.run(
                [str(GUARD)],
                env={
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_RUN_ID": "123",
                    "MODE": "review",
                    "EVENT": "pull_request",
                    "EVENT_PR_NUMBER": "42",
                    "PULL_BASE_SHA": "base-sha",
                    "PULL_HEAD_SHA": "head-sha",
                    "PULL_BASE_REPO": "owner/repo",
                    "PULL_HEAD_REPO": "owner/repo",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            values = output.read_text(encoding="utf-8")
            self.assertIn("merge_base_sha=base-sha\n", values)
            self.assertIn("behind_by=0\n", values)
            # The fallback comparison is announced, not silently substituted.
            self.assertIn("merge_base_resolved=false\n", values)

    def test_review_guard_accepts_trusted_fork_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            gh = fake_bin / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                'case "$2" in\n'
                "  */compare/*) printf 'merge-base-sha\\t2\\n'; exit 0 ;;\n"
                "esac\n"
                "cat <<'JSON'\n"
                '{"number":42,"base":{"sha":"base-sha","repo":{"full_name":"owner/repo"}},'
                '"head":{"sha":"head-sha","repo":{"full_name":"contributor/repo"}}}\n'
                "JSON\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            output = root / "github-output"
            result = subprocess.run(
                [str(GUARD)],
                env={
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_RUN_ID": "123",
                    "MODE": "review",
                    "EVENT": "issue_comment",
                    "COMMENT_BODY": "/codeboarding",
                    "AUTHOR_ASSOCIATION": "COLLABORATOR",
                    "ISSUE_PR_URL": "repos/owner/repo/pulls/42",
                    "GH_HOST": "github.com",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            values = output.read_text(encoding="utf-8")
            self.assertIn("skip=false\n", values)
            self.assertIn("checkout_repo=contributor/repo\n", values)
            self.assertIn("checkout_ref=head-sha\n", values)

    def test_review_guard_rejects_an_unusable_post_comment_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            result = subprocess.run(
                [str(GUARD)],
                env={
                    "PATH": os.environ["PATH"],
                    "GITHUB_OUTPUT": str(output),
                    "MODE": "review",
                    "EVENT": "pull_request",
                    "POST_COMMENT_INPUT": "no",
                    "EVENT_PR_NUMBER": "42",
                    "PULL_BASE_SHA": "base-sha",
                    "PULL_HEAD_SHA": "head-sha",
                    "PULL_BASE_REPO": "owner/repo",
                    "PULL_HEAD_REPO": "owner/repo",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            # Silently posting when the author asked for silence is worse than
            # failing, so an unusable value stops the run.
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("post_comment must be true or false", result.stdout)

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

            (checkout / "constants.py").write_text(
                "raise RuntimeError('imported target constants')\n", encoding="utf-8"
            )
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
                output / "notes.md": "hand-written notes\n",
            }
            for path, content in preserved.items():
                path.write_text(content, encoding="utf-8")
            legacy = (
                output / "overview.md",
                output / "health" / "health_report.json",
                checkout / "docs" / "development" / "architecture.md",
            )
            for path in legacy:
                path.write_text(
                    "old generated content\nhttps://img.shields.io/badge/Generated%20by-CodeBoarding\n",
                    encoding="utf-8",
                )

            github_output = root / "github-output"
            result = subprocess.run(
                [str(INSTALL_SYNC)],
                cwd=checkout,
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(fake_core),
                    "ACTION_PATH": str(ROOT),
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

            architecture = checkout / "docs" / "development" / "architecture.md"
            architecture.write_text("hand-written architecture\n", encoding="utf-8")
            result = subprocess.run(
                [str(INSTALL_SYNC)],
                cwd=checkout,
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(fake_core),
                    "ACTION_PATH": str(ROOT),
                    "ANALYSIS_DIR": str(analysis),
                    "CHECKOUT_DIR": str(checkout),
                    "GITHUB_OUTPUT": str(github_output),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(architecture.read_text(encoding="utf-8"), "hand-written architecture\n")

    def test_installs_the_health_report_the_engine_produced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            analysis = root / "analysis"
            fake_core = root / "core"
            static_analyzer = fake_core / "static_analyzer"
            for directory in (checkout / ".codeboarding", analysis / "health", static_analyzer):
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
            (analysis / "analysis.json").write_text("{}\n", encoding="utf-8")
            (analysis / "health" / "health_report.json").write_text('{"overall_score": 1.0}', encoding="utf-8")

            result = subprocess.run(
                [str(INSTALL_SYNC)],
                cwd=checkout,
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(fake_core),
                    "ACTION_PATH": str(ROOT),
                    "ANALYSIS_DIR": str(analysis),
                    "CHECKOUT_DIR": str(checkout),
                    "GITHUB_OUTPUT": str(root / "github-output"),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            installed = checkout / ".codeboarding" / "health" / "health_report.json"
            self.assertEqual(installed.read_text(encoding="utf-8"), '{"overall_score": 1.0}')
            # Printed paths are what the delivery step stages, so an installed
            # file that is never printed is silently left out of the commit.
            self.assertIn(str(installed), result.stdout.splitlines())

    def test_empty_review_is_successful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_core = root / "core"
            (fake_core / "codeboarding_workflows").mkdir(parents=True)
            (fake_core / "diagram_analysis").mkdir()
            (fake_core / "codeboarding_workflows" / "rendering.py").write_text(
                "def project_relations_to_level(*args): return []\n", encoding="utf-8"
            )
            (fake_core / "diagram_analysis" / "analysis_json.py").write_text(
                "from types import SimpleNamespace\n"
                "def parse_unified_analysis(data): return SimpleNamespace(components=[], components_relations=[]), {}\n"
                "def build_id_to_name_map(*args): return {}\n",
                encoding="utf-8",
            )
            analysis = root / "analysis.json"
            analysis.write_text('{"components": [], "components_relations": []}', encoding="utf-8")
            github_output = root / "github-output"
            result = subprocess.run(
                [str(RENDER_REVIEW)],
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(fake_core),
                    "ACTION_PATH": str(ROOT),
                    "BASE_ANALYSIS_PATH": str(analysis),
                    "HEAD_ANALYSIS_PATH": str(analysis),
                    "GITHUB_OUTPUT": str(github_output),
                    "RUNNER_TEMP": str(root),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("n_changed=0\n", github_output.read_text(encoding="utf-8"))
            self.assertIn("truncated=false\n", github_output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class SyncDeliveryTests(unittest.TestCase):
    """deliver-sync.sh against a real local remote: no network, real git."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.remote = self.root / "owner" / "repo.git"
        self.remote.mkdir(parents=True)
        self._git(self.remote, "init", "--bare", "-b", "main")

        self.checkout = self.root / "checkout"
        self._git(self.root, "clone", "--quiet", str(self.remote), str(self.checkout))
        for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            self._git(self.checkout, "config", key, value)
        (self.checkout / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (self.checkout / ".codeboarding").mkdir()
        self._git(self.checkout, "add", "-A")
        self._git(self.checkout, "commit", "-m", "initial")
        self._git(self.checkout, "push", "--quiet", "origin", "main")
        self.analyzed_sha = self._git(self.checkout, "rev-parse", "HEAD")

        # What the engine left behind for this run.
        self.analysis = self.root / "analysis"
        self.analysis.mkdir()
        for name in ("analysis.json", "fingerprint.json", "static_analysis.pkl"):
            (self.analysis / name).write_text(f"fresh {name}\n", encoding="utf-8")

        self.core = self.root / "core"
        (self.core / "static_analyzer").mkdir(parents=True)
        (self.core / "utils.py").write_text(
            "ANALYSIS_FILENAME = 'analysis.json'\nFINGERPRINT_FILENAME = 'fingerprint.json'\n",
            encoding="utf-8",
        )
        (self.core / "static_analyzer" / "__init__.py").touch()
        (self.core / "static_analyzer" / "analysis_cache.py").write_text(
            "STATIC_ANALYSIS_PKL = 'static_analysis.pkl'\nSTATIC_ANALYSIS_SHA = 'static_analysis.sha'\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()

    def test_it_publishes_the_analysis_under_both_commits(self) -> None:
        output = self.root / "github-output"
        result = subprocess.run(
            [str(ROOT / "scripts" / "action" / "deliver-sync.sh")],
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(self.core),
                "ACTION_PATH": str(ROOT),
                "ANALYSIS_DIR": str(self.analysis),
                "CHECKOUT_DIR": str(self.checkout),
                "GITHUB_OUTPUT": str(output),
                "RUNNER_TEMP": str(self.root),
                "GITHUB_SERVER_URL": str(self.root),
                "GITHUB_TOKEN": "unused",
                "GH_HOST": "github.com",
                "REPOSITORY": "owner/repo",
                "TARGET_BRANCH": "main",
                "SYNC_STRATEGY": "push",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
        self.assertEqual(values["committed"], "true")
        baseline_sha = self._git(self.checkout, "rev-parse", "HEAD")
        # The analysis describes the tree it ran on and the baseline commit
        # written on top, which differs only in files the fingerprint ignores.
        # A pull request branched either side of that commit must hit the cache.
        self.assertEqual(values["baseline_sha"], baseline_sha)
        self.assertEqual(values["analyzed_sha"], self.analyzed_sha)
        self.assertNotEqual(values["baseline_sha"], values["analyzed_sha"])
