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
        self.assertIn("[**Cursor**](https://open-vsx.org/extension/CodeBoarding/codeboarding)", out)
        self.assertNotIn("cursor:extension", out)
        self.assertNotIn("Get the extension", out)  # dropped without a proxy
        self.assertNotIn("VS Code", out)  # cursor-only repo

    def test_no_proxy_vscode_marketplace_https_no_banner_at_zero(self):
        out = bc.build_cta("", "o", "r", "1", repo_with())  # neither dir, no issues
        self.assertIn(
            "[**VS Code**](https://marketplace.visualstudio.com/items?itemName=Codeboarding.codeboarding)",
            out,
        )
        self.assertNotIn("vscode:extension", out)  # custom scheme stripped by GitHub
        self.assertNotIn("Get the extension", out)
        self.assertNotIn("architecture issue", out)  # banner suppressed at 0 issues

    def test_links_banner_and_cursor_only(self):
        out = bc.build_cta("https://x.dev/", "Org", "Repo", "9", repo_with(".cursor"), issues=2)
        self.assertIn("2 architecture issues found", out)
        self.assertIn("open-in-editor?owner=Org&repo=Repo&pr=9&editor=cursor", out)
        self.assertIn("use-marketplace?owner=Org&repo=Repo&pr=9", out)  # proxy "Get the extension"
        self.assertNotIn("VS Code", out)  # cursor-only repo

    def test_no_banner_when_zero_issues_and_default_vscode(self):
        out = bc.build_cta("https://x.dev", "o", "r", "1", repo_with(), issues=0)
        self.assertNotIn("architecture issue", out)
        self.assertIn("VS Code", out)
        self.assertNotIn("Cursor", out)

    def test_both_editors_singular_issue(self):
        out = bc.build_cta("https://x.dev", "o", "r", "1", repo_with(".vscode", ".cursor"), issues=1)
        self.assertIn("1 architecture issue found", out)  # singular
        self.assertIn("VS Code", out)
        self.assertIn("Cursor", out)

    def test_trailing_slash_in_base_is_normalized(self):
        a = bc.build_cta("https://x.dev/", "o", "r", "1", repo_with())
        b = bc.build_cta("https://x.dev", "o", "r", "1", repo_with())
        self.assertNotIn("x.dev//", a)
        self.assertEqual(a, b)


class TestWebviewUrl(unittest.TestCase):
    WV = "https://app.codeboarding.org"

    def test_url_built_with_head_ref_and_compare_base(self):
        url = bc.webview_url(self.WV, "Org", "Repo", "headsha", "basesha")
        self.assertIn("https://app.codeboarding.org/?", url)
        self.assertIn("repo=Org%2FRepo", url)
        self.assertIn("ref=headsha", url)
        self.assertIn("compare=basesha", url)
        self.assertFalse(url.startswith("🌐"))  # a bare URL now, not a markdown line

    def test_url_omits_compare_when_no_base(self):
        url = bc.webview_url(self.WV, "o", "r", "headsha", "")
        self.assertIn("ref=headsha", url)
        self.assertNotIn("compare=", url)

    def test_url_none_without_head_sha_or_base(self):
        self.assertIsNone(bc.webview_url(self.WV, "o", "r", "", "basesha"))
        self.assertIsNone(bc.webview_url("", "o", "r", "headsha", "basesha"))

    def test_cta_includes_browser_link_when_ready(self):
        out = bc.build_cta(
            "",
            "Org",
            "Repo",
            "9",
            repo_with(),
            issues=0,
            webview_base=self.WV,
            head_sha="headsha",
            base_sha="basesha",
            webview_ready=True,
        )
        self.assertIn("Explore this PR", out)
        self.assertIn("your [**browser**](", out)
        self.assertIn("ref=headsha", out)
        self.assertIn("compare=basesha", out)
        self.assertIn("VS Code", out)  # editor merged into the same line

    def test_cta_omits_browser_link_when_not_ready(self):
        # Fork PR / head analysis not committed -> webview can't fetch at head SHA.
        out = bc.build_cta(
            "",
            "Org",
            "Repo",
            "9",
            repo_with(),
            issues=0,
            webview_base=self.WV,
            head_sha="headsha",
            base_sha="basesha",
            webview_ready=False,
        )
        self.assertNotIn("ref=headsha", out)  # no browser link
        self.assertNotIn("[**browser**]", out)
        self.assertIn("Explore this PR", out)  # the line is still there, editor-only
        self.assertIn("VS Code", out)

    def test_cta_omits_browser_link_when_ready_but_no_base_url(self):
        out = bc.build_cta(
            "",
            "Org",
            "Repo",
            "9",
            repo_with(),
            issues=0,
            webview_base="",
            head_sha="headsha",
            base_sha="basesha",
            webview_ready=True,
        )
        self.assertNotIn("[**browser**]", out)
        self.assertNotIn("ref=headsha", out)


class TestJoinOr(unittest.TestCase):
    def test_join_shapes(self):
        self.assertEqual(bc._join_or(["a"]), "a")
        self.assertEqual(bc._join_or(["a", "b"]), "a or b")
        self.assertEqual(bc._join_or(["a", "b", "c"]), "a, b, or c")


class TestMergedExploreLine(unittest.TestCase):
    WV = "https://app.codeboarding.org"

    def _ready(self, repo, cta=""):
        return bc.build_cta(
            cta, "o", "r", "1", repo, webview_base=self.WV, head_sha="h", base_sha="b", webview_ready=True
        )

    def test_browser_and_single_editor_joined_with_or(self):
        out = self._ready(repo_with())  # default VS Code
        self.assertIn("in your [**browser**](", out)
        self.assertIn(") or [**VS Code**](", out)  # browser <or> editor on one line

    def test_editor_only_has_no_your_and_no_browser(self):
        out = bc.build_cta("", "o", "r", "1", repo_with())  # no webview
        self.assertIn("architecture in [**VS Code**](", out)  # "in <editor>" with no "your"
        self.assertNotIn("browser", out)

    def test_browser_and_two_editors_use_oxford_or(self):
        out = self._ready(repo_with(".vscode", ".cursor"))
        self.assertIn("your [**browser**](", out)
        self.assertIn(", or [**Cursor**](", out)  # 3 targets -> ", or" before the last

    def test_two_editors_no_browser_joined_with_or(self):
        out = bc.build_cta("", "o", "r", "1", repo_with(".vscode", ".cursor"))
        self.assertIn(" or [**Cursor**](", out)
        self.assertNotIn(", or [**Cursor**]", out)  # 2 targets -> plain "or", no Oxford comma
        self.assertNotIn("browser", out)


if __name__ == "__main__":
    unittest.main()
