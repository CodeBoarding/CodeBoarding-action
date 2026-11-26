"""The review comment's shape: the status line the web platform reads, and the machine-readable line."""

import json
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

    def test_merge_risk_rides_on_its_own_hidden_line_that_nothing_in_it_can_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = {
                "headSha": "abc123",
                "sensitive": True,
                "facts": {
                    "srcLines": 1815,
                    "sourceFiles": 31,
                    "tests": 0,
                    "dirs": 2,
                    "deps": ["zod"],
                    "areas": [
                        {"id": "signin", "name": "Sign-in and access", "critical": True},
                        {"id": "review", "name": "Review", "critical": False},
                    ],
                },
                "reasons": [
                    {
                        "level": "medium",
                        "code": "area_changed",
                        "params": {
                            "area": "signin",
                            "name": "Sign-in and access",
                            "files": ["a-->b<c>=.ts"],
                            "count": 1,
                        },
                        "file": "a-->b<c>=.ts",
                    }
                ],
            }
            (root / "merge_risk.json").write_text(json.dumps(result), encoding="utf-8")
            lines = _build(root, MERGE_RISK_FILE=str(root / "merge_risk.json")).rstrip("\n").splitlines()
            self.assertTrue(lines[-1].startswith("<!-- codeboarding: platform_url="))
            line = lines[-2]
            match = re.fullmatch(r"<!-- codeboarding:merge_risk (\{.*\}) -->", line)
            self.assertIsNotNone(match)
            payload = match.group(1) if match else ""
            for char in "<>-=":
                self.assertNotIn(char, payload)
            self.assertEqual(
                json.loads(payload),
                {
                    "v": 1,
                    "head": "abc123",
                    "sensitive": True,
                    "areas": ["Sign-in and access"],
                    "deps": ["zod"],
                    "srcLines": 1815,
                    "sourceFiles": 31,
                    "tests": 0,
                    "dirs": 2,
                    "reasons": [{"level": "medium", "code": "area_changed", "params": result["reasons"][0]["params"]}],
                },
            )

    def test_no_merge_risk_answer_means_no_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = _build(Path(tmp), MERGE_RISK_FILE=str(Path(tmp) / "missing.json"))
            self.assertNotIn("merge_risk", body)


if __name__ == "__main__":
    unittest.main()
