#!/usr/bin/env python3
"""Thin wrapper around installed CodeBoarding renderer.

No analysis or rendering orchestration lives here, only direct calls into the
package's renderer.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from codeboarding_workflows.rendering import render_docs


def concat_overview(docs_dir: Path, out_path: Path) -> None:
    overview = docs_dir / "overview.md"
    if not overview.is_file():
        raise SystemExit("Missing required root docs file: overview.md")

    files = [overview]
    files.extend(sorted(p for p in docs_dir.glob("*.md") if p.name != "overview.md"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n\n".join(p.read_text(encoding="utf-8").rstrip() for p in files) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render markdown docs from an analysis JSON.")
    parser.add_argument("--analysis", required=True, type=Path, help="Path to analysis.json")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory to write docs")
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--repo-ref", required=True)
    parser.add_argument("--format", default=".md")
    parser.add_argument("--architecture-file", required=False, type=Path)
    args = parser.parse_args()

    render_docs(
        args.analysis,
        repo_name=args.repo_name,
        repo_ref=args.repo_ref,
        temp_dir=args.output_dir,
        format=args.format,
        root_name="overview",
    )

    if args.architecture_file:
        concat_overview(args.output_dir, args.architecture_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
