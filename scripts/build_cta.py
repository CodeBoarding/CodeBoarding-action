"""Build the call-to-action footer appended to the architecture-diff PR comment.

The footer drives to the VS Code/Cursor **extension** with an editor link, and a
warning banner when real health findings exist. With a click proxy (``cta_base``)
the links route through it (owner/repo/pr tracked) and can deep-link into the
editor (the proxy redirects to a ``vscode:``/``cursor:`` URL) plus a separate
"install the extension" link. Without a proxy GitHub's comment sanitizer strips
custom ``vscode:``/``cursor:`` schemes — a deep link would render as dead text —
so the editor link points at the extension's plain-https listing instead (VS Code
Marketplace, Cursor via Open VSX), which is the only clickable option. A no-install
hosted-webview ("explore in browser") tier is intentionally deferred (see
docs/COMMIT_STRATEGY.md) — the committed analysis already supports it later.

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


def build_cta(cta_base: str, owner: str, repo: str, pr: str, repo_path: Path, issues: int = 0) -> str:
    """Return the markdown CTA footer: a health-warning banner plus an editor link.

    With a ``cta_base`` proxy the links route through it (owner/repo/pr tracked),
    deep-link into the editor, and add a separate "get the extension" link. Without
    a proxy the editor link is the extension's https listing (GitHub strips custom
    ``vscode:``/``cursor:`` schemes), and the redundant install link is dropped.
    The ⚠️ banner shows whenever ``issues > 0``.
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

    editor_links = " · ".join(f"[**Open in {_EDITOR_LABEL[e]} →**]({editor_href[e]})" for e in editors)
    parts.append(f"See this architecture in your editor: {editor_links}")
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
    args = p.parse_args()

    try:
        issues = int(args.issues or 0)
    except ValueError:
        issues = 0
    print(build_cta(args.cta_base, args.owner, args.repo, args.pr, args.repo_path, issues))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
