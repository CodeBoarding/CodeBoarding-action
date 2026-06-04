"""Engine orchestration for the action — extracted from inline ``python -c`` blocks
in action.yml so it is checked in, reviewable, and unit-testable.

Subcommands (all paths/refs come in as argv, never interpolated into source):

  base    --repo P --out D --name N --run-id ID --depth K --source-sha SHA
  head    --repo P --out D --name N --run-id ID --depth K --base-ref B --target-ref T --source-sha SHA
  health  --artifact-dir D --repo P --name N --issues-out FILE

``base`` runs a full analysis; ``head`` runs incremental, falling back to full on
``IncrementalCacheMissingError``/``BaselineUnavailableError``; ``health`` writes the
WARNING/CRITICAL finding count to ``--issues-out`` (and never fails the run).

The engine (``codeboarding_workflows`` etc.) is imported lazily inside each
function so this module imports without the engine venv present — the tests stub
those modules and assert we call the engine with the right arguments.
"""

from __future__ import annotations

import argparse
from pathlib import Path

_BASE_LOG = "/tmp/cb-base.log"
_HEAD_LOG = "/tmp/cb-head.log"


def run_base(repo: str, out: str, name: str, run_id: str, depth: int, source_sha: str) -> None:
    from codeboarding_workflows.analysis import run_full

    res = run_full(
        repo_name=name,
        repo_path=Path(repo),
        output_dir=Path(out),
        run_id=run_id,
        log_path=_BASE_LOG,
        depth_level=int(depth),
        source_sha=source_sha,
    )
    print(f"Base analysis written: {res}")


def run_head(repo: str, out: str, name: str, run_id: str, depth: int, base_ref: str, target_ref: str, source_sha: str) -> None:
    from codeboarding_workflows.analysis import BaselineUnavailableError, run_full, run_incremental
    from diagram_analysis.exceptions import IncrementalCacheMissingError

    try:
        res = run_incremental(
            repo_path=Path(repo),
            output_dir=Path(out),
            project_name=name,
            run_id=run_id,
            log_path=_HEAD_LOG,
            base_ref=base_ref,
            target_ref=target_ref,
            source_sha=source_sha,
        )
    except (IncrementalCacheMissingError, BaselineUnavailableError) as exc:
        print(f"Incremental unavailable ({exc}); running full analysis on head.")
        for p in Path(out).glob("*"):
            if p.is_file():
                p.unlink()
        res = run_full(
            repo_name=name,
            repo_path=Path(repo),
            output_dir=Path(out),
            run_id=run_id,
            log_path=_HEAD_LOG,
            depth_level=int(depth),
            source_sha=source_sha,
        )
    print(f"Head analysis written: {res}")


def run_health(artifact_dir: str, repo: str, name: str) -> int:
    """Return the WARNING/CRITICAL finding count; 0 on any failure (best-effort)."""
    try:
        from health.models import Severity
        from health.runner import run_health_checks
        from static_analyzer.analysis_cache import StaticAnalysisCache
    except Exception as exc:  # engine without the health module
        print(f"Health check skipped ({exc}).")
        return 0
    try:
        cache = StaticAnalysisCache(artifact_dir=Path(artifact_dir), repo_root=Path(repo))
        sa = cache.get()
        issues = 0
        if sa is not None:
            report = run_health_checks(sa, repo_name=name, repo_path=Path(repo))
            if report is not None:
                for cs in report.check_summaries:
                    for fg in getattr(cs, "finding_groups", []):
                        if getattr(fg, "severity", None) in (Severity.WARNING, Severity.CRITICAL):
                            issues += len(fg.entities)
        print(f"Architecture issues found: {issues}")
        return issues
    except Exception as exc:
        print(f"Health check skipped ({exc}).")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("base")
    for a in ("--repo", "--out", "--name", "--run-id", "--depth", "--source-sha"):
        b.add_argument(a, required=True)

    h = sub.add_parser("head")
    for a in ("--repo", "--out", "--name", "--run-id", "--depth", "--base-ref", "--target-ref", "--source-sha"):
        h.add_argument(a, required=True)

    hc = sub.add_parser("health")
    for a in ("--artifact-dir", "--repo", "--name", "--issues-out"):
        hc.add_argument(a, required=True)

    args = p.parse_args(argv)
    if args.cmd == "base":
        run_base(args.repo, args.out, args.name, args.run_id, args.depth, args.source_sha)
    elif args.cmd == "head":
        run_head(args.repo, args.out, args.name, args.run_id, args.depth, args.base_ref, args.target_ref, args.source_sha)
    elif args.cmd == "health":
        Path(args.issues_out).write_text(str(run_health(args.artifact_dir, args.repo, args.name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
