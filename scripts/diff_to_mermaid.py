"""Diff two CodeBoarding analysis.json files and render the delta as a colored Mermaid graph.

Reads a *base* (before) and *head* (after) ``analysis.json`` — both already
materialized on disk by the engine — computes a component/relation diff, and
emits a GitHub-renderable ```mermaid block where:

  * nodes are colored  green=added / yellow=modified / red=deleted (deleted dashed)
  * arrows are colored the same way (red dashed for deleted)

GitHub renders ```mermaid fenced blocks natively inside PR/issue comments, so the
output goes straight into the sticky comment — no image, no Playwright.

The diff set-arithmetic is a port of the action's ``compute_diff.py``, with two
differences for this use case: both sides are read from plain file paths (not
``git show``), and a relation whose ``(src, dst)`` is unchanged but whose label
text changed is reported as ``modified`` (the original only did added/deleted).

Self-contained stdlib.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# GitHub's mermaid config caps (config.schema.yaml defaults; NOT raisable on
# GitHub). Exceeding either renders a red error box with no diagram, so we stay
# comfortably under and degrade to a changed-only / text fallback instead.
MAX_EDGES = 480          # hard cap 500
MAX_TEXT = 45_000        # hard cap 50000 chars

# Primer-ish fills that read on both light and dark GitHub backgrounds. White
# label text is set explicitly so it survives dark mode.
COLORS = {
    "added": {"fill": "#1f883d", "stroke": "#0b5d23"},
    "modified": {"fill": "#bf8700", "stroke": "#7d4e00"},
    "deleted": {"fill": "#cf222e", "stroke": "#82071e"},
}
CHANGED = ("added", "modified", "deleted")
_EDGE_LABEL_MAX = 48


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #
def load_analysis(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"::error::Could not read analysis JSON at {path}: {exc}")


# --------------------------------------------------------------------------- #
# diff (ported from compute_diff.py; relation diff extended with 'modified')
# --------------------------------------------------------------------------- #
def _comp_id(c: dict) -> str:
    return c.get("component_id") or c.get("name", "")


def _comp_name(c: dict) -> str:
    return c.get("name", "")


def _file_methods(c: dict) -> list:
    return c.get("file_methods") or []


def _methods_by_file(c: dict) -> dict:
    by_file: dict = {}
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
    if len(base.get("components") or []) != len(current.get("components") or []):
        return True
    return False


def _diff_methods(base: dict, current: dict) -> dict:
    base_by_file = _methods_by_file(base)
    current_by_file = _methods_by_file(current)
    added: dict = {}
    removed: dict = {}
    for file_path in set(base_by_file) | set(current_by_file):
        a = sorted(current_by_file.get(file_path, set()) - base_by_file.get(file_path, set()))
        r = sorted(base_by_file.get(file_path, set()) - current_by_file.get(file_path, set()))
        if a:
            added[file_path] = a
        if r:
            removed[file_path] = r
    return {"added": added, "removed": removed}


def _rel_key(r: dict) -> tuple:
    # Name is the stable join across two independent analyses; component ids are
    # positional and can be reshuffled on a full re-run, so prefer names.
    return (r.get("src_name") or r.get("src_id") or "", r.get("dst_name") or r.get("dst_id") or "")


def _diff_relations(base_rels: list, current_rels: list) -> list:
    base_edges = {_rel_key(r): r for r in (base_rels or [])}
    current_edges = {_rel_key(r): r for r in (current_rels or [])}
    result: list = []
    for key, rel in current_edges.items():
        if key not in base_edges:
            status = "added"
        elif (base_edges[key].get("relation") or "") != (rel.get("relation") or ""):
            status = "modified"
        else:
            status = "unchanged"
        result.append({**rel, "diff_status": status})
    for key, rel in base_edges.items():
        if key not in current_edges:
            result.append({**rel, "diff_status": "deleted"})
    return result


def _diff_components(base_components: list, current_components: list) -> list:
    base = base_components or []
    current = current_components or []
    base_by_name = {_comp_name(c): c for c in base}  # name is the stable cross-analysis join
    matched_names: set = set()
    result: list = []

    for comp in current:
        base_match = base_by_name.get(_comp_name(comp))
        if base_match is None:
            result.append({**comp, "diff_status": "added"})
            continue
        matched_names.add(_comp_name(base_match))
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
        if _comp_name(comp) not in matched_names:
            ghost = {k: v for k, v in comp.items() if k not in ("components", "components_relations", "can_expand")}
            ghost["diff_status"] = "deleted"
            result.append(ghost)

    return result


def build_diff(base: dict, head: dict) -> dict:
    return {
        "components": _diff_components(base.get("components") or [], head.get("components") or []),
        "components_relations": _diff_relations(
            base.get("components_relations") or [],
            head.get("components_relations") or [],
        ),
    }


# --------------------------------------------------------------------------- #
# mermaid emit
# --------------------------------------------------------------------------- #
def _sanitize(name: str) -> str:
    """Match the engine's node-id sanitization (utils.sanitize)."""
    return re.sub(r"\W+", "_", name or "")


