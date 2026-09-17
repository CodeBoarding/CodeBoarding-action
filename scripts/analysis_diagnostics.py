#!/usr/bin/env python3
"""Turn an analysis.json's run diagnostics into what the run and the reader see.

CodeBoarding writes ``metadata.run_diagnostics`` for every degradation it
survived — a language server that never started, a language nothing indexed
under, naming that stopped answering. A run that only checks the exit code
publishes those diagrams as though nothing happened, so this reads them back
and gives the run three things: annotations on the failing step, a Markdown
block for the job summary and the review comment, and a count to branch on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Where a reader goes when the cause is ours rather than theirs.
DISCORD_URL = "https://discord.gg/T5zHTJYFuy"

MAX_RENDERED_ENTRIES = 10


def load_entries(analysis_path: Path) -> list[dict]:
    """Diagnostic entries from an analysis document, newest schema or none at all.

    An analysis written before the field existed, or by a build that never sets
    it, has nothing to say — that is not an error, so it reads as an empty list.
    """
    try:
        with analysis_path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning::Could not read run diagnostics from {analysis_path}: {exc}", file=sys.stderr)
        return []

    report = document.get("metadata", {}).get("run_diagnostics")
    if not isinstance(report, dict):
        return []
    entries = report.get("entries")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def annotations(entries: list[dict]) -> list[str]:
    """One workflow annotation per entry, so the run page shows them without scrolling."""
    lines = []
    for entry in entries:
        level = "warning" if entry.get("severity") == "degraded" else "notice"
        remedy = entry.get("remedy") or f"Nothing on your side causes this; please report it: {DISCORD_URL}"
        lines.append(f"::{level}::{entry.get('title', 'Analysis diagnostic')} {entry.get('detail', '')} {remedy}")
    return lines


def markdown(entries: list[dict]) -> str:
    """The block that goes in the job summary and the review comment.

    Degraded entries lead with a GitHub alert, because the point is that the
    diagram beneath is missing something and would otherwise be read as complete.
    """
    degraded = [entry for entry in entries if entry.get("severity") == "degraded"]
    if not entries:
        return ""

    lines: list[str] = []
    if degraded:
        lines.append("> [!WARNING]")
        lines.append(
            "> This analysis did not complete cleanly, so the diagram is missing structure "
            "a clean run would have had."
        )
        lines.append("")

    for entry in entries[:MAX_RENDERED_ENTRIES]:
        title = entry.get("title", "Analysis diagnostic")
        count = entry.get("count", 1)
        repeated = f" (×{count})" if isinstance(count, int) and count > 1 else ""
        lines.append(f"- **{title}**{repeated}: {entry.get('detail', '')}")
        remedy = entry.get("remedy")
        if remedy:
            lines.append(f"  - {remedy}")
        else:
            lines.append(f"  - Nothing on your side causes this. Please report it on [Discord]({DISCORD_URL}).")

    hidden = len(entries) - MAX_RENDERED_ENTRIES
    if hidden > 0:
        lines.append(f"- …and {hidden} more, in the run log.")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, help="Path to the analysis.json the run produced")
    parser.add_argument("--out", required=True, help="File to write the Markdown block to")
    args = parser.parse_args(argv)

    analysis_path = Path(args.analysis)
    entries = load_entries(analysis_path) if analysis_path.is_file() else []
    degraded = sum(1 for entry in entries if entry.get("severity") == "degraded")

    for line in annotations(entries):
        print(line)

    body = markdown(entries)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"degraded={degraded}\n")
            handle.write(f"entries={len(entries)}\n")
            handle.write(f"markdown_path={out_path if body else ''}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
