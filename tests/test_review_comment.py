"""The review comment's shape: the status line the web platform reads, and the machine-readable line."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_COMMENT = ROOT / "scripts" / "action" / "build-review-comment.sh"


def _build(root: Path, **extra: str) -> str:
    diagram = root / "diagram.md"
    diagram.write_text("```mermaid\ngraph LR\n```\n", encoding="utf-8")
    output = root / "github-output"
    output.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(root),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "1234",
        "DIAGRAM": str(diagram),
        "N_CHANGED": "3",
        "ARTIFACT_URL": "",
        "PR_NUMBER": "605",
        "HEAD_SHA": "abc123",
        **extra,
    }
    subprocess.run([str(BUILD_COMMENT)], env=env, capture_output=True, text=True, check=True)
    outputs: dict[str, str] = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return Path(outputs["path"]).read_text(encoding="utf-8")


class ReviewCommentTests(unittest.TestCase):
    def test_status_line_and_platform_link_as_the_web_platform_reads_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = _build(Path(tmp))
            self.assertTrue(body.startswith("### CodeBoarding review\n\n**Status:** 3 changed components\n"))
            self.assertIn(
                "See the full change in [CodeBoarding](https://app.codeboarding.org/owner/repo/pull/605?utm_source=github",
                body,
            )

    def test_the_machine_readable_line_ends_the_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = _build(Path(tmp))
            last = body.rstrip("\n").splitlines()[-1]
            self.assertEqual(
                last,
                "<!-- codeboarding: platform_url=https://app.codeboarding.org/owner/repo/pull/605 changed=3 analysed_files_changed=unknown head=abc123 -->",
            )
            # The status regex the web platform uses must still find the status line, not the marker.
            match = re.search(r"\*\*Status:\*\*\s*(\d+)\s+changed\s+components?", body)
            self.assertIsNotNone(match)
            self.assertEqual(match.group(1) if match else None, "3")

    def test_no_analysed_file_changed_is_said_in_the_status_and_the_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = _build(Path(tmp), N_CHANGED="0", ANALYSED_FILES_CHANGED="0")
            self.assertIn("**Status:** 0 changed components (no analysed file changed)\n", body)
            self.assertNotIn("grouped the same code", body)
            self.assertIn("changed=0 analysed_files_changed=0 head=abc123", body)

    def test_components_that_differ_with_no_file_changed_are_called_regrouping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = _build(Path(tmp), N_CHANGED="3", ANALYSED_FILES_CHANGED="0")
            self.assertIn("**Status:** 3 changed components (no analysed file changed)\n", body)
            self.assertIn("grouped the same code differently", body)
            self.assertIn("changed=3 analysed_files_changed=0 head=abc123", body)

    def test_a_changed_analysed_file_gets_no_verdict_even_at_zero_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = _build(Path(tmp), N_CHANGED="0", ANALYSED_FILES_CHANGED="2")
            self.assertIn("**Status:** 0 changed components\n", body)
            self.assertIn("changed=0 analysed_files_changed=2 head=abc123", body)


class BaseLineTests(unittest.TestCase):
    """One line under the diagram saying how the base analysis was obtained, with measured times only."""

    def _body(self, **extra: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            return _build(Path(tmp), BASE_REF="main", MERGE_BASE_SHA="f00dfeed" * 5, **extra)

    def _base_line(self, **extra: str) -> str:
        body = self._body(**extra)
        lines = [line for line in body.splitlines() if line.startswith("<sub>Base: ")]
        self.assertEqual(len(lines), 1, body)
        return lines[0]

    def test_a_full_analysis_says_why_and_how_long(self) -> None:
        line = self._base_line(
            BASE_ANALYSIS_METHOD="full",
            BASE_ANALYSIS_REASON="no usable analysis was available",
            BASE_SECONDS="534",
            HEAD_SECONDS="192",
        )
        self.assertEqual(
            line, "<sub>Base: full in 8 m 54 s (no usable analysis was available) · changes 3 m 12 s</sub>"
        )

    def test_a_reused_analysis_has_no_base_time(self) -> None:
        line = self._base_line(
            BASE_ANALYSIS_METHOD="reused",
            BASE_ANALYSIS_REASON="a1b2c3d already has a saved analysis",
            BASE_SECONDS="3",
            HEAD_SECONDS="159",
        )
        self.assertEqual(line, "<sub>Base: reused (a1b2c3d already has a saved analysis) · changes 2 m 39 s</sub>")

    def test_an_incremental_analysis_carries_where_it_started(self) -> None:
        line = self._base_line(
            BASE_ANALYSIS_METHOD="incremental",
            BASE_ANALYSIS_REASON="updated the analysis of 9f8e7d6 to a1b2c3d, 4 commits caught up",
            BASE_SECONDS="41",
            HEAD_SECONDS="159",
        )
        self.assertEqual(
            line,
            "<sub>Base: incremental in 41 s (updated the analysis of 9f8e7d6 to a1b2c3d, 4 commits caught up)"
            " · changes 2 m 39 s</sub>",
        )

    def test_the_base_line_sits_under_the_diagram_above_the_run_links(self) -> None:
        body = self._body(BASE_ANALYSIS_METHOD="reused", BASE_ANALYSIS_REASON="r", HEAD_SECONDS="4")
        diagram = body.index("```mermaid")
        base = body.index("<sub>Base: ")
        footer = body.index("<sub>run [1234]")
        self.assertLess(diagram, base)
        self.assertLess(base, footer)
        self.assertIn("· changes 4 s</sub>\n\n<sub>run [1234]", body)

    def test_the_marker_carries_the_method_after_the_existing_keys(self) -> None:
        body = self._body(
            BASE_ANALYSIS_METHOD="full",
            BASE_ANALYSIS_REASON="no usable analysis was available",
            BASE_SECONDS="534",
            HEAD_SECONDS="192",
        )
        self.assertTrue(
            body.rstrip("\n").endswith("head=abc123 base_analysis_method=full base_seconds=534 head_seconds=192 -->"),
            body,
        )

    def test_without_a_method_there_is_no_line_and_no_keys(self) -> None:
        body = self._body()
        self.assertNotIn("Base:", body)
        self.assertNotIn("base_analysis_method=", body)


if __name__ == "__main__":
    unittest.main()
