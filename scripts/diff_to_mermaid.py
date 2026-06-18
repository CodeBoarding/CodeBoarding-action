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
from collections import defaultdict
import json
import re
import sys
from pathlib import Path

# GitHub's mermaid config caps (config.schema.yaml defaults; NOT raisable on
# GitHub). Exceeding either renders a red error box with no diagram, so we stay
# comfortably under and degrade to a changed-only / text fallback instead.
MAX_EDGES = 480  # hard cap 500
MAX_TEXT = 45_000  # hard cap 50000 chars

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
    return base_files != current_files or len(base.get("components") or []) != len(current.get("components") or [])


def _has_method_changes(base: dict, current: dict) -> bool:
    base_by_file = _methods_by_file(base)
    current_by_file = _methods_by_file(current)
    return any(
        base_by_file.get(fp, set()) != current_by_file.get(fp, set()) for fp in set(base_by_file) | set(current_by_file)
    )


def _rel_key(r: dict) -> tuple:
    # Name is the stable join across two independent analyses; component ids are
    # positional and can be reshuffled on a full re-run, so prefer names.
    return (r.get("src_name") or r.get("src_id") or "", r.get("dst_name") or r.get("dst_id") or "")


def _diff_relations(base_rels: list, current_rels: list) -> list:
    base_by_endpoint: dict = defaultdict(list)
    current_by_endpoint: dict = defaultdict(list)
    for rel in base_rels or []:
        base_by_endpoint[_rel_key(rel)].append(rel)
    for rel in current_rels or []:
        current_by_endpoint[_rel_key(rel)].append(rel)

    result: list = []
    keys = list(current_by_endpoint)
    keys.extend(k for k in base_by_endpoint if k not in current_by_endpoint)
    for key in keys:
        base_group = base_by_endpoint.get(key, [])
        current_group = current_by_endpoint.get(key, [])
        if not base_group:
            result.extend({**rel, "diff_status": "added"} for rel in current_group)
            continue
        if not current_group:
            result.extend({**rel, "diff_status": "deleted"} for rel in base_group)
            continue

        if len(base_group) == 1 and len(current_group) == 1:
            status = (
                "unchanged"
                if (base_group[0].get("relation") or "") == (current_group[0].get("relation") or "")
                else "modified"
            )
            result.append({**current_group[0], "diff_status": status})
            continue

        unmatched_base = list(base_group)
        unmatched_current = []
        for rel in current_group:
            label = rel.get("relation") or ""
            match_idx = next((i for i, b in enumerate(unmatched_base) if (b.get("relation") or "") == label), None)
            if match_idx is None:
                unmatched_current.append(rel)
            else:
                unmatched_base.pop(match_idx)
                result.append({**rel, "diff_status": "unchanged"})

        if len(unmatched_base) == 1 and len(unmatched_current) == 1:
            result.append({**unmatched_current[0], "diff_status": "modified"})
        else:
            result.extend({**rel, "diff_status": "added"} for rel in unmatched_current)
            result.extend({**rel, "diff_status": "deleted"} for rel in unmatched_base)
    return result


def _has_changes(components: list, relations: list) -> bool:
    if any(r.get("diff_status") in CHANGED for r in relations or []):
        return True
    for comp in components or []:
        if comp.get("diff_status") in CHANGED:
            return True
        if _has_changes(comp.get("components") or [], comp.get("components_relations") or []):
            return True
    return False


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
        diff_status = "modified" if (structural or _has_method_changes(base_match, comp)) else "unchanged"

        annotated = {**comp, "diff_status": diff_status}

        base_subs = base_match.get("components") or []
        current_subs = comp.get("components") or []
        if base_subs or current_subs:
            annotated["components"] = _diff_components(base_subs, current_subs)

        base_sub_rels = base_match.get("components_relations") or []
        current_sub_rels = comp.get("components_relations") or []
        if base_sub_rels or current_sub_rels:
            annotated["components_relations"] = _diff_relations(base_sub_rels, current_sub_rels)

        if diff_status == "unchanged" and _has_changes(
            annotated.get("components") or [],
            annotated.get("components_relations") or [],
        ):
            annotated["display_status"] = "modified"

        result.append(annotated)

    for comp in base:
        if _comp_name(comp) not in matched_names:
            # Keep the subtree: a deleted parent's children/relations render as a
            # deleted subgraph (the renderer forces 'deleted' down), mirroring how
            # an added parent renders its whole subtree.
            ghost = {k: v for k, v in comp.items() if k != "can_expand"}
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


# Mermaid label metacharacters → numeric char refs (the ``#NNN;`` form GitHub's
# strict renderer accepts). A bare ``]`` / ``)`` / ``}`` terminates a node label
# and breaks the whole diagram, so escape the shape chars too — not just ``#``
# and ``"``.
_ESC_MAP = {
    "&": "#38;",
    '"': "#34;",
    "<": "#60;",
    ">": "#62;",
    "[": "#91;",
    "]": "#93;",
    "(": "#40;",
    ")": "#41;",
    "{": "#123;",
    "}": "#125;",
    "|": "#124;",
}


def _esc(text: str) -> str:
    """Escape arbitrary text for a Mermaid label under GitHub's strict renderer."""
    out = (text or "").replace("\n", " ").replace("\r", " ").strip()
    out = out.replace("#", "#35;")  # first: literal '#'; the entities below add their own '#'
    for ch, ent in _ESC_MAP.items():
        out = out.replace(ch, ent)
    return out


