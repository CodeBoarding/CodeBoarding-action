"""Problem history: the facts a sync run collects, the request it sends, and the ledger commit it makes."""

import gzip
import importlib.util
import io
import json
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

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("problem_history", ROOT / "scripts" / "problem_history.py")
problem_history = ph = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = problem_history
_SPEC.loader.exec_module(problem_history)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
ENV = {
    "REPOSITORY": "o/r",
    "BRANCH": "main",
    "PUSH_SHA": "abc1234def",
    "PUSH_BEFORE": "0001112223",
    "CLOSED_FUNCTIONS_URL": "https://svc.test/",
    "GITHUB_TOKEN": "t",
}
ENVELOPE = {"feature": "architecture", "schema": 1, "status": "ok", "pr": 41, "data": {"findings": [{}]}}


def ledger(**fields):
    return {"schema": 1, "repo": "o/r", **fields}


def numbers(items):
    return [i["number"] for i in items]


def _http_error(code, message=None):
    body = json.dumps({"message": message}).encode() if message else b""
    return HTTPError("https://api.test", code, "error", {}, io.BytesIO(body))


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, value in files.items():
            z.writestr(name, value if isinstance(value, str) else json.dumps(value))
    return buf.getvalue()


def issue_node(number, state="OPEN", updated="2026-11-07T10:00:00Z", closed=None, **extra):
    return {
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
        **extra,
    }


def closed_node(number, closed="2026-10-20T00:00:00Z", **extra):
    return issue_node(number, "CLOSED", closed=closed, **extra)


