"""CLI adapter between the action and the CodeBoarding analysis ENGINE.

No analysis logic lives here. The engine is the published ``codeboarding`` PyPI
package installed by the action (``codeboarding_workflows`` etc.); this module
just turns the action's shell steps into typed, tested calls into it. The engine
imports are best-effort at module load, so this file imports fine without the
package present — the metadata-only subcommands (``baseline-info``,
``baseline-depth``, ``validate-base``) run with the stdlib alone, and the tests
stub the engine modules to assert we call the engine with the right args.

Subcommands (all paths/refs come in as argv, never interpolated into source):

  base           --repo P --out D --name N --run-id ID --depth K --source-sha SHA
  seed           --repo P --out D --source-sha SHA
  head           --repo P --out D --name N --run-id ID --depth K --base-ref B --target-ref T --source-sha SHA
  health         --artifact-dir D --repo P --name N --issues-out FILE
  validate-base  --analysis F --expected-sha SHA [--expected-depth K]
  baseline-info  --analysis F
  baseline-depth --analysis F
  analyze        --repo P --out D --name N --run-id ID --source-sha SHA --depth K [--force-full]
  render         --analysis F --out D --repo-name N --repo-ref R [--format .md]
  concat         --docs-dir D --out F

REVIEW mode uses base/seed/head/health/validate-base; SYNC mode uses
baseline-info/analyze/render/concat. ``base`` runs a full analysis; ``seed``
builds the SHA-tagged static-analysis pkl for a committed-analysis.json baseline
(LSP + clustering, no LLM) so the incremental path can run; ``health`` writes
the WARNING/CRITICAL finding count to ``--issues-out`` (never fails the run);
``validate-base`` exits non-zero only when the committed baseline is unreadable
or — with ``--expected-depth`` — its depth_level is DEEPER than requested;
shallower is accepted because the engine records the depth reached, not the
depth asked. The baseline's metadata.commit_hash is informational in review
mode: sync commits generated artifacts on top of the analyzed source commit, so
review trusts the analysis.json committed at the PR target branch tip rather than
regenerating because the metadata SHA differs. ``baseline-info`` prints the
baseline's ``commit_hash=`` (empty unless present and SHA-shaped).

``head`` (review) and ``analyze`` (sync) are the SAME incremental-or-full
operation via the shared ``_incremental_or_full`` helper; they differ only in
where the base ref comes from (the PR target-branch SHA vs the committed analysis.json)
and in that ``analyze`` is baseline-aware (full when the baseline is missing,
lacks a commit_hash, or ``--force-full`` is set) and prints
``analysis_mode=full|incremental`` on stdout for the action to grep. Once an
analysis.json exists, its metadata.depth_level is the source of truth for
incremental and fallback-full depth; ``--depth`` is the cold-start/force-full
depth.
``render`` writes per-component markdown with root name ``overview``; ``concat``
joins overview.md first plus the remaining *.md (sorted) into one architecture
file.

Telemetry: ``CODEBOARDING_SOURCE`` is defaulted after argument parsing —
``sync`` for analyze/render/concat, ``github_action`` for everything else
(the sync seed step overrides it to ``sync`` via env, since ``seed`` is shared).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

# The engine packages are imported best-effort so the metadata-only subcommands
# (``baseline-info``, ``baseline-depth``, ``validate-base``) run with the stdlib
# alone — they parse a committed analysis.json and never touch the engine. The
# action invokes them BEFORE the engine package is pip-installed (e.g. while
# resolving the review depth), so a hard import here would break that step. The
# analysis subcommands that DO need the engine fail loudly when these are None.
try:
    from codeboarding_workflows.analysis import BaselineUnavailableError, run_full, run_incremental
    from codeboarding_workflows.rendering import render_docs
    from diagram_analysis.exceptions import IncrementalCacheMissingError
    from static_analyzer import get_static_analysis
    from static_analyzer.analysis_cache import StaticAnalysisCache
    from static_analyzer.cluster_helpers import build_all_cluster_results
except Exception:  # engine package not installed (metadata-only subcommands don't need it)
    BaselineUnavailableError = IncrementalCacheMissingError = _MissingEngine = type("_MissingEngine", (Exception,), {})
    run_full = run_incremental = render_docs = None
    get_static_analysis = StaticAnalysisCache = build_all_cluster_results = None

try:
    from health.models import Severity
    from health.runner import run_health_checks
except Exception as _health_import_error:  # engine without the health module
    Severity = None
    run_health_checks = None

# A committed analysis.json's commit_hash is trusted only as far as a SHA shape:
# it flows into GITHUB_OUTPUT, cache keys, and git refs, so anything else must
# be rejected before it reaches the action shell.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_DEFAULT_DEPTH = 2

# On the free hosted tier the proxy returns HTTP 402 with this message when the
# repo owner's weekly token budget is spent. We detect it so the action can post
# a helpful "add a key/license" comment instead of a generic failure.
_QUOTA_MARKER = "Resource exhausted: token limit reached"


def _is_quota_exhausted(exc: BaseException) -> bool:
    """True when *exc* (or its cause chain) looks like the proxy's 402 quota
    error. Matches on the HTTP status 402 when the exception exposes one, and on
    the marker string as a fallback (the engine wraps provider errors, so the
    status may only survive as text)."""
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        status = getattr(cur, "status_code", None) or getattr(cur, "status", None)
        if status == 402:
            return True
        if _QUOTA_MARKER in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _flag_quota_exhausted() -> None:
    """Drop the sentinel the action's failure-comment step branches on. Path comes
    from the action via CB_QUOTA_SENTINEL; best-effort (never masks the real error)."""
    sentinel = os.environ.get("CB_QUOTA_SENTINEL")
    if not sentinel:
        return
    try:
        Path(sentinel).write_text("1")
    except OSError:
        pass


def _log_path(output_dir: str, filename: str) -> str:
    return str(Path(output_dir) / filename)


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


def _supported_depth(metadata: dict) -> int | None:
    depth = _metadata_depth(metadata)
    return depth if depth in range(1, 5) else None


def _analysis_depth_or_default(output_dir: Path, default_depth: int = _DEFAULT_DEPTH) -> int:
    metadata = _load_metadata(output_dir / "analysis.json")
    if not isinstance(metadata, dict):
        return default_depth
    depth = _supported_depth(metadata)
    return depth if depth is not None else default_depth


def _metadata_commit(metadata: dict) -> str:
    value = metadata.get("commit_hash")
    return value if isinstance(value, str) else ""


def baseline_info(analysis_path: Path) -> str:
    """Return the committed baseline's commit_hash when present and SHA-shaped,
    else "". Sync mode uses this (via the ``baseline-info`` subcommand) instead
    of an inline shell heredoc, so baseline parsing + the SHA-shape guard live
    in one tested place.
    """
    metadata = _load_metadata(analysis_path)
    if not isinstance(metadata, dict):
        return ""
    commit = _metadata_commit(metadata)
    return commit if _SHA_RE.match(commit) else ""


def baseline_depth(analysis_path: Path) -> int | None:
    """Return the committed baseline's metadata.depth_level when present and a
    supported value (1-4), else None. Review mode uses this (via the
    ``baseline-depth`` subcommand) to analyze the PR head at the SAME depth the
    committed baseline was generated with, so head and base diff apples-to-apples
    instead of defaulting to a shallower depth and reporting phantom changes.
    Parsing + the supported-range guard live here so the action shell never reads
    the JSON inline (mirrors ``baseline_info``).
    """
    metadata = _load_metadata(analysis_path)
    if not isinstance(metadata, dict):
        return None
    return _supported_depth(metadata)


def validate_base_analysis(
    analysis_path: Path, expected_sha: str, expected_depth: int | None = None
) -> tuple[bool, str]:
    """Return whether ``analysis.json`` is valid for ``expected_sha``.

    Review mode reuses the analysis.json that is committed at the PR target branch tip. The
    diagram's metadata.commit_hash records the source commit analyzed by sync,
    but the sync commit itself necessarily has a newer SHA because it adds the
    generated artifacts. Treat that metadata SHA as provenance, not freshness.

    When ``expected_depth`` is given, a baseline whose metadata.depth_level
    parses as an int and is DEEPER than expected is rejected (review mode
    regenerates instead of diffing against a deeper analysis). A shallower
    depth_level is accepted: the engine records the depth actually REACHED,
    not the depth requested, so a depth-2 run on a repo where no component
    expands persists depth_level 1 — rejecting that would force a full
    regeneration on every PR without ever converging. A missing or
    unparseable depth_level is accepted — legacy baselines predate the field.

    Review now derives ``expected_depth`` from the committed baseline's own
    depth_level (via the ``baseline-depth`` subcommand), so the deeper-than-expected
    rejection no longer fires for the normal case — head and base are analyzed at
    the same depth. The rejection remains a safety net for an explicit
    ``depth_level`` input that is shallower than the committed baseline.
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
    if isinstance(actual_sha, str) and actual_sha:
        if actual_sha == expected_sha:
            message = f"Baseline analysis commit matches target branch commit {expected_sha}."
        else:
            message = f"Using committed baseline at target branch commit {expected_sha}; analysis metadata source is {actual_sha}."
    else:
        message = f"Using committed baseline at target branch commit {expected_sha}; analysis metadata.commit_hash is missing."

    if expected_depth is not None:
        baseline_depth = _metadata_depth(metadata)
        if baseline_depth is not None and baseline_depth > expected_depth:
            return (
                False,
                f"Baseline analysis depth_level {baseline_depth} is deeper than expected depth {expected_depth}.",
            )

    return True, message


