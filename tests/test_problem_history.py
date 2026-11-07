"""Problem history: the facts a sync run collects, the request it sends, and the ledger commit it makes."""

import gzip
import importlib.util
import io
import json
import os
import re
import sys
import threading
import unittest
import zipfile
from contextlib import nullcontext
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "problem_history.py"
_SPEC = importlib.util.spec_from_file_location("problem_history", _SCRIPT)
problem_history = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = problem_history
_SPEC.loader.exec_module(problem_history)

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
ENV = {
    "REPOSITORY": "o/r",
    "BRANCH": "main",
    "PUSH_SHA": "abc1234def",
    "PUSH_BEFORE": "0001112223",
    "CLOSED_FUNCTIONS_URL": "https://svc.test/",
    "GITHUB_TOKEN": "t",
}
ENVELOPE = {
    "feature": "architecture",
    "schema": 1,
    "status": "ok",
    "pr": 41,
    "data": {"findings": [{"severity": "high"}]},
}


def _http_error(code):
    return HTTPError("https://api.test", code, "error", {}, io.BytesIO(b""))


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, value in files.items():
            z.writestr(name, value if isinstance(value, str) else json.dumps(value))
    return buf.getvalue()


def issue_node(number, state="OPEN", updated="2026-11-07T10:00:00Z", closed=None, **extra):
    node = {
        "number": number,
        "title": f"Issue {number} crashes",
        "body": "It breaks when saving.",
        "url": f"https://github.com/o/r/issues/{number}",
        "state": state,
        "createdAt": "2026-10-01T00:00:00Z",
        "closedAt": closed,
        "updatedAt": updated,
        "authorAssociation": "NONE",
        "author": {"__typename": "User", "login": "reporter"},
        "repository": {"nameWithOwner": "o/r"},
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "timelineItems": {"nodes": []},
    }
    node.update(extra)
    return node


def pr_node(number, merged, updated=None, paths=("a.py",), approvers=(), more=False, head=None):
    return {
        "number": number,
        "title": f"Fix {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "mergedAt": merged,
        "updatedAt": updated or merged,
        "headRefOid": head or f"head{number}",
        "author": {"login": "dev"},
        "reviews": {"nodes": [{"author": {"login": a}} for a in approvers]},
        "files": {"pageInfo": {"hasNextPage": more, "endCursor": "0"}, "nodes": [{"path": p} for p in paths]},
    }


def _page(pages, cursor):
    i = int(cursor or 0)
    nodes = pages[i] if i < len(pages) else []
    return {"pageInfo": {"hasNextPage": i + 1 < len(pages), "endCursor": str(i + 1)}, "nodes": nodes}


