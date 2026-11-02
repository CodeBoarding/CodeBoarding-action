"""Unit tests for scripts/diff_to_mermaid.py — diff logic + Mermaid rendering."""

import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

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


class TestAnalysedFilesChanged(unittest.TestCase):
    @staticmethod
    def analysis(files, components=None):
        return {
            "files": {path: {"content_hash": digest} for path, digest in files.items()},
            "components": components or [],
        }

    def test_the_same_code_grouped_differently_changes_no_file(self):
        base = self.analysis({"a.py": "h1", "b.py": "h2"}, [comp("A", {"a.py": ["f"]}), comp("B", {"b.py": ["g"]})])
        head = self.analysis({"a.py": "h1", "b.py": "h2"}, [comp("A", {"a.py": ["f"], "b.py": ["g"]})])
        self.assertEqual(dm.analysed_files_changed(base, head), 0)

    def test_every_edited_added_or_removed_file_counts(self):
        base = self.analysis({"a.py": "h1", "b.py": "h2", "gone.py": "h3"})
        head = self.analysis({"a.py": "h1", "b.py": "h2-edited", "new.py": "h4"})
        self.assertEqual(dm.analysed_files_changed(base, head), 3)

    def test_an_analysis_that_cannot_vouch_for_its_files_gives_no_count(self):
        full = self.analysis({"a.py": "h1"})
        self.assertIsNone(dm.analysed_files_changed({"components": []}, full))
        self.assertIsNone(dm.analysed_files_changed(full, self.analysis({"a.py": ""})))

    def test_the_count_rides_on_the_machine_readable_summary(self):
        loaded = {"base.json": self.analysis({"a.py": "h1"}), "head.json": self.analysis({"a.py": "h2"})}
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["diff_to_mermaid.py", "--base", "base.json", "--head", "head.json", "--out", f"{tmp}/diagram.md"]
            with (
                patch.object(sys, "argv", argv),
                patch.object(dm, "load_analysis", side_effect=lambda path: loaded[str(path)]),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                dm.main()
        self.assertEqual(json.loads(stdout.getvalue())["analysed_files_changed"], 1)


class TestDiff(unittest.TestCase):
    def test_core_loader_projects_global_relation_into_rendered_mermaid(self):
        root_a = SimpleNamespace(component_id="1")
        root_b = SimpleNamespace(component_id="2")
        child_a = SimpleNamespace(component_id="1.1")
        child_b = SimpleNamespace(component_id="2.1")

        def parse_unified_analysis(data):
            relations = [SimpleNamespace(**relation) for relation in data.get("components_relations", [])]
            root = SimpleNamespace(components=[root_a, root_b], components_relations=relations)
            return root, {
                "1": SimpleNamespace(components=[child_a]),
                "2": SimpleNamespace(components=[child_b]),
            }

        def build_id_to_name_map(_root, _subs):
            return {"1": "Root A", "2": "Root B", "1.1": "Child A", "2.1": "Child B"}

        def project_relations_to_level(relations, level_ids, id_to_name):
            projected = []
            for relation in relations:
                src_id = next(
                    (cid for cid in level_ids if relation.src_id == cid or relation.src_id.startswith(cid + ".")), None
                )
                dst_id = next(
                    (cid for cid in level_ids if relation.dst_id == cid or relation.dst_id.startswith(cid + ".")), None
                )
                if src_id is None or dst_id is None or src_id == dst_id:
                    continue
                projected.append(
                    SimpleNamespace(
                        relation=relation.relation,
                        src_name=id_to_name[src_id],
                        dst_name=id_to_name[dst_id],
                        src_id=src_id,
                        dst_id=dst_id,
                    )
                )
            return projected

        rendering = ModuleType("output_generators.rendering")
        rendering.project_relations_to_level = project_relations_to_level
        analysis_json = ModuleType("diagram_analysis.analysis_json")
        analysis_json.parse_unified_analysis = parse_unified_analysis
        analysis_json.build_id_to_name_map = build_id_to_name_map
        modules = {
            "diagram_analysis": ModuleType("diagram_analysis"),
            "diagram_analysis.analysis_json": analysis_json,
            "output_generators": ModuleType("output_generators"),
            "output_generators.rendering": rendering,
        }
        analysis = {
            "components": [
                {"name": "Root A", "component_id": "1", "components": [{"name": "Child A", "component_id": "1.1"}]},
                {"name": "Root B", "component_id": "2", "components": [{"name": "Child B", "component_id": "2.1"}]},
            ],
            "components_relations": [],
        }

        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, modules):
            base_path = Path(tmp) / "base.json"
            head_path = Path(tmp) / "head.json"
            base_path.write_text(json.dumps(analysis))
            head_path.write_text(
                json.dumps(
                    {
                        **analysis,
                        "components_relations": [
                            {
                                "relation": "calls",
                                "src_name": "Child A",
                                "dst_name": "Child B",
                                "src_id": "1.1",
                                "dst_id": "2.1",
                            }
                        ],
                    }
                )
            )
            diff = dm.build_diff(dm.load_analysis(base_path), dm.load_analysis(head_path))

        text, meta = dm.render_mermaid(diff)
        self.assertIn('n_Root_A -- "calls" --> n_Root_B', text)
        self.assertEqual(meta["n_edges"], 1)
        self.assertTrue(meta["changed"])

    def test_projects_global_relations_at_every_component_level(self):
        child = SimpleNamespace(component_id="1.1")
        peer = SimpleNamespace(component_id="1.2")
        root_a = SimpleNamespace(component_id="1")
        root_b = SimpleNamespace(component_id="2")
        global_relation = SimpleNamespace(
            relation="calls",
            src_name="Child",
            dst_name="Root B",
            src_id="1.1",
            dst_id="2",
        )
        sibling_relation = SimpleNamespace(
            relation="delegates",
            src_name="Child",
            dst_name="Peer",
            src_id="1.1",
            dst_id="1.2",
        )
        root_analysis = SimpleNamespace(
            components=[root_a, root_b], components_relations=[global_relation, sibling_relation]
        )
        sub_analysis = SimpleNamespace(components=[child, peer])
        data = {
            "components": [
                {
                    "name": "Root A",
                    "component_id": "1",
                    "components": [
                        {"name": "Child", "component_id": "1.1"},
                        {"name": "Peer", "component_id": "1.2"},
                    ],
                    "components_relations": [{"relation": "stale"}],
                },
                {"name": "Root B", "component_id": "2"},
            ],
            "components_relations": [],
        }

        def projector(relations, level_ids, _id_to_name):
            self.assertEqual(relations, [global_relation, sibling_relation])
            if level_ids == {"1", "2"}:
                return [
                    SimpleNamespace(
                        relation="calls",
                        src_name="Root A",
                        dst_name="Root B",
                        src_id="1",
                        dst_id="2",
                    )
                ]
            if level_ids == {"1.1", "1.2"}:
                return [sibling_relation]
            self.fail(f"unexpected component level: {level_ids}")

        projected = dm._project_analysis(
            data,
            root_analysis,
            {"1": sub_analysis},
            {"1": "Root A", "2": "Root B", "1.1": "Child", "1.2": "Peer"},
            projector,
        )

        self.assertEqual(projected["components_relations"][0]["src_id"], "1")
        nested_relations = projected["components"][0]["components_relations"]
        self.assertEqual(nested_relations[0]["src_id"], "1.1")
        self.assertEqual(nested_relations[0]["dst_id"], "1.2")
        self.assertEqual(
            [r["src_id"] for r in projected["leaf_relations"]],
            [global_relation.src_id, sibling_relation.src_id],
            "an edge between two parents' children survives only unprojected",
        )

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

    def test_method_body_change_marks_owning_top_level_component(self):
        base = {
            "components": [
                comp("Owner", {"shared.py": ["Owner.changed"]}, subs=[comp("Inner", {"shared.py": ["Owner.changed"]})]),
                comp("Reference", {"shared.py": ["Reference.unchanged"]}),
            ],
            "components_relations": [],
            "methods_index": {
                "shared.py|Owner.changed": {
                    "file_path": "shared.py",
                    "qualified_name": "Owner.changed",
                    "content_hash": "before",
                }
            },
        }
        head = {
            **base,
            "methods_index": {
                "shared.py|Owner.changed": {
                    "file_path": "shared.py",
                    "qualified_name": "Owner.changed",
                    "content_hash": "after",
                }
            },
        }

        diff = dm.build_diff(base, head)
        statuses = {component["name"]: component["diff_status"] for component in diff["components"]}
        text, meta = dm.render_mermaid(diff, render_depth=1)

        self.assertEqual(statuses, {"Owner": "modified", "Reference": "unchanged"})
        self.assertIn("class n_Owner modified;", text)
        self.assertNotIn("class n_Reference modified;", text)
        self.assertEqual(meta["n_changed"], 1)

    def test_line_number_shift_does_not_mark_method_changed(self):
        component = comp("Owner", {"shared.py": ["Owner.method"]})
        base = {
            "components": [component],
            "components_relations": [],
            "methods_index": {
                "shared.py|Owner.method": {
                    "file_path": "shared.py",
                    "qualified_name": "Owner.method",
                    "content_hash": "same",
                    "start_line": 10,
                }
            },
        }
        head = {
            **base,
            "methods_index": {
                "shared.py|Owner.method": {
                    "file_path": "shared.py",
                    "qualified_name": "Owner.method",
                    "content_hash": "same",
                    "start_line": 11,
                }
            },
        }

        self.assertEqual(dm.build_diff(base, head)["components"][0]["diff_status"], "unchanged")

    def test_rename_is_add_plus_delete(self):
        base = {"components": [comp("Old")], "components_relations": []}
        head = {"components": [comp("New")], "components_relations": []}
        status = {c["name"]: c["diff_status"] for c in dm.build_diff(base, head)["components"]}
        self.assertEqual(status, {"New": "added", "Old": "deleted"})

    def test_removed_method_does_not_turn_deleted_component_into_modified(self):
        base = {
            "components": [comp("Gone", {"gone.py": ["Gone.run"]})],
            "components_relations": [],
            "methods_index": {
                "gone.py|Gone.run": {
                    "file_path": "gone.py",
                    "qualified_name": "Gone.run",
                    "content_hash": "before",
                }
            },
        }
        head = {"components": [], "components_relations": [], "methods_index": {}}

        self.assertEqual(dm.build_diff(base, head)["components"][0]["diff_status"], "deleted")

    def test_relation_modified_on_label_change(self):
        base = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B", "uses")]}
        head = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B", "calls")]}
        self.assertEqual(dm.build_diff(base, head)["components_relations"][0]["diff_status"], "modified")

    def test_relation_added_and_deleted(self):
        base = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B")]}
        head = {"components": [comp("A"), comp("B")], "components_relations": [rel("B", "A")]}
        statuses = sorted(r["diff_status"] for r in dm.build_diff(base, head)["components_relations"])
        self.assertEqual(statuses, ["added", "deleted"])

    def test_parallel_relation_deletion_is_not_label_modification(self):
        base = {
            "components": [comp("A"), comp("B")],
            "components_relations": [rel("A", "B", "uses"), rel("A", "B", "publishes")],
        }
        head = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B", "uses")]}
        statuses = sorted(r["diff_status"] for r in dm.build_diff(base, head)["components_relations"])
        self.assertEqual(statuses, ["deleted", "unchanged"])