def run_base(repo_path: str, output_dir: str, repo_name: str, run_id: str, depth: int, source_sha: str) -> None:
    res = run_full(
        repo_name=repo_name,
        repo_path=Path(repo_path),
        output_dir=Path(output_dir),
        run_id=run_id,
        log_path=_log_path(output_dir, "cb-base.log"),
        depth_level=depth,
        source_sha=source_sha,
    )
    print(f"Base analysis written: {res}")


def run_seed(repo_path: str, output_dir: str, source_sha: str) -> None:
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
    results = get_static_analysis(Path(repo_path), cache_dir=Path(output_dir), source_sha=source_sha)
    cluster_results = build_all_cluster_results(results)
    StaticAnalysisCache(Path(output_dir), Path(repo_path)).save(results, source_sha=source_sha)
    summary = ", ".join(f"{lang}={len(cr.clusters)}" for lang, cr in sorted(cluster_results.items()))
    print(f"Seeded static-analysis baseline in {output_dir} (clusters: {summary or 'none'})")


def _incremental_or_full(
    *,
    repo_path: str,
    output_dir: str,
    repo_name: str,
    run_id: str,
    depth: int,
    base_ref: str,
    target_ref: str,
    source_sha: str,
    log_name: str,
) -> str:
    """Run incremental from *base_ref*; on a cache/baseline miss, clear the
    output dir and run a full analysis. Returns "incremental" or "full".

    Shared by the review head path and the sync analyze path so the fallback
    rule (which exceptions degrade to full, and the clear-before-full) lives in
    exactly one place — a bug fixed here is fixed for both modes.
    """
    try:
        res = run_incremental(
            repo_path=Path(repo_path),
            output_dir=Path(output_dir),
            project_name=repo_name,
            run_id=run_id,
            log_path=_log_path(output_dir, log_name),
            base_ref=base_ref,
            target_ref=target_ref,
            source_sha=source_sha,
        )
        print(f"Analysis written: {res}")
        return "incremental"
    except (IncrementalCacheMissingError, BaselineUnavailableError) as exc:
        print(f"Incremental unavailable ({exc}); running full analysis.")
        out_path = Path(output_dir)
        fallback_depth = _analysis_depth_or_default(out_path, depth)
        _clear_dir(out_path)
        res = run_full(
            repo_name=repo_name,
            repo_path=Path(repo_path),
            output_dir=out_path,
            run_id=run_id,
            log_path=_log_path(output_dir, log_name),
            depth_level=fallback_depth,
            source_sha=source_sha,
        )
        print(f"Analysis written: {res}")
        return "full"