class FakeGitHub(problem_history.GitHub):
    """The ledger branch, issues, pull requests, runs and artifacts of one repository, in memory."""

    def __init__(self, head=None, ledger=None):
        self.api, self.graphql_url, self.token = "https://api.test", "https://api.test/graphql", "t"
        self.head = head
        self.ledgers = {head: ledger} if head and ledger is not None else {}
        self.issue_pages, self.merged_pages, self.file_pages = {}, [], {}
        self.runs, self.artifacts, self.archives = {}, {}, {}
        self.graphql_calls, self.writes, self.commits = [], [], {}
        self.races = []  # (head, ledger) another run writes just before our next ref update

    def graphql(self, query, variables):
        self.graphql_calls.append((query, variables))
        if "issues(" in query:
            key = (tuple(variables["states"]) if variables["states"] else None, variables["dir"])
            return {"repository": {"issues": _page(self.issue_pages.get(key, []), variables["c"])}}
        if "pullRequests(" in query:
            return {"repository": {"pullRequests": _page(self.merged_pages, variables["c"])}}
        return {"repository": {"pullRequest": {"files": _page(self.file_pages[variables["n"]], variables["c"])}}}

    def download(self, url):
        return self.archives[url]

    def call(self, method, path, payload=None, accept="application/vnd.github+json"):
        return json.dumps(self._route(method, path, payload)).encode()

    def _route(self, method, path, payload):
        if method == "GET" and path.endswith("/git/ref/heads/codeboarding/briefing"):
            if self.head is None:
                raise _http_error(404)
            return {"object": {"sha": self.head}}
        if method == "GET" and "/contents/problems.json?ref=" in path:
            ref = path.split("ref=")[1]
            if ref not in self.ledgers:
                raise _http_error(404)
            return self.ledgers[ref]
        if method == "GET" and "/actions/runs?head_sha=" in path:
            return {"workflow_runs": self.runs.get(re.search(r"head_sha=(\w+)", path).group(1), [])}
        if method == "GET" and "/artifacts?name=" in path:
            run = int(re.search(r"runs/(\d+)/", path).group(1))
            return {"artifacts": self.artifacts.get(run, [])}
        self.writes.append((method, path, payload))
        if path.endswith("/git/blobs"):
            return {"sha": "blob1"}
        if path.endswith("/git/trees"):
            return {"sha": "tree1"}
        if path.endswith("/git/commits"):
            sha = f"commit{len(self.commits) + 1}"
            self.commits[sha] = payload
            return {"sha": sha}
        if self.races:
            self.head, ledger = self.races.pop(0)
            self.ledgers[self.head] = ledger
        if method == "PATCH" and path.endswith("/git/refs/heads/codeboarding/briefing"):
            if self.commits[payload["sha"]]["parents"] != [self.head] or payload["force"]:
                raise _http_error(422)
        elif method == "POST" and path.endswith("/git/refs"):
            if self.head is not None:
                raise _http_error(422)
        else:
            raise AssertionError(f"unexpected {method} {path}")
        self.head = payload["sha"]
        self.ledgers[self.head] = json.loads(self.writes[-4][2]["content"])
        return {}


def _service(seen, problems=3):
    def post(url, data, headers):
        body = json.loads(gzip.decompress(data))
        seen.append({"url": url, "body": body, "headers": headers})
        merged = (body["ledger"] or {}).get("merged", []) + body["merged"]
        ledger = {"schema": 1, "repo": body["repo"], "merged": merged, "problems": [{}] * problems}
        return {"ledger": ledger, "summary": {"problems": problems, "open": 2, "added": 1}}

    return post


