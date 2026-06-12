"""Engine orchestration for the action — extracted from inline ``python -c`` blocks
in action.yml so it is checked in, reviewable, and unit-testable.

Subcommands (all paths/refs come in as argv, never interpolated into source):

  base           --repo P --out D --name N --run-id ID --depth K --source-sha SHA
  seed           --repo P --out D --source-sha SHA
  head           --repo P --out D --name N --run-id ID --depth K --base-ref B --target-ref T --source-sha SHA
  health         --artifact-dir D --repo P --name N --issues-out FILE
  validate-base  --analysis F --expected-sha SHA [--expected-depth K]
  analyze        --repo P --out D --name N --run-id ID --source-sha SHA --depth K
  render         --analysis F --out D --repo-name N --repo-ref R [--format .md]
  concat         --docs-dir D --out F

``base`` runs a full analysis; ``seed`` builds the SHA-tagged static-analysis
pkl for a baseline that came from a committed analysis.json (LSP + clustering,
no LLM) so ``head`` can run incrementally; ``head`` runs incremental, falling
back to full on ``IncrementalCacheMissingError``/``BaselineUnavailableError``;
``health`` writes the WARNING/CRITICAL finding count to ``--issues-out`` (and
never fails the run); ``validate-base`` exits non-zero when the committed
baseline cannot be reused for the PR base (wrong commit, or — with
``--expected-depth`` — a depth_level DEEPER than requested; shallower is
accepted because the engine records the depth reached, not the depth asked).

``analyze``/``render``/``concat`` power docs mode. ``analyze`` is
baseline-aware: full when the committed analysis.json is missing, deeper
than requested, or lacks a commit_hash; otherwise incremental with a fallback to
full on the same cache-miss errors as ``head`` — it prints
``analysis_mode=full`` or ``analysis_mode=incremental`` on stdout, which the
action greps. ``render`` writes per-component markdown with root name
``overview``; ``concat`` joins overview.md first plus the remaining *.md
(sorted) into one architecture file.

Telemetry: ``CODEBOARDING_SOURCE`` is defaulted after argument parsing —
``docs_action`` for analyze/render/concat, ``github_action`` for everything
else.

The engine (``codeboarding_workflows`` etc.) is imported lazily inside each
function so this module imports without the engine venv present — the tests stub
those modules and assert we call the engine with the right arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
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


def _load_metadata(analysis_path: Path) -> dict | None:
    try:
        data = json.loads(analysis_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _metadata_depth(metadata: dict) -> int | None:
    try:
        return int(metadata.get("depth_level"))
    except (TypeError, ValueError):
        return None


def _metadata_commit(metadata: dict) -> str:
    value = metadata.get("commit_hash")
    return value if isinstance(value, str) else ""


def _docs_only_baseline_drift(actual_sha: str, expected_sha: str) -> tuple[bool, str]:
    allowed_prefixes = (".codeboarding/", "docs/development/")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    ancestor = git("merge-base", "--is-ancestor", actual_sha, expected_sha)
    if ancestor.returncode != 0:
        detail = ancestor.stderr.strip() or f"{actual_sha} is not an ancestor of {expected_sha}"
        return False, detail

    diff = git("diff", "--name-only", f"{actual_sha}..{expected_sha}")
    if diff.returncode != 0:
        return False, diff.stderr.strip() or f"Could not diff {actual_sha}..{expected_sha}"

    paths = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    disallowed = [
        path
        for path in paths
        if not path.startswith(allowed_prefixes) and path not in (".codeboarding", "docs/development")
    ]
    if disallowed:
        preview = ", ".join(disallowed[:5])
        suffix = "" if len(disallowed) <= 5 else f", and {len(disallowed) - 5} more"
        return False, f"non-doc paths changed: {preview}{suffix}"
    return True, f"only generated docs changed between {actual_sha} and {expected_sha}"


def validate_base_analysis(
    analysis_path: Path, expected_sha: str, expected_depth: int | None = None
) -> tuple[bool, str]:
    """Return whether ``analysis.json`` is valid for ``expected_sha``.

    The PR action can only reuse a committed baseline when the diagram's own
    source commit matches the PR base commit, or when the PR base is a docs-bot
    commit whose only drift from that source commit is generated docs.

    When ``expected_depth`` is given, a baseline whose metadata.depth_level
    parses as an int and is DEEPER than expected is rejected (review mode
    regenerates instead of diffing against a deeper analysis). A shallower
    depth_level is accepted: the engine records the depth actually REACHED,
    not the depth requested, so a depth-2 run on a repo where no component
    expands persists depth_level 1 — rejecting that would force a full
    regeneration on every PR without ever converging. A missing or
    unparseable depth_level is accepted — legacy baselines predate the field.
    """
    try:
        data = json.loads(analysis_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"Baseline analysis is missing: {analysis_path}"
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Baseline analysis is unreadable: {exc}"

    if not isinstance(data, dict):
        return False, "Baseline analysis root is not a JSON object."

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return False, "Baseline analysis metadata is missing."

    actual_sha = metadata.get("commit_hash")
    if not isinstance(actual_sha, str) or not actual_sha:
        return False, "Baseline analysis metadata.commit_hash is missing."

    if actual_sha != expected_sha:
        ok, reason = _docs_only_baseline_drift(actual_sha, expected_sha)
        if not ok:
            return (
                False,
                f"Baseline analysis was generated for {actual_sha}, expected PR base {expected_sha} ({reason}).",
            )
        message = f"Baseline analysis was generated for {actual_sha}; valid for PR base {expected_sha} ({reason})."
    else:
        message = f"Baseline analysis commit matches PR base {expected_sha}."

    if expected_depth is not None:
        baseline_depth = _metadata_depth(metadata)
        if baseline_depth is not None and baseline_depth > expected_depth:
            return (
                False,
                f"Baseline analysis depth_level {baseline_depth} is deeper than expected depth {expected_depth}.",
            )

    return True, message


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


def run_head(
    repo: str, out: str, name: str, run_id: str, depth: int, base_ref: str, target_ref: str, source_sha: str
) -> None:
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


def run_analyze(repo: str, out: str, name: str, run_id: str, source_sha: str, depth: int) -> str:
    from codeboarding_workflows.analysis import BaselineUnavailableError, run_full, run_incremental
    from diagram_analysis.exceptions import IncrementalCacheMissingError

    out_path = Path(out)
    analysis_path = out_path / "analysis.json"

    def full(reason: str) -> str:
        print(f"{reason}; running full analysis.")
        _clear_dir(out_path)
        res = run_full(
            repo_name=name,
            repo_path=Path(repo),
            output_dir=out_path,
            run_id=run_id,
            log_path=_log_path(out, "cb-docs.log"),
            depth_level=depth,
            source_sha=source_sha,
        )
        print(f"Analysis written: {res}")
        print("analysis_mode=full")
        return "full"

    metadata = _load_metadata(analysis_path)
    if metadata is None:
        return full("No baseline analysis.json found")

    # The engine records the depth REACHED, not the depth requested, so a
    # shallower baseline is normal for repos that never expand (a strict !=
    # would force a full run on every push, never converging). Only a deeper
    # baseline proves the requested depth was lowered; honor it with a full run.
    baseline_depth = _metadata_depth(metadata)
    if baseline_depth is None:
        return full("Baseline metadata.depth_level is missing")
    if baseline_depth > depth:
        return full(f"Baseline depth {baseline_depth} is deeper than requested depth {depth}")

    base_ref = _metadata_commit(metadata)
    if not base_ref:
        return full("Baseline metadata.commit_hash is missing")

    try:
        res = run_incremental(
            repo_path=Path(repo),
            output_dir=out_path,
            project_name=name,
            run_id=run_id,
            log_path=_log_path(out, "cb-docs.log"),
            base_ref=base_ref,
            target_ref=source_sha,
            source_sha=source_sha,
        )
    except (IncrementalCacheMissingError, BaselineUnavailableError) as exc:
        return full(f"Incremental unavailable ({exc})")

    print(f"Analysis written: {res}")
    print("analysis_mode=incremental")
    return "incremental"


def run_render(analysis: str, out: str, repo_name: str, repo_ref: str, output_format: str) -> None:
    from codeboarding_workflows.rendering import render_docs

    out_path = Path(out)
    _clear_dir(out_path)
    render_docs(
        Path(analysis),
        repo_name=repo_name,
        repo_ref=repo_ref,
        temp_dir=out_path,
        format=output_format,
        root_name="overview",
    )
    print(f"Rendered docs in {out_path}")


def run_concat(docs_dir: str, out: str) -> None:
    docs_path = Path(docs_dir)
    overview = docs_path / "overview.md"
    if not overview.is_file():
        raise FileNotFoundError(f"Missing required root docs file: {overview}")

    files = [overview]
    files.extend(sorted(p for p in docs_path.glob("*.md") if p.name != "overview.md"))

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sections = [path.read_text(encoding="utf-8").rstrip() for path in files]
    out_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"Concatenated {len(files)} docs into {out_path}")


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

    vb = sub.add_parser("validate-base")
    vb.add_argument("--analysis", required=True)
    vb.add_argument("--expected-sha", required=True)
    vb.add_argument("--expected-depth", type=int, choices=range(1, 4))

    an = sub.add_parser("analyze")
    for a in ("--repo", "--out", "--name", "--run-id", "--source-sha"):
        an.add_argument(a, required=True)
    an.add_argument("--depth", required=True, type=int, choices=range(1, 4))

    rn = sub.add_parser("render")
    for a in ("--analysis", "--out", "--repo-name", "--repo-ref"):
        rn.add_argument(a, required=True)
    rn.add_argument("--format", default=".md")

    cc = sub.add_parser("concat")
    for a in ("--docs-dir", "--out"):
        cc.add_argument(a, required=True)

    args = p.parse_args(argv)
    source = "docs_action" if args.cmd in ("analyze", "render", "concat") else "github_action"
    os.environ.setdefault("CODEBOARDING_SOURCE", source)
    if args.cmd == "base":
        run_base(args.repo, args.out, args.name, args.run_id, args.depth, args.source_sha)
    elif args.cmd == "seed":
        run_seed(args.repo, args.out, args.source_sha)
    elif args.cmd == "head":
        run_head(
            args.repo, args.out, args.name, args.run_id, args.depth, args.base_ref, args.target_ref, args.source_sha
        )
    elif args.cmd == "health":
        Path(args.issues_out).write_text(str(run_health(args.artifact_dir, args.repo, args.name)))
    elif args.cmd == "validate-base":
        ok, message = validate_base_analysis(Path(args.analysis), args.expected_sha, args.expected_depth)
        print(message)
        return 0 if ok else 1
    elif args.cmd == "analyze":
        run_analyze(args.repo, args.out, args.name, args.run_id, args.source_sha, args.depth)
    elif args.cmd == "render":
        run_render(args.analysis, args.out, args.repo_name, args.repo_ref, args.format)
    elif args.cmd == "concat":
        run_concat(args.docs_dir, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