class TestRender(unittest.TestCase):
    def _diff(self):
        base = {
            "components": [comp("A"), comp("B"), comp("Gone")],
            "components_relations": [rel("A", "B"), rel("A", "Gone")],
        }
        head = {
            "components": [comp("A", {"x.py": ["f"]}), comp("B"), comp("New")],
            "components_relations": [rel("A", "B"), rel("A", "New")],
        }
        return dm.build_diff(base, head)

    def test_flat_default_has_no_subgraphs(self):
        text, _ = dm.render_mermaid(self._diff(), render_depth=1)
        self.assertNotIn("subgraph", text)
        for cls in ("added", "modified", "deleted"):
            self.assertIn(f"classDef {cls}", text)
        self.assertTrue(linkstyle_indices_in_range(text))

    def test_nested_subgraphs_balanced_and_valid(self):
        base = {
            "components": [comp("P", subs=[comp("c1"), comp("c2")], subrels=[rel("c1", "c2")])],
            "components_relations": [],
        }
        head = {
            "components": [comp("P", subs=[comp("c1"), comp("c3")], subrels=[rel("c1", "c3")])],
            "components_relations": [],
        }
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
        self.assertIn("#34;", text)
        self.assertIn("#35;", text)

    def test_label_escaping_brackets_break_chars(self):
        # `]` / `(` / `&` would break GitHub's renderer if left raw.
        self.assertEqual(dm._esc("Has]Bracket"), "Has#93;Bracket")
        self.assertEqual(dm._esc("f(x)"), "f#40;x#41;")
        self.assertEqual(dm._esc("A & B"), "A #38; B")
        head = {"components": [comp("Weird]Name(x)"), comp("B")], "components_relations": []}
        base = {"components": [comp("B")], "components_relations": []}
        text, _ = dm.render_mermaid(dm.build_diff(base, head))
        self.assertNotIn("]Name", text)  # no raw ] inside a label
        self.assertIn("#93;", text)

    def test_esc_strips_newlines(self):
        # A raw newline/CR in a label breaks the whole Mermaid block.
        self.assertNotIn("\n", dm._esc("line1\nline2"))
        self.assertNotIn("\r", dm._esc("a\r\nb"))

    def test_truncate_caps_long_edge_label_with_ellipsis(self):
        out = dm._truncate("x" * 60)
        self.assertLessEqual(len(out), dm._EDGE_LABEL_MAX)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(dm._truncate("short"), "short")  # under the cap: unchanged

    def test_changed_flag_relation_only(self):
        # A label-only relation change leaves n_changed=0 but must report changed=True.
        base = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B", "uses")]}
        head = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B", "calls")]}
        text, meta = dm.render_mermaid(dm.build_diff(base, head))
        self.assertEqual(meta["n_changed"], 0)
        self.assertTrue(meta["changed"])
        self.assertIsNotNone(text)

    def test_changed_flag_false_when_identical(self):
        d = {"components": [comp("A"), comp("B")], "components_relations": [rel("A", "B")]}
        _, meta = dm.render_mermaid(dm.build_diff(d, d))
        self.assertEqual(meta["n_changed"], 0)
        self.assertFalse(meta["changed"])

    def test_changed_flag_counts_nested(self):
        base = {"components": [comp("P", subs=[comp("c1")], subrels=[])], "components_relations": []}
        head = {"components": [comp("P", subs=[comp("c1", {"x.py": ["f"]})], subrels=[])], "components_relations": []}
        _, meta = dm.render_mermaid(dm.build_diff(base, head), render_depth=2)
        self.assertEqual(meta["n_changed"], 1)  # the nested child counts
        self.assertTrue(meta["changed"])

    def test_nested_method_change_highlights_collapsed_parent(self):
        base = {"components": [comp("P", subs=[comp("c1")], subrels=[])], "components_relations": []}
        head = {"components": [comp("P", subs=[comp("c1", {"x.py": ["f"]})], subrels=[])], "components_relations": []}
        text, meta = dm.render_mermaid(dm.build_diff(base, head), render_depth=1)
        self.assertEqual(meta["n_changed"], 1)
        self.assertIn("class n_P modified;", text)

    def test_nested_relation_change_highlights_collapsed_parent(self):
        base = {
            "components": [comp("P", subs=[comp("c1"), comp("c2")], subrels=[rel("c1", "c2", "uses")])],
            "components_relations": [],
        }
        head = {
            "components": [comp("P", subs=[comp("c1"), comp("c2")], subrels=[rel("c1", "c2", "calls")])],
            "components_relations": [],
        }
        text, meta = dm.render_mermaid(dm.build_diff(base, head), render_depth=1)
        self.assertEqual(meta["n_changed"], 0)
        self.assertTrue(meta["changed"])
        self.assertIn("class n_P modified;", text)

    def test_changed_only_keeps_nested_change(self):
        base = {"components": [comp("P", subs=[comp("c1"), comp("c2")], subrels=[])], "components_relations": []}
        head = {
            "components": [comp("P", subs=[comp("c1", {"x.py": ["f"]}), comp("c2")], subrels=[])],
            "components_relations": [],
        }
        text, meta = dm.render_mermaid(dm.build_diff(base, head), render_depth=2, changed_only=True)
        self.assertIsNotNone(text)
        self.assertTrue(meta["changed"])
        self.assertFalse(meta["truncated"])
        self.assertIn("subgraph n_P", text)
        self.assertIn("class n_c1 modified;", text)
        self.assertNotIn('n_c2["c2"]', text)

    def test_changed_only_prunes_unchanged_children_of_modified_parent(self):
        base = {
            "components": [comp("P", {"p.py": ["old"]}, subs=[comp("c1"), comp("c2")], subrels=[])],
            "components_relations": [],
        }
        head = {
            "components": [comp("P", {"p.py": ["old", "new"]}, subs=[comp("c1"), comp("c2")], subrels=[])],
            "components_relations": [],
        }
        text, meta = dm.render_mermaid(dm.build_diff(base, head), render_depth=2, changed_only=True)
        self.assertIsNotNone(text)
        self.assertTrue(meta["changed"])
        self.assertIn('n_P["P"]', text)
        self.assertNotIn('n_c1["c1"]', text)
        self.assertNotIn('n_c2["c2"]', text)

    def test_changed_only_is_not_auto_truncated(self):
        text, meta = dm.render_mermaid(self._diff(), render_depth=1, changed_only=True)
        self.assertIsNotNone(text)
        self.assertFalse(meta["truncated"])
        self.assertTrue(meta["changed_only"])
        self.assertTrue(meta["requested_changed_only"])

    def test_auto_truncation_reports_rendered_changed_only(self):
        base = {
            "components": [comp("A"), comp("B"), comp("C")],
            "components_relations": [rel("B", "C", "uses"), rel("C", "B", "uses")],
        }
        head = {
            "components": [comp("A", {"a.py": ["f"]}), comp("B"), comp("C")],
            "components_relations": [rel("B", "C", "uses"), rel("C", "B", "uses")],
        }
        old = dm.MAX_EDGES
        try:
            dm.MAX_EDGES = 1
            text, meta = dm.render_mermaid(dm.build_diff(base, head), render_depth=1)
        finally:
            dm.MAX_EDGES = old
        self.assertIsNotNone(text)
        self.assertTrue(meta["truncated"])
        self.assertTrue(meta["changed_only"])
        self.assertFalse(meta["requested_changed_only"])

    def test_empty_returns_none(self):
        text, meta = dm.render_mermaid({"components": [], "components_relations": []})
        self.assertIsNone(text)
        self.assertEqual(meta["n_nodes"], 0)
        self.assertTrue(meta["empty"])
        self.assertFalse(meta["truncated"])

    def test_no_edge_labels(self):
        text, _ = dm.render_mermaid(self._diff(), render_depth=1, edge_labels=False)
        self.assertNotIn(' -- "', text)


