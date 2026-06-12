"""Unit tests for scripts/build_component_files.py — per-component changed-file dropdowns."""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_component_files as bcf  # noqa: E402
import diff_to_mermaid as dm  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_component_files.py"


def comp(name, files=None, subs=None, key_files=None):
    c = {
        "name": name,
        "component_id": name,
        "file_methods": [{"file_path": f, "methods": m} for f, m in (files or {}).items()],
    }
    if key_files is not None:
        c["key_entities"] = [{"reference_file": f} for f in key_files]
    if subs is not None:
        c["components"] = subs
    return c


def render(base, head, changed_files=None, max_chars=bcf.MAX_TEXT):
    diff = dm.build_diff(base, head)
    return bcf.render_component_files(diff, base, changed_files, max_chars)


class TestGitIntersection(unittest.TestCase):
    def test_modified_component_lists_only_touched_files(self):
        base = {"components": [comp("Auth", {"a.py": ["f"], "b.py": ["g"], "c.py": ["h"]})]}
        head = {"components": [comp("Auth", {"a.py": ["f", "f2"], "b.py": ["g"], "c.py": ["h"]})]}
        text, meta = render(base, head, changed_files={"a.py", "unrelated.py"})
        self.assertIn("<code>a.py</code>", text)
        self.assertNotIn("b.py", text)  # owned but untouched
        self.assertNotIn("unrelated.py", text)  # touched but not owned
        self.assertIn("<b>Auth</b> : 1 file changed", text)
        self.assertEqual(meta["n_components"], 1)
        self.assertEqual(meta["n_files"], 1)

    def test_added_component_wording(self):
        base = {"components": []}
        head = {"components": [comp("RateLimiter", {"rl/bucket.py": ["acquire"], "rl/config.py": ["load"]})]}
        text, _ = render(base, head, changed_files={"rl/bucket.py", "rl/config.py"})
        self.assertIn("<b>RateLimiter</b> : 2 files added", text)

    def test_deleted_component_lists_base_files(self):
        base = {"components": [comp("Legacy", {"legacy/store.py": ["get"], "legacy/migrations.py": ["mig"]})]}
        head = {"components": []}
        text, _ = render(base, head, changed_files={"legacy/store.py", "legacy/migrations.py"})
        self.assertIn("<b>Legacy</b> : 2 files removed", text)
        self.assertIn("<code>legacy/migrations.py</code>", text)

    def test_unchanged_component_emits_nothing(self):
        base = {"components": [comp("A", {"a.py": ["f"]})]}
        head = {"components": [comp("A", {"a.py": ["f"]})]}
        text, meta = render(base, head, changed_files={"a.py"})
        self.assertEqual(text, "")
        self.assertFalse(meta["rendered"])

    def test_changed_component_with_no_touched_files_is_skipped(self):
        # Model reorg: file moved between components, but the PR's git diff is elsewhere.
        base = {"components": [comp("A", {"a.py": ["f"], "b.py": ["g"]})]}
        head = {"components": [comp("A", {"a.py": ["f"]})]}
        text, _ = render(base, head, changed_files={"elsewhere.py"})
        self.assertEqual(text, "")

    def test_empty_changed_files_set_means_no_dropdowns_not_fallback(self):
        # Empty git diff (net-zero PR / re-run): an empty set must NOT fall
        # back to analysis-derived changes — only None (flag omitted) does.
        base = {"components": [comp("A", {"a.py": ["f"]})]}
        head = {"components": [comp("A", {"a.py": ["f", "g"]})]}
        text, meta = render(base, head, changed_files=set())
        self.assertEqual(text, "")
        self.assertFalse(meta["rendered"])

    def test_nested_subtree_files_aggregate_to_top_level(self):
        base = {"components": [comp("Parent", {}, subs=[comp("Child", {"deep/x.py": ["f"]})])]}
        head = {"components": [comp("Parent", {}, subs=[comp("Child", {"deep/x.py": ["f", "g"]})])]}
        text, _ = render(base, head, changed_files={"deep/x.py"})
        self.assertIn("<b>Parent</b>", text)
        self.assertIn("<code>deep/x.py</code>", text)
        self.assertNotIn("<b>Child</b>", text)  # one dropdown per top-level component

    def test_rollup_parent_labels_changed_subcomponents(self):
        # Parent unchanged itself (display_status rollup): the summary carries the
        # recursive count the headline/diagram use, so counts don't contradict.
        base = {"components": [comp("Parent", {"p.py": ["f"]}, subs=[comp("Child", {"c.py": ["g"]})])]}
        head = {"components": [comp("Parent", {"p.py": ["f"]}, subs=[comp("Child", {"c.py": ["g", "g2"]})])]}
        text, _ = render(base, head, changed_files={"c.py"})
        self.assertIn("<b>Parent</b> : 1 changed sub-component, 1 file changed", text)

    def test_deleted_nested_child_files_list_under_modified_parent(self):
        base = {"components": [comp("Parent", {"p.py": ["f"]}, subs=[comp("Child", {"child/x.py": ["g"]})])]}
        head = {"components": [comp("Parent", {"p.py": ["f"]}, subs=[])]}
        text, _ = render(base, head, changed_files={"child/x.py"})
        self.assertIn("<b>Parent</b>", text)
        self.assertIn("<code>child/x.py</code>", text)

    def test_key_entities_only_shape(self):
        # Some engine outputs have file_methods: [] everywhere and carry file
        # linkage only in key_entities[].reference_file (observed on TS repos).
        base = {"components": []}
        head = {"components": [comp("Webview", key_files=["src/panel.ts", "src/render.ts"])]}
        text, _ = render(base, head, changed_files={"src/panel.ts", "src/render.ts"})
        self.assertIn("<b>Webview</b> : 2 files added", text)
        self.assertIn("<code>src/panel.ts</code>", text)

    def test_duplicate_deleted_names_attribute_files_to_own_block(self):
        base = {"components": [comp("Dup", {"one.py": ["f"]}), comp("Dup", {"two.py": ["g"]})]}
        head = {"components": []}
        for changed in (None, {"one.py", "two.py"}):
            text, _ = render(base, head, changed_files=changed)
            self.assertEqual(text.count("one.py"), 1, text)
            self.assertEqual(text.count("two.py"), 1, text)