class _Patched(unittest.TestCase):
    def setUp(self):
        self.seen = []
        for name, kwargs in (("_post", {"side_effect": _service(self.seen)}), ("_bearer", {"return_value": {}})):
            patch = mock.patch.object(problem_history, name, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    def run_update(self, gh, env=ENV):
        with mock.patch("builtins.print"):
            return problem_history.update(gh, dict(env), NOW)


class RequestTests(_Patched):
    def test_first_run_reads_open_issues_recent_closed_and_56_days_of_merges(self):
        gh = FakeGitHub()
        gh.issue_pages[(("OPEN",), "DESC")] = [[issue_node(1)], [issue_node(2)]]
        gh.issue_pages[(("CLOSED",), "DESC")] = [
            [
                issue_node(3, "CLOSED", closed="2026-10-20T00:00:00Z"),
                issue_node(4, "CLOSED", updated="2026-10-01T00:00:00Z", closed="2026-07-01T00:00:00Z"),
            ]
        ]
        gh.merged_pages = [
            [
                pr_node(41, "2026-11-06T00:00:00Z", approvers=("ana", "ana", "bo")),
                pr_node(40, "2026-08-01T00:00:00Z", updated="2026-11-05T00:00:00Z"),
                pr_node(39, "2026-07-01T00:00:00Z", updated="2026-07-01T00:00:00Z"),
            ]
        ]
        self.run_update(gh)

        body = self.seen[0]["body"]
        self.assertEqual(self.seen[0]["url"], "https://svc.test/v1/briefing")
        self.assertEqual(
            {k: body[k] for k in ("version", "repo", "now", "branch", "push", "ledger", "issuesThrough")},
            {
                "version": 1,
                "repo": "o/r",
                "now": "2026-11-07T12:00:00Z",
                "branch": "main",
                "push": {"sha": "abc1234def", "before": "0001112223"},
                "ledger": None,
                "issuesThrough": "2026-11-07T12:00:00Z",
            },
        )
        self.assertEqual([i["number"] for i in body["issues"]], [1, 2, 3])
        closed_pass = [v for q, v in gh.graphql_calls if "issues(" in q and v["states"] == ["CLOSED"]][0]
        self.assertEqual(closed_pass["since"], "2026-08-09T12:00:00Z")
        self.assertEqual(
            body["merged"],
            [
                {
                    "number": 41,
                    "title": "Fix 41",
                    "url": "https://github.com/o/r/pull/41",
                    "author": "dev",
                    "mergedAt": "2026-11-06T00:00:00Z",
                    "approvers": ["ana", "bo"],
                    "paths": ["a.py"],
                }
            ],
        )
        self.assertEqual(body["findings"], {})

    def test_later_run_reads_since_the_ledger_with_overlap(self):
        ledger = {
            "schema": 1,
            "repo": "o/r",
            "issuesThrough": "2026-11-07T09:00:00Z",
            "updatedAt": "2026-11-07T09:00:00Z",
            "through": {"sha": "s", "mergedAt": "2026-11-07T08:30:00Z"},
            "merged": [{"number": 50, "mergedAt": "2026-11-07T08:30:00Z"}],
        }
        gh = FakeGitHub("h1", ledger)
        gh.issue_pages[(None, "ASC")] = [[issue_node(9, updated="2026-11-07T11:00:00Z")]]
        gh.merged_pages = [
            [
                pr_node(51, "2026-11-07T11:50:00Z"),
                pr_node(50, "2026-11-07T08:30:00Z"),
                pr_node(49, "2026-11-07T07:00:00Z", updated="2026-11-07T07:20:00Z"),
                pr_node(48, "2026-11-07T06:00:00Z"),
            ]
        ]
        self.run_update(gh)

        body = self.seen[0]["body"]
        issue_calls = [v for q, v in gh.graphql_calls if "issues(" in q]
        self.assertEqual(len(issue_calls), 1)
        self.assertEqual((issue_calls[0]["states"], issue_calls[0]["since"]), (None, "2026-11-07T08:55:00Z"))
        self.assertEqual(body["ledger"], ledger)
        self.assertEqual([i["number"] for i in body["issues"]], [9])
        self.assertEqual(body["issuesThrough"], "2026-11-07T12:00:00Z")
        # An hour of overlap re-reads #50; #49 merged earlier and the list stops at it.
        self.assertEqual([m["number"] for m in body["merged"]], [51, 50])

    def test_findings_only_for_merges_the_ledger_has_not_folded_in(self):
        ledger = {"schema": 1, "repo": "o/r", "issuesThrough": "2026-11-07T11:00:00Z", "merged": [{"number": 50}]}
        ledger["through"] = {"mergedAt": "2026-11-07T11:00:00Z"}
        gh = FakeGitHub("h1", ledger)
        gh.merged_pages = [[pr_node(51, "2026-11-07T11:50:00Z"), pr_node(50, "2026-11-07T11:00:00Z")]]
        for n in (50, 51):
            gh.runs[f"head{n}"] = [{"id": n, "name": "CodeBoarding", "run_attempt": 1}]
            gh.artifacts[n] = [{"name": f"codeboarding-review-{n}-1", "archive_download_url": f"zip{n}"}]
            gh.archives[f"zip{n}"] = _zip({"closed/architecture.json": {**ENVELOPE, "pr": n}})
        self.run_update(gh)
        self.assertEqual(list(self.seen[0]["body"]["findings"]), ["51"])

    def test_capped_later_run_records_where_it_stopped(self):
        gh = FakeGitHub("h1", {"schema": 1, "repo": "o/r", "issuesThrough": "2026-11-01T00:00:00Z"})
        gh.issue_pages[(None, "ASC")] = [
            [issue_node(1, updated="2026-11-02T00:00:00Z"), issue_node(2, updated="2026-11-03T00:00:00Z")],
            [issue_node(3, updated="2026-11-04T00:00:00Z")],
        ]
        with mock.patch.object(problem_history, "MAX_ISSUES", 2):
            notice = self.run_update(gh)
        body = self.seen[0]["body"]
        self.assertEqual([i["number"] for i in body["issues"]], [1, 2])
        self.assertEqual(body["issuesThrough"], "2026-11-03T00:00:00Z")
        self.assertIn("first 2 issues", notice)

    def test_changed_paths_page_and_are_capped(self):
        gh = FakeGitHub()
        gh.merged_pages = [[pr_node(41, "2026-11-06T00:00:00Z", paths=[f"a{i}" for i in range(100)], more=True)]]
        gh.file_pages[41] = [[{"path": f"b{i}"} for i in range(100)], [{"path": f"c{i}"} for i in range(100)]]
        gh.file_pages[41].append([{"path": f"d{i}"} for i in range(100)])
        gh.file_pages[41].append([{"path": "never"}])
        self.run_update(gh)
        paths = self.seen[0]["body"]["merged"][0]["paths"]
        self.assertEqual(len(paths), 300)
        self.assertEqual((paths[0], paths[-1]), ("a0", "c99"))

    def test_oversized_bodies_are_trimmed_before_the_request_is_refused(self):
        big = {"issues": [{"body": os.urandom(50_000).hex()}]}
        with mock.patch.object(problem_history, "MAX_BUNDLE_BYTES", 20_000):
            data = problem_history.pack(big)
        self.assertEqual(len(json.loads(gzip.decompress(data))["issues"][0]["body"]), problem_history.MAX_BODY_CHARS)
        with mock.patch.object(problem_history, "MAX_BUNDLE_BYTES", 10), self.assertRaises(problem_history.Skip):
            problem_history.pack(big)


class IssueFactsTests(unittest.TestCase):
    def pr(self, number, repo="o/r", **extra):
        return {
            "__typename": "PullRequest",
            "number": number,
            "title": f"PR {number}",
            "url": f"https://github.com/{repo}/pull/{number}",
            "state": "OPEN",
            "isDraft": False,
            "reviewDecision": None,
            "updatedAt": "2026-11-05T00:00:00Z",
            "author": {"login": "dev"},
            "repository": {"nameWithOwner": repo},
            **extra,
        }

    def test_node_maps_to_issue_facts(self):
        timeline = [
            {"__typename": "CrossReferencedEvent", "willCloseTarget": False, "source": self.pr(5)},
            {
                "__typename": "CrossReferencedEvent",
                "willCloseTarget": True,
                "source": self.pr(
                    6,
                    state="MERGED",
                    reviewDecision="APPROVED",
                    commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                ),
            },
            {"__typename": "ConnectedEvent", "subject": {"__typename": "Issue"}, "source": self.pr(7, isDraft=True)},
            {"__typename": "ConnectedEvent", "subject": self.pr(8, "o/other"), "source": {"__typename": "Issue"}},
            {"__typename": "DisconnectedEvent", "subject": self.pr(8, "o/other"), "source": {"__typename": "Issue"}},
            {"__typename": "CrossReferencedEvent", "willCloseTarget": True, "source": None},
            {"__typename": "CrossReferencedEvent", "source": {"__typename": "Issue", "number": 2}},
            {
                "__typename": "ClosedEvent",
                "closer": {
                    "__typename": "PullRequest",
                    "number": 6,
                    "author": None,
                    "repository": {"nameWithOwner": "o/r"},
                },
            },
            {
                "__typename": "CrossReferencedEvent",
                "willCloseTarget": False,
                "source": self.pr(9, reviewDecision="CHANGES_REQUESTED", author=None),
            },
        ]
        node = issue_node(
            12,
            state="CLOSED",
            closed="2026-11-06T00:00:00Z",
            body=None,
            author={"__typename": "Bot", "login": "renovate"},
            authorAssociation="CONTRIBUTOR",
            labels={"nodes": [{"name": "bug"}]},
            assignees={"nodes": [{"login": "ana"}]},
            timelineItems={"nodes": timeline},
        )
        facts = problem_history.issue_facts(node)
        linked = facts.pop("linked")
        self.assertEqual(
            facts,
            {
                "repo": "o/r",
                "number": 12,
                "title": "Issue 12 crashes",
                "body": "",
                "url": "https://github.com/o/r/issues/12",
                "state": "CLOSED",
                "createdAt": "2026-10-01T00:00:00Z",
                "closedAt": "2026-11-06T00:00:00Z",
                "updatedAt": "2026-11-07T10:00:00Z",
                "author": "renovate",
                "authorBot": True,
                "association": "CONTRIBUTOR",
                "labels": ["bug"],
                "assignees": ["ana"],
                "closedBy": {"repo": "o/r", "number": 6},
            },
        )
        by_number = {p["number"]: p for p in linked}
        self.assertEqual(list(by_number), [5, 6, 7, 8, 9])
        self.assertEqual(
            by_number[6],
            {
                "repo": "o/r",
                "number": 6,
                "title": "PR 6",
                "url": "https://github.com/o/r/pull/6",
                "state": "MERGED",
                "draft": False,
                "updatedAt": "2026-11-05T00:00:00Z",
                "review": "approved",
                "checks": "SUCCESS",
                "closes": True,
                "author": "dev",
            },
        )
        self.assertEqual(
            {n: p["closes"] for n, p in by_number.items()}, {5: False, 6: True, 7: True, 8: False, 9: False}
        )
        self.assertTrue(by_number[7]["draft"])
        self.assertEqual(by_number[8]["repo"], "o/other")
        self.assertEqual((by_number[9]["review"], "author" in by_number[9]), ("changes", False))
        self.assertIsNone(by_number[5]["checks"])

    def test_an_issue_with_no_closer_or_author(self):
        facts = problem_history.issue_facts(issue_node(3, author=None, authorAssociation=None))
        self.assertNotIn("author", facts)
        self.assertEqual((facts["authorBot"], facts["association"], facts["closedBy"]), (False, "NONE", None))

    def test_graphql_keeps_partial_answers_and_refuses_missing_ones(self):
        gh = problem_history.GitHub(ENV)
        partial = {"data": {"repository": {"x": 1}}, "errors": [{"message": "Resource not accessible"}]}
        with mock.patch.object(gh, "call", return_value=json.dumps(partial).encode()):
            self.assertEqual(gh.graphql("q", {}), {"repository": {"x": 1}})
        refused = {"data": {"repository": None}, "errors": [{"message": "Could not resolve"}]}
        with mock.patch.object(gh, "call", return_value=json.dumps(refused).encode()):
            with self.assertRaisesRegex(RuntimeError, "Could not resolve"):
                gh.graphql("q", {})


class ArtifactTests(unittest.TestCase):
    def test_architecture_read_only_when_it_has_findings_for_this_pr(self):
        read = problem_history.read_architecture
        self.assertEqual(
            read(_zip({"metadata.json": {"pr_number": "41"}, "closed/architecture.json": ENVELOPE}), 41), ENVELOPE
        )
        self.assertEqual(
            read(_zip({"closed/architecture.json": {**ENVELOPE, "status": "partial"}}), 41)["status"], "partial"
        )
        self.assertIsNone(read(_zip({"closed/architecture.json": {**ENVELOPE, "status": "failed"}}), 41))
        self.assertIsNone(read(_zip({"metadata.json": {"pr_number": "7"}, "closed/architecture.json": ENVELOPE}), 41))
        self.assertIsNone(read(_zip({"analysis.json": "{}"}), 41))

    def test_newest_codeboarding_run_with_a_live_artifact(self):
        gh = FakeGitHub()
        gh.runs["h"] = [
            {"id": 1, "name": "Tests", "run_attempt": 1},
            {"id": 2, "name": "CodeBoarding", "run_attempt": 2},
            {"id": 3, "name": "CodeBoarding", "run_attempt": 1},
        ]
        gh.artifacts[2] = [{"name": "codeboarding-review-2-2", "expired": True, "archive_download_url": "z2"}]
        gh.artifacts[3] = [
            {"name": "codeboarding-review-3-1-extra", "archive_download_url": "nope"},
            {"name": "codeboarding-review-3-1", "archive_download_url": "z3"},
        ]
        gh.archives["z3"] = _zip({"closed/architecture.json": ENVELOPE})
        self.assertEqual(problem_history.review_findings(gh, "o/r", 41, "h"), ENVELOPE)

    def test_unreadable_artifacts_are_counted_not_fatal(self):
        gh = FakeGitHub()
        merged = [{"number": 41, "mergedAt": "2026-11-06T00:00:00Z"}]
        with mock.patch.object(gh, "_route", side_effect=_http_error(403)):
            findings, errors = problem_history.collect_findings(gh, "o/r", merged, {41: "h"}, None)
        self.assertEqual(findings, {})
        self.assertEqual(len(errors), 1)

    def test_first_run_reads_at_most_40_artifacts_newest_first(self):
        gh = FakeGitHub()
        merged = [{"number": n, "mergedAt": f"2026-10-{n % 28 + 1:02d}T{n % 24:02d}:00:00Z"} for n in range(1, 61)]
        with mock.patch.object(problem_history, "review_findings", return_value=None) as rf:
            problem_history.collect_findings(gh, "o/r", merged, {n: f"h{n}" for n in range(1, 61)}, None)
        self.assertEqual(rf.call_count, 40)
        newest = sorted(merged, key=lambda m: m["mergedAt"], reverse=True)[:40]
        self.assertEqual([c.args[2] for c in rf.call_args_list], [m["number"] for m in newest])

    def test_download_does_not_send_the_token_to_the_storage_redirect(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/zip":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/blob")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "3")
                self.end_headers()
                self.wfile.write(b"zip")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            gh = problem_history.GitHub(ENV)
            self.assertEqual(gh.download(f"http://127.0.0.1:{server.server_port}/zip"), b"zip")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen, [("/zip", "Bearer t"), ("/blob", None)])


