#!/usr/bin/env python3
"""Execute CodeBoarding CLI commands and validate their JSON contract."""

from __future__ import annotations

import argparse
import json
import shutil
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


def _parse_cli_response(raw: str, working_dir: str) -> tuple[bool, Path | None, dict]:
    if not raw.strip():
        raise AnalysisError("CodeBoarding command produced no JSON output")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        payload = None
        lines = raw.splitlines()
        for index, line in enumerate(lines):
            if line.lstrip().startswith("{"):
                try:
                    payload = json.loads("\n".join(lines[index:]))
                except json.JSONDecodeError:
                    continue
        if payload is None:
            raise AnalysisError(f"Invalid CodeBoarding JSON response: {exc}") from exc

    if not isinstance(payload, dict):
        raise AnalysisError("CodeBoarding JSON response is not an object")

    requires_full = _parse_bool(payload.get("requiresFullAnalysis"), field="requiresFullAnalysis")
    if requires_full and not payload.get("analysis_path"):
        return True, None, payload

    path = payload.get("analysis_path")
    if not isinstance(path, str) or not path.strip():
        raise AnalysisError("Missing or empty 'analysis_path' in CLI response")
    analysis_path = Path(path)
    if not analysis_path.is_absolute():
        analysis_path = Path(working_dir) / analysis_path
    if not analysis_path.is_file():
        raise AnalysisError(f"analysis_path points to a non-file: {analysis_path}")

    return requires_full, analysis_path, payload


def _run_command(args: list[str], output_dir: Path) -> str:
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(output_dir.parent),
    )
    stdout_lines: list[str] = []
    for line in process.stdout or ():
        stdout_lines.append(line)
        # The shell captures this helper's stdout as its result contract. Mirror
        # CLI stdout to stderr so engine progress remains visible in Actions.
        print(line, end="", file=sys.stderr, flush=True)

    return_code = process.wait()
    stdout = "".join(stdout_lines)
    if return_code != 0:
        details = stdout.strip() or f"exit code {return_code}; see command logs above"
        raise AnalysisError(f"Command failed ({' '.join(args)}): {details}")

    return stdout


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

    output_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "incremental":
        command = [PROG, "incremental", "--local", str(checkout), "--output-dir", str(output_dir)]
        requires_full, analysis_path, _ = _parse_cli_response(_run_command(command, output_dir), str(output_dir.parent))
        print(f"analysis_mode=incremental")
        print(f"requires_full_analysis={str(requires_full).lower()}")
        print(f"analysis_path={analysis_path or ''}")
        return 0

    if not args.depth_level:
        raise SystemExit("--depth-level is required for mode=full")
    shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    command = [
        PROG,
        "full",
        "--local",
        str(checkout),
        "--output-dir",
        str(output_dir),
        "--depth-level",
        args.depth_level,
        "--force",
    ]
    _run_command(command, output_dir)
    analysis_path = output_dir / "analysis.json"
    if not analysis_path.is_file():
        raise AnalysisError(f"Full analysis did not produce: {analysis_path}")
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
