#!/usr/bin/env python3
"""Print the review comment's merge-risk line from closed/merge_risk.json, or nothing without one.

The web platform's dashboard reads merge risk for every open pull request from this line rather than
from each run's artifact. It is one HTML comment, `<!-- codeboarding:merge_risk {json} -->`, so it
renders as nothing. The JSON is compact and escapes `<`, `>`, `-` and `=`: none of them can end the
comment, and the status line's `key=value` reader cannot match inside it. A JSON parser reads the
escapes back as the characters they stand for.

Usage: merge_risk_marker.py <closed/merge_risk.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The most dependency names the line carries; the copy shows four and the count.
MAX_DEPS = 20


def marker(result: dict) -> str:
    facts = result["facts"]
    critical = [area["name"] for area in facts.get("areas") or [] if area.get("critical")]
    compact = {
        "v": 1,
        "head": result["headSha"],
        "sensitive": bool(result.get("sensitive")),
        "areas": critical,
        "deps": (facts.get("deps") or [])[:MAX_DEPS],
        "srcLines": facts.get("srcLines", 0),
        "sourceFiles": facts.get("sourceFiles", 0),
        "tests": facts.get("tests", 0),
        "dirs": facts.get("dirs", 0),
        "reasons": [
            {"level": reason["level"], "code": reason["code"], "params": reason.get("params") or {}}
            for reason in result.get("reasons") or []
        ],
    }
    text = json.dumps(compact, separators=(",", ":"), ensure_ascii=True)
    for char, escape in (("<", "\\u003c"), (">", "\\u003e"), ("-", "\\u002d"), ("=", "\\u003d")):
        text = text.replace(char, escape)
    return f"<!-- codeboarding:merge_risk {text} -->"


def main(argv: list[str]) -> int:
    try:
        result = json.loads(Path(argv[1]).read_text())
        print(marker(result))
    except Exception as error:  # no file, or one we cannot read: the comment goes without the line
        if not isinstance(error, (FileNotFoundError, IndexError)):
            print(f"::notice::merge-risk line left out: {type(error).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