class CommitTests(_Patched):
    def test_first_update_creates_an_orphan_branch_with_one_file(self):
        gh = FakeGitHub()
        gh.merged_pages = [[pr_node(41, "2026-11-06T00:00:00Z")]]
        notice = self.run_update(gh)
        blob, tree, commit, ref = gh.writes
        self.assertEqual(blob[1], "/repos/o/r/git/blobs")
        self.assertEqual(
            tree[2], {"tree": [{"path": "problems.json", "mode": "100644", "type": "blob", "sha": "blob1"}]}
        )
        self.assertEqual(
            commit[2], {"message": "Update problem history (3 problems, through #41)", "tree": "tree1", "parents": []}
        )
        self.assertEqual(
            ref, ("POST", "/repos/o/r/git/refs", {"ref": "refs/heads/codeboarding/briefing", "sha": "commit1"})
        )
        self.assertEqual(gh.ledgers[gh.head]["repo"], "o/r")
        self.assertEqual(
            notice,
            "Problem history: 3 problems (2 open, 1 new), through #41; committed commit1 to codeboarding/briefing",
        )

    def test_later_update_fast_forwards_the_branch(self):
        gh = FakeGitHub(
            "h1", {"schema": 1, "repo": "o/r", "merged": [{"number": 40, "mergedAt": "2026-11-01T00:00:00Z"}]}
        )
        self.run_update(gh)
        commit, ref = gh.writes[2:]
        self.assertEqual(commit[2]["parents"], ["h1"])
        self.assertEqual(commit[2]["message"], "Update problem history (3 problems, through #40)")
        self.assertEqual(
            ref, ("PATCH", "/repos/o/r/git/refs/heads/codeboarding/briefing", {"sha": "commit1", "force": False})
        )
        self.assertEqual(gh.head, "commit1")

    def test_no_merged_pull_request_names_the_push(self):
        gh = FakeGitHub()
        self.run_update(gh)
        self.assertEqual(gh.writes[2][2]["message"], "Update problem history (3 problems, through abc1234)")

    def test_a_concurrent_update_is_reread_and_the_request_rebuilt_once(self):
        gh = FakeGitHub("h1", {"schema": 1, "repo": "o/r", "merged": []})
        theirs = {"schema": 1, "repo": "o/r", "merged": [{"number": 44, "mergedAt": "2026-11-07T11:00:00Z"}]}
        gh.races.append(("h2", theirs))
        self.run_update(gh)
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.seen[0]["body"]["ledger"]["merged"], [])
        self.assertEqual(self.seen[1]["body"]["ledger"], theirs)
        self.assertEqual(self.seen[0]["body"]["issuesThrough"], self.seen[1]["body"]["issuesThrough"])
        self.assertEqual(gh.commits["commit2"]["parents"], ["h2"])
        self.assertEqual(gh.head, "commit2")

    def test_two_conflicts_give_up_without_failing_the_run(self):
        gh = FakeGitHub("h1", {"schema": 1, "repo": "o/r"})
        gh.races += [("h2", {"schema": 1, "repo": "o/r"}), ("h3", {"schema": 1, "repo": "o/r"})]
        with mock.patch.object(problem_history, "GitHub", return_value=gh), mock.patch("builtins.print") as out:
            self.assertEqual(problem_history.main(dict(ENV)), 0)
        self.assertEqual(len(self.seen), 2)
        self.assertIn("::warning::Updating the problem history failed: Conflict", out.call_args.args[0])
        self.assertEqual(gh.head, "h3")

    def test_a_ledger_for_another_repository_is_not_committed(self):
        gh = FakeGitHub()
        with mock.patch.object(problem_history, "_post", return_value={"ledger": {"schema": 1, "repo": "x/y"}}):
            with self.assertRaisesRegex(ValueError, "no ledger"):
                self.run_update(gh)
        self.assertEqual(gh.writes, [])