def pr_node(number, merged, updated=None, paths=("a.py",), approvers=(), more=False):
    return {
        "number": number,
        "title": f"Fix {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "mergedAt": merged,
        "updatedAt": updated or merged,
        "headRefOid": f"head{number}",
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

    def __init__(self, head=None, stored=None):
        self.api, self.graphql_url, self.token = "https://api.test", "https://api.test/graphql", "t"
        self.head = head
        self.ledgers = {head: stored} if head and stored is not None else {}
        self.issue_pages, self.merged_pages, self.file_pages = {}, [], {}
        self.open_pages, self.oldest_open, self.by_number = [], None, {}
        self.runs, self.artifacts, self.archives, self.downloads = {}, {}, {}, []
        self.graphql_calls, self.writes, self.commits = [], [], {}
        self.races = []  # (head, ledger) another run writes just before our next ref update
        self.refusal = None  # the message of a 422 every ref update gets, like a ruleset's

    def add_review(self, pr, run, envelope=ENVELOPE, **artifact):
        """A successful CodeBoarding run on head{pr} whose review artifact holds `envelope`."""
        self.runs.setdefault(f"head{pr}", []).append({"id": run, "name": "CodeBoarding", "run_attempt": 1})
        url = f"zip{run}"
        self.artifacts[run] = [{"name": f"codeboarding-review-{run}-1", "archive_download_url": url, **artifact}]
        self.archives[url] = _zip({"closed/architecture.json": {**envelope, "pr": pr}})

    def calls(self, needle):
        """The variables of every GraphQL request whose query contains `needle`."""
        return [v for q, v in self.graphql_calls if needle in q]

    def graphql(self, query, variables):
        self.graphql_calls.append((query, variables))
        if query is ph.OPEN_NUMBERS_QUERY:
            return {"repository": {"issues": _page(self.open_pages, variables["c"])}}
        if query is ph.OLDEST_OPEN_QUERY:
            return {"repository": {"issues": {"nodes": [{"updatedAt": self.oldest_open}]}}}
        if "issue(number:" in query:
            found = {k: self.by_number.get(v) for k, v in variables.items() if re.fullmatch(r"n\d+", k)}
            return {"repository": {f"i{k[1:]}": node for k, node in found.items()}}
        if "issues(" in query:
            key = (tuple(variables["states"]) if variables["states"] else None, variables["dir"])
            return {"repository": {"issues": _page(self.issue_pages.get(key, []), variables["c"])}}
        if "pullRequests(" in query:
            return {"repository": {"pullRequests": _page(self.merged_pages, variables["c"])}}
        return {"repository": {"pullRequest": {"files": _page(self.file_pages[variables["n"]], variables["c"])}}}

    def download(self, url):
        self.downloads.append(url)
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
            return {"artifacts": self.artifacts.get(int(re.search(r"runs/(\d+)/", path).group(1)), [])}
        if method == "GET" and re.search(r"/pulls/\d+$", path):
            return {"head": {"sha": f"head{path.rsplit('/', 1)[1]}"}}
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
            self.head, raced = self.races.pop(0)
            self.ledgers[self.head] = raced
        if self.refusal:
            raise _http_error(422, self.refusal)
        if method == "PATCH" and path.endswith("/git/refs/heads/codeboarding/briefing"):
            if self.commits[payload["sha"]]["parents"] != [self.head] or payload["force"]:
                raise _http_error(422, "Update is not a fast forward")
        elif method == "POST" and path.endswith("/git/refs"):
            if self.head is not None:
                raise _http_error(422, "Reference already exists")
        else:
            raise AssertionError(f"unexpected {method} {path}")
        self.head = payload["sha"]
        self.ledgers[self.head] = json.loads(self.writes[-4][2]["content"])
        return {}


def _patched(**names):
    return mock.patch.multiple(problem_history, **names) if names else nullcontext()


def _service(seen, problems=3):
    def post(url, data, headers, timeout=None):
        body = json.loads(gzip.decompress(data))
        seen.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        merged = (body["ledger"] or {}).get("merged", []) + body["merged"]
        answer = {"schema": 1, "repo": body["repo"], "merged": merged, "problems": [{}] * problems}
        return {"ledger": answer, "summary": {"problems": problems, "open": 2, "added": 1}}

    return post


class _Patched(unittest.TestCase):
    """The service and its credentials faked; `seen` holds every request sent."""

    def setUp(self):
        self.seen = []
        for name, kwargs in (("_post", {"side_effect": _service(self.seen)}), ("_bearer", {"return_value": {}})):
            patch = mock.patch.object(problem_history, name, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    @property
    def body(self):
        return self.seen[0]["body"]

    def run_update(self, gh, **caps):
        with mock.patch("builtins.print"), _patched(**caps):
            return problem_history.update(gh, dict(ENV), NOW)

    def run_main(self, gh):
        with mock.patch.object(problem_history, "GitHub", return_value=gh), mock.patch("builtins.print") as out:
            self.assertEqual(problem_history.main(dict(ENV)), 0)
        return out.call_args.args[0]


class RequestTests(_Patched):
    def test_first_run_reads_open_issues_recent_closed_and_56_days_of_merges(self):
        gh = FakeGitHub()
        gh.issue_pages[(("OPEN",), "DESC")] = [[issue_node(1)], [issue_node(2)]]
        gh.issue_pages[(("CLOSED",), "DESC")] = [
            [closed_node(3), closed_node(4, closed="2026-07-01T00:00:00Z", updated="2026-10-01T00:00:00Z")]
        ]
        gh.merged_pages = [
            [
                pr_node(41, "2026-11-06T00:00:00Z", approvers=("ana", "ana", "bo")),
                pr_node(40, "2026-08-01T00:00:00Z", updated="2026-11-05T00:00:00Z"),
                pr_node(39, "2026-07-01T00:00:00Z"),
            ]
        ]
        self.run_update(gh)

        self.assertEqual(self.seen[0]["url"], "https://svc.test/v1/briefing")
        self.assertEqual(
            {k: self.body[k] for k in ("version", "repo", "now", "branch", "push", "ledger", "issuesThrough")},
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
        self.assertEqual(numbers(self.body["issues"]), [1, 2, 3])
        closed_pass = [v["since"] for v in gh.calls(ph.ISSUES_QUERY) if v["states"] == ["CLOSED"]]
        self.assertEqual(closed_pass, ["2026-08-09T12:00:00Z"])
        self.assertEqual(
            self.body["merged"],
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
        self.assertEqual(self.body["findings"], {})

    def test_later_run_reads_since_the_ledger_with_overlap(self):
        stored = ledger(
            issuesThrough="2026-11-07T09:00:00Z",
            updatedAt="2026-11-07T09:00:00Z",
            through={"sha": "s", "mergedAt": "2026-11-07T08:30:00Z"},
            merged=[{"number": 50, "mergedAt": "2026-11-07T08:30:00Z"}],
        )
        gh = FakeGitHub("h1", stored)
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

        self.assertEqual(
            [(v["states"], v["since"]) for v in gh.calls(ph.ISSUES_QUERY)], [(None, "2026-11-07T08:55:00Z")]
        )
        self.assertEqual(self.body["ledger"], stored)
        self.assertEqual(numbers(self.body["issues"]), [9])
        self.assertEqual(self.body["issuesThrough"], "2026-11-07T12:00:00Z")
        # An hour of overlap re-reads #50; #49 merged earlier and the list stops at it.
        self.assertEqual(numbers(self.body["merged"]), [51, 50])

    def test_findings_only_for_merges_the_ledger_has_not_folded_in(self):
        stored = ledger(
            issuesThrough="2026-11-07T11:00:00Z",
            merged=[{"number": 50, "warningsRead": True}],
            through={"mergedAt": "2026-11-07T11:00:00Z"},
        )
        gh = FakeGitHub("h1", stored)
        gh.merged_pages = [[pr_node(51, "2026-11-07T11:50:00Z"), pr_node(50, "2026-11-07T11:00:00Z")]]
        gh.add_review(50, 50)
        gh.add_review(51, 51)
        self.run_update(gh)
        self.assertEqual(list(self.body["findings"]), ["51"])

    def test_capped_later_run_records_where_it_stopped(self):
        gh = FakeGitHub("h1", ledger(issuesThrough="2026-11-01T00:00:00Z"))
        gh.issue_pages[(None, "ASC")] = [
            [issue_node(1, updated="2026-11-02T00:00:00Z"), issue_node(2, updated="2026-11-03T00:00:00Z")],
            [issue_node(3, updated="2026-11-04T00:00:00Z")],
        ]
        notice = self.run_update(gh, MAX_ISSUES=2)
        self.assertEqual(numbers(self.body["issues"]), [1, 2])
        self.assertEqual(self.body["issuesThrough"], "2026-11-03T00:00:00Z")
        self.assertIn("first 2 issues", notice)

    def test_changed_paths_page_and_stop_at_the_cap(self):
        gh = FakeGitHub()
        gh.merged_pages = [[pr_node(41, "2026-11-06T00:00:00Z", paths=[f"a{i}" for i in range(100)], more=True)]]
        gh.file_pages[41] = [[{"path": f"{p}{i}"} for i in range(100)] for p in "bcd"] + [[{"path": "never"}]]
        self.run_update(gh)
        paths = self.body["merged"][0]["paths"]
        self.assertEqual((len(paths), paths[0], paths[-1]), (300, "a0", "c99"))
        self.assertEqual([v["c"] for v in gh.calls(ph.FILES_QUERY)], ["0", "1"])

    def test_every_body_is_cut_to_2000_characters_and_an_oversized_request_is_refused(self):
        gh = FakeGitHub()
        gh.issue_pages[(("OPEN",), "DESC")] = [[issue_node(1, body="x" * 5000), issue_node(2)]]
        self.run_update(gh)
        self.assertEqual([i["body"] for i in self.body["issues"]], ["x" * 2000, "It breaks when saving."])
        with mock.patch.object(problem_history, "MAX_BUNDLE_BYTES", 10), self.assertRaises(problem_history.Skip):
            problem_history.pack({"issues": []})

    def test_a_cut_counts_utf16_units_and_never_splits_a_surrogate_pair(self):
        smile = "\U0001f600"  # two UTF-16 code units
        for text, want in (
            ("a" * 1999 + smile, "a" * 1999),
            ("a" * 1998 + smile + "b", "a" * 1998 + smile),
            (smile * 1500, smile * 1000),
            ("x\ud83dy", "xy"),
        ):
            self.assertEqual(problem_history.cut(text), want)

    def test_limits_stay_under_the_service_caps_and_inside_the_step(self):
        self.assertLessEqual(max(ph.MAX_FIRST_OPEN + ph.MAX_FIRST_CLOSED, ph.MAX_ISSUES + ph.MAX_RELINKED), 3000)
        self.assertLessEqual(ph.MAX_MERGED, 1000)
        self.assertLessEqual(ph.MAX_ARTIFACTS + ph.MAX_FINDINGS_RETRIES, 200)
        self.run_update(FakeGitHub())
        self.assertEqual(self.seen[0]["timeout"], 300)
        self.assertLess(ph.SERVICE_TIMEOUT, ph.STEP_LIMIT_SECONDS)


class OpenIssuesTests(_Patched):
    def test_every_open_number_is_sent_as_complete(self):
        gh = FakeGitHub()
        gh.open_pages = [[{"number": n} for n in range(1, 101)], [{"number": 205}]]
        self.run_update(gh)
        self.assertEqual(self.body["openIssues"], {"numbers": [*range(1, 101), 205], "complete": True})
        self.assertEqual([v["c"] for v in gh.calls(ph.OPEN_NUMBERS_QUERY)], [None, "1"])
        self.assertIn("first:100", ph.OPEN_NUMBERS_QUERY)
        self.assertIn("nodes{ number }", ph.OPEN_NUMBERS_QUERY)

    def test_5000_is_complete_and_past_it_the_list_is_incomplete(self):
        for pages, complete in ((50, True), (51, False)):
            with self.subTest(pages=pages):
                self.seen.clear()
                gh = FakeGitHub()
                gh.open_pages = [[{"number": p * 100 + k} for k in range(100)] for p in range(pages)]
                notice = self.run_update(gh)
                sent = self.body["openIssues"]
                self.assertEqual((sent["complete"], len(sent["numbers"])), (complete, 5000 if complete else 0))
                self.assertEqual(len(gh.calls(ph.OPEN_NUMBERS_QUERY)), 50)
                self.assertEqual("over 5000 open issues" in notice, not complete)

    def test_a_capped_issue_read_or_a_retry_lets_the_service_drop_nothing(self):
        gh = FakeGitHub("h1", ledger(issuesThrough="2026-11-01T00:00:00Z"))
        gh.open_pages = [[{"number": 1}]]
        gh.issue_pages[(None, "ASC")] = [[issue_node(1), issue_node(2)]]
        self.run_update(gh, MAX_ISSUES=1)
        self.assertEqual(self.body["openIssues"]["complete"], False)

        self.seen.clear()
        gh = FakeGitHub("h1", ledger(merged=[]))
        gh.open_pages = [[{"number": 1}]]
        gh.races.append(("h2", ledger(merged=[])))
        self.run_update(gh)
        self.assertEqual([s["body"]["openIssues"]["complete"] for s in self.seen], [True, False])

    def test_an_unreadable_list_is_a_note(self):
        with mock.patch.object(problem_history, "open_issue_numbers", side_effect=RuntimeError("boom")):
            notice = self.run_update(FakeGitHub())
        self.assertEqual(self.body["openIssues"], {"numbers": [], "complete": False})
        self.assertIn("open issues not listed (RuntimeError: boom)", notice)


class FirstRunCapTests(_Patched):
    def test_each_pass_has_its_own_cap_and_never_claims_what_it_skipped(self):
        for open_cap, closed_cap, read, through, note in (
            # A cut open pass stops at the oldest open issue's update.
            (2, 3, [1, 2, 11, 12, 13], "2025-01-01T00:00:00Z", "first 2 open issues"),
            # A cut closed pass stops at the start of its 90 days.
            (3, 1, [1, 2, 3, 11], "2026-08-09T12:00:00Z", "first 1 recently closed issues"),
            (3, 3, [1, 2, 3, 11, 12, 13], "2026-11-07T12:00:00Z", None),
        ):
            with self.subTest(open_cap=open_cap, closed_cap=closed_cap):
                self.seen.clear()
                gh = FakeGitHub()
                gh.oldest_open = "2025-01-01T00:00:00Z"
                gh.issue_pages[(("OPEN",), "DESC")] = [[issue_node(1), issue_node(2), issue_node(3)]]
                gh.issue_pages[(("CLOSED",), "DESC")] = [[closed_node(n) for n in (11, 12, 13)]]
                notice = self.run_update(gh, MAX_FIRST_OPEN=open_cap, MAX_FIRST_CLOSED=closed_cap)
                self.assertEqual((numbers(self.body["issues"]), self.body["issuesThrough"]), (read, through))
                if note:
                    self.assertIn(note, notice)
                else:
                    self.assertNotIn("issues only", notice)


class RelinkedTests(_Patched):
    @staticmethod
    def stored(number, state="OPEN", linked=()):
        links = [{"repo": "o/r", "number": n, "state": s, "updatedAt": u} for n, s, u in linked]
        return {"repo": "o/r", "number": number, "state": state, "updatedAt": "2026-10-01T00:00:00Z", "linked": links}

    def test_open_issues_with_an_open_pull_request_are_read_again(self):
        day = "2026-11-01T00:00:00Z"
        stored = ledger(issuesThrough="2026-11-07T11:00:00Z")
        stored["issues"] = [
            self.stored(1, linked=[(40, "OPEN", day)]),
            self.stored(2, linked=[(41, "MERGED", day)]),
            self.stored(3, "CLOSED", linked=[(42, "OPEN", day)]),
            self.stored(4, linked=[(43, "OPEN", day)]),
            self.stored(5),
            {**self.stored(6, linked=[(44, "OPEN", day)]), "repo": "o/other"},
            self.stored(7, linked=[(45, "OPEN", day)]),
        ]
        gh = FakeGitHub("h1", stored)
        gh.issue_pages[(None, "ASC")] = [[issue_node(4, updated="2026-11-07T11:30:00Z")]]
        gh.by_number = {1: issue_node(1), 7: issue_node(7, repository={"nameWithOwner": "o/elsewhere"})}
        self.run_update(gh)
        self.assertEqual(gh.calls("issue(number:"), [{"owner": "o", "name": "r", "n0": 7, "n1": 1}])
        # #4 was read as updated already; #7 now answers from another repository and is left out.
        self.assertEqual(numbers(self.body["issues"]), [4, 1])

    def test_at_most_100_newest_pull_request_activity_first_25_per_query(self):
        stored = [
            self.stored(n, linked=[(1000 + n, "OPEN", f"2026-{n % 12 + 1:02d}-01T00:00:00Z")]) for n in range(1, 151)
        ]
        picked = problem_history.relinked({"issues": stored}, "o/r", set())
        self.assertEqual((len(picked), picked[0]), (100, 143))  # the latest December, highest number first
        gh = FakeGitHub()
        problem_history.issues_by_number(gh, "o/r", picked)
        self.assertEqual(len(gh.graphql_calls), 4)

    def test_a_first_run_reads_nothing_again(self):
        gh = FakeGitHub()
        self.run_update(gh)
        self.assertEqual(gh.calls("issue(number:"), [])


class IssueFactsTests(unittest.TestCase):
    @staticmethod
    def pr(number, repo="o/r", **extra):
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
        issue = {"__typename": "Issue"}
        rollup = {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}
        merged = self.pr(6, state="MERGED", reviewDecision="APPROVED", commits=rollup)
        closer = {"__typename": "PullRequest", "number": 6, "author": None, "repository": {"nameWithOwner": "o/r"}}
        timeline = [
            {"__typename": "CrossReferencedEvent", "willCloseTarget": False, "source": self.pr(5)},
            {"__typename": "CrossReferencedEvent", "willCloseTarget": True, "source": merged},
            {"__typename": "ConnectedEvent", "subject": issue, "source": self.pr(7, isDraft=True)},
            {"__typename": "ConnectedEvent", "subject": self.pr(8, "o/other"), "source": issue},
            {"__typename": "DisconnectedEvent", "subject": self.pr(8, "o/other"), "source": issue},
            {"__typename": "CrossReferencedEvent", "willCloseTarget": True, "source": None},
            {"__typename": "CrossReferencedEvent", "source": {"__typename": "Issue", "number": 2}},
            {"__typename": "ClosedEvent", "closer": closer},
            {
                "__typename": "CrossReferencedEvent",
                "source": self.pr(9, reviewDecision="CHANGES_REQUESTED", author=None),
            },
        ]
        node = closed_node(
            12,
            closed="2026-11-06T00:00:00Z",
            body=None,
            author={"__typename": "Bot", "login": "renovate"},
            authorAssociation="CONTRIBUTOR",
            labels={"nodes": [{"name": "bug"}]},
            assignees={"nodes": [{"login": "ana"}]},
            timelineItems={"nodes": timeline},
        )
        facts = problem_history.issue_facts(node)
        linked = {p["number"]: p for p in facts.pop("linked")}
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
        self.assertEqual(list(linked), [5, 6, 7, 8, 9])
        self.assertEqual(
            linked[6],
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
        self.assertEqual({n: p["closes"] for n, p in linked.items()}, {5: False, 6: True, 7: True, 8: False, 9: False})
        self.assertTrue(linked[7]["draft"])
        self.assertEqual(linked[8]["repo"], "o/other")
        self.assertEqual((linked[9]["review"], "author" in linked[9]), ("changes", False))
        self.assertIsNone(linked[5]["checks"])

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


class ArtifactTests(_Patched):
    def test_architecture_read_only_when_it_answers_for_this_pr(self):
        skipped = {**ENVELOPE, "status": "skipped", "data": None}
        for files, want in (
            ({"metadata.json": {"pr_number": "41"}, "closed/architecture.json": ENVELOPE}, ENVELOPE),
            ({"closed/architecture.json": {**ENVELOPE, "status": "partial"}}, {**ENVELOPE, "status": "partial"}),
            ({"closed/architecture.json": {**ENVELOPE, "status": "failed"}}, None),
            ({"closed/architecture.json": skipped}, skipped),
            ({"metadata.json": {"pr_number": "7"}, "closed/architecture.json": ENVELOPE}, None),
            ({"analysis.json": "{}"}, None),
        ):
            with self.subTest(files=files):
                self.assertEqual(problem_history.read_architecture(_zip(files), 41), want)

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

    def test_an_artifact_over_50_mb_is_not_downloaded(self):
        gh = FakeGitHub()
        gh.add_review(41, 2, size_in_bytes=50 * 2**20 + 1)
        gh.add_review(41, 1, size_in_bytes=900)
        del gh.runs["head41"][1]["run_attempt"]  # read as the first attempt
        self.assertEqual(problem_history.review_findings(gh, "o/r", 41, "head41"), ENVELOPE)
        self.assertEqual(gh.downloads, ["zip1"])
        gh.artifacts[1] = []
        with self.assertRaisesRegex(ValueError, "over 50 MB"):
            problem_history.review_findings(gh, "o/r", 41, "head41")
        self.assertEqual(gh.downloads, ["zip1"])

    def test_unreadable_artifacts_are_counted_not_fatal(self):
        gh = FakeGitHub()
        merged = [{"number": 41, "mergedAt": "2026-11-06T00:00:00Z"}]
        with mock.patch.object(gh, "_route", side_effect=_http_error(403)):
            findings, errors = problem_history.collect_findings(gh, "o/r", merged, {41: "h"}, None)
        self.assertEqual((findings, len(errors)), ({}, 1))

    def asked(self, merged, heads, stored=None):
        """The (pr, head) pairs collect_findings looks up a review for."""
        with mock.patch.object(problem_history, "review_findings", return_value=None) as rf:
            problem_history.collect_findings(FakeGitHub(), "o/r", merged, heads, stored)
        return [c.args[2:] for c in rf.call_args_list]

    def test_first_run_reads_at_most_40_artifacts_newest_first(self):
        merged = [{"number": n, "mergedAt": f"2026-10-{n % 28 + 1:02d}T{n % 24:02d}:00:00Z"} for n in range(1, 61)]
        asked = self.asked(merged, {n: f"h{n}" for n in range(1, 61)})
        newest = sorted(merged, key=lambda m: m["mergedAt"], reverse=True)[:40]
        self.assertEqual([n for n, _ in asked], numbers(newest))

    def test_unread_merges_are_asked_again_newest_first_at_most_10(self):
        merged = [{"number": n, "mergedAt": f"2026-10-{n:02d}T00:00:00Z"} for n in range(1, 21)]
        merged[19]["warningsRead"] = True  # #20 read
        merged[18]["warningsRead"] = False  # #19 not
        stored = {"merged": merged, "warnings": [{"id": "warning:o/r#18:x", "pr": 18}]}
        new = [{"number": 30, "mergedAt": "2026-11-06T00:00:00Z"}]
        asked = self.asked(new, {30: "h30"}, stored)
        # #18 has warnings stored, which a ledger from before warningsRead counts as read.
        self.assertEqual([n for n, _ in asked], [30, 19, 17, 16, 15, 14, 13, 12, 11, 10, 9])
        self.assertEqual([h for _, h in asked][:3], ["h30", "head19", "head17"])

    def test_a_retried_merge_sends_what_it_found(self):
        stored = ledger(
            issuesThrough="2026-11-07T11:00:00Z",
            merged=[{"number": 50, "mergedAt": "2026-11-01T00:00:00Z", "warningsRead": False}],
        )
        gh = FakeGitHub("h1", stored)
        gh.add_review(50, 5)
        self.run_update(gh)
        self.assertEqual(list(self.body["findings"]), ["50"])

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
    def test_each_update_is_one_commit_of_one_file_on_the_branch(self):
        branch = "refs/heads/codeboarding/briefing"
        created = ("POST", "/repos/o/r/git/refs", {"ref": branch, "sha": "commit1"})
        moved = ("PATCH", f"/repos/o/r/git/{branch}", {"sha": "commit1", "force": False})
        last = ledger(merged=[{"number": 40, "mergedAt": "2026-11-01T00:00:00Z"}])
        for head, stored, merged, through, ref in (
            (None, None, [[pr_node(41, "2026-11-06T00:00:00Z")]], "#41", created),  # an orphan branch
            ("h1", last, [], "#40", moved),  # fast-forwarded
            (None, None, [], "abc1234", created),  # no merged pull request names the push
        ):
            with self.subTest(through=through):
                gh = FakeGitHub(head, stored)
                gh.merged_pages = merged
                notice = self.run_update(gh)
                blob, tree, commit, written = gh.writes
                self.assertEqual(blob[1], "/repos/o/r/git/blobs")
                self.assertEqual(
                    tree[2]["tree"], [{"path": "problems.json", "mode": "100644", "type": "blob", "sha": "blob1"}]
                )
                message = f"Update problem history (3 problems, through {through})"
                self.assertEqual(commit[2], {"message": message, "tree": "tree1", "parents": [head] if head else []})
                self.assertEqual((written, gh.head, gh.ledgers[gh.head]["repo"]), (ref, "commit1", "o/r"))
                summary = f"Problem history: 3 problems (2 open, 1 new), through {through}"
                self.assertEqual(notice, f"{summary}; committed commit1 to codeboarding/briefing")

    def test_a_concurrent_update_is_reread_and_the_request_rebuilt_once(self):
        gh = FakeGitHub("h1", ledger(merged=[]))
        theirs = ledger(merged=[{"number": 44, "mergedAt": "2026-11-07T11:00:00Z"}])
        gh.races.append(("h2", theirs))
        self.run_update(gh)
        first, second = (s["body"] for s in self.seen)
        self.assertEqual((first["ledger"]["merged"], second["ledger"]), ([], theirs))
        self.assertEqual(first["issuesThrough"], second["issuesThrough"])
        self.assertEqual((gh.commits["commit2"]["parents"], gh.head), (["h2"], "commit2"))

    def test_two_conflicts_give_up_without_failing_the_run(self):
        gh = FakeGitHub("h1", ledger())
        gh.races += [("h2", ledger()), ("h3", ledger())]
        line = self.run_main(gh)
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(
            line,
            "::notice::Problem history skipped: codeboarding/briefing moved twice while this run updated it;"
            " the next sync carries on",
        )
        self.assertEqual(gh.head, "h3")

    def test_a_ruleset_refusal_is_a_clear_notice_and_not_retried(self):
        for head, message in (
            ("h1", "Repository rule violations found\n\nCannot update this protected ref.\n\n"),
            (None, "Reference update failed"),
        ):
            with self.subTest(message=message):
                self.seen.clear()
                gh = FakeGitHub(head, ledger() if head else None)
                gh.refusal = message
                line = self.run_main(gh)
                self.assertEqual(len(self.seen), 1)
                self.assertTrue(line.startswith("::notice::Problem history skipped: GitHub refused to write"))
                self.assertIn(" ".join(message.split()), line)
                self.assertIn("a branch named codeboarding", line)
                self.assertNotIn("\n", line)

    def test_a_ledger_for_another_repository_is_not_committed(self):
        gh = FakeGitHub()
        with mock.patch.object(problem_history, "_post", return_value={"ledger": ledger(repo="x/y")}):
            with self.assertRaisesRegex(ValueError, "no ledger"):
                self.run_update(gh)
        self.assertEqual(gh.writes, [])


class MainTests(unittest.TestCase):
    def run_main(self, env=ENV, update=None, **patches):
        if update is not None:
            patches["update"] = mock.Mock(side_effect=update)
        with mock.patch("builtins.print") as out, _patched(**patches):
            self.assertEqual(problem_history.main(dict(env)), 0)
        return [c.args[0] for c in out.call_args_list]

    def test_never_raises_and_says_why(self):
        failed = "::warning::Updating the problem history failed"
        no_token = {k: v for k, v in ENV.items() if k != "GITHUB_TOKEN"}
        for env, error, want in (
            (ENV, _http_error(403), failed),
            (ENV, RuntimeError("the service answered 500: failed"), failed),
            (ENV, KeyError("BRANCH"), failed),
            (ENV, ValueError("bad json"), failed),
            (no_token, None, failed),
            (ENV, ph.Skip("the service answered 402"), "::notice::Problem history skipped: the service answered 402"),
        ):
            with self.subTest(error=error):
                lines = self.run_main(env, error)
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith(want))
        self.assertIn("issues: read", self.run_main(update=_http_error(403))[0])

    def test_the_script_stops_itself_at_the_step_limit(self):
        gate = threading.Event()

        def stuck(*_args):
            gate.wait(5)
            return "late"

        exit_ = mock.Mock(side_effect=lambda code: gate.set())
        with mock.patch.object(problem_history.os, "_exit", exit_):
            lines = self.run_main(update=stuck, STEP_LIMIT_SECONDS=0.05)
        exit_.assert_called_once_with(0)
        self.assertEqual(lines[0], "::warning::Updating the problem history stopped after 0 minutes")

    def test_empty_url_does_nothing(self):
        github = mock.Mock()
        self.assertEqual(self.run_main({**ENV, "CLOSED_FUNCTIONS_URL": " "}, GitHub=github), [])
        github.assert_not_called()


class WiringTests(unittest.TestCase):
    ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")

    def step(self):
        start = self.ACTION.index("    - name: Update problem history\n")
        return self.ACTION[start : self.ACTION.index("\n\n", start)]

    def test_opt_in_sync_only_and_never_fails_the_run(self):
        block = self.ACTION[self.ACTION.index("  problem_history:\n") :]
        self.assertIn("default: 'false'", block[: block.index("\n  github_token:")])
        for wired in (
            "steps.guard.outputs.mode == 'sync'",
            "inputs.problem_history == 'true'",
            "inputs.review_guide_url != ''",
            "continue-on-error: true",
            'scripts/problem_history.py"',
        ):
            self.assertIn(wired, self.step())
        self.assertLess(
            self.ACTION.index("- name: Publish baseline analysis for the analyzed commit"),
            self.ACTION.index("- name: Update problem history"),
        )

    def test_the_step_uses_only_keys_a_composite_run_step_accepts(self):
        # The runner refuses an action.yml whose composite step has any other key, timeout-minutes included,
        # which would break every workflow using the action.
        allowed = {"name", "id", "if", "run", "env", "continue-on-error", "working-directory", "shell"}
        keys = set(re.findall(r"^ {4}-? ?\s*([a-z-]+):", self.step(), re.M))
        self.assertLessEqual(keys, allowed)
        self.assertIn("continue-on-error", keys)

    def test_shares_the_closed_features_transport(self):
        for shared in (problem_history._bearer, problem_history._post):
            self.assertEqual(shared.__module__, "closed_features")


if __name__ == "__main__":
    unittest.main()
