"""PROTECTED TEST — DO NOT MODIFY WITHOUT EXPLICIT HUMAN CONSENT.

No agent, assistant, or automated tool may edit, weaken, skip, rename or delete
this file. If a change here looks necessary, stop and ask a human to decide. A
failure in this file means the behaviour changed, not that the test is wrong.

WHAT THIS PROTECTS
------------------
A review must compare a pull request against its **merge base** — the commit the
branch actually forked from — and never against the base branch tip.

    main:  X ──► Z          Z landed after this branch forked
           │
    PR:    └──► P           the pull request's own work

The event payload's `base.sha` is Z. Comparing against Z reports Z's components
as this pull request's changes (and reports them backwards, as removals). The
merge base is X, which is what `git diff main...PR` and GitHub's own "Files
changed" tab use.

The tests below build a real git history in that exact shape and assert that
X, not Z, is what the action resolves and analyzes.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "action" / "guard.sh"
ANALYZE = ROOT / "scripts" / "action" / "analyze.sh"

GH_STUB = '''#!/usr/bin/env python3
"""Minimal `gh` stand-in answering from a real git repository."""
import json, os, subprocess, sys

argv = sys.argv[1:]
path = argv[1]
with open(os.environ["CB_GH_LOG"], "a") as log:
    log.write(path + "\\n")

if "/compare/" in path:
    repo = os.environ["CB_FIXTURE_REPO"]
    base, head = path.split("/compare/", 1)[1].split("...")
    head = head.split(":")[-1]
    def git(*args):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout.strip()
    body = json.dumps({
        "merge_base_commit": {"sha": git("merge-base", base, head)},
        "behind_by": int(git("rev-list", "--count", f"{head}..{base}") or 0),
        "status": "diverged",
    })
else:
    body = os.environ["CB_PR_JSON"]

if "--jq" in argv:
    expr = argv[argv.index("--jq") + 1]
    sys.stdout.write(subprocess.run(["jq", "-r", expr], input=body, capture_output=True, text=True).stdout)
else:
    sys.stdout.write(body)
'''

ENGINE_STUB = '''#!/usr/bin/env python3
"""Minimal CodeBoarding CLI stand-in recording which tree it was given."""
import json, os, subprocess, sys

argv = sys.argv[1:]
checkout = argv[argv.index("--local") + 1]
output = argv[argv.index("--output-dir") + 1]
head = subprocess.run(["git", "-C", checkout, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
os.makedirs(output, exist_ok=True)
with open(os.environ["CB_ENGINE_LOG"], "a") as log:
    log.write(json.dumps({
        "mode": argv[0],
        "head": head,
        "files": sorted(p for p in os.listdir(checkout) if not p.startswith(".")),
    }) + "\\n")
analysis = os.path.join(output, "analysis.json")
with open(analysis, "w") as handle:
    json.dump({"metadata": {"depth_level": 2}, "components": [], "components_relations": []}, handle)
print(json.dumps({"requiresFullAnalysis": False, "analysis_path": analysis}))
'''


def _write_stub(directory: Path, name: str, source: str) -> None:
    path = directory / name
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


class MergeBaseContractTests(unittest.TestCase):
    """Every test here builds the X → Z / X → P history described above."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")

        # X — the fork point, the only commit both branches share.
        (self.upstream / "shared.txt").write_text("shared\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "shared")
        self.fork_point = self._git("rev-parse", "HEAD")

        # P — this pull request's work, branched from X.
        self._git("checkout", "-b", "feature")
        (self.upstream / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "feature")
        self.head_sha = self._git("rev-parse", "HEAD")

        # Z — somebody else's commit, landed on main after the branch forked.
        self._git("checkout", "main")
        (self.upstream / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "unrelated")
        self.base_tip = self._git("rev-parse", "HEAD")

        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        _write_stub(self.bin_dir, "gh", GH_STUB)
        _write_stub(self.bin_dir, "codeboarding", ENGINE_STUB)
        self.gh_log = root / "gh.log"
        self.engine_log = root / "engine.log"
        self.output = root / "github-output"
        self.runner_temp = root / "runner"
        self.runner_temp.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.upstream), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(self.output),
            "RUNNER_TEMP": str(self.runner_temp),
            "CB_FIXTURE_REPO": str(self.upstream),
            "CB_GH_LOG": str(self.gh_log),
            "CB_ENGINE_LOG": str(self.engine_log),
            "CB_PR_JSON": "{}",
            **extra,
        }

    def _outputs(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.output.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return values

    def _run_guard(self, **extra: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(GUARD)],
            env=self._env(
                MODE="review",
                EVENT="pull_request",
                EVENT_PR_NUMBER="7",
                PULL_BASE_SHA=self.base_tip,
                PULL_HEAD_SHA=self.head_sha,
                PULL_BASE_REPO="owner/repo",
                PULL_HEAD_REPO="owner/repo",
                PULL_BASE_REF="main",
                GITHUB_RUN_ID="1",
                GH_HOST="https://github.com",
                **extra,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def test_guard_resolves_the_merge_base_not_the_base_branch_tip(self) -> None:
        self._run_guard()
        outputs = self._outputs()

        self.assertEqual(outputs["merge_base_sha"], self.fork_point)
        self.assertEqual(outputs["base_sha"], self.base_tip)
        self.assertNotEqual(
            outputs["merge_base_sha"],
            outputs["base_sha"],
            "the merge base must not collapse onto the base branch tip",
        )
        self.assertEqual(outputs["behind_by"], "1")

    def test_review_analyzes_the_merge_base_tree_not_the_base_branch_tip(self) -> None:
        checkout = Path(self.temp_dir.name) / "checkout"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.upstream), str(checkout)],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", self.head_sha],
            capture_output=True,
            text=True,
            check=True,
        )

        result = subprocess.run(
            [str(ANALYZE)],
            env=self._env(
                ACTION_PATH=str(ROOT),
                ANALYSIS_KIND="review",
                CHECKOUT_DIR=str(checkout),
                REVIEW_BASE_SHA=self.fork_point,
                REVIEW_HEAD_SHA=self.head_sha,
                REVIEW_BASE_REPO="owner/repo",
                GIT_TOKEN="unused",
                GITHUB_SERVER_URL="https://github.com",
                PR_NUMBER="7",
                SEED_MODE="chain",
                CACHE_OUT_DIR=str(Path(self.temp_dir.name) / "cache-out"),
                ENGINE_VERSION="test",
                CFG_HASH="test",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        runs = [json.loads(line) for line in self.engine_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(runs), 2, f"expected a base and a head analysis, got {runs}")
        base_run, head_run = runs

        self.assertEqual(base_run["head"], self.fork_point)
        self.assertNotEqual(base_run["head"], self.base_tip)
        self.assertNotIn(
            "unrelated.txt",
            base_run["files"],
            "the baseline tree must not contain commits this pull request never forked from",
        )
        self.assertEqual(head_run["head"], self.head_sha)
        self.assertIn("feature.txt", head_run["files"])

    def test_fork_pull_requests_compare_across_repositories(self) -> None:
        self.output.write_text("", encoding="utf-8")
        pr_json = json.dumps(
            {
                "number": 7,
                "base": {"sha": self.base_tip, "ref": "main", "repo": {"full_name": "owner/repo"}},
                "head": {"sha": self.head_sha, "repo": {"full_name": "contributor/repo"}},
            }
        )
        result = subprocess.run(
            [str(GUARD)],
            env=self._env(
                MODE="review",
                EVENT="issue_comment",
                COMMENT_BODY="/codeboarding",
                AUTHOR_ASSOCIATION="COLLABORATOR",
                ISSUE_PR_URL="repos/owner/repo/pulls/7",
                GITHUB_RUN_ID="1",
                GH_HOST="https://github.com",
                CB_PR_JSON=pr_json,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        requested = self.gh_log.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            f"repos/owner/repo/compare/{self.base_tip}...contributor:{self.head_sha}",
            requested,
            "a fork's head must be owner-qualified or the comparison silently falls back",
        )
        self.assertEqual(self._outputs()["merge_base_sha"], self.fork_point)

    def test_unresolvable_merge_base_falls_back_to_the_event_base(self) -> None:
        _write_stub(self.bin_dir, "gh", "#!/bin/sh\nexit 1\n")
        self._run_guard()
        outputs = self._outputs()

        self.assertEqual(outputs["merge_base_sha"], self.base_tip)
        self.assertEqual(outputs["behind_by"], "0")


if __name__ == "__main__":
    unittest.main()
