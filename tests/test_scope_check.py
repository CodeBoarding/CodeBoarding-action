"""The scope check: which changed files the engine would analyse, and the shortcut it allows."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE_CHECK_PY = ROOT / "scripts" / "action" / "scope_check.py"
SCOPE_CHECK_SH = ROOT / "scripts" / "action" / "scope-check.sh"

# A stand-in for the installed engine: the extension map and the ignore manager with the
# rules the real ones apply (extension by suffix, hidden and always-excluded directories, then
# the ignore file's patterns), small enough to read in one go.
FAKE_ENGINE = {
    "static_analyzer/__init__.py": "",
    "static_analyzer/config.py": (
        "SOURCE_EXTENSION_TO_LANGUAGE = {ext: 'x' for ext in ('.py', '.ts', '.tsx', '.java', '.go', '.php', '.rs', '.cs', '.cpp')}\n"
    ),
    "repo_utils/__init__.py": "",
    "repo_utils/ignore.py": (
        "from pathlib import Path\n"
        "import fnmatch\n"
        "ALWAYS = {'.git', '.codeboarding', 'node_modules', '__pycache__', 'build', 'dist', 'coverage', 'target'}\n"
        "class RepoIgnoreManager:\n"
        "    def __init__(self, repo_root):\n"
        "        self.repo_root = Path(repo_root)\n"
        "        f = self.repo_root / '.codeboarding' / '.codeboardingignore'\n"
        "        self.patterns = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith('#')] if f.exists() else []\n"
        "    def should_ignore(self, path):\n"
        "        rel = Path(path)\n"
        "        if rel.is_absolute(): rel = rel.relative_to(self.repo_root)\n"
        "        if any(p in ALWAYS or p.startswith('.') for p in rel.parts[:-1]): return True\n"
        "        return any(fnmatch.fnmatch(str(rel), pat) or fnmatch.fnmatch(str(rel), pat.rstrip('/') + '/*') for pat in self.patterns)\n"
    ),
}


def _plant(root: Path, files: dict) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _run_py(paths: list, repo_root: Path, engine: Path) -> dict:
    result = subprocess.run(
        ["python3", str(SCOPE_CHECK_PY), "--repo-root", str(repo_root)],
        input="\n".join(paths) + "\n",
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(engine)},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class ScopeCheckPyTests(unittest.TestCase):
    def test_counts_only_files_the_engine_would_analyse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = root / "engine"
            _plant(engine, FAKE_ENGINE)
            repo = root / "repo"
            _plant(repo, {".codeboarding/.codeboardingignore": "# ignore\n**/tests/**\nvendor/\n"})
            counts = _run_py(
                [
                    "README.md",
                    "docs/development/architecture.md",
                    ".github/workflows/ci.yml",
                    "src/lib/turn.ts",
                    "src/lib/tests/turn_test.ts",
                    "node_modules/x/index.js",
                    "vendor/lib.py",
                    "gradle/libs.versions.toml",
                ],
                repo,
                engine,
            )
            self.assertEqual(counts, {"changed": 8, "analysed": 1})

    def test_without_the_engine_every_file_counts_as_analysed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counts = _run_py(["README.md", "docs/a.md"], root / "repo", root / "no-engine")
            self.assertEqual(counts, {"changed": 2, "analysed": 2})


class ScopeCheckShTests(unittest.TestCase):
    def _run(self, root: Path, files: list, base: Path | None) -> dict:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        gh = fake_bin / "gh"
        gh.write_text("#!/usr/bin/env bash\n" + "".join(f"printf '%s\\n' '{f}'\n" for f in files), encoding="utf-8")
        gh.chmod(0o755)
        engine = root / "engine"
        _plant(engine, FAKE_ENGINE)
        checkout = root / "checkout"
        checkout.mkdir(exist_ok=True)
        output = root / "github-output"
        output.write_text("", encoding="utf-8")
        temp = root / "runner-temp"
        temp.mkdir(exist_ok=True)
        env = {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(engine),
            "ACTION_PATH": str(ROOT),
            "REPOSITORY": "owner/repo",
            "PR_NUMBER": "7",
            "CHECKOUT_DIR": str(checkout),
            "BASE_DIR": str(base) if base else str(root / "no-base"),
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(temp),
        }
        result = subprocess.run([str(SCOPE_CHECK_SH)], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = {}
        for line in output.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            out[key] = value
        return out

    def test_skips_when_nothing_analysed_changed_and_a_base_is_at_hand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            (base / "analysis.json").write_text('{"description": "base"}', encoding="utf-8")
            out = self._run(root, ["README.md", ".github/workflows/ci.yml"], base)
            self.assertEqual(out["skip"], "true")
            self.assertEqual(out["changed_files"], "2")
            self.assertEqual(out["analysed_files"], "0")
            self.assertEqual(out["analysis_mode"], "unchanged")
            self.assertEqual(out["base_analysis_path"], str(base / "analysis.json"))
            self.assertEqual(Path(out["analysis_path"]).read_text(encoding="utf-8"), '{"description": "base"}')

    def test_runs_as_usual_when_an_analysed_file_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            (base / "analysis.json").write_text("{}", encoding="utf-8")
            out = self._run(root, ["README.md", "src/a.py"], base)
            self.assertEqual(out["skip"], "false")
            self.assertEqual(out["analysed_files"], "1")
            self.assertNotIn("analysis_path", out)

    def test_runs_as_usual_without_a_base_to_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = self._run(root, ["README.md"], None)
            self.assertEqual(out["skip"], "false")
            self.assertEqual(out["analysed_files"], "0")

    def test_reuses_the_committed_baseline_when_no_base_was_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            _plant(checkout, {".codeboarding/analysis.json": '{"description": "committed"}'})
            out = self._run(root, ["docs/a.md"], None)
            self.assertEqual(out["skip"], "true")
            self.assertEqual(out["base_analysis_path"], str(checkout / ".codeboarding" / "analysis.json"))


if __name__ == "__main__":
    unittest.main()
