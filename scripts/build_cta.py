"""Build the call-to-action footer appended to the architecture-diff PR comment.

The body is a single line — "Explore this PR's architecture in your browser or
VS Code" — that merges the hosted-webview link with the editor link(s), preceded by
a warning banner when real health findings exist. The "browser" link (a no-install
hosted webview) is included only when ``webview_ready`` — i.e. the head
``analysis.json`` was committed to the PR branch and this isn't a fork PR — so the
webview can fetch a committed analysis at the head SHA (see docs/COMMIT_STRATEGY.md);
otherwise the line is just the editor link(s). With a click proxy (``cta_base``) the
editor link routes through it (owner/repo/pr tracked) and deep-links into the editor
(the proxy redirects to a ``vscode:``/``cursor:`` URL), and a separate "install the
extension" link is appended. Without a proxy GitHub's comment sanitizer strips custom
``vscode:``/``cursor:`` schemes — a deep link would render as dead text — so the editor
link points at the extension's plain-https listing instead (VS Code Marketplace, Cursor
via Open VSX), which is the only clickable option.

Editor coverage is deliberately limited to **VS Code and Cursor**. Per the 2025
Stack Overflow Developer Survey (https://survey.stackoverflow.co/2025/technology/),
editor usage is VS Code 75.9%, Cursor 17.9%, VSCodium 6.2%, Windsurf 4.9%,
Trae 0.8% — so VS Code + Cursor alone cover ~94% of developers. The long-tail
forks each carry their own URL scheme and extension registry, and don't justify
that upkeep for <7% reach apiece.

Which editor link(s) appear is inferred from the analyzed repo's own signals:
a ``.vscode`` directory -> VS Code, a ``.cursor`` directory -> Cursor, both ->
both, neither -> VS Code (the safe majority default).

Self-contained stdlib.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlencode


def detect_editors(repo_path: Path) -> list[str]:
    """Return the editor link(s) to offer, from the repo's ``.vscode``/``.cursor`` dirs.

    ``.vscode`` -> ['vscode'], ``.cursor`` -> ['cursor'], both -> both (VS Code
    first), neither -> ['vscode']. Only VS Code and Cursor are considered (see
    module docstring for the market-share rationale).
    """
    editors: list[str] = []
    if (repo_path / ".vscode").is_dir():
        editors.append("vscode")
    if (repo_path / ".cursor").is_dir():
        editors.append("cursor")
    return editors or ["vscode"]


_EDITOR_LABEL = {"vscode": "VS Code", "cursor": "Cursor"}

# No-proxy editor targets. Must be plain https: GitHub strips custom URI schemes
# (vscode:/cursor:) from comment links, so a deep link renders as dead text. Each
# editor points at its extension listing instead — clickable, and installs there.
_EDITOR_MARKETPLACE = {
    "vscode": "https://marketplace.visualstudio.com/items?itemName=Codeboarding.codeboarding",
    "cursor": "https://open-vsx.org/extension/CodeBoarding/codeboarding",
}


def webview_url(webview_base: str, owner: str, repo: str, head_sha: str, base_sha: str) -> str | None:
    """Return the hosted-webview deep-link URL for this PR's head-vs-base diff, or None.

    Deep-links straight to the diff: ``?repo=owner/repo&ref=<head_sha>&compare=<base_sha>``.
    Pinned to exact SHAs so the committed ``analysis.json`` the webview fetches matches
    this run. For a private repo the webview itself sends the viewer through GitHub
    sign-in and then loads the same diff. Returns None when the base/head pieces aren't
    all present.
    """
    if not (webview_base and owner and repo and head_sha):
        return None
    base = webview_base.rstrip("/")
    params = {"repo": f"{owner}/{repo}", "ref": head_sha}
    if base_sha:
        params["compare"] = base_sha
    return f"{base}/?{urlencode(params)}"


def _join_or(items: list[str]) -> str:
    """Join with commas and a trailing 'or': 'a' / 'a or b' / 'a, b, or c'."""
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"


def build_cta(
    cta_base: str,
    owner: str,
    repo: str,
    pr: str,
    repo_path: Path,
    issues: int = 0,
    *,
    webview_base: str = "",
    head_sha: str = "",
    base_sha: str = "",
    webview_ready: bool = False,
) -> str:
    """Return the markdown CTA footer: a health-warning banner plus an editor link.

    With a ``cta_base`` proxy the links route through it (owner/repo/pr tracked),
    deep-link into the editor, and add a separate "get the extension" link. Without
    a proxy the editor link is the extension's https listing (GitHub strips custom
    ``vscode:``/``cursor:`` schemes), and the redundant install link is dropped.
    The ⚠️ banner shows whenever ``issues > 0``.

    When ``webview_ready`` (the head ``analysis.json`` was committed and this isn't a
    fork PR) a "explore in browser" line deep-links the hosted webview to this PR's
    head-vs-base diff. Otherwise that line is omitted (the webview couldn't fetch a
    committed analysis at the head SHA).
    """
    parts: list[str] = []
    if issues > 0:
        noun = "issue" if issues == 1 else "issues"
        parts.append(f"⚠️ **{issues} architecture {noun} found** — open CodeBoarding to explore them.")

    editors = detect_editors(repo_path)
    if cta_base:
        base = cta_base.rstrip("/")

        def link(path: str, **extra: str) -> str:
            return f"{base}/{path}?" + urlencode({"owner": owner, "repo": repo, "pr": pr, **extra})

        editor_href = {e: link("open-in-editor", editor=e) for e in editors}
        extension_href: str | None = link("use-marketplace")
    else:
        editor_href = {e: _EDITOR_MARKETPLACE[e] for e in editors}
        extension_href = None

    # One line that merges the hosted-webview "browser" link — only when
    # ``webview_ready`` (the head analysis was committed and this isn't a fork PR) —
    # with the editor link(s), which always render. "your" rides with the browser
    # entry alone, so the sentence reads naturally with or without it:
    # "in your browser or VS Code" / "in VS Code".
    targets: list[str] = []
    if webview_ready:
        wv = webview_url(webview_base, owner, repo, head_sha, base_sha)
        if wv:
            targets.append(f"your [**browser**]({wv})")
    targets += [f"[**{_EDITOR_LABEL[e]}**]({editor_href[e]})" for e in editors]
    parts.append(f"Explore this PR’s architecture in {_join_or(targets)}.")
    if extension_href:
        parts.append(f"💡 New to CodeBoarding? [**Get the extension →**]({extension_href})")

    lines = ["", "---"]
    for p in parts:
        lines += ["", p]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Build the architecture-diff PR-comment CTA footer.")
    p.add_argument("--cta-base", required=True, help="Click-proxy base URL (empty -> no footer)")
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", required=True)
    p.add_argument("--repo-path", required=True, type=Path, help="Path to the analyzed repo checkout")
    p.add_argument("--issues", default="0", help="Real architecture-issue count (0 -> no warning banner)")
    p.add_argument("--webview-base", default="", help="Hosted webview base URL (e.g. https://app.codeboarding.org)")
    p.add_argument("--head-sha", default="", help="PR head SHA the webview link pins to")
    p.add_argument("--base-sha", default="", help="PR base SHA the webview link compares against")
    p.add_argument(
        "--webview-ready",
        action="store_true",
        help="Emit the webview link (head analysis.json was committed; not a fork PR)",
    )
    args = p.parse_args()

    try:
        issues = int(args.issues or 0)
    except ValueError:
        issues = 0
    print(
        build_cta(
            args.cta_base,
            args.owner,
            args.repo,
            args.pr,
            args.repo_path,
            issues,
            webview_base=args.webview_base,
            head_sha=args.head_sha,
            base_sha=args.base_sha,
            webview_ready=args.webview_ready,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
