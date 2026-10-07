"""The review artifact's metadata records how many analysed files changed, as the render step counted them."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_ARTIFACT = ROOT / "scripts" / "action" / "build-review-artifact.sh"


def _build(root: Path, **extra: str) -> tuple[dict, dict]:
    head = root / "head.json"
    head.write_text('{"components": ["head"]}', encoding="utf-8")
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
            **extra,
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


class ReviewArtifactMetadataTests(unittest.TestCase):
    def test_the_analysed_file_count_is_recorded_as_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, outputs = _build(Path(tmp), ANALYSED_FILES_CHANGED="0")
            # A string, like every other `--arg` field the webview reads from this file.
            self.assertEqual(metadata["analysed_files_changed"], "0")
            self.assertEqual(set(outputs), {"artifact_dir"})

    def test_a_count_the_render_step_could_not_make_is_recorded_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _outputs = _build(Path(tmp))
            self.assertEqual(metadata["analysed_files_changed"], "unknown")

    def test_how_the_base_was_obtained_is_recorded_as_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _outputs = _build(
                Path(tmp),
                BASE_SOURCE="committed",
                BASE_REASON="",
                BASE_FROM_SHA="abc",
                CATCHUP_COMMITS="3",
                BASE_SECONDS="41",
                HEAD_SECONDS="159",
            )
            self.assertEqual(
                {key: metadata[key] for key in metadata if key.startswith("base_") or key.endswith("_seconds")}
                | {"catchup_commits": metadata["catchup_commits"]},
                {
                    "base_sha": "tip-sha",
                    "base_artifact": "codeboarding-base-cfg-mergebasesha",
                    "base_artifact_id": "4242",
                    "base_source": "committed",
                    "base_reason": "",
                    "base_from_sha": "abc",
                    "catchup_commits": "3",
                    "base_seconds": "41",
                    "head_seconds": "159",
                },
            )

    def test_an_older_run_without_provenance_records_empty_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _outputs = _build(Path(tmp))
            for key in (
                "base_source",
                "base_reason",
                "base_from_sha",
                "catchup_commits",
                "base_seconds",
                "head_seconds",
            ):
                self.assertEqual(metadata[key], "", key)


if __name__ == "__main__":
    unittest.main()