def run_head(
    repo_path: str,
    output_dir: str,
    repo_name: str,
    run_id: str,
    depth: int,
    base_ref: str,
    target_ref: str,
    source_sha: str,
    force_full: bool = False,
) -> None:
    """Review PR head: incremental from the PR target branch tip, full on a cache miss.

    Print the selected mode explicitly so GitHub Action logs make it obvious
    whether review used the incremental path or fell back to a full analysis.
    """
    if force_full:
        out_path = Path(output_dir)
        _clear_dir(out_path)
        res = run_full(
            repo_name=repo_name,
            repo_path=Path(repo_path),
            output_dir=out_path,
            run_id=run_id,
            log_path=_log_path(output_dir, "cb-head.log"),
            depth_level=depth,
            source_sha=source_sha,
        )
        print(f"Analysis written: {res}")
        print("head_analysis_mode=full")
        return

    mode = _incremental_or_full(
        repo_path=repo_path,
        output_dir=output_dir,
        repo_name=repo_name,
        run_id=run_id,
        depth=depth,
        base_ref=base_ref,
        target_ref=target_ref,
        source_sha=source_sha,
        log_name="cb-head.log",
    )
    print(f"head_analysis_mode={mode}")


def run_analyze(
    repo_path: str,
    output_dir: str,
    repo_name: str,
    run_id: str,
    source_sha: str,
    depth: int,
    force_full: bool = False,
) -> str:
    """Sync analysis: incremental from the committed baseline, full when the
    baseline is missing/unparseable or ``force_full`` is set. Prints
    ``analysis_mode=full|incremental`` on stdout — the action greps that line,
    so it must be printed exactly once per run.
    """
    out_path = Path(output_dir)

    def full(reason: str, analysis_depth: int) -> str:
        print(f"{reason}; running full analysis.")
        _clear_dir(out_path)
        res = run_full(
            repo_name=repo_name,
            repo_path=Path(repo_path),
            output_dir=out_path,
            run_id=run_id,
            log_path=_log_path(output_dir, "cb-sync.log"),
            depth_level=analysis_depth,
            source_sha=source_sha,
        )
        print(f"Analysis written: {res}")
        print("analysis_mode=full")
        return "full"

    if force_full:
        return full("Full analysis forced (force_full)", depth)

    metadata = _load_metadata(out_path / "analysis.json")
    if metadata is None:
        return full("No baseline analysis.json found", depth)

    baseline_depth = _supported_depth(metadata)
    if baseline_depth is None:
        return full("Baseline metadata.depth_level is missing", _DEFAULT_DEPTH)

    base_ref = _metadata_commit(metadata)
    if not base_ref:
        return full("Baseline metadata.commit_hash is missing", baseline_depth)

    mode = _incremental_or_full(
        repo_path=repo_path,
        output_dir=output_dir,
        repo_name=repo_name,
        run_id=run_id,
        depth=baseline_depth,
        base_ref=base_ref,
        target_ref=source_sha,
        source_sha=source_sha,
        log_name="cb-sync.log",
    )
    print(f"analysis_mode={mode}")
    return mode


