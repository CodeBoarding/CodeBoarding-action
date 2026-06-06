"""Build the call-to-action footer appended to the architecture-diff PR comment.

The footer links into CodeBoarding's click proxy (so owner/repo/pr are tracked)
and currently drives straight to the VS Code/Cursor **extension**: an "open this
architecture in your editor" link (editor-specific) plus an "install the
extension" link, and a warning banner when real health findings exist. A
no-install hosted-webview ("explore in browser") tier is intentionally deferred
(see docs/COMMIT_STRATEGY.md) — the committed analysis already supports it later.

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


def build_cta(cta_base: str, owner: str, repo: str, pr: str, repo_path: Path, issues: int = 0) -> str:
    """Return the markdown CTA footer (the warning banner shows even without a proxy URL).

    The ⚠️ health banner is informational and needs no proxy, so it renders
    whenever ``issues > 0``; the editor/marketplace links require ``cta_base``.
    Returns '' only when there's nothing to show.
    """
    parts: list[str] = []
    if issues > 0:
        noun = "issue" if issues == 1 else "issues"
        parts.append(f"⚠️ **{issues} architecture {noun} found** — open CodeBoarding to explore them.")

    if cta_base:
        base = cta_base.rstrip("/")

        def link(path: str, **extra: str) -> str:
            return f"{base}/{path}?" + urlencode({"owner": owner, "repo": repo, "pr": pr, **extra})

        editor_links = " · ".join(
            f"[**Open in {_EDITOR_LABEL[e]} →**]({link('open-in-editor', editor=e)})" for e in detect_editors(repo_path)
        )
        parts.append(f"See this architecture in your editor: {editor_links}")
        parts.append(f"💡 New to CodeBoarding? [**Get the extension →**]({link('use-marketplace')})")

    if not parts:
        return ""
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
