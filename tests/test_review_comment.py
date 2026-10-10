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


if __name__ == "__main__":
    unittest.main()
