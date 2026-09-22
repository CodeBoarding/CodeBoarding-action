"""The artifact relays the engine's early-exit verdict, and reads a pre-flag analysis as not unchanged."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_ARTIFACT = ROOT / "scripts" / "action" / "build-review-artifact.sh"


def _build(root: Path, analysis: str) -> tuple[dict, dict]:
    head = root / "head.json"
    head.write_text(analysis, encoding="utf-8")
    base = root / "base.json"
    base.write_text('{"components": ["base"]}', encoding="utf-8")
    output = root / "github-output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        [str(BUILD_ARTIFACT)],
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(root),
            "GITHUB_OUTPUT": str(output),
            "ANALYSIS_PATH": str(head),
            "BASE_ARTIFACT_NAME": "codeboarding-base-cfg-mergebasesha",
            "BASE_ARTIFACT_ID": "4242",
            "BASE_ANALYSIS_PATH": str(base),
            "INLINE_BASE": "false",
            "ANALYSIS_MODE": "incremental",
            "BASE_SHA": "tip-sha",
            "MERGE_BASE_SHA": "merge-base-sha",
            "MERGE_BASE_RESOLVED": "true",
            "HEAD_SHA": "head-sha",
            "PR_NUMBER": "81",
            "SEED_SOURCE": "pr-chain",
            "CHAIN_DEPTH": "2",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    metadata = json.loads((root / "cb-review-artifact" / "metadata.json").read_text(encoding="utf-8"))
    outputs: dict[str, str] = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return metadata, outputs


class ReviewArtifactUnchangedTests(unittest.TestCase):
    def test_the_early_exit_flag_is_relayed_to_the_metadata_and_the_comment_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, outputs = _build(Path(tmp), '{"metadata": {"incremental_unchanged": true}, "components": []}')
            # A string, like every other `--arg` field the webview reads from this file.
            self.assertEqual(metadata["incremental_unchanged"], "true")
            self.assertEqual(outputs["unchanged"], "true")

    def test_a_re_detailed_run_and_a_pre_flag_analysis_both_read_as_not_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, outputs = _build(Path(tmp), '{"metadata": {"incremental_unchanged": false}, "components": []}')
            self.assertEqual(metadata["incremental_unchanged"], "false")
            self.assertEqual(outputs["unchanged"], "false")
        with tempfile.TemporaryDirectory() as tmp:
            metadata, outputs = _build(Path(tmp), '{"components": ["head"]}')
            self.assertEqual(metadata["incremental_unchanged"], "false")
            self.assertEqual(outputs["unchanged"], "false")


if __name__ == "__main__":
    unittest.main()
