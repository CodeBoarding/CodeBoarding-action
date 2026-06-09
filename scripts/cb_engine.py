"""Engine orchestration for the action — extracted from inline ``python -c`` blocks
in action.yml so it is checked in, reviewable, and unit-testable.

Subcommands (all paths/refs come in as argv, never interpolated into source):

  base    --repo P --out D --name N --run-id ID --depth K --source-sha SHA
  seed    --repo P --out D --source-sha SHA
  head    --repo P --out D --name N --run-id ID --depth K --base-ref B --target-ref T --source-sha SHA
  health  --artifact-dir D --repo P --name N --issues-out FILE

``base`` runs a full analysis; ``seed`` builds the SHA-tagged static-analysis
pkl for a baseline that came from a committed analysis.json (LSP + clustering,
no LLM) so ``head`` can run incrementally; ``head`` runs incremental, falling
back to full on ``IncrementalCacheMissingError``/``BaselineUnavailableError``;
``health`` writes the WARNING/CRITICAL finding count to ``--issues-out`` (and
never fails the run).

The engine (``codeboarding_workflows`` etc.) is imported lazily inside each
function so this module imports without the engine venv present — the tests stub
those modules and assert we call the engine with the right arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def _log_path(out: str, name: str) -> str:
    return str(Path(out) / name)


def _clear_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def run_base(repo: str, out: str, name: str, run_id: str, depth: int, source_sha: str) -> None:
    from codeboarding_workflows.analysis import run_full

    res = run_full(
        repo_name=name,
        repo_path=Path(repo),
        output_dir=Path(out),
        run_id=run_id,
        log_path=_log_path(out, "cb-base.log"),
        depth_level=depth,
        source_sha=source_sha,
    )
    print(f"Base analysis written: {res}")


def run_seed(repo: str, out: str, source_sha: str) -> None:
    """Build the SHA-tagged static-analysis artifact for *repo* with no LLM calls.

    A committed analysis.json gives the head analysis its component ids, but
    the engine's incremental path also needs the base ``static_analysis.pkl``
    with a populated cluster cache — which ``git show`` of analysis.json can
    never provide. LSP indexing plus Leiden clustering are deterministic and
    cost no LLM spend, so the action seeds the pkl here instead of letting the
    head run degrade to a full analysis.

    ``build_all_cluster_results`` is the same call the full run's abstraction
    agent makes, so the seeded cluster baseline matches a real full analysis.
    The explicit ``save`` AFTER clustering matters: ``get_static_analysis``
    persists the pkl on LSP teardown, before clustering — saving only there
    would recreate the pkl-without-cluster-baseline state this fixes.

    Errors propagate; the action step treats a failed seed as fail-open (the
    head run falls back to a full analysis, today's behavior).
    """
    from static_analyzer import get_static_analysis
    from static_analyzer.analysis_cache import StaticAnalysisCache
    from static_analyzer.cluster_helpers import build_all_cluster_results

    results = get_static_analysis(Path(repo), cache_dir=Path(out), source_sha=source_sha)
    cluster_results = build_all_cluster_results(results)
    StaticAnalysisCache(Path(out), Path(repo)).save(results, source_sha=source_sha)
    summary = ", ".join(f"{lang}={len(cr.clusters)}" for lang, cr in sorted(cluster_results.items()))
    print(f"Seeded static-analysis baseline in {out} (clusters: {summary or 'none'})")


def run_head(repo: str, out: str, name: str, run_id: str, depth: int, base_ref: str, target_ref: str, source_sha: str) -> None:
    from codeboarding_workflows.analysis import BaselineUnavailableError, run_full, run_incremental
    from diagram_analysis.exceptions import IncrementalCacheMissingError

    try:
        res = run_incremental(
            repo_path=Path(repo),
            output_dir=Path(out),
            project_name=name,
            run_id=run_id,
            log_path=_log_path(out, "cb-head.log"),
            base_ref=base_ref,
            target_ref=target_ref,
            source_sha=source_sha,
        )
    except (IncrementalCacheMissingError, BaselineUnavailableError) as exc:
        print(f"Incremental unavailable ({exc}); running full analysis on head.")
        _clear_dir(Path(out))
        res = run_full(
            repo_name=name,
            repo_path=Path(repo),
            output_dir=Path(out),
            run_id=run_id,
            log_path=_log_path(out, "cb-head.log"),
            depth_level=depth,
            source_sha=source_sha,
        )
    print(f"Head analysis written: {res}")


def _count_report_issues(report: dict) -> int:
    issues = 0
    if not isinstance(report, dict):
        raise ValueError("health report root is not an object")
    for cs in report.get("check_summaries") or []:
        if not isinstance(cs, dict):
            continue
        for fg in cs.get("finding_groups") or []:
            if not isinstance(fg, dict):
                continue
            if fg.get("severity") in ("warning", "critical"):
                entities = fg.get("entities") or []
                issues += len(entities) if isinstance(entities, list) else 0
    return issues


def _count_health_report(artifact_dir: str) -> int | None:
    report_path = Path(artifact_dir) / "health" / "health_report.json"
    if not report_path.is_file():
        return None
    try:
        return _count_report_issues(json.loads(report_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Health report unreadable ({exc}); falling back to health runner.")
        return None


def run_health(artifact_dir: str, repo: str, name: str) -> int:
    """Return the WARNING/CRITICAL finding count; 0 on any failure (best-effort)."""
    report_count = _count_health_report(artifact_dir)
    if report_count is not None:
        print(f"Architecture issues found in health report: {report_count}")
        return report_count

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
    os.environ.setdefault("CODEBOARDING_SOURCE", "github_action")
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("base")
    for a in ("--repo", "--out", "--name", "--run-id", "--source-sha"):
        b.add_argument(a, required=True)
    b.add_argument("--depth", required=True, type=int, choices=range(1, 4))

    s = sub.add_parser("seed")
    for a in ("--repo", "--out", "--source-sha"):
        s.add_argument(a, required=True)

    h = sub.add_parser("head")
    for a in ("--repo", "--out", "--name", "--run-id", "--base-ref", "--target-ref", "--source-sha"):
        h.add_argument(a, required=True)
    h.add_argument("--depth", required=True, type=int, choices=range(1, 4))

    hc = sub.add_parser("health")
    for a in ("--artifact-dir", "--repo", "--name", "--issues-out"):
        hc.add_argument(a, required=True)

    args = p.parse_args(argv)
    if args.cmd == "base":
        run_base(args.repo, args.out, args.name, args.run_id, args.depth, args.source_sha)
    elif args.cmd == "seed":
        run_seed(args.repo, args.out, args.source_sha)
    elif args.cmd == "head":
        run_head(args.repo, args.out, args.name, args.run_id, args.depth, args.base_ref, args.target_ref, args.source_sha)
    elif args.cmd == "health":
        Path(args.issues_out).write_text(str(run_health(args.artifact_dir, args.repo, args.name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
