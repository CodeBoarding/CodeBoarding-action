"""Diff two analysis.json files into the ComponentDiffResult shape the webview expects.

Vendored port of ``CodeBoarding-wrapper/codeboarding_pro/diff/{tree_diff,loader,types}.py``.
Pure stdlib so it has no dependency on Core's pydantic models — the action's
analysis step writes plain JSON which we read directly here.

Wire format target: ``CodeBoarding-vscode/webview-ui/src/types/commitDiff.ts``.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def load_analysis_at_commit(repo_dir: Path, commit_hash: str, path_in_repo: str) -> dict | None:
    if not _COMMIT_HASH_RE.match(commit_hash):
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{commit_hash}:{path_in_repo}"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def load_analysis_from_path(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _comp_id(c: dict) -> str:
    return c.get("component_id") or c.get("name", "")


def _comp_name(c: dict) -> str:
    return c.get("name", "")


def _file_methods(c: dict) -> list[dict]:
    return c.get("file_methods") or []


def _methods_by_file(c: dict) -> dict[str, set[str]]:
    by_file: dict[str, set[str]] = {}
    for fm in _file_methods(c):
        fp = fm.get("file_path") or ""
        names = {m for m in (fm.get("methods") or []) if isinstance(m, str)}
        if names:
            by_file.setdefault(fp, set()).update(names)
    return by_file


def _has_structural_changes(base: dict, current: dict) -> bool:
    base_files = {fm.get("file_path", "") for fm in _file_methods(base)}
    current_files = {fm.get("file_path", "") for fm in _file_methods(current)}
    if base_files != current_files:
        return True
    base_sub_count = len(base.get("components") or [])
    current_sub_count = len(current.get("components") or [])
    if base_sub_count != current_sub_count:
        return True
    return False


def _diff_methods(base: dict, current: dict) -> dict:
    base_by_file = _methods_by_file(base)
    current_by_file = _methods_by_file(current)
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    for file_path in set(base_by_file) | set(current_by_file):
        added_in_file = sorted(current_by_file.get(file_path, set()) - base_by_file.get(file_path, set()))
        removed_in_file = sorted(base_by_file.get(file_path, set()) - current_by_file.get(file_path, set()))
        if added_in_file:
            added[file_path] = added_in_file
        if removed_in_file:
            removed[file_path] = removed_in_file
    return {"added": added, "removed": removed}


def _rel_key(r: dict) -> tuple[str, str]:
    src = r.get("src_id") or r.get("src_name") or ""
    dst = r.get("dst_id") or r.get("dst_name") or ""
    return (src, dst)


def _diff_relations(base_rels: list[dict], current_rels: list[dict]) -> list[dict]:
    base_edges = {_rel_key(r): r for r in (base_rels or [])}
    current_edges = {_rel_key(r): r for r in (current_rels or [])}
    result: list[dict] = []
    for key, rel in current_edges.items():
        status = "unchanged" if key in base_edges else "added"
        result.append({**rel, "diff_status": status})
    for key, rel in base_edges.items():
        if key not in current_edges:
            result.append({**rel, "diff_status": "deleted"})
    return result


def _diff_components(base_components: list[dict], current_components: list[dict]) -> list[dict]:
    base = base_components or []
    current = current_components or []
    base_by_id = {_comp_id(c): c for c in base}
    base_by_name = {_comp_name(c): c for c in base}
    matched_base_ids: set[str] = set()
    result: list[dict] = []

    for comp in current:
        base_match = base_by_id.get(_comp_id(comp)) or base_by_name.get(_comp_name(comp))
        if base_match is None:
            result.append({**comp, "diff_status": "added"})
            continue
        matched_base_ids.add(_comp_id(base_match))
        structural = _has_structural_changes(base_match, comp)
        method_diff = _diff_methods(base_match, comp)
        has_method_changes = bool(method_diff["added"] or method_diff["removed"])
        diff_status = "modified" if (structural or has_method_changes) else "unchanged"

        annotated = {**comp, "diff_status": diff_status, "method_diff": method_diff}

        base_subs = base_match.get("components") or []
        current_subs = comp.get("components") or []
        if base_subs or current_subs:
            annotated["components"] = _diff_components(base_subs, current_subs)

        base_sub_rels = base_match.get("components_relations") or []
        current_sub_rels = comp.get("components_relations") or []
        if base_sub_rels or current_sub_rels:
            annotated["components_relations"] = _diff_relations(base_sub_rels, current_sub_rels)

        result.append(annotated)

    for comp in base:
        if _comp_id(comp) not in matched_base_ids:
            ghost = {
                k: v for k, v in comp.items()
                if k not in ("components", "components_relations", "can_expand")
            }
            ghost["diff_status"] = "deleted"
            ghost["can_expand"] = False
            result.append(ghost)

    return result


def build_commit_diff_result(base: dict, current: dict, base_commit: str) -> dict:
    return {
        "baseCommit": base_commit,
        "components": _diff_components(base.get("components") or [], current.get("components") or []),
        "components_relations": _diff_relations(
            base.get("components_relations") or [],
            current.get("components_relations") or [],
        ),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo-dir", required=True, type=Path)
    p.add_argument("--base-commit", required=True)
    p.add_argument("--current-analysis", required=True, type=Path,
                   help="Path to the freshly-generated analysis.json (after PR head was analyzed)")
    p.add_argument("--analysis-path-in-repo", default=".codeboarding/analysis.json")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    base = load_analysis_at_commit(args.repo_dir, args.base_commit, args.analysis_path_in_repo)
    if base is None:
        print(f"::warning::No analysis.json at base commit {args.base_commit} ({args.analysis_path_in_repo}). "
              f"Cannot produce a diff.", file=sys.stderr)
        args.out.write_text(json.dumps({"error": "no_base_analysis", "baseCommit": args.base_commit}))
        return 2

    current = load_analysis_from_path(args.current_analysis)
    if current is None:
        print(f"::error::Could not read current analysis.json at {args.current_analysis}", file=sys.stderr)
        return 1

    diff = build_commit_diff_result(base, current, args.base_commit)
    args.out.write_text(json.dumps(diff))
    n_changed = sum(
        1 for c in diff["components"] if c.get("diff_status") in ("added", "deleted", "modified")
    )
    print(f"Diff written to {args.out}. {n_changed} top-level component(s) changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
