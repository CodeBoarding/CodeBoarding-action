"""The templates must reproduce, byte for byte, what the webview generates today.

These fixtures were rendered by the webview at the commit this template set was lifted
from. They are the reason the move can be trusted: a template that merely looks right would
silently re-write every repository's workflow on its next update.

The round-trip tests matter for the other direction. A template is matched by turning it
into a regular expression, so a hole that renders correctly but captures wrongly would make
every repository read as hand-edited, and every one of them would be told to check what
changed instead of being updated.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "generated"
_spec = importlib.util.spec_from_file_location(
    "workflow_templates", ROOT / "scripts" / "action" / "workflow_templates.py"
)
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

# name -> (branch, tier, delivery), matching the fixture files committed beside this test.
CASES = {
    "free-push-main": ("main", "hosted", "push"),
    "free-push-trunk": ("trunk", "hosted", "push"),
    "free-pr-main": ("main", "hosted", "pull_request"),
    "lic-push-main": ("main", "license", "push"),
    "byok-push-main": ("main", "byok:anthropic", "push"),
}


class TemplateRenderTests(unittest.TestCase):
    def test_every_fixture_is_reproduced_exactly(self) -> None:
        for name, (branch, tier, delivery) in CASES.items():
            for kind in ("review", "sync"):
                with self.subTest(case=name, kind=kind):
                    expected = (FIXTURES / f"{name}.{kind}.yml").read_text(encoding="utf-8")
                    self.assertEqual(
                        wt.render(kind, branch=branch, tier=tier, delivery=delivery),
                        expected,
                        "the template no longer produces the workflow the webview ships",
                    )


class TemplateMatchTests(unittest.TestCase):
    def test_every_configuration_round_trips(self) -> None:
        """Render it, then recognise it, and get the configuration back."""
        for name, (branch, tier, delivery) in CASES.items():
            for kind in ("review", "sync"):
                with self.subTest(case=name, kind=kind):
                    found = wt.match(kind, wt.render(kind, branch=branch, tier=tier, delivery=delivery))
                    self.assertIsNotNone(found, "a file we generated must be recognised")
                    self.assertEqual(found["tier"], tier)
                    self.assertEqual(found["delivery"], delivery)
                    if kind == "sync":
                        self.assertEqual(found["branch"], branch)

    def test_every_provider_round_trips(self) -> None:
        """The byok fill is expanded from the provider table, so all of them must work."""
        table = json.loads((ROOT / "scripts" / "action" / "supported-providers.json").read_text())
        for provider in table["providers"]:
            with self.subTest(provider=provider):
                rendered = wt.render("review", branch="main", tier=f"byok:{provider}", delivery="push")
                self.assertEqual(wt.match("review", rendered)["tier"], f"byok:{provider}")

    def test_an_edited_workflow_matches_nothing(self) -> None:
        """The whole point: any edit is a fact, not a heuristic. One added comment is enough."""
        rendered = wt.render("review", branch="main", tier="hosted", delivery="push")
        self.assertIsNone(wt.match("review", rendered + "\n# my own note\n"))
        self.assertIsNone(wt.match("review", rendered.replace("timeout-minutes: 60", "timeout-minutes: 90")))

    def test_a_credential_block_we_never_wrote_is_not_a_match(self) -> None:
        """The hole would happily capture anything, so the capture is checked against the
        fills rather than trusted."""
        rendered = wt.render("review", branch="main", tier="hosted", delivery="push")
        self.assertIsNone(wt.match("review", rendered.replace("llm: hosted", "llm: mystery")))

    def test_line_endings_do_not_decide_the_answer(self) -> None:
        """A workflow committed from Windows is the same workflow."""
        rendered = wt.render("sync", branch="main", tier="hosted", delivery="push")
        self.assertIsNotNone(wt.match("sync", rendered.replace("\n", "\r\n")))


class ReadmeTests(unittest.TestCase):
    """The README's copy-and-paste blocks are the templates, or they drift.

    They already had: the README's review workflow lacked the `closed` trigger, so anyone
    setting up by hand got a workflow that ran an in-flight review to completion after the
    pull request was closed. Nobody noticed, because nothing compared them.
    """

    def test_the_copyable_workflows_are_the_templates(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for anchor, kind in (
            ("Create `.github/workflows/codeboarding.yml`", "review"),
            ("Create `.github/workflows/codeboarding-sync.yml`", "sync"),
        ):
            with self.subTest(kind=kind):
                start = readme.index("```yaml", readme.index(anchor)) + len("```yaml\n")
                block = readme[start : readme.index("```", start)]
                expected = wt.render(kind, branch="main", tier="hosted", delivery="push")
                self.assertEqual(block, expected, "regenerate the README from templates/")


class BundleTests(unittest.TestCase):
    """The published bundle must be the templates, not a copy that drifts from them.

    The webview cannot read these files: its generator is bundled into a browser build. So
    the action publishes them as data and the webview vendors that. This is the assertion
    that keeps the authored .yml the thing under review, rather than a decorative original
    beside the JSON everyone actually uses.
    """

    def setUp(self) -> None:
        self.bundle = json.loads((ROOT / "templates" / "bundle.json").read_text(encoding="utf-8"))

    def test_the_committed_bundle_is_current(self) -> None:
        self.assertEqual(
            self.bundle,
            wt.bundle(),
            "run `python3 scripts/action/workflow_templates.py` to rebuild templates/bundle.json",
        )

    def test_the_bundle_renders_what_the_templates_render(self) -> None:
        """Rendering from the bundle alone, the way a consumer will, reaches the same file."""
        for name, (branch, tier, delivery) in CASES.items():
            for kind in ("review", "sync"):
                with self.subTest(case=name, kind=kind):
                    holes = {
                        "BRANCH": branch,
                        "CREDENTIALS": self.bundle["credentials"][tier],
                        "SYNC_PR_GUARD": self.bundle["delivery"][delivery]["sync_pr_guard"],
                        "DELIVERY_PERMISSION": self.bundle["delivery"][delivery]["permission"],
                        "DELIVERY_INPUT": self.bundle["delivery"][delivery]["input"],
                    }
                    rendered = wt.HOLE.sub(lambda m: holes[m.group(1)], self.bundle["templates"][kind])
                    self.assertEqual(rendered, (FIXTURES / f"{name}.{kind}.yml").read_text())


class ChangelogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = json.loads((ROOT / "templates" / "CHANGELOG.json").read_text(encoding="utf-8"))

    def test_every_version_composes_and_reads_as_a_sentence(self) -> None:
        for entry in self.log["versions"]:
            with self.subTest(version=entry["version"]):
                self.assertIn(entry["type"], ("update", "replace"))
                self.assertTrue(entry["summary"].endswith("."))
                self.assertGreater(len(entry["summary"]), 30, "this string is shown to users")

    def test_the_current_version_exists_and_is_the_newest(self) -> None:
        versions = [e["version"] for e in self.log["versions"]]
        self.assertEqual(sorted(versions, reverse=True), versions, "newest first")
        self.assertIn(self.log["current"], versions)
        self.assertEqual(self.log["current"], max(versions))

    def test_the_oldest_entry_replaces_rather_than_updates(self) -> None:
        """Rendering walks back to the last `replace`. Without one at the bottom, a
        repository older than every entry would be described by nothing."""
        self.assertEqual(min(self.log["versions"], key=lambda e: e["version"])["type"], "replace")


if __name__ == "__main__":
    unittest.main()
