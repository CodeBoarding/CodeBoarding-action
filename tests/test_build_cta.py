"""Unit tests for scripts/build_cta.py — editor detection + CTA footer."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_cta as bc  # noqa: E402


def repo_with(*dirs):
    d = Path(tempfile.mkdtemp())
    for x in dirs:
        (d / x).mkdir()
    return d


class TestDetectEditors(unittest.TestCase):
    def test_neither_defaults_to_vscode(self):
        self.assertEqual(bc.detect_editors(repo_with()), ["vscode"])

    def test_vscode_only(self):
        self.assertEqual(bc.detect_editors(repo_with(".vscode")), ["vscode"])

    def test_cursor_only(self):
        self.assertEqual(bc.detect_editors(repo_with(".cursor")), ["cursor"])

    def test_both_vscode_first(self):
        self.assertEqual(bc.detect_editors(repo_with(".vscode", ".cursor")), ["vscode", "cursor"])


class TestBuildCta(unittest.TestCase):
    def test_no_proxy_links_editor_to_https_listing_no_get_extension(self):
        out = bc.build_cta("", "o", "r", "1", repo_with(".cursor"), issues=3)
        self.assertIn("3 architecture issues found", out)
        # Cursor -> Open VSX https listing. A cursor: scheme would be stripped by GitHub.
        self.assertIn("[**Open in Cursor →**](https://open-vsx.org/extension/CodeBoarding/codeboarding)", out)
        self.assertNotIn("cursor:extension", out)
        self.assertNotIn("Get the extension", out)  # dropped without a proxy
        self.assertNotIn("Open in VS Code", out)  # cursor-only repo

    def test_no_proxy_vscode_marketplace_https_no_banner_at_zero(self):
        out = bc.build_cta("", "o", "r", "1", repo_with())  # neither dir, no issues
        self.assertIn(
            "[**Open in VS Code →**](https://marketplace.visualstudio.com/items?itemName=Codeboarding.codeboarding)", out
        )
        self.assertNotIn("vscode:extension", out)  # custom scheme stripped by GitHub
        self.assertNotIn("Get the extension", out)
        self.assertNotIn("architecture issue", out)  # banner suppressed at 0 issues

    def test_links_banner_and_cursor_only(self):
        out = bc.build_cta("https://x.dev/", "Org", "Repo", "9", repo_with(".cursor"), issues=2)
        self.assertIn("2 architecture issues found", out)
        self.assertNotIn("use-workspace", out)  # webview/browser tier deferred — extension-direct
        self.assertIn("open-in-editor?owner=Org&repo=Repo&pr=9&editor=cursor", out)
        self.assertIn("use-marketplace?owner=Org&repo=Repo&pr=9", out)
        self.assertNotIn("Open in VS Code", out)  # cursor-only repo

    def test_no_banner_when_zero_issues_and_default_vscode(self):
        out = bc.build_cta("https://x.dev", "o", "r", "1", repo_with(), issues=0)
        self.assertNotIn("architecture issue", out)
        self.assertIn("Open in VS Code", out)
        self.assertNotIn("Open in Cursor", out)

    def test_both_editors_singular_issue(self):
        out = bc.build_cta("https://x.dev", "o", "r", "1", repo_with(".vscode", ".cursor"), issues=1)
        self.assertIn("1 architecture issue found", out)  # singular
        self.assertIn("Open in VS Code", out)
        self.assertIn("Open in Cursor", out)

    def test_trailing_slash_in_base_is_normalized(self):
        a = bc.build_cta("https://x.dev/", "o", "r", "1", repo_with())
        b = bc.build_cta("https://x.dev", "o", "r", "1", repo_with())
        self.assertNotIn("x.dev//", a)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
