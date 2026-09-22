"""Count how many of a pull request's changed files the engine would analyse.

Reads one path per line on stdin and prints a JSON object: ``changed`` (paths read) and
``analysed`` (paths in the analysed scope). A path is analysed when its extension belongs to a
language the engine reads and the engine's own ignore rules (``.codeboarding/.codeboardingignore``
over the repository root, plus the directories it always drops) let it through. Both come from
the installed engine, so this says exactly what the engine would look at, no more and no less.

When the engine cannot be imported every path counts as analysed: the answer this feeds is
"skip the run", and a doubt must never skip one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _engine_scope(repo_root: Path):
    """The engine's extension map and ignore manager, or None when it is not importable."""
    try:
        from repo_utils.ignore import RepoIgnoreManager  # type: ignore[import-not-found]
        from static_analyzer.config import SOURCE_EXTENSION_TO_LANGUAGE  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - any import failure means "do not decide"
        return None
    extensions = {str(ext).lower() for ext in SOURCE_EXTENSION_TO_LANGUAGE}
    return extensions, RepoIgnoreManager(repo_root)


def count_analysed(paths: list[str], repo_root: Path) -> dict[str, int]:
    scope = _engine_scope(repo_root)
    if scope is None:
        return {"changed": len(paths), "analysed": len(paths)}
    extensions, ignores = scope
    analysed = 0
    for raw in paths:
        path = Path(raw)
        if path.suffix.lower() not in extensions:
            continue
        if ignores.should_ignore(path):
            continue
        analysed += 1
    return {"changed": len(paths), "analysed": analysed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, help="The checked-out repository, where the ignore file lives.")
    args = parser.parse_args()
    paths = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    print(json.dumps(count_analysed(paths, Path(args.repo_root))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