def run_render(analysis: str, output_dir: str, repo_name: str, repo_ref: str, output_format: str) -> None:
    out_path = Path(output_dir)
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


def run_health(artifact_dir: str, repo_path: str, repo_name: str) -> int:
    """Return the WARNING/CRITICAL finding count; 0 on any failure (best-effort)."""
    report_count = _count_health_report(artifact_dir)
    if report_count is not None:
        print(f"Architecture issues found in health report: {report_count}")
        return report_count

    if Severity is None or run_health_checks is None:
        print(f"Health check skipped ({_health_import_error}).")
        return 0

    try:
        cache = StaticAnalysisCache(artifact_dir=Path(artifact_dir), repo_root=Path(repo_path))
        sa = cache.get()
        issues = 0
        if sa is not None:
            report = run_health_checks(sa, repo_name=repo_name, repo_path=Path(repo_path))
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
    b.add_argument("--depth", required=True, type=int, choices=range(1, 5))

    s = sub.add_parser("seed")
    for a in ("--repo", "--out", "--source-sha"):
        s.add_argument(a, required=True)

    h = sub.add_parser("head")
    for a in ("--repo", "--out", "--name", "--run-id", "--base-ref", "--target-ref", "--source-sha"):
        h.add_argument(a, required=True)
    h.add_argument("--depth", required=True, type=int, choices=range(1, 5))
    h.add_argument("--force-full", action="store_true", help="Run a full PR-head analysis instead of incremental.")

    hc = sub.add_parser("health")
    for a in ("--artifact-dir", "--repo", "--name", "--issues-out"):
        hc.add_argument(a, required=True)

    vb = sub.add_parser("validate-base")
    vb.add_argument("--analysis", required=True)
    vb.add_argument("--expected-sha", required=True)
    vb.add_argument("--expected-depth", type=int, choices=range(1, 5))

    bi = sub.add_parser("baseline-info")
    bi.add_argument("--analysis", required=True)

    bd = sub.add_parser("baseline-depth")
    bd.add_argument("--analysis", required=True)

    an = sub.add_parser("analyze")
    for a in ("--repo", "--out", "--name", "--run-id", "--source-sha"):
        an.add_argument(a, required=True)
    an.add_argument("--depth", required=True, type=int, choices=range(1, 5))
    an.add_argument("--force-full", action="store_true", help="Ignore any committed baseline and run a full analysis.")

    rn = sub.add_parser("render")
    for a in ("--analysis", "--out", "--repo-name", "--repo-ref"):
        rn.add_argument(a, required=True)
    rn.add_argument("--format", default=".md")

    cc = sub.add_parser("concat")
    for a in ("--docs-dir", "--out"):
        cc.add_argument(a, required=True)

    args = p.parse_args(argv)
    source = "sync" if args.cmd in ("analyze", "render", "concat") else "github_action"
    os.environ.setdefault("CODEBOARDING_SOURCE", source)
    try:
        if args.cmd == "base":
            run_base(args.repo, args.out, args.name, args.run_id, args.depth, args.source_sha)
        elif args.cmd == "seed":
            run_seed(args.repo, args.out, args.source_sha)
        elif args.cmd == "head":
            run_head(
                args.repo,
                args.out,
                args.name,
                args.run_id,
                args.depth,
                args.base_ref,
                args.target_ref,
                args.source_sha,
                # action.yml adds --force-full for EMPTY_BASE PRs (no comparison baseline).
                args.force_full,
            )
        elif args.cmd == "health":
            Path(args.issues_out).write_text(str(run_health(args.artifact_dir, args.repo, args.name)))
        elif args.cmd == "validate-base":
            ok, message = validate_base_analysis(Path(args.analysis), args.expected_sha, args.expected_depth)
            print(message)
            return 0 if ok else 1
        elif args.cmd == "baseline-info":
            print(f"commit_hash={baseline_info(Path(args.analysis))}")
        elif args.cmd == "baseline-depth":
            depth = baseline_depth(Path(args.analysis))
            print(f"depth_level={depth if depth is not None else ''}")
        elif args.cmd == "analyze":
            run_analyze(args.repo, args.out, args.name, args.run_id, args.source_sha, args.depth, args.force_full)
        elif args.cmd == "render":
            run_render(args.analysis, args.out, args.repo_name, args.repo_ref, args.format)
        elif args.cmd == "concat":
            run_concat(args.docs_dir, args.out)
    except BaseException as exc:
        # Free-tier weekly cap hit: drop a sentinel so the action posts a
        # "add a key/license" comment, then re-raise so the step still fails.
        if _is_quota_exhausted(exc):
            _flag_quota_exhausted()
            print(f"::error::{_QUOTA_MARKER}", flush=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
