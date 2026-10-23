"""A setup PR's run previews the pull requests the app listed in its description."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVE = ROOT / "scripts" / "action" / "resolve-preview-prs.sh"
PREVIEW = ROOT / "scripts" / "action" / "review-preview-pr.sh"
NOTIFY = ROOT / "scripts" / "action" / "notify-setup-ready.sh"

PULLS = {
    "412": {"state": "open", "head": {"repo": {"full_name": "owner/repo"}, "sha": "h412"}, "base": {"ref": "main", "sha": "t"}, "title": "Retry failed webhooks"},
    "409": {"state": "open", "head": {"repo": {"full_name": "owner/repo"}, "sha": "h409"}, "base": {"ref": "main", "sha": "t"}, "title": "Split ledger"},
    "401": {"state": "open", "head": {"repo": {"full_name": "owner/repo"}, "sha": "h401"}, "base": {"ref": "main", "sha": "t"}, "title": "Bump SDK"},
    "400": {"state": "open", "head": {"repo": {"full_name": "owner/repo"}, "sha": "h400"}, "base": {"ref": "main", "sha": "t"}, "title": "Fourth"},
    "377": {"state": "open", "head": {"repo": {"full_name": "someone/fork"}, "sha": "h377"}, "base": {"ref": "main", "sha": "t"}, "title": "From a fork"},
    "350": {"state": "closed", "head": {"repo": {"full_name": "owner/repo"}, "sha": "h350"}, "base": {"ref": "main", "sha": "t"}, "title": "Closed"},
    "340": {"state": "open", "head": {"repo": {"full_name": "owner/repo"}, "sha": "h340"}, "base": {"ref": "release", "sha": "t"}, "title": "Other branch"},
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class ResolvePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "bin").mkdir()
        (self.root / "pulls.json").write_text(json.dumps(PULLS))
        self.calls = self.root / "calls"
        gh = self.root / "bin" / "gh"
        # pulls/418 is the setup PR; its body carries the list. compare returns a merge base per head.
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(self.calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            f"pulls = json.load(open({str(self.root / 'pulls.json')!r}))\n"
            "path = sys.argv[2]\n"
            "if path.endswith('/pulls/418'):\n"
            f"    print(open({str(self.root / 'body')!r}).read())\n"
            "elif '/compare/' in path:\n"
            "    print('mb-' + path.rsplit('...', 1)[1])\n"
            "else:\n"
            "    n = path.rsplit('/', 1)[1]\n"
            "    if n not in pulls: sys.exit(1)\n"
            "    print(json.dumps(pulls[n]))\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, body: str, **extra: str) -> dict[str, str]:
        (self.root / "body").write_text(body)
        output = self.root / "output"
        output.write_text("")
        subprocess.run(
            [str(RESOLVE)],
            env={
                "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(self.root),
                "GITHUB_OUTPUT": str(output),
                "REPOSITORY": "owner/repo",
                "PR_NUMBER": "418",
                "HEAD_REF": "codeboarding/setup-action",
                "BASE_REF": "main",
                **extra,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        return dict(line.split("=", 1) for line in output.read_text().splitlines() if line)

    def _entry(self, n: int) -> dict:
        return json.loads((self.root / "codeboarding-preview" / f"{n}.json").read_text())

    def test_the_listed_pull_requests_are_previewed_in_order(self) -> None:
        out = self._run("Adds CodeBoarding.\n\n<!-- codeboarding:preview 412,409 -->\n")
        self.assertEqual(out, {"count": "2", "prs": "412,409"})
        self.assertEqual(self._entry(1), {"pr_number": "412", "head_sha": "h412", "merge_base_sha": "mb-h412", "title": "Retry failed webhooks"})
        self.assertEqual(self._entry(2)["pr_number"], "409")

    def test_forks_closed_and_other_branches_are_skipped_and_the_list_is_capped(self) -> None:
        out = self._run("<!-- codeboarding:preview 377, 350, 340, 412, 412, 418, 409, 401, 400 -->")
        self.assertEqual(out, {"count": "3", "prs": "412,409,401"})

    def test_no_list_means_no_previews(self) -> None:
        self.assertEqual(self._run("Adds CodeBoarding."), {"count": "0", "prs": ""})

    def test_other_pull_requests_read_nothing(self) -> None:
        self.assertEqual(self._run("<!-- codeboarding:preview 412 -->", HEAD_REF="feat/x"), {"count": "0", "prs": ""})
        self.assertFalse(self.calls.exists())


class ReviewPreviewTests(unittest.TestCase):
    """Real git history: main moved on after the PR forked, and the preview applies only the PR's own work."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        repo = self.repo = self.root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "shared.txt").write_text("one\ntwo\nthree\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "X")
        self.fork_point = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-qb", "pr")
        (repo / "webhooks.txt").write_text("retry\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "P")
        self.pr_head = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "main")
        (repo / "ledger.txt").write_text("landed after the fork\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Z")
        self.base = git(repo, "rev-parse", "HEAD")

        # The base analysis this run built, and the setup PR's own review artifact.
        self.base_dir = self.root / "base-state"
        self.base_dir.mkdir()
        (self.base_dir / "analysis.json").write_text('{"components": []}')
        self.review_dir = self.root / "review-artifact"
        self.review_dir.mkdir()
        (self.review_dir / "metadata.json").write_text(json.dumps({"base_artifact": "cb-base-Z", "base_artifact_id": "77"}))

        # The action tree with the engine and renderer stubbed; the packager is the real one.
        action = self.action = self.root / "action"
        (action / "scripts" / "action").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "action" / "build-review-artifact.sh", action / "scripts" / "action")
        analyze = action / "scripts" / "action" / "analyze.sh"
        analyze.write_text(
            "#!/usr/bin/env bash\n"
            'ls "$2" | sort > "$3/seen-files"\n'
            'echo \'{"components": ["Webhooks"]}\' > "$3/analysis.json"\n'
            'printf "analysis_mode=incremental\\nanalysis_path=%s\\n" "$3/analysis.json"\n',
            encoding="utf-8",
        )
        analyze.chmod(0o755)
        (action / "scripts" / "diff_to_mermaid.py").write_text('print(\'{"n_changed": 1, "analysed_files_changed": 1}\')\n')

        self.preview_dir = self.root / "codeboarding-preview"
        self.preview_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, pr_head: str | None = None) -> dict[str, str]:
        (self.preview_dir / "1.json").write_text(json.dumps(
            {"pr_number": "412", "head_sha": pr_head or self.pr_head, "merge_base_sha": self.fork_point, "title": "Retry failed webhooks"}))
        output = self.root / "output"
        output.write_text("")
        subprocess.run(
            [str(PREVIEW), "1"],
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(self.root),
                "GITHUB_OUTPUT": str(output),
                "ACTION_PATH": str(self.action),
                "CHECKOUT_DIR": str(self.repo),
                "REPOSITORY": "owner/repo",
                "SETUP_PR": "418",
                "REVIEW_BASE_SHA": self.base,
                "BASE_ANALYSIS_PATH": str(self.base_dir / "analysis.json"),
                "REVIEW_ARTIFACT_DIR": str(self.review_dir),
            },
            check=True,
            capture_output=True,
            text=True,
        )
        return dict(line.split("=", 1) for line in output.read_text().splitlines() if line)

    def test_the_pull_request_is_applied_to_the_analysed_commit(self) -> None:
        out = self._run()
        self.assertEqual(out["pr_number"], "412")
        self.assertEqual(out["head_sha"], self.pr_head, "the artifact name carries it, so the app needs no download")
        seen = (self.preview_dir / "state-1" / "seen-files").read_text().split()
        # Z's file is there because the base is Z; the PR's file because it was applied; nothing else.
        self.assertIn("ledger.txt", seen)
        self.assertIn("webhooks.txt", seen)

    def test_the_artifact_names_the_pull_request_and_the_comparison(self) -> None:
        out = self._run()
        meta = json.loads((Path(out["artifact_dir"]) / "metadata.json").read_text())
        self.assertEqual(meta["pr_number"], "412")
        self.assertEqual(meta["head_sha"], self.pr_head)
        self.assertEqual(meta["merge_base_sha"], self.base, "the commit the diagram compared against")
        self.assertEqual(meta["pr_base_sha"], self.base)
        self.assertEqual(meta["basis"], "preview")
        self.assertEqual(meta["setup_pr"], "418")
        self.assertEqual(meta["base_artifact"], "cb-base-Z")
        self.assertEqual(meta["base_artifact_id"], "77")
        self.assertFalse((Path(out["artifact_dir"]) / "base_analysis.json").exists())

    def test_the_base_is_inlined_when_the_setup_review_inlined_it(self) -> None:
        (self.review_dir / "base_analysis.json").write_text("{}")
        out = self._run()
        self.assertTrue((Path(out["artifact_dir"]) / "base_analysis.json").exists())

    def test_a_pull_request_that_conflicts_with_the_analysed_commit_is_skipped(self) -> None:
        git(self.repo, "checkout", "-qb", "clash", self.fork_point)
        (self.repo / "ledger.txt").write_text("a different ledger\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "C")
        clash = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(self._run(pr_head=clash), {})


class FirstPreviewNoticeTests(unittest.TestCase):
    def test_the_email_links_the_first_reviewed_pull_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            calls = root / "calls"
            gh = root / "bin" / "gh"
            gh.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n----\\n" "$*" >> "{calls}"\n', encoding="utf-8")
            gh.chmod(0o755)
            subprocess.run(
                [str(NOTIFY)],
                env={
                    "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                    "REPOSITORY": "owner/repo", "PR_NUMBER": "418", "PR_AUTHOR": "maria-k",
                    "HEAD_REF": "codeboarding/setup-action", "BASE_REF": "main",
                    "FIRST_PR": "412", "FIRST_TITLE": "Retry failed webhooks", "PREVIEW_PRS": "412,409,401",
                },
                check=True,
                capture_output=True,
                text=True,
            )
            (post,) = [c for c in calls.read_text().split("\n----\n") if c.startswith("api -X POST")]
        self.assertIn("@maria-k your CodeBoarding map of `main` is ready, and #412 is reviewed on it.", post)
        self.assertIn("[Open #412 in CodeBoarding](https://app.codeboarding.org/owner/repo/pull/412?", post)
        self.assertIn("#409, #401 follow in a few minutes.", post)
        self.assertIn("(https://app.codeboarding.org/owner/repo/pull/418?", post)


if __name__ == "__main__":
    unittest.main()
