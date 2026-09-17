"""What the run publishes when the engine finished with less than it should have."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analysis_diagnostics as ad


def _entry(**overrides: object) -> dict:
    entry = {
        "code": "static.language_server_unavailable",
        "severity": "degraded",
        "title": "CSharp could not be analyzed",
        "detail": "Its language server failed to start.",
        "remedy": "Install the CSharp toolchain, then run the analysis again.",
        "subject": "CSharp",
        "count": 1,
    }
    entry.update(overrides)
    return entry


def _document(entries: list[dict]) -> dict:
    degraded = sum(1 for e in entries if e["severity"] == "degraded")
    return {
        "metadata": {
            "run_diagnostics": {
                "version": 1,
                "degraded": degraded,
                "notices": len(entries) - degraded,
                "entries": entries,
            }
        }
    }


class LoadEntriesTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "analysis.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_an_analysis_without_the_field_has_nothing_to_say(self):
        """Older engines wrote no diagnostics; that is silence, not a failure."""
        self.assertEqual(ad.load_entries(self._write(json.dumps({"metadata": {"depth_cap": 2}}))), [])

    def test_unparseable_json_does_not_take_the_run_down_with_it(self):
        self.assertEqual(ad.load_entries(self._write("{not json")), [])

    def test_entries_are_read_back_intact(self):
        entries = ad.load_entries(self._write(json.dumps(_document([_entry()]))))
        self.assertEqual([e["code"] for e in entries], ["static.language_server_unavailable"])


class AnnotationTests(unittest.TestCase):
    def test_a_degraded_entry_annotates_as_a_warning(self):
        self.assertTrue(ad.annotations([_entry()])[0].startswith("::warning::"))

    def test_a_notice_stays_a_notice(self):
        self.assertTrue(ad.annotations([_entry(severity="notice")])[0].startswith("::notice::"))

    def test_an_entry_nobody_can_act_on_points_at_the_community(self):
        self.assertIn(ad.DISCORD_URL, ad.annotations([_entry(remedy="")])[0])


class MarkdownTests(unittest.TestCase):
    def test_a_clean_run_renders_nothing(self):
        self.assertEqual(ad.markdown([]), "")

    def test_a_degradation_leads_with_an_alert(self):
        body = ad.markdown([_entry()])
        self.assertTrue(body.startswith("> [!WARNING]"))
        self.assertIn("Install the CSharp toolchain", body)

    def test_notices_alone_do_not_raise_an_alert(self):
        body = ad.markdown([_entry(severity="notice")])
        self.assertNotIn("[!WARNING]", body)
        self.assertIn("CSharp could not be analyzed", body)

    def test_a_repeated_entry_shows_its_count(self):
        self.assertIn("(×3)", ad.markdown([_entry(count=3)]))

    def test_a_long_list_is_capped_and_says_so(self):
        entries = [_entry(subject=str(i), title=f"Entry {i}") for i in range(ad.MAX_RENDERED_ENTRIES + 4)]
        body = ad.markdown(entries)
        self.assertIn("…and 4 more, in the run log.", body)
        self.assertNotIn("Entry 12", body)


class MainTests(unittest.TestCase):
    def _run(self, document: object | None) -> tuple[Path, dict[str, str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        analysis = root / "analysis.json"
        if document is not None:
            analysis.write_text(json.dumps(document), encoding="utf-8")
        out = root / "diagnostics.md"
        github_output = root / "github-output"
        github_output.write_text("", encoding="utf-8")
        os.environ["GITHUB_OUTPUT"] = str(github_output)
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT", None)

        ad.main(["--analysis", str(analysis), "--out", str(out)])

        outputs = dict(
            line.split("=", 1) for line in github_output.read_text(encoding="utf-8").splitlines() if "=" in line
        )
        return out, outputs

    def test_a_degraded_run_reports_its_count_and_a_body(self):
        out, outputs = self._run(_document([_entry(), _entry(subject="Go", title="Go could not be analyzed")]))
        self.assertEqual(outputs["degraded"], "2")
        self.assertEqual(outputs["markdown_path"], str(out))
        self.assertTrue(out.read_text(encoding="utf-8"))

    def test_a_clean_run_publishes_an_empty_path_so_callers_skip_the_block(self):
        _, outputs = self._run(_document([]))
        self.assertEqual(outputs["degraded"], "0")
        self.assertEqual(outputs["markdown_path"], "")

    def test_a_missing_analysis_is_not_an_error(self):
        """The step runs on always(); an analysis that never got written has no diagnostics."""
        _, outputs = self._run(None)
        self.assertEqual(outputs["degraded"], "0")


if __name__ == "__main__":
    unittest.main()