def _esc(text: str) -> str:
    """Escape arbitrary text for a mermaid label under GitHub's strict security.

    ``#`` first (so the entities we inject are not re-escaped), then ``"``.
    """
    out = (text or "").replace("\n", " ").replace("\r", " ").strip()
    out = out.replace("#", "#35;").replace('"', "#quot;")
    return out


def _truncate(text: str, limit: int = _EDGE_LABEL_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _Scope:
    """Per-level name/id -> mermaid key resolver for one nesting level.

    Deleted ghosts get a separate ``del_`` key namespace from present nodes so a
    reused id/name can't merge an added node onto a deleted one. Keys are made
    globally unique via the shared ``used`` set. Resolution is name-first (the
    stable cross-analysis join); present edges resolve head-first, deleted edges
    ghost-first. ``force`` overrides the per-component diff_status (used when a
    wholly-added/deleted parent colors its whole subtree).
    """

    def __init__(self, components: list, used: set, force: str | None = None):
        self.entries: list = []  # (key, label, status, component)
        self.head_by_id: dict = {}
        self.head_by_name: dict = {}
        self.del_by_id: dict = {}
        self.del_by_name: dict = {}
        for comp in components:
            status = force or comp.get("diff_status", "unchanged")
            present = status != "deleted"
            cid, cname = _comp_id(comp), _comp_name(comp)
            base = ("n_" if present else "del_") + _sanitize(cname or cid or "node")
            key, n = base, 1
            while key in used:
                n += 1
                key = f"{base}_{n}"
            used.add(key)
            self.entries.append((key, cname or cid or "(unnamed)", status, comp))
            by_id = self.head_by_id if present else self.del_by_id
            by_name = self.head_by_name if present else self.del_by_name
            if cname:
                by_name[cname] = key
            if cid:
                by_id[cid] = key

    def resolve(self, rid: str, rname: str, present: bool) -> str | None:
        maps = [(self.head_by_id, self.head_by_name), (self.del_by_id, self.del_by_name)]
        if not present:
            maps.reverse()
        for by_id, by_name in maps:
            if rname and rname in by_name:  # name-first: stable cross-analysis join
                return by_name[rname]
            if rid and rid in by_id:
                return by_id[rid]
        return None


def _filter_changed(components: list, relations: list) -> tuple:
    """Keep changed components, the endpoints of changed edges, and edges among the kept — the size fallback."""
    changed_rels = [r for r in relations if r.get("diff_status") in CHANGED]
    keep_ids: set = set()
    keep_names: set = set()
    for c in components:
        if c.get("diff_status") in CHANGED:
            keep_ids.add(_comp_id(c))
            keep_names.add(_comp_name(c))
    for r in changed_rels:  # so a changed edge between two unchanged nodes still draws its endpoints
        keep_ids.update((r.get("src_id", ""), r.get("dst_id", "")))
        keep_names.update((r.get("src_name", ""), r.get("dst_name", "")))

    kept = [c for c in components if _comp_id(c) in keep_ids or _comp_name(c) in keep_names]
    kept_ids = {_comp_id(c) for c in kept}
    kept_names = {_comp_name(c) for c in kept}

    def touches(r: dict, side_id: str, side_name: str) -> bool:
        return r.get(side_id, "") in kept_ids or r.get(side_name, "") in kept_names

    rels = [
        r
        for r in relations
        if r.get("diff_status") in CHANGED
        or (touches(r, "src_id", "src_name") and touches(r, "dst_id", "dst_name"))
    ]
    return kept, rels


def _init_directive(font_size, node_padding, node_spacing, rank_spacing) -> str | None:
    """Build a Mermaid ``%%{init}%%`` directive to enlarge nodes / spacing.

    Nodes auto-size to their label, so the effective levers are font size and
    interior padding (bigger nodes) plus node/rank spacing (less cramped). These
    config keys are honored by GitHub's strict renderer.
    """
    flowchart: dict = {}
    if node_padding is not None:
        flowchart["padding"] = node_padding
    if node_spacing is not None:
        flowchart["nodeSpacing"] = node_spacing
    if rank_spacing is not None:
        flowchart["rankSpacing"] = rank_spacing
    cfg: dict = {}
    if flowchart:
        cfg["flowchart"] = flowchart
    if font_size is not None:
        cfg["themeVariables"] = {"fontSize": f"{font_size}px"}
    return "%%{init: " + json.dumps(cfg) + "}%%" if cfg else None


def render_mermaid(
    diff: dict,
    direction: str = "LR",
    changed_only: bool = False,
    edge_labels: bool = True,
    render_depth: int = 1,
    font_size: int | None = None,
    node_padding: int | None = None,
    node_spacing: int | None = None,
    rank_spacing: int | None = None,
) -> tuple:
    """Return (mermaid_text, meta). ``mermaid_text`` is None when there's nothing to draw.

    ``render_depth`` controls how many component levels are drawn, independent of
    the engine's analysis depth: 1 = top-level flat (default), 2 = top-level plus
    one level of sub-components as subgraphs, etc. So you can analyze deep
    (depth_level=2) yet render a clean level-1 PR diagram. At each drawn nesting
    level, parent containers get a stroke-only ``*Box`` class and leaf nodes a
    filled class. A wholly-added parent forces ``added`` onto its subtree (the
    engine only diff-annotates surviving branches; an added subtree arrives raw).
    """
    components = diff.get("components") or []
    relations = diff.get("components_relations") or []
    n_changed = sum(1 for c in components if c.get("diff_status") in CHANGED)

    if changed_only or len(relations) > MAX_EDGES:
        components, relations = _filter_changed(components, relations)

    used: set = set()
    body: list = []
    node_classes: dict = {"added": [], "modified": [], "deleted": []}
    box_classes: dict = {"added": [], "modified": [], "deleted": []}
    edge_styles: dict = {"added": [], "modified": [], "deleted": []}
    counters = {"edges": 0, "nodes": 0}

    def emit_edges(rels: list, scope: _Scope, pad: str, force: str | None) -> None:
        for rel in rels:
            status = force or rel.get("diff_status", "unchanged")
            present = status != "deleted"
            src = scope.resolve(rel.get("src_id", ""), rel.get("src_name", ""), present)
            dst = scope.resolve(rel.get("dst_id", ""), rel.get("dst_name", ""), present)
            if src is None or dst is None:
                continue  # endpoint not drawn — skip, don't consume an edge index
            label = _esc(_truncate(rel.get("relation", ""))) if edge_labels else ""
            body.append(f'{pad}{src} -- "{label}" --> {dst}' if label else f"{pad}{src} --> {dst}")
            if status in edge_styles:
                edge_styles[status].append(counters["edges"])
            counters["edges"] += 1

    def emit_level(comps: list, rels: list, indent: int, force: str | None, level: int) -> None:
        pad = "    " * indent
        scope = _Scope(comps, used, force)
        for key, label, status, comp in scope.entries:
            children = comp.get("components") if level < render_depth else None  # cap drawn nesting
            if children:
                body.append(f'{pad}subgraph {key}["{_esc(label)}"]')
                if status in box_classes:
                    box_classes[status].append(key)
                child_force = force or (status if status == "added" else None)
                emit_level(children, comp.get("components_relations") or [], indent + 1, child_force, level + 1)
                body.append(f"{pad}end")
            else:
                body.append(f'{pad}{key}["{_esc(label)}"]')
                if status in node_classes:
                    node_classes[status].append(key)
            counters["nodes"] += 1
        emit_edges(rels, scope, pad, force)

    emit_level(components, relations, 1, None, 1)
    if counters["nodes"] == 0:
        return None, {"n_changed": n_changed, "n_nodes": 0, "n_edges": 0, "truncated": False}

    style: list = [
        f'    classDef added fill:{COLORS["added"]["fill"]},stroke:{COLORS["added"]["stroke"]},color:#ffffff;',
        f'    classDef modified fill:{COLORS["modified"]["fill"]},stroke:{COLORS["modified"]["stroke"]},color:#ffffff;',
        f'    classDef deleted fill:{COLORS["deleted"]["fill"]},stroke:{COLORS["deleted"]["stroke"]},'
        f"color:#ffffff,stroke-dasharray:5 3;",
    ]
    if any(box_classes.values()):  # stroke-only containers so big parents aren't solid blocks
        for st in CHANGED:
            dash = ",stroke-dasharray:5 3" if st == "deleted" else ""
            style.append(f'    classDef {st}Box stroke:{COLORS[st]["stroke"]},stroke-width:2px,fill:none{dash};')
    for status in CHANGED:
        if node_classes[status]:
            style.append(f'    class {",".join(node_classes[status])} {status};')
        if box_classes[status]:
            style.append(f'    class {",".join(box_classes[status])} {status}Box;')
    for status in CHANGED:
        idxs = edge_styles[status]
        if not idxs:
            continue
        s = f'stroke:{COLORS[status]["stroke"]},stroke-width:2px'
        if status == "deleted":
            s += ",stroke-dasharray:5 3"
        style.append(f'    linkStyle {",".join(str(i) for i in idxs)} {s};')

    directive = _init_directive(font_size, node_padding, node_spacing, rank_spacing)
    head = ["```mermaid"] + ([directive] if directive else []) + [f"graph {direction}"]
    text = "\n".join(head + body + style + ["```"])
    meta = {
        "n_changed": n_changed,
        "n_nodes": counters["nodes"],
        "n_edges": counters["edges"],
        "truncated": bool(changed_only or len(diff.get("components_relations") or []) > MAX_EDGES),
    }
    if len(text) > MAX_TEXT or counters["edges"] > MAX_EDGES:  # never trip GitHub's red error box
        meta["truncated"] = True
        return None, meta
    return text, meta


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True, type=Path, help="Path to the base (before) analysis.json")
    p.add_argument("--head", required=True, type=Path, help="Path to the head (after) analysis.json")
    p.add_argument("--out", required=True, type=Path, help="Where to write the ```mermaid block")
    p.add_argument("--direction", default="LR", choices=["LR", "TD", "TB", "RL", "BT"])
    p.add_argument("--changed-only", action="store_true", help="Render only changed components + incident edges")
    p.add_argument("--no-edge-labels", dest="edge_labels", action="store_false", help="Draw arrows without relation labels")
    p.add_argument("--render-depth", type=int, default=1, help="Component levels to draw: 1=top-level flat, 2=+one nesting level, ...")
    p.add_argument("--font-size", type=int, default=None, help="Node label font size in px (bigger label ⇒ bigger node)")
    p.add_argument("--node-padding", type=int, default=None, help="Interior padding around each node label")
    p.add_argument("--node-spacing", type=int, default=None, help="Space between nodes in the same rank")
    p.add_argument("--rank-spacing", type=int, default=None, help="Space between ranks")
    args = p.parse_args()

    diff = build_diff(load_analysis(args.base), load_analysis(args.head))
    mermaid, meta = render_mermaid(
        diff,
        direction=args.direction,
        changed_only=args.changed_only,
        edge_labels=args.edge_labels,
        render_depth=args.render_depth,
        font_size=args.font_size,
        node_padding=args.node_padding,
        node_spacing=args.node_spacing,
        rank_spacing=args.rank_spacing,
    )

    args.out.write_text(mermaid if mermaid is not None else "", encoding="utf-8")
    meta["rendered"] = mermaid is not None
    # Machine-readable summary on stdout for the action to consume.
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