def _truncate(text: str, limit: int = _EDGE_LABEL_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _display_status(comp: dict, force: str | None = None) -> str:
    return force or comp.get("display_status") or comp.get("diff_status", "unchanged")


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
            status = _display_status(comp, force)
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
    """Keep changed components, changed-edge endpoints, ancestors, and edges among the kept."""
    changed_rels = [r for r in relations if r.get("diff_status") in CHANGED]
    keep_ids: set = set()
    keep_names: set = set()
    filtered_children: dict[int, tuple] = {}
    for c in components:
        child_components, child_relations = _filter_changed(
            c.get("components") or [],
            c.get("components_relations") or [],
        )
        filtered_children[id(c)] = (child_components, child_relations)
        if _display_status(c) in CHANGED or child_components or child_relations:
            keep_ids.add(_comp_id(c))
            keep_names.add(_comp_name(c))
    for r in changed_rels:  # so a changed edge between two unchanged nodes still draws its endpoints
        keep_ids.update(x for x in (r.get("src_id", ""), r.get("dst_id", "")) if x)
        keep_names.update(x for x in (r.get("src_name", ""), r.get("dst_name", "")) if x)
    keep_ids.discard("")
    keep_names.discard("")

    kept = []
    for c in components:
        if not ((_comp_id(c) and _comp_id(c) in keep_ids) or (_comp_name(c) and _comp_name(c) in keep_names)):
            continue
        child_components, child_relations = filtered_children[id(c)]
        status = _display_status(c)
        if child_components or child_relations or status == "modified":
            c = {**c, "components": child_components, "components_relations": child_relations}
        kept.append(c)
    kept_ids = {_comp_id(c) for c in kept if _comp_id(c)}
    kept_names = {_comp_name(c) for c in kept if _comp_name(c)}

    def touches(r: dict, side_id: str, side_name: str) -> bool:
        rid, rname = r.get(side_id, ""), r.get(side_name, "")
        return bool((rid and rid in kept_ids) or (rname and rname in kept_names))

    rels = [
        r
        for r in relations
        if r.get("diff_status") in CHANGED or (touches(r, "src_id", "src_name") and touches(r, "dst_id", "dst_name"))
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


def _count_changed_components(components: list) -> int:
    """Recursively count components whose diff_status is added/modified/deleted."""
    n = 0
    for c in components or []:
        if c.get("diff_status") in CHANGED:
            n += 1
        n += _count_changed_components(c.get("components") or [])
    return n


def _has_changed_relations(components: list, relations: list) -> bool:
    """Recursively: is any relation (at any nesting level) added/modified/deleted?"""
    return _has_changes([], relations) or any(
        _has_changed_relations(c.get("components") or [], c.get("components_relations") or []) for c in components or []
    )


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
    one level of sub-components as subgraphs, etc. ``meta`` reports ``n_changed``
    (recursive changed-component count) and ``changed`` (any changed component OR
    relation at any level) so the caller never mistakes a relation/nested change
    for "no changes". On overflow of GitHub's Mermaid caps the full graph degrades
    to a changed-only graph (and finally to None) rather than emitting an
    unrenderable blob.
    """
    all_components = diff.get("components") or []
    all_relations = diff.get("components_relations") or []
    n_changed = _count_changed_components(all_components)
    changed = n_changed > 0 or _has_changed_relations(all_components, all_relations)
    directive = _init_directive(font_size, node_padding, node_spacing, rank_spacing)

    def build(only_changed: bool):
        components, relations = (
            _filter_changed(all_components, all_relations) if only_changed else (all_components, all_relations)
        )
        used: set = set()
        body: list = []
        node_classes: dict = {"added": [], "modified": [], "deleted": []}
        box_classes: dict = {"added": [], "modified": [], "deleted": []}
        edge_styles: dict = {"added": [], "modified": [], "deleted": []}
        counters = {"edges": 0, "nodes": 0}

        def emit_edges(rels, scope, pad, force):
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

        def emit_level(comps, rels, indent, force, level):
            pad = "    " * indent
            scope = _Scope(comps, used, force)
            for key, label, status, comp in scope.entries:
                children = comp.get("components") if level < render_depth else None  # cap drawn nesting
                if children:
                    body.append(f'{pad}subgraph {key}["{_esc(label)}"]')
                    if status in box_classes:
                        box_classes[status].append(key)
                    child_force = force or (status if status in ("added", "deleted") else None)
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
            return None, 0, 0

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

        head = ["```mermaid"] + ([directive] if directive else []) + [f"graph {direction}"]
        return "\n".join(head + body + style + ["```"]), counters["nodes"], counters["edges"]

    text, n_nodes, n_edges = build(changed_only)
    rendered_changed_only = changed_only
    truncated = False
    # Degrade an oversized full graph to changed-only before giving up (GitHub caps).
    if text is not None and (n_edges > MAX_EDGES or len(text) > MAX_TEXT) and not changed_only:
        t2, nn2, ne2 = build(True)
        if t2 is not None:
            text, n_nodes, n_edges, truncated = t2, nn2, ne2, True
            rendered_changed_only = True

    meta = {
        "n_changed": n_changed,
        "changed": changed,
        "n_nodes": n_nodes if text is not None else 0,
        "n_edges": n_edges if text is not None else 0,
        "truncated": bool(truncated or text is None),
        "changed_only": bool(rendered_changed_only),
        "requested_changed_only": bool(changed_only),
    }
    if text is None or n_edges > MAX_EDGES or len(text) > MAX_TEXT:  # never trip GitHub's red error box
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
    p.add_argument(
        "--no-edge-labels", dest="edge_labels", action="store_false", help="Draw arrows without relation labels"
    )
    p.add_argument(
        "--render-depth",
        type=int,
        default=1,
        help="Component levels to draw: 1=top-level flat, 2=+one nesting level, ...",
    )
    p.add_argument(
        "--font-size", type=int, default=None, help="Node label font size in px (bigger label ⇒ bigger node)"
    )
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
