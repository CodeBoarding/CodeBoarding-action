"""comment_detail: which view the PR comment's diagram draws, and what a bad value does.

The view itself is in test_diff_to_mermaid.py. These run the action's scripts, because the
input is only worth having if it reaches the renderer, and only safe if a bad value stops the
run before the analysis is paid for.
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
    """Just enough of Core for diff_to_mermaid.py: nesting and root relations pass through."""
    core = root / "core"
    (core / "diagram_analysis").mkdir(parents=True)
    (core / "output_generators").mkdir(parents=True)
    (core / "output_generators" / "rendering.py").write_text(
        "def project_relations_to_level(*args): return []\n", encoding="utf-8"
    )
    (core / "diagram_analysis" / "analysis_json.py").write_text(
        "from types import SimpleNamespace\n"
        "def parse_unified_analysis(data):\n"
        "    relations = [SimpleNamespace(**r) for r in data['components_relations']]\n"
        "    return SimpleNamespace(components=[], components_relations=relations), {}\n"
        "def build_id_to_name_map(*args): return {}\n",
        encoding="utf-8",
    )
    return core


def component(name: str, *children: dict, files: dict | None = None) -> dict:
    return {
        "component_id": name,
        "name": name,
        "file_methods": [{"file_path": path, "methods": methods} for path, methods in (files or {}).items()],
        "components": list(children),
        "components_relations": [],
    }


def relation(src: str, dst: str) -> dict:
    return {"relation": "calls", "src_name": src, "dst_name": dst, "src_id": src, "dst_id": dst}


class CommentDetailRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.core = write_core(self.root)

    def _render(self, base: dict, head: dict, **env: str) -> tuple[subprocess.CompletedProcess, str, dict[str, str]]:
        paths = {}
        for name, analysis in (("base", base), ("head", head)):
            paths[name] = self.root / f"{name}.json"
            paths[name].write_text(json.dumps(analysis), encoding="utf-8")
        github_output = self.root / "github-output"
        github_output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [str(RENDER_REVIEW)],
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(self.core),
                "ACTION_PATH": str(ROOT),
                "BASE_ANALYSIS_PATH": str(paths["base"]),
                "HEAD_ANALYSIS_PATH": str(paths["head"]),
                "GITHUB_OUTPUT": str(github_output),
                "RUNNER_TEMP": str(self.root),
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        outputs = dict(line.split("=", 1) for line in github_output.read_text(encoding="utf-8").splitlines())
        return result, (self.root / "diagram.md").read_text(encoding="utf-8"), outputs

    @staticmethod
    def _a_nested_change() -> tuple[dict, dict]:
        base = {
            "components": [
                component("Parent", component("Alpha", files={"a.py": ["f"]}), component("Quiet")),
                component("Other", component("Beta")),
            ],
            "components_relations": [],
        }
        head = {
            "components": [
                component("Parent", component("Alpha", files={"a.py": ["f", "g"]}), component("Quiet")),
                component("Other", component("Beta")),
            ],
            "components_relations": [relation("Alpha", "Beta")],
        }
        return base, head

    def test_the_default_draws_the_top_level_as_it_always_has(self) -> None:
        base, head = self._a_nested_change()
        result, diagram, _ = self._render(base, head)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('["Parent"]', diagram)
        self.assertNotIn("Alpha", diagram)

    def test_top_named_explicitly_is_the_default(self) -> None:
        base, head = self._a_nested_change()
        _, default, _ = self._render(base, head)
        _, top, _ = self._render(base, head, COMMENT_DETAIL="top")
        self.assertEqual(top, default)

    def test_specific_draws_only_what_changed_flat(self) -> None:
        base, head = self._a_nested_change()
        _, _, top_outputs = self._render(base, head)
        result, diagram, outputs = self._render(base, head, COMMENT_DETAIL="specific")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('["Alpha"]', diagram)
        self.assertIn('["Beta"]', diagram, "the far end of a changed relation")
        self.assertIn("n_Alpha -- ", diagram)
        for absent in ("subgraph", "Parent", "Other", "Quiet"):
            self.assertNotIn(absent, diagram)
        self.assertEqual(outputs["n_changed"], top_outputs["n_changed"], "the count describes the change")

    def test_a_diagram_too_large_to_be_specific_falls_back_to_the_top_level(self) -> None:
        """Everything here is added, so the specific view is every child and cannot be drawn."""
        empty = {"components": [], "components_relations": []}
        name = "child-{}-of-a-component-with-a-long-enough-name"
        head = {
            "components": [
                component(f"Parent{p}", *[component(f"{p}-{name.format(c)}") for c in range(40)]) for p in range(60)
            ],
            "components_relations": [],
        }
        result, diagram, outputs = self._render(empty, head, COMMENT_DETAIL="specific")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("::warning::The specific diagram is too large to draw", result.stderr)
        self.assertIn('["Parent0"]', diagram)
        self.assertNotIn("child-", diagram)
        self.assertEqual(outputs["truncated"], "true")

    def test_with_nothing_changed_specific_shows_the_top_level_without_a_warning(self) -> None:
        base, _ = self._a_nested_change()
        result, diagram, outputs = self._render(base, base, COMMENT_DETAIL="specific")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertNotIn("::warning::", result.stderr)
        self.assertIn('["Parent"]', diagram)
        self.assertEqual(outputs["truncated"], "false")


class CommentDetailGuardTests(unittest.TestCase):
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

    def test_anything_else_stops_the_run_with_the_reason(self) -> None:
        for value in ("2", "1", "full", "TOP", "specific ", "top,specific"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                result, outputs = self._guard(Path(tmp), COMMENT_DETAIL=value)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("::error::comment_detail must be top or specific.", result.stdout)
                self.assertIn("failure_reason=comment_detail must be top or specific.", outputs)
                self.assertIn("pr_number=42", outputs, "the failure comment needs its pull request")

    def test_the_two_views_and_the_default_pass(self) -> None:
        for env in ({}, {"COMMENT_DETAIL": ""}, {"COMMENT_DETAIL": "top"}, {"COMMENT_DETAIL": "specific"}):
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
                COMMENT_DETAIL="nonsense",
            )
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