class TestAnalysisFallback(unittest.TestCase):
    def test_fallback_lists_structural_and_method_changes(self):
        base = {"components": [comp("A", {"kept.py": ["f"], "gone.py": ["g"], "same.py": ["h"]})]}
        head = {"components": [comp("A", {"kept.py": ["f", "f2"], "new.py": ["n"], "same.py": ["h"]})]}
        text, _ = render(base, head, changed_files=None)
        self.assertIn("<code>kept.py</code>", text)  # method set changed
        self.assertIn("<code>gone.py</code>", text)  # removed from component
        self.assertIn("<code>new.py</code>", text)  # added to component
        self.assertNotIn("same.py", text)

    def test_fallback_deleted_component_lists_all_base_files(self):
        base = {"components": [comp("Legacy", {"l/a.py": ["f"], "l/b.py": ["g"]})]}
        head = {"components": []}
        text, _ = render(base, head, changed_files=None)
        self.assertIn("<b>Legacy</b> : 2 files removed", text)

    def test_missing_file_path_entry_emits_no_phantom(self):
        base = {"components": [comp("A", {"a.py": ["f"]})]}
        head = {"components": [comp("A", {"a.py": ["f"]})]}
        head["components"][0]["file_methods"].append({"methods": ["orphan"]})  # no file_path
        text, _ = render(base, head, changed_files=None)
        self.assertNotIn("<code></code>", text)