class MainTests(unittest.TestCase):
    def run_main(self, env, **patches):
        with mock.patch("builtins.print") as out:
            with mock.patch.multiple(problem_history, **patches) if patches else nullcontext():
                self.assertEqual(problem_history.main(env), 0)
        return [c.args[0] for c in out.call_args_list]

    def test_never_raises(self):
        failures = [
            {"update": mock.Mock(side_effect=_http_error(403))},
            {"update": mock.Mock(side_effect=RuntimeError("the service answered 500: failed"))},
            {"update": mock.Mock(side_effect=KeyError("BRANCH"))},
            {"update": mock.Mock(side_effect=ValueError("bad json"))},
        ]
        for patches in failures:
            with self.subTest(error=patches["update"].side_effect):
                lines = self.run_main(dict(ENV), **patches)
                self.assertTrue(lines[0].startswith("::warning::Updating the problem history failed"))
        self.assertIn("issues: read", self.run_main(dict(ENV), update=mock.Mock(side_effect=_http_error(403)))[0])

    def test_missing_token_is_a_warning(self):
        env = {k: v for k, v in ENV.items() if k != "GITHUB_TOKEN"}
        self.assertTrue(self.run_main(env)[0].startswith("::warning::"))

    def test_quota_is_a_notice(self):
        lines = self.run_main(dict(ENV), update=mock.Mock(side_effect=problem_history.Skip("the service answered 402")))
        self.assertEqual(lines, ["::notice::Problem history skipped: the service answered 402"])

    def test_empty_url_does_nothing(self):
        with mock.patch.object(problem_history, "GitHub") as gh:
            self.assertEqual(self.run_main({**ENV, "CLOSED_FUNCTIONS_URL": " "}), [])
        gh.assert_not_called()


class WiringTests(unittest.TestCase):
    ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")

    def step(self):
        start = self.ACTION.index("    - name: Update problem history\n")
        return self.ACTION[start : self.ACTION.index("\n\n", start)]

    def test_opt_in_and_off_by_default(self):
        block = self.ACTION[self.ACTION.index("  problem_history:\n") :]
        self.assertIn("default: 'false'", block[: block.index("\n  github_token:")])

    def test_sync_only_never_fails_the_run(self):
        step = self.step()
        self.assertIn("steps.guard.outputs.mode == 'sync'", step)
        self.assertIn("inputs.problem_history == 'true'", step)
        self.assertIn("inputs.review_guide_url != ''", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn('scripts/problem_history.py"', step)
        self.assertLess(
            self.ACTION.index("- name: Publish baseline analysis for the analyzed commit"),
            self.ACTION.index("- name: Update problem history"),
        )

    def test_shares_the_closed_features_transport(self):
        for shared in (problem_history._bearer, problem_history._post):
            self.assertEqual(shared.__module__, "closed_features")


if __name__ == "__main__":
    unittest.main()
