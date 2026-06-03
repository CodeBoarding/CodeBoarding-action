"""Unit tests for scripts/diff_to_mermaid.py — diff logic + Mermaid rendering."""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import diff_to_mermaid as dm  # noqa: E402


def comp(name, files=None, cid=None, subs=None, subrels=None):
    c = {
        "name": name,
        "component_id": cid or name,
        "file_methods": [{"file_path": f, "methods": m} for f, m in (files or {}).items()],
    }
    if subs is not None:
        c["components"] = subs
    if subrels is not None:
        c["components_relations"] = subrels
    return c


def rel(src, dst, label="calls"):
    return {"src_name": src, "dst_name": dst, "src_id": src, "dst_id": dst, "relation": label}


def linkstyle_indices_in_range(text):
    n_edges = text.count("-->")
    idxs = [int(x) for m in re.finditer(r"linkStyle ([\d,]+)", text) for x in m.group(1).split(",")]
    return all(i < n_edges for i in idxs)


class TestDiff(unittest.TestCase):
    def test_added_modified_deleted_unchanged(self):
        base = {"components": [comp("A", {"a.py": ["f"]}), comp("B"), comp("D")], "components_relations": []}
        head = {"components": [comp("A", {"a.py": ["f", "g"]}), comp("B"), comp("C")], "components_relations": []}
        status = {c["name"]: c["diff_status"] for c in dm.build_diff(base, head)["components"]}
        self.assertEqual(status["A"], "modified")  # method added inside the component
        self.assertEqual(status["B"], "unchanged")
        self.assertEqual(status["C"], "added")
        self.assertEqual(status["D"], "deleted")

    def test_structural_change_is_modified(self):
        base = {"components": [comp("A", {"a.py": ["f"]})], "components_relations": []}
        head = {"components": [comp("A", {"a.py": ["f"], "b.py": ["h"]})], "components_relations": []}
        self.assertEqual(dm.build_diff(base, head)["components"][0]["diff_status"], "modified")

    def test_rename_is_add_plus_delete(self):
        base = {"components": [comp("Old")], "components_relations": []}
        head = {"components": [comp("New")], "components_relations": []}
        status = {c["name"]: c["diff_status"] for c in dm.build_diff(base, head)["components"]}
        self.assertEqual(status, {"New": "added", "Old": "deleted"})

    def test_relation_modified_on_label_change(self):
        base = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B", "uses")]}
        head = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B", "calls")]}
        self.assertEqual(dm.build_diff(base, head)["components_relations"][0]["diff_status"], "modified")

    def test_relation_added_and_deleted(self):
        base = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B")]}
        head = {"components": [comp("A"), comp("B")], "components_relations": [rel("B", "A")]}
        statuses = sorted(r["diff_status"] for r in dm.build_diff(base, head)["components_relations"])
        self.assertEqual(statuses, ["added", "deleted"])


class TestRender(unittest.TestCase):
    def _diff(self):
        base = {"components": [comp("A"), comp("B"), comp("Gone")], "components_relations": [rel("A", "B"), rel("A", "Gone")]}
        head = {"components": [comp("A", {"x.py": ["f"]}), comp("B"), comp("New")], "components_relations": [rel("A", "B"), rel("A", "New")]}
        return dm.build_diff(base, head)

    def test_flat_default_has_no_subgraphs(self):
        text, _ = dm.render_mermaid(self._diff(), render_depth=1)
        self.assertNotIn("subgraph", text)
        for cls in ("added", "modified", "deleted"):
            self.assertIn(f"classDef {cls}", text)
        self.assertTrue(linkstyle_indices_in_range(text))

    def test_nested_subgraphs_balanced_and_valid(self):
        base = {"components": [comp("P", subs=[comp("c1"), comp("c2")], subrels=[rel("c1", "c2")])], "components_relations": []}
        head = {"components": [comp("P", subs=[comp("c1"), comp("c3")], subrels=[rel("c1", "c3")])], "components_relations": []}
        text, _ = dm.render_mermaid(dm.build_diff(base, head), render_depth=2)
        sg = sum(1 for line in text.splitlines() if line.strip().startswith("subgraph "))
        en = sum(1 for line in text.splitlines() if line.strip() == "end")
        self.assertGreater(sg, 0)
        self.assertEqual(sg, en)
        self.assertTrue(linkstyle_indices_in_range(text))

    def test_render_depth_caps_at_data_depth(self):
        base = {"components": [comp("P", subs=[comp("c1")], subrels=[])], "components_relations": []}
        head = {"components": [comp("P", subs=[comp("c1"), comp("c2")], subrels=[])], "components_relations": []}
        diff = dm.build_diff(base, head)
        deep = dm.render_mermaid(diff, render_depth=5)[0]
        two = dm.render_mermaid(diff, render_depth=2)[0]
        self.assertEqual(deep, two)  # no level-3 data, so depth 5 == depth 2

    def test_label_escaping(self):
        head = {"components": [comp('A "q" #h'), comp("B")], "components_relations": []}
        base = {"components": [comp("B")], "components_relations": []}
        text, _ = dm.render_mermaid(dm.build_diff(base, head), render_depth=1)
        self.assertIn("#quot;", text)
        self.assertIn("#35;", text)

    def test_changed_only_truncates(self):
        text, meta = dm.render_mermaid(self._diff(), render_depth=1, changed_only=True)
        self.assertIsNotNone(text)
        self.assertTrue(meta["truncated"])

    def test_empty_returns_none(self):
        text, meta = dm.render_mermaid({"components": [], "components_relations": []})
        self.assertIsNone(text)
        self.assertEqual(meta["n_nodes"], 0)

    def test_no_edge_labels(self):
        text, _ = dm.render_mermaid(self._diff(), render_depth=1, edge_labels=False)
        self.assertNotIn(' -- "', text)


if __name__ == "__main__":
    unittest.main()
