"""comment_depth: how many levels the PR comment's diagram draws, and what a bad value does.

The renderer's own depth behaviour is in test_diff_to_mermaid.py. These run the action's
scripts, because the input is only worth having if it reaches the renderer, and only safe
if a bad value stops the run before the analysis is paid for.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER_REVIEW = ROOT / "scripts" / "action" / "render-review.sh"
GUARD = ROOT / "scripts" / "action" / "resolve-github-event.sh"


def write_core(root: Path) -> Path:
    """Just enough of Core for diff_to_mermaid.py: analysis.json's nesting passes through."""
    core = root / "core"
    (core / "diagram_analysis").mkdir(parents=True)
    (core / "output_generators").mkdir(parents=True)
    (core / "output_generators" / "rendering.py").write_text(
        "def project_relations_to_level(*args): return []\n", encoding="utf-8"
    )
    (core / "diagram_analysis" / "analysis_json.py").write_text(
        "from types import SimpleNamespace\n"
        "def parse_unified_analysis(data): return SimpleNamespace(components=[], components_relations=[]), {}\n"
        "def build_id_to_name_map(*args): return {}\n",
        encoding="utf-8",
    )
    return core


def component(name: str, *children: dict) -> dict:
    return {"component_id": name, "name": name, "components": list(children), "components_relations": []}


class CommentDepthRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.core = write_core(self.root)

    def _render(self, head: list[dict], **env: str) -> tuple[subprocess.CompletedProcess, str, dict[str, str]]:
        base_path = self.root / "base.json"
        base_path.write_text('{"components": [], "components_relations": []}', encoding="utf-8")
        head_path = self.root / "head.json"
        head_path.write_text(json.dumps({"components": head, "components_relations": []}), encoding="utf-8")
        github_output = self.root / "github-output"
        github_output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(RENDER_REVIEW)],
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(self.core),
                "ACTION_PATH": str(ROOT),
                "BASE_ANALYSIS_PATH": str(base_path),
                "HEAD_ANALYSIS_PATH": str(head_path),
                "GITHUB_OUTPUT": str(github_output),
                "RUNNER_TEMP": str(self.root),
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        outputs = dict(line.split("=", 1) for line in github_output.read_text(encoding="utf-8").splitlines())
        diagram = (self.root / "diagram.md").read_text(encoding="utf-8")
        return result, diagram, outputs

    def test_the_default_draws_the_top_level_as_it_always_has(self) -> None:
        tree = [component("Parent", component("Child"))]
        result, diagram, _ = self._render(tree)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('["Parent"]', diagram)
        self.assertNotIn("Child", diagram)
        self.assertNotIn("subgraph", diagram)

    def test_a_deeper_value_draws_sub_components_inside_their_parent(self) -> None:
        tree = [component("Parent", component("Child"))]
        result, diagram, _ = self._render(tree, COMMENT_DEPTH="2")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertRegex(diagram, r'subgraph \w+\["Parent"\]')
        self.assertIn('["Child"]', diagram)

    def test_a_diagram_too_large_to_draw_deep_does_not_fail_the_review(self) -> None:
        """Everything here is added, so changed-only cannot shrink it, and depth 2 cannot be drawn."""
        children = [component(f"child-{i}-of-a-component-with-a-long-enough-name") for i in range(12)]
        tree = [
            component(f"Parent{p}", *[{**c, "component_id": f"{p}-{c['name']}"} for c in children]) for p in range(60)
        ]
        result, diagram, outputs = self._render(tree, COMMENT_DEPTH="2")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("::warning::The architecture diff is too large to draw at comment_depth 2", result.stdout)
        self.assertIn('["Parent0"]', diagram)
        self.assertNotIn("subgraph", diagram)
        self.assertEqual(outputs["truncated"], "true")


class CommentDepthGuardTests(unittest.TestCase):
    def _guard(self, root: Path, **env: str) -> tuple[subprocess.CompletedProcess, str]:
        gh = root / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text("#!/bin/sh\nprintf 'merge-base\\t0'\n", encoding="utf-8")
        gh.chmod(0o755)
        output = root / "github-output"
        result = subprocess.run(
            [str(GUARD)],
            env={
                "PATH": f"{gh.parent}{os.pathsep}{os.environ['PATH']}",
                "GITHUB_OUTPUT": str(output),
                "MODE": "review",
                "EVENT": "pull_request",
                "EVENT_PR_NUMBER": "42",
                "PULL_BASE_SHA": "base-sha",
                "PULL_HEAD_SHA": "head-sha",
                "PULL_BASE_REPO": "owner/repo",
                "PULL_HEAD_REPO": "owner/repo",
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return result, output.read_text(encoding="utf-8")

    def test_anything_but_a_positive_integer_stops_the_run_with_the_reason(self) -> None:
        for value in ("0", "-1", "1.5", "two", "2 ", "02"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                result, outputs = self._guard(Path(tmp), COMMENT_DEPTH=value)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("::error::comment_depth must be a positive integer.", result.stdout)
                self.assertIn("failure_reason=comment_depth must be a positive integer.", outputs)
                self.assertIn("pr_number=42", outputs, "the failure comment needs its pull request")

    def test_the_default_and_a_deeper_value_pass(self) -> None:
        for env in ({}, {"COMMENT_DEPTH": ""}, {"COMMENT_DEPTH": "1"}, {"COMMENT_DEPTH": "3"}):
            with self.subTest(env=env), tempfile.TemporaryDirectory() as tmp:
                result, outputs = self._guard(Path(tmp), **env)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("failure_reason=", outputs)

    def test_sync_does_not_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._guard(
                Path(tmp),
                MODE="sync",
                EVENT="push",
                REF_NAME="main",
                REF_TYPE="branch",
                HEAD_AUTHOR_EMAIL="dev@example.com",
                DEFAULT_BRANCH="main",
                REPOSITORY="owner/repo",
                COMMENT_DEPTH="nonsense",
            )
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