class TestOrdering(unittest.TestCase):
    def test_file_lists_are_sorted(self):
        files = {f: ["m"] for f in ["e.py", "b.py", "f.py", "a.py", "d.py", "c.py"]}
        base = {"components": []}
        head = {"components": [comp("A", files)]}
        text, _ = render(base, head, changed_files=set(files))
        paths = re.findall(r"<code>([^<]+)</code>", text)
        self.assertEqual(paths, ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"])

    def test_blocks_follow_diagram_order_deleted_ghosts_last(self):
        # Head order first (matches Mermaid node emission), deleted ghosts appended
        # last — NOT alphabetical: Alpha is deleted and must render after Zeta.
        base = {"components": [comp("Zeta", {"z.py": ["f"]}), comp("Alpha", {"a.py": ["g"]})]}
        head = {"components": [comp("Zeta", {"z.py": ["f", "f2"]})]}
        text, _ = render(base, head, changed_files={"z.py", "a.py"})
        self.assertEqual(re.findall(r"<b>(\w+)</b>", text), ["Zeta", "Alpha"])


class TestCapsAndEscaping(unittest.TestCase):
    def test_per_component_file_cap(self):
        files = {f"src/f{i:02}.py": ["m"] for i in range(20)}
        base = {"components": []}
        head = {"components": [comp("Big", files)]}
        text, meta = render(base, head, changed_files=set(files))
        self.assertEqual(text.count("<code>"), bcf.MAX_FILES_PER_COMPONENT)
        self.assertIn("…and 5 more</sub>", text)
        self.assertIn(": 20 files added", text)  # count reflects reality, list is capped
        self.assertTrue(meta["truncated"])

    def test_total_char_budget_drops_whole_components(self):
        base = {"components": []}
        head = {"components": [comp(f"C{i}", {f"c{i}/f.py": ["m"]}) for i in range(10)]}
        text, meta = render(base, head, changed_files={f"c{i}/f.py" for i in range(10)}, max_chars=300)
        self.assertIn("more changed components</sub>", text)
        self.assertTrue(meta["truncated"])
        # meta counts what actually rendered, not what the budget dropped
        self.assertEqual(meta["n_files"], text.count("<code>"))
        self.assertEqual(meta["n_components"], text.count("<details>"))

    def test_first_block_exceeding_budget_renders_nothing(self):
        # Never a dangling "…and N more" with no blocks above it.
        base = {"components": []}
        head = {"components": [comp("Big", {f"very/long/path/file{i}.py": ["m"] for i in range(15)})]}
        text, meta = render(base, head, changed_files={f"very/long/path/file{i}.py" for i in range(15)}, max_chars=100)
        self.assertEqual(text, "")
        self.assertFalse(meta["rendered"])
        self.assertEqual(meta["n_files"], 0)
        self.assertTrue(meta["truncated"])

    def test_html_escaping_of_names_and_paths(self):
        base = {"components": []}
        head = {"components": [comp("A <& B", {"weird/<path>&.py": ["m"]})]}
        text, _ = render(base, head, changed_files={"weird/<path>&.py"})
        self.assertIn("<b>A &lt;&amp; B</b>", text)
        self.assertIn("<code>weird/&lt;path&gt;&amp;.py</code>", text)
        self.assertNotIn("<path>", text)

    def test_blank_line_after_summary(self):
        # GitHub only renders markdown inside <details> after a blank line.
        base = {"components": []}
        head = {"components": [comp("A", {"a.py": ["m"]})]}
        text, _ = render(base, head, changed_files={"a.py"})
        self.assertIn("</summary>\n\n-", text)
        self.assertIn("\n\n</details>", text)


class TestCLI(unittest.TestCase):
    def _analyses(self, d):
        (d / "base.json").write_text(json.dumps({"components": [comp("Auth", {"a.py": ["f"], "b.py": ["g"]})]}))
        (d / "head.json").write_text(json.dumps({"components": [comp("Auth", {"a.py": ["f", "f2"], "b.py": ["g"]})]}))

    def _run(self, d, *extra):
        out = d / "out.md"
        args = [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(d / "base.json"),
            "--head",
            str(d / "head.json"),
            "--out",
            str(out),
        ]
        return out, subprocess.run([*args, *extra], capture_output=True, text=True)

    def test_main_writes_out_file_and_prints_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._analyses(d)
            (d / "changed.txt").write_text("a.py\nunrelated.py\n")
            out, r = self._run(d, "--changed-files", str(d / "changed.txt"))
            self.assertEqual(r.returncode, 0, r.stderr)
            content = out.read_text(encoding="utf-8")
            self.assertIn("<code>a.py</code>", content)
            self.assertTrue(content.endswith("</details>\n"))  # trailing newline: see main()
            meta = json.loads(r.stdout)
            self.assertEqual(set(meta), {"rendered", "n_components", "n_files", "truncated"})
            self.assertTrue(meta["rendered"])

    def test_non_utf8_changed_files_does_not_crash(self):
        # core.quotepath=off emits raw filename bytes; a non-UTF-8 path must not
        # kill the section — it just can't intersect with the analysis's paths.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._analyses(d)
            (d / "changed.txt").write_bytes(b"r\xe9sum\xe9.py\na.py\n")
            out, r = self._run(d, "--changed-files", str(d / "changed.txt"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("<code>a.py</code>", out.read_text(encoding="utf-8"))

    def test_empty_result_writes_zero_bytes(self):
        # The action gates the section on [ -s "$FILES_MD" ].
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._analyses(d)
            (d / "changed.txt").write_text("elsewhere.py\n")
            out, r = self._run(d, "--changed-files", str(d / "changed.txt"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(out.read_bytes(), b"")


class TestEngineGitPathContract(unittest.TestCase):
    """file_methods[].file_path must be repo-relative forward-slash paths identical
    to git --name-only output; pinned against the committed engine artifact (the
    dogfood workflows regenerate it on engine bumps, so format drift fails here)."""

    def test_committed_analysis_paths_are_git_name_only_format(self):
        root = Path(__file__).resolve().parent.parent
        analysis = json.loads((root / ".codeboarding" / "analysis.json").read_text())
        paths = set()

        def collect(c):
            for fm in c.get("file_methods") or []:
                paths.add(fm["file_path"])
            for ke in c.get("key_entities") or []:
                if ke.get("reference_file"):
                    paths.add(ke["reference_file"])
            for s in c.get("components") or []:
                collect(s)

        for c in analysis.get("components") or []:
            collect(c)
        self.assertTrue(paths, "committed analysis.json has no file paths")
        tracked = set(
            subprocess.run(
                ["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True
            ).stdout.splitlines()
        )
        self.assertLessEqual(paths, tracked, f"paths not in git --name-only format: {sorted(paths - tracked)[:5]}")


if __name__ == "__main__":
    unittest.main()