class TestSpecificView(unittest.TestCase):
    """The deepest changed components, flat, joined by the relations that changed."""

    def _diff(self, base_leaf=(), head_leaf=()):
        def tree(alpha_methods):
            return [
                comp(
                    "One", subs=[comp("Alpha", {"a.py": alpha_methods}, cid="1.1"), comp("Quiet", cid="1.2")], cid="1"
                ),
                comp("Two", subs=[comp("Beta", {"b.py": ["b"]}, cid="2.1"), comp("Gamma", cid="2.2")], cid="2"),
            ]

        base = {"components": tree(["f"]), "components_relations": [], "leaf_relations": list(base_leaf)}
        head = {"components": tree(["f", "g"]), "components_relations": [], "leaf_relations": list(head_leaf)}
        return dm.build_diff(base, head)

    @staticmethod
    def _names(diff):
        return {c["name"]: c["diff_status"] for c in dm._specific_view(diff)["components"]}

    def test_the_deepest_changed_component_stands_in_for_its_parent(self):
        self.assertEqual(self._names(self._diff()), {"Alpha": "modified"})

    def test_a_body_only_edit_lands_on_the_nested_component_that_owns_it(self):
        """Nothing moved between components, so only the file's hash says anything changed."""
        method = "a.py|f"

        def analysis(digest):
            return {
                "components": [comp("One", {"a.py": ["f"]}, subs=[comp("Owner", {"a.py": ["f"]}), comp("Bystander")])],
                "components_relations": [],
                "methods_index": {method: {"file_path": "a.py", "content_hash": digest}},
            }

        diff = dm.build_diff(analysis("h1"), analysis("h2"))
        self.assertEqual(self._names(diff), {"Owner": "modified"})
        self.assertEqual(dm.render_mermaid(diff)[1]["n_changed"], 1, "owning a change adds nothing to the count")

    def test_a_parent_with_no_changed_children_is_drawn_itself(self):
        base = {"components": [comp("One", {"a.py": ["f"]}, subs=[comp("Child")])], "components_relations": []}
        head = {"components": [comp("One", {"a.py": ["f", "g"]}, subs=[comp("Child")])], "components_relations": []}
        self.assertEqual(self._names(dm.build_diff(base, head)), {"One": "modified"})

    def test_an_added_parent_is_drawn_as_its_added_children(self):
        base = {"components": [], "components_relations": []}
        head = {"components": [comp("New", subs=[comp("Kid"), comp("Sibling")])], "components_relations": []}
        self.assertEqual(self._names(dm.build_diff(base, head)), {"Kid": "added", "Sibling": "added"})

    def test_a_relation_across_parents_is_drawn(self):
        edge = {**rel("Alpha", "Beta"), "src_id": "1.1", "dst_id": "2.1"}
        text, meta = dm.render_specific(self._diff(head_leaf=[edge]))
        self.assertRegex(text, r'n_Alpha -- "calls" --> n_Beta')
        self.assertNotIn("subgraph", text)
        self.assertEqual(meta["n_edges"], 1)

    def test_an_unchanged_component_is_drawn_only_for_a_changed_relation(self):
        changed = rel("Alpha", "Beta")
        steady = rel("Alpha", "Gamma")
        names = self._names(self._diff(base_leaf=[steady], head_leaf=[steady, changed]))
        self.assertEqual(names, {"Alpha": "modified", "Beta": "unchanged"})

    def test_a_neighbour_is_not_coloured(self):
        text, _ = dm.render_specific(self._diff(head_leaf=[rel("Alpha", "Beta")]))
        self.assertRegex(text, r"class n_Alpha modified;")
        self.assertNotIn("class n_Alpha,n_Beta", text)

    def test_a_relation_that_did_not_change_is_not_drawn(self):
        steady = rel("Alpha", "Gamma")
        text, meta = dm.render_specific(self._diff(base_leaf=[steady], head_leaf=[steady]))
        self.assertEqual(meta["n_edges"], 0)
        self.assertNotIn("Gamma", text)

    def test_a_changed_relation_alone_is_enough_to_draw(self):
        tree = [comp("One", subs=[comp("A")]), comp("Two", subs=[comp("B")])]
        base = {"components": tree, "components_relations": [], "leaf_relations": []}
        head = {"components": tree, "components_relations": [], "leaf_relations": [rel("A", "B")]}
        self.assertEqual(self._names(dm.build_diff(base, head)), {"A": "unchanged", "B": "unchanged"})

    def test_the_top_view_ignores_the_unprojected_relations(self):
        with_edge = self._diff(head_leaf=[rel("Alpha", "Beta")])
        without = self._diff()
        self.assertEqual(dm.render_mermaid(with_edge), dm.render_mermaid(without))


if __name__ == "__main__":
    unittest.main()
