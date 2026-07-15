"""Render per-component changed-file dropdowns for the sticky PR comment.

Takes the same base/head ``analysis.json`` pair as ``diff_to_mermaid.py`` (the
diff logic is imported from there, so the dropdowns and the diagram always
agree on what changed) and emits one collapsed ``<details>`` block per changed
top-level component, listing the files that made it change color — the question
the colored diagram raises but can't answer.

Which files count as "changed" for a component:

  * With ``--changed-files`` (a ``git diff --no-renames --name-only`` listing of
    the PR's own changes, merge-base..head — the same set as the Files-changed
    tab): the intersection of the component subtree's file paths with that
    listing. A component can own 40 files while the PR touched 2 — listing all
    40 would answer the wrong question. ``--no-renames`` matters: with rename
    detection on, a moved file's old path never appears in ``--name-only`` and
    the donor component's dropdown would silently vanish. Node colors compare
    head against the target branch tip, so a node colored only by target-branch
    churn on a stale PR intentionally gets no dropdown.
  * Without it: the analysis-derived change set — files added to / removed
    from the component plus files whose method set changed. This misses
    body-only edits (the analysis can't see them), so the git listing is
    preferred.

Component file paths come from ``file_methods[].file_path`` plus
``key_entities[].reference_file`` — some engine outputs (observed on
TypeScript repos) carry file linkage only in ``key_entities``.

Names and paths are emitted as HTML (``<b>``/``<code>``, escaped) rather than
markdown spans so arbitrary repo content can't break the comment markup.
Per-component and total size caps keep the block a small fraction of GitHub's
65,536-char comment limit (the Mermaid diagram alone may use ~45k).

Self-contained stdlib; imports the diff from its sibling diff_to_mermaid.py.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff_to_mermaid as dm  # noqa: E402

# Budget: ~45k diagram + this block + ~1.5k header/CTA/footer must stay under
# GitHub's 65,536-char comment cap; overflow drops dropdowns, never the diagram.
MAX_TEXT = 10_000
MAX_FILES_PER_COMPONENT = 15

_NOUN = {"added": "added", "modified": "changed", "deleted": "removed"}


def _walk(comp: dict, skip_deleted: bool = False):
    """Yield ``comp`` and its whole subtree; optionally prune deleted ghosts."""
    if skip_deleted and comp.get("diff_status") == "deleted":
        return
    yield comp
    for sub in comp.get("components") or []:
        yield from _walk(sub, skip_deleted)


def _subtree_files(comp: dict | None, skip_deleted: bool = False) -> set:
    if comp is None:
        return set()
    files: set = set()
    for c in _walk(comp, skip_deleted):
        files.update(fm.get("file_path") or "" for fm in c.get("file_methods") or [])
        # Some engine outputs carry file linkage only in key_entities.
        files.update(ke.get("reference_file") or "" for ke in c.get("key_entities") or [])
    files.discard("")
    return files


def _subtree_methods(comp: dict | None, skip_deleted: bool = False) -> dict:
    merged: dict = {}
    if comp is None:
        return merged
    for c in _walk(comp, skip_deleted):
        for fp, names in dm._methods_by_file(c).items():
            merged.setdefault(fp, set()).update(names)
    merged.pop("", None)  # entries lacking file_path must not become a phantom file
    return merged


def _changed_files_for(comp: dict, base_match: dict | None, changed_files: set | None) -> list:
    """Files to list for one changed top-level component (see module docstring)."""
    if changed_files is not None:
        # Ghost subtrees inside the diff carry base-side files, so the union of
        # both sides covers added, modified, and deleted components alike.
        return sorted((_subtree_files(comp) | _subtree_files(base_match)) & changed_files)
    head_files = _subtree_files(comp, skip_deleted=True)
    base_files = _subtree_files(base_match)
    head_methods = _subtree_methods(comp, skip_deleted=True)
    base_methods = _subtree_methods(base_match)
    method_changed = {
        fp for fp in set(head_methods) | set(base_methods) if head_methods.get(fp, set()) != base_methods.get(fp, set())
    }
    return sorted((head_files ^ base_files) | method_changed)


def _block(name: str, status: str, files: list, n_sub: int = 0) -> str:
    shown = files[:MAX_FILES_PER_COMPONENT]
    hidden = len(files) - len(shown)
    n = len(files)
    # Rollup parents (only nested components changed) carry the recursive count
    # the headline and diagram use, so the dropdown explains "3 components
    # changed" instead of contradicting it.
    rollup = f"{n_sub} changed sub-component{'' if n_sub == 1 else 's'}, " if n_sub else ""
    lines = [
        "<details>",
        f"<summary><b>{html.escape(name)}</b> : {rollup}{n} file{'' if n == 1 else 's'} {_NOUN[status]}</summary>",
        "",  # blank line: required for GitHub to render markdown after </summary>
    ]
    lines += [f"- <code>{html.escape(fp)}</code>" for fp in shown]
    if hidden:
        lines.append(f"- <sub>…and {hidden} more</sub>")
    lines += ["", "</details>"]
    return "\n".join(lines)


def render_component_files(
    diff: dict,
    base: dict,
    changed_files: set | None = None,
    max_chars: int = MAX_TEXT,
) -> tuple:
    """Return (markdown_text, meta). ``markdown_text`` is "" when there's nothing to list."""
    base_by_name = {dm._comp_name(c): c for c in base.get("components") or []}
    blocks: list = []  # (block_text, n_files_listed)
    truncated = False
    for comp in diff.get("components") or []:
        status = dm._display_status(comp)
        if status not in dm.CHANGED:
            continue
        # A deleted ghost IS its base component (the diff builds it from base),
        # so use it directly; the name lookup would misattribute files when two
        # top-level components share a name.
        base_match = comp if comp.get("diff_status") == "deleted" else base_by_name.get(dm._comp_name(comp))
        files = _changed_files_for(comp, base_match, changed_files)
        if not files:
            continue  # e.g. relation-only change, comparison-branch churn, or a reorg the PR didn't touch
        truncated = truncated or len(files) > MAX_FILES_PER_COMPONENT
        n_sub = (
            dm._count_changed_components(comp.get("components") or [])
            if comp.get("diff_status") not in dm.CHANGED
            else 0
        )
        blocks.append(
            (
                _block(dm._comp_name(comp) or dm._comp_id(comp) or "(unnamed)", status, files, n_sub),
                min(len(files), MAX_FILES_PER_COMPONENT),
            )
        )

    rendered: list = []
    size = 0
    n_components = n_files = 0
    for i, (block, n_listed) in enumerate(blocks):
        if size + len(block) > max_chars:
            truncated = True
            if rendered:  # never emit a dangling "…and N more" with no blocks above it
                left = len(blocks) - i
                rendered.append(f"<sub>…and {left} more changed component{'' if left == 1 else 's'}</sub>")
            break
        rendered.append(block)
        size += len(block) + 1
        n_components += 1
        n_files += n_listed

    text = "\n".join(rendered)
    meta = {"rendered": bool(text), "n_components": n_components, "n_files": n_files, "truncated": truncated}
    return text, meta


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True, type=Path, help="Path to the base (before) analysis.json")
    p.add_argument("--head", required=True, type=Path, help="Path to the head (after) analysis.json")
    p.add_argument("--out", required=True, type=Path, help="Where to write the <details> markdown block")
    p.add_argument(
        "--changed-files",
        type=Path,
        default=None,
        help="File with the PR's changed paths, one per line (git diff --no-renames "
        "--name-only merge-base..head). Omit to fall back to analysis-derived changes.",
    )
    args = p.parse_args()

    changed: set | None = None
    if args.changed_files is not None:
        try:
            # surrogateescape: core.quotepath=off emits raw filename bytes; a
            # non-UTF-8 path must not kill the whole section (it simply won't
            # intersect with the analysis's UTF-8 paths).
            raw = args.changed_files.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as exc:
            sys.exit(f"::error::Could not read changed-files list at {args.changed_files}: {exc}")
        changed = {line.strip() for line in raw.splitlines() if line.strip()}

    # Use the same projected relation view as Mermaid so relation-only changes
    # agree on which parent component changed. Projection does not alter the
    # component file membership consumed below.
    base = dm.load_analysis(args.base)
    head = dm.load_analysis(args.head)
    text, meta = render_component_files(dm.build_diff(base, head), base, changed)

    # Trailing newline so the following CTA's "---" isn't absorbed into the
    # </details> HTML block; conditional so an empty result stays 0 bytes for
    # the action's [ -s ] gate.
    args.out.write_text(text + "\n" if text else "", encoding="utf-8")
    # Machine-readable summary on stdout for the action to consume.
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
