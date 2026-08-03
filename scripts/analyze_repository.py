#!/usr/bin/env python3
"""Thin helper to execute CodeBoarding CLI incremental/full commands.

The action is intentionally logic-light: all analysis orchestration happens in
shell through this script's small JSON contract parser, which only invokes
CodeBoarding's own ``incremental`` and ``full`` commands.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROG = "codeboarding"


class AnalysisError(RuntimeError):
    pass


def _parse_bool(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    raise AnalysisError(f"Invalid contract field '{field}': {value!r}")


def _normalize_analysis_path(payload: dict, output_dir: str) -> Path:
    path = payload.get("analysis_path")
    if not isinstance(path, str) or not path.strip():
        raise AnalysisError("Missing or empty 'analysis_path' in CLI response")

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(output_dir) / candidate
    return candidate


def _parse_cli_response(raw: str, output_dir: str) -> tuple[bool, Path | None, dict]:
    if not raw.strip():
        raise AnalysisError("CodeBoarding command produced no JSON output")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"Invalid CodeBoarding JSON response: {exc}") from exc

    if not isinstance(payload, dict):
        raise AnalysisError("CodeBoarding JSON response is not an object")

    requires_full = _parse_bool(payload.get("requiresFullAnalysis"), field="requiresFullAnalysis")
    if requires_full and not payload.get("analysis_path"):
        return True, None, payload

    analysis_path = _normalize_analysis_path(payload, output_dir)

    if not analysis_path.is_file():
        raise AnalysisError(f"analysis_path points to a non-file: {analysis_path}")

    return requires_full, analysis_path, payload


def _run_command(args: list[str], output_dir: Path) -> str:
    try:
        completed = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            cwd=str(output_dir.parent),
            env=None,
        )
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        details = (stderr or stdout or "no command output").strip()
        raise AnalysisError(f"Command failed ({' '.join(args)}): {details}") from exc

    return completed.stdout


def run_incremental(checkout: Path, output_dir: Path) -> tuple[bool, Path | None, dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = _run_command([PROG, "incremental", "--local", str(checkout), "--output-dir", str(output_dir)], output_dir)
    return _parse_cli_response(raw, str(output_dir))


def run_full(checkout: Path, output_dir: Path, depth_level: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    _run_command(
        [
            PROG,
            "full",
            "--local",
            str(checkout),
            "--output-dir",
            str(output_dir),
            "--depth-level",
            str(depth_level),
            "--force",
        ],
        output_dir,
    )
    analysis_path = output_dir / "analysis.json"
    if not analysis_path.is_file():
        raise AnalysisError(f"Full analysis did not produce: {analysis_path}")
    return analysis_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["incremental", "full"], help="Which CLI command to invoke")
    parser.add_argument("--checkout", required=True, help="Path to repository checkout")
    parser.add_argument("--output-dir", required=True, help="Action-owned output directory")
    parser.add_argument("--depth-level", help="Depth passed to full analyses")

    args = parser.parse_args(argv)
    checkout = Path(args.checkout)
    output_dir = Path(args.output_dir)
    if not checkout.is_dir():
        raise SystemExit(f"Missing checkout directory: {checkout}")

    if args.mode == "incremental":
        requires_full, analysis_path, _ = run_incremental(checkout, output_dir)
        print(f"analysis_mode=incremental")
        print(f"requires_full_analysis={str(requires_full).lower()}")
        print(f"analysis_path={analysis_path or ''}")
        return 0

    if not args.depth_level:
        raise SystemExit("--depth-level is required for mode=full")
    analysis_path = run_full(checkout, output_dir, args.depth_level)
    print(f"analysis_mode=full")
    print("requires_full_analysis=false")
    print(f"analysis_path={analysis_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AnalysisError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1)
