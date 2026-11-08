#!/usr/bin/env python3
"""Keep this repository's problem history on codeboarding/briefing, when the workflow opts in.

Collects facts only: the issues and merged pull requests since the last update, and each merged pull
request's architecture findings from its review artifact. POST /v1/briefing folds them into the current
ledger and returns the new one, which is committed as problems.json on an orphan branch that is never
merged. Every rule about what counts as a problem, how urgent it is and whether it is fixed lives in the
service. Nothing here fails the run.

Environment: CLOSED_FUNCTIONS_URL, REPOSITORY, BRANCH, PUSH_SHA, PUSH_BEFORE, GITHUB_TOKEN, GITHUB_API_URL,
GITHUB_GRAPHQL_URL, and optionally LICENSE_KEY, ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from closed_features import Skip, _bearer, _check_size, _error_message, _github, _gzip, _post  # noqa: E402

LEDGER_BRANCH = "codeboarding/briefing"
LEDGER_PATH = "problems.json"
ISSUE_OVERLAP = timedelta(minutes=5)
# A pull request merged moments before the last one folded in may have been missed by that run.
MERGED_OVERLAP = timedelta(hours=1)
FIRST_CLOSED_DAYS = 90
FIRST_MERGED_DAYS = 56
# The service refuses more than 3000 issues, 1000 merged pull requests or 200 findings in one request.
# These stay under that by construction: 1000 + 1000 issues on a first run, 1000 + 100 later.
MAX_FIRST_OPEN = 1000
MAX_FIRST_CLOSED = 1000
MAX_ISSUES = 1000
MAX_RELINKED = 100
MAX_MERGED = 500
# Pages of 50 merged pull requests scanned, newest update first, before the scan stops short of the window.
MAX_MERGED_PAGES = 20
MAX_PATHS = 300
MAX_ARTIFACTS = 40
MAX_FINDINGS_RETRIES = 10
# Past this many open issues the list is sent as incomplete, and the service drops none.
MAX_OPEN_NUMBERS = 5000
# Successful runs on a head commit whose artifacts are listed before giving up on its review.
MAX_RUNS_PER_PR = 6
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_BODY_CHARS = 2000
SERVICE_TIMEOUT = 300
# A composite action's step cannot set timeout-minutes, so the script stops itself at the step's budget.
STEP_LIMIT_SECONDS = 600

# The webview's Briefing query (src/lib/github.ts, BRIEFING_ISSUES_QUERY), plus a linked pull
# request's title and url, read from the repository's own issue list rather than search so an issue
# updated a moment ago is never missed.
_BRIEF_PR = "fragment BriefPr on PullRequest{ number title url state isDraft reviewDecision updatedAt author{ login } repository{ nameWithOwner } }"
_ISSUE_FIELDS = """
fragment IssueFields on Issue{ number title body url state createdAt closedAt updatedAt authorAssociation
  author{ __typename login } repository{ nameWithOwner } labels(first:10){ nodes{ name } } assignees(first:5){ nodes{ login } }
  timelineItems(last:30, itemTypes:[CLOSED_EVENT, CROSS_REFERENCED_EVENT, CONNECTED_EVENT, DISCONNECTED_EVENT]){ nodes{ __typename
    ... on ClosedEvent{ closer{ __typename ... on PullRequest{ number author{ login } repository{ nameWithOwner } } } }
    ... on CrossReferencedEvent{ willCloseTarget source{ __typename ...BriefPr ... on PullRequest{ commits(last:1){ nodes{ commit{ statusCheckRollup{ state } } } } } } }
    ... on ConnectedEvent{ subject{ __typename ...BriefPr } source{ __typename ...BriefPr } }
    ... on DisconnectedEvent{ subject{ __typename ...BriefPr } source{ __typename ...BriefPr } } } } }"""
ISSUES_QUERY = (
    _BRIEF_PR
    + _ISSUE_FIELDS
    + """
query($owner:String!,$name:String!,$states:[IssueState!],$since:DateTime,$dir:OrderDirection!,$c:String){
  repository(owner:$owner, name:$name){
    issues(first:50, after:$c, states:$states, filterBy:{since:$since}, orderBy:{field:UPDATED_AT, direction:$dir}){
      pageInfo{ hasNextPage endCursor } nodes{ ...IssueFields } } } }"""
)
# Creation order, so an issue updated while the pages are read cannot move past the cursor and be missed.
OPEN_NUMBERS_QUERY = """
query($owner:String!,$name:String!,$c:String){
  repository(owner:$owner, name:$name){
    issues(first:100, after:$c, states:[OPEN], orderBy:{field:CREATED_AT, direction:ASC}){
      pageInfo{ hasNextPage endCursor } nodes{ number } } } }"""
OLDEST_OPEN_QUERY = """
query($owner:String!,$name:String!){
  repository(owner:$owner, name:$name){
    issues(first:1, states:[OPEN], orderBy:{field:UPDATED_AT, direction:ASC}){ nodes{ updatedAt } } } }"""
# Issues read again by number, this many to a request.
ISSUES_PER_QUERY = 25
MERGED_QUERY = """
query($owner:String!,$name:String!,$base:String!,$c:String){
  repository(owner:$owner, name:$name){
    pullRequests(first:50, after:$c, states:[MERGED], baseRefName:$base, orderBy:{field:UPDATED_AT, direction:DESC}){
      pageInfo{ hasNextPage endCursor }
      nodes{ number title url mergedAt updatedAt headRefOid author{ login }
        reviews(first:50, states:[APPROVED]){ nodes{ author{ login } } }
        files(first:100){ pageInfo{ hasNextPage endCursor } nodes{ path } } } } } }"""
FILES_QUERY = """
query($owner:String!,$name:String!,$n:Int!,$c:String){
  repository(owner:$owner, name:$name){ pullRequest(number:$n){
    files(first:100, after:$c){ pageInfo{ hasNextPage endCursor } nodes{ path } } } } }"""


class Conflict(Exception):
    """Another run moved the ledger branch between our read and our write."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class GitHub:
    def __init__(self, env: dict):
        self.api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.graphql_url = env.get("GITHUB_GRAPHQL_URL") or f"{self.api}/graphql"
        self.token = env["GITHUB_TOKEN"]

    def _headers(self, accept: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": accept, "User-Agent": "codeboarding-action"}

    def call(self, method: str, path: str, payload=None, accept: str = "application/vnd.github+json") -> bytes:
        return _github(path, accept, method=method, payload=payload, api=self.api, token=self.token, timeout=60)

    def json(self, path: str, method: str = "GET", payload=None):
        return json.loads(self.call(method, path, payload) or b"null")

    def graphql(self, query: str, variables: dict) -> dict:
        out = json.loads(self.call("POST", self.graphql_url, {"query": query, "variables": variables}))
        data = out.get("data")
        # A node the token cannot read (a pull request in another private repository) comes back null
        # beside an error; only a missing answer is a failure.
        if not data or any(v is None for v in data.values()):
            errors = "; ".join(e.get("message", "") for e in out.get("errors") or []) or "no data"
            raise RuntimeError(f"GitHub GraphQL: {errors}")
        return data

    def download(self, url: str) -> bytes:
        """An artifact archive. The redirect goes to storage that must not receive the token."""
        try:
            with build_opener(_NoRedirect).open(Request(url, headers=self._headers("*/*")), timeout=60) as res:
                return res.read()
        except HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308) or not e.headers.get("Location"):
                raise
            location = e.headers["Location"]
            e.close()
        storage = Request(location, headers={"User-Agent": "codeboarding-action"})
        with urlopen(storage, timeout=300) as res:  # nosec B310 - the artifact URL GitHub redirected to
            return res.read()


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# Yielded last when `pages` ran out with more to read.
MORE = object()


def _nodes(gh: GitHub, query: str, variables: dict, *path: str, pages: int | None = None, conn: dict | None = None):
    """The nodes of the connection at repository.<path>, page by page, from `conn` when the first page
    came with another query."""
    cursor, read = None, 0
    while True:
        if conn is None:
            conn = gh.graphql(query, {**variables, "c": cursor})["repository"]
            for key in path:
                conn = conn[key]
        read += 1
        yield from conn.get("nodes") or []
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return
        if pages and read >= pages:
            yield MORE
            return
        cursor, conn = page.get("endCursor"), None


def _login(actor) -> str | None:
    return (actor or {}).get("login") or None


def _author(actor) -> dict:
    """{"author": login}, or nothing when there is no login, ready to spread into a fact."""
    login = _login(actor)
    return {"author": login} if login else {}


# Where each timeline event names the pull request; a link event's subject or source may be the issue.
_LINK_SIDES = {
    "CrossReferencedEvent": ("source",),
    "ConnectedEvent": ("subject", "source"),
    "DisconnectedEvent": ("subject", "source"),
}


def linked_pull_requests(node: dict) -> list[dict]:
    """webview src/lib/briefing/linked.ts `linkedPullRequests`: the pull requests an issue's timeline
    links to, and whether each closes it (a closing keyword, or a Development link not since undone)."""
    prs: dict[str, tuple[dict, str | None]] = {}
    keyword, connected = set(), set()
    for t in (node.get("timelineItems") or {}).get("nodes") or []:
        kind = (t or {}).get("__typename")
        sides = _LINK_SIDES.get(kind, ())
        pr = next((t[s] for s in sides if (t.get(s) or {}).get("__typename") == "PullRequest"), None)
        if not pr or not all(pr.get(k) for k in ("repository", "number", "state")):
            continue
        key = f"{pr['repository']['nameWithOwner']}#{pr['number']}".lower()
        commits = (pr.get("commits") or {}).get("nodes") or []
        rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") if commits else None
        checks = (rollup or {}).get("state") or (prs[key][1] if key in prs else None)
        prs[key] = (pr, checks)
        if kind == "CrossReferencedEvent" and t.get("willCloseTarget"):
            keyword.add(key)
        if kind == "ConnectedEvent":
            connected.add(key)
        if kind == "DisconnectedEvent":
            connected.discard(key)
    return [
        {
            "repo": s["repository"]["nameWithOwner"],
            "number": s["number"],
            **{k: s[k] for k in ("title", "url") if s.get(k) is not None},
            "state": s["state"],
            "draft": bool(s.get("isDraft")),
            "updatedAt": s.get("updatedAt") or node.get("updatedAt"),
            "review": {"APPROVED": "approved", "CHANGES_REQUESTED": "changes"}.get(s.get("reviewDecision"), "review"),
            "checks": checks,
            "closes": key in keyword or key in connected,
            **_author(s.get("author")),
        }
        for key, (s, checks) in prs.items()
    ]


def cut(text: str, limit: int = MAX_BODY_CHARS) -> str:
    """The first `limit` characters of `text` as JavaScript counts them (UTF-16 code units), so also at
    most `limit` Python characters, never ending on half a surrogate pair."""
    return text.encode("utf-16-le", "surrogatepass")[: 2 * limit].decode("utf-16-le", "ignore")


def issue_facts(node: dict) -> dict:
    """An issue node as IssueFacts: what the webview's `fetchBriefingIssues` builds from the same query."""
    timeline = (node.get("timelineItems") or {}).get("nodes") or []
    closer = next(
        (
            t["closer"]
            for t in timeline
            if (t or {}).get("__typename") == "ClosedEvent"
            and (t.get("closer") or {}).get("__typename") == "PullRequest"
        ),
        None,
    )
    author = node.get("author") or {}
    closed_by = None
    if closer and closer.get("repository") and closer.get("number"):
        closed_by = {
            "repo": closer["repository"]["nameWithOwner"],
            "number": closer["number"],
            **_author(closer.get("author")),
        }
    return {
        "repo": node["repository"]["nameWithOwner"],
        "number": node["number"],
        "title": node.get("title") or "",
        # The service reads a body for its first sentence and two marker lines, all near the top.
        "body": cut(node.get("body") or ""),
        "url": node.get("url"),
        "state": node.get("state"),
        "createdAt": node.get("createdAt"),
        "closedAt": node.get("closedAt"),
        "updatedAt": node.get("updatedAt"),
        **_author(author),
        "authorBot": author.get("__typename") == "Bot",
        "association": node.get("authorAssociation") or "NONE",
        "labels": [label["name"] for label in (node.get("labels") or {}).get("nodes") or [] if label],
        "assignees": [a["login"] for a in (node.get("assignees") or {}).get("nodes") or [] if a],
        "closedBy": closed_by,
        "linked": linked_pull_requests(node),
    }


def read_ledger(gh: GitHub, repo: str) -> tuple[str | None, dict | None]:
    """The ledger branch's head and its problems.json; (None, None) before the first update."""
    head = None
    try:
        head = gh.json(f"/repos/{repo}/git/ref/heads/{LEDGER_BRANCH}")["object"]["sha"]
        raw = gh.call("GET", f"/repos/{repo}/contents/{LEDGER_PATH}?ref={head}", accept="application/vnd.github.raw")
    except HTTPError as e:
        if e.code == 404:
            return head, None
        raise
    ledger = json.loads(raw)
    if not isinstance(ledger, dict):
        raise Skip(f"{LEDGER_PATH} on {LEDGER_BRANCH} is not a ledger")
    return head, ledger


def open_issue_numbers(gh: GitHub, repo: str) -> dict:
    """`openIssues`: every issue open now, by number, or an incomplete list past MAX_OPEN_NUMBERS."""
    owner, name = repo.split("/", 1)
    numbers = []
    for n in _nodes(gh, OPEN_NUMBERS_QUERY, {"owner": owner, "name": name}, "issues", pages=MAX_OPEN_NUMBERS // 100):
        if n is MORE:
            return {"numbers": [], "complete": False}
        if n and isinstance(n.get("number"), int):
            numbers.append(n["number"])
    return {"numbers": numbers, "complete": True}


def _oldest_open(gh: GitHub, repo: str) -> datetime | None:
    owner, name = repo.split("/", 1)
    nodes = gh.graphql(OLDEST_OPEN_QUERY, {"owner": owner, "name": name})["repository"]["issues"].get("nodes")
    return _parse(((nodes or [None])[0] or {}).get("updatedAt"))


def collect_issues(gh: GitHub, repo: str, ledger: dict | None, started: datetime) -> tuple[list[dict], str, list[str]]:
    """Issues for the request, the `issuesThrough` to record, and a note per cap that cut a pass short.

    `issuesThrough` never claims an issue a cap skipped, so the next run reads it. A later run reads oldest
    update first and stops at the last issue read. A first run reads newest first: a cut open pass stops at
    the oldest open issue's update, and a cut closed pass at the start of its 90 days."""
    owner, name = repo.split("/", 1)
    since = _parse((ledger or {}).get("issuesThrough"))
    cutoff = None
    if since is None:
        cutoff = started - timedelta(days=FIRST_CLOSED_DAYS)
        passes = [
            ("open", ["OPEN"], None, "DESC", MAX_FIRST_OPEN),
            ("recently closed", ["CLOSED"], _iso(cutoff), "DESC", MAX_FIRST_CLOSED),
        ]
    else:
        passes = [("", None, _iso(since - ISSUE_OVERLAP), "ASC", MAX_ISSUES)]
    out: dict[int, dict] = {}
    through, notes = started, []
    for label, states, after, direction, cap in passes:
        variables = {"owner": owner, "name": name, "states": states, "since": after, "dir": direction}
        read, last = 0, None
        for n in _nodes(gh, ISSUES_QUERY, variables, "issues"):
            if not n or "number" not in n:
                continue
            if cutoff and n.get("state") == "CLOSED" and (_parse(n.get("closedAt")) or cutoff) <= cutoff:
                continue
            if read >= cap:
                notes.append(f"read the first {cap} {label + ' ' if label else ''}issues only")
                if since is not None:
                    stop = last
                elif states == ["OPEN"]:
                    stop = _oldest_open(gh, repo)
                else:
                    stop = cutoff
                through = min(through, stop or started)
                break
            read += 1
            out[n["number"]] = issue_facts(n)
            last = _parse(n.get("updatedAt")) or last
    return list(out.values()), _iso(through), notes


def relinked(ledger: dict | None, repo: str, read: set[int]) -> list[int]:
    """Stored open issues with an open linked pull request, which can move without updating the issue:
    those whose pull requests moved most recently first, capped."""
    picked = []
    for i in (ledger or {}).get("issues") or []:
        if not isinstance(i, dict) or i.get("state") != "OPEN" or str(i.get("repo", "")).lower() != repo.lower():
            continue
        number = i.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number in read:
            continue
        moved = [str(p.get("updatedAt") or "") for p in i.get("linked") or [] if (p or {}).get("state") == "OPEN"]
        if moved:
            picked.append((max(moved), number))
    return [n for _, n in sorted(picked, reverse=True)[:MAX_RELINKED]]


def issues_by_number(gh: GitHub, repo: str, numbers: list[int]) -> list[dict]:
    """IssueFacts for these issue numbers; one deleted, transferred or never an issue is left out."""
    owner, name = repo.split("/", 1)
    out = []
    for start in range(0, len(numbers), ISSUES_PER_QUERY):
        batch = numbers[start : start + ISSUES_PER_QUERY]
        params = "".join(f",$n{k}:Int!" for k in range(len(batch)))
        fields = " ".join(f"i{k}: issue(number:$n{k}){{ ...IssueFields }}" for k in range(len(batch)))
        query = (
            _BRIEF_PR
            + _ISSUE_FIELDS
            + f"\nquery($owner:String!,$name:String!{params}){{ repository(owner:$owner, name:$name){{ {fields} }} }}"
        )
        variables = {"owner": owner, "name": name, **{f"n{k}": n for k, n in enumerate(batch)}}
        found = gh.graphql(query, variables)["repository"]
        for k in range(len(batch)):
            node = found.get(f"i{k}") or {}
            if (
                node.get("number")
                and str((node.get("repository") or {}).get("nameWithOwner", "")).lower() == repo.lower()
            ):
                out.append(issue_facts(node))
    return out


def _merged_since(ledger: dict | None, now: datetime) -> datetime:
    if ledger:
        last = _parse((ledger.get("through") or {}).get("mergedAt")) or _parse(ledger.get("updatedAt"))
        if last:
            return last - MERGED_OVERLAP
    return now - timedelta(days=FIRST_MERGED_DAYS)


def collect_merged(
    gh: GitHub, repo: str, branch: str, ledger: dict | None, now: datetime
) -> tuple[list[dict], dict[int, str], bool]:
    """Pull requests merged into `branch` since the ledger's last one, each one's head commit, and whether
    the scan stopped at its page cap before reaching the window's start."""
    owner, name = repo.split("/", 1)
    since = _merged_since(ledger, now)
    out, heads = [], {}
    variables = {"owner": owner, "name": name, "base": branch}
    for n in _nodes(gh, MERGED_QUERY, variables, "pullRequests", pages=MAX_MERGED_PAGES):
        if n is MORE:
            return out, heads, True
        if not n:
            continue
        # Merging updates a pull request, so past this point everything merged earlier.
        if (_parse(n.get("updatedAt")) or since) < since:
            break
        merged_at = _parse(n.get("mergedAt"))
        if not merged_at or merged_at < since:
            continue
        if len(out) >= MAX_MERGED:
            break
        reviews = (n.get("reviews") or {}).get("nodes") or []
        pr = {"owner": owner, "name": name, "n": n["number"]}
        files = _nodes(gh, FILES_QUERY, pr, "pullRequest", "files", conn=n.get("files") or {})
        out.append(
            {
                "number": n["number"],
                "title": n.get("title") or "",
                "url": n.get("url"),
                **_author(n.get("author")),
                "mergedAt": n["mergedAt"],
                "approvers": list(dict.fromkeys(filter(None, (_login((r or {}).get("author")) for r in reviews)))),
                # islice asks for no page past the cap.
                "paths": [f["path"] for f in islice(filter(None, files), MAX_PATHS)],
            }
        )
        heads[n["number"]] = n.get("headRefOid")
    return out, heads, False


def read_architecture(archive: bytes, pr: int) -> dict | None:
    """closed/architecture.json from a review artifact, when it answers for this pull request. A skipped
    one carries no findings, and still tells the service the pull request was read."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        names = set(z.namelist())
        if "metadata.json" in names:
            meta = json.loads(z.read("metadata.json"))
            if str(meta.get("pr_number", pr)) != str(pr):
                return None
        if "closed/architecture.json" not in names:
            return None
        envelope = json.loads(z.read("closed/architecture.json"))
    if not isinstance(envelope, dict) or envelope.get("status") not in ("ok", "partial", "skipped"):
        return None
    return envelope


def review_findings(gh: GitHub, repo: str, pr: int, sha: str) -> dict | None:
    """The architecture envelope from the newest successful CodeBoarding review of this head commit."""
    runs = gh.json(f"/repos/{repo}/actions/runs?head_sha={quote(sha)}&status=success&per_page=30")
    runs = sorted(
        runs.get("workflow_runs") or [], key=lambda r: not re.search("codeboarding", r.get("name") or "", re.I)
    )
    too_big = False
    for run in runs[:MAX_RUNS_PER_PR]:
        want = f"codeboarding-review-{run['id']}-{run.get('run_attempt') or 1}"
        listed = gh.json(f"/repos/{repo}/actions/runs/{run['id']}/artifacts?name={quote(want)}")
        for artifact in listed.get("artifacts") or []:
            if artifact.get("name") != want or artifact.get("expired"):
                continue
            if (artifact.get("size_in_bytes") or 0) > MAX_ARTIFACT_BYTES:
                too_big = True
                continue
            envelope = read_architecture(gh.download(artifact["archive_download_url"]), pr)
            if envelope:
                return envelope
    if too_big:
        raise ValueError(f"its review artifact is over {MAX_ARTIFACT_BYTES >> 20} MB, not downloaded")
    return None


def _unread(ledger: dict | None) -> list[dict]:
    """The ledger's merged pull requests whose findings were never read. A ledger from before
    `warningsRead` has no flag; a merge with warnings stored counts as read."""
    warned = {w.get("pr") for w in (ledger or {}).get("warnings") or [] if isinstance(w, dict)}
    return [
        m
        for m in (ledger or {}).get("merged") or []
        if isinstance(m, dict)
        and isinstance(m.get("number"), int)
        and m.get("warningsRead") is not True
        and m["number"] not in warned
    ]


def collect_findings(
    gh: GitHub, repo: str, merged: list[dict], heads: dict[int, str], ledger: dict | None
) -> tuple[dict[str, dict], list[str]]:
    """Findings per merged pull request the ledger has not folded in yet, and again for a few it holds
    unread, newest first, each capped."""
    known = {m.get("number") for m in (ledger or {}).get("merged") or [] if isinstance(m, dict)}

    def newest(items):
        return sorted(items, key=lambda m: str(m.get("mergedAt") or ""), reverse=True)

    todo = newest(m for m in merged if m["number"] not in known)[:MAX_ARTIFACTS]
    todo += newest(_unread(ledger))[:MAX_FINDINGS_RETRIES]
    out, errors = {}, []
    for m in todo:
        try:
            # A merge read from the ledger has no head commit stored; its pull request names it.
            head = heads.get(m["number"]) or gh.json(f"/repos/{repo}/pulls/{m['number']}")["head"]["sha"]
            if not head:
                continue
            envelope = review_findings(gh, repo, m["number"], head)
        except Exception as e:  # a missing or unreadable artifact only means no findings for it
            errors.append(f"#{m['number']}: {type(e).__name__}: {e}")
            continue
        if envelope:
            out[str(m["number"])] = envelope
    return out, errors


def collect(gh: GitHub, env: dict, ledger: dict | None, now: datetime) -> dict:
    repo = env["REPOSITORY"]
    notes = []
    # Read before the issues, so an issue closed in between is still listed rather than dropped.
    try:
        open_issues = open_issue_numbers(gh, repo)
        if not open_issues["complete"]:
            notes.append(f"over {MAX_OPEN_NUMBERS} open issues, so none was checked for removal")
    except Exception as e:  # without the list the service drops nothing, which is only slower
        open_issues = {"numbers": [], "complete": False}
        notes.append(f"open issues not listed ({type(e).__name__}: {e})")
    issues, through, capped = collect_issues(gh, repo, ledger, now)
    notes += capped
    if capped:
        # An issue the cap skipped may have closed since the ledger stored it open; it must not be dropped.
        open_issues = {"numbers": [], "complete": False}
    if ledger:
        again = relinked(ledger, repo, {i["number"] for i in issues})
        try:
            issues += issues_by_number(gh, repo, again)
        except Exception as e:  # their pull requests are read again next time
            notes.append(f"{len(again)} issues with open pull requests not read again ({type(e).__name__}: {e})")
    merged, heads, scan_cut = collect_merged(gh, repo, env["BRANCH"], ledger, now)
    if scan_cut:
        notes.append(f"scanned the {MAX_MERGED_PAGES * 50} latest updated pull requests only")
    findings, errors = collect_findings(gh, repo, merged, heads, ledger)
    if errors:
        notes.append(f"no findings for {len(errors)} merged pull requests ({errors[0]})")
    return {
        "now": _iso(now),
        "issues": issues,
        "issuesThrough": through,
        "openIssues": open_issues,
        "merged": merged,
        "findings": findings,
        "notes": notes,
    }


def build_request(env: dict, ledger: dict | None, facts: dict) -> dict:
    return {
        "version": 1,
        "repo": env["REPOSITORY"],
        "now": facts["now"],
        "branch": env["BRANCH"],
        "push": {"sha": env.get("PUSH_SHA", ""), "before": env.get("PUSH_BEFORE", "")},
        "ledger": ledger,
        **{k: facts[k] for k in ("issues", "issuesThrough", "openIssues", "merged", "findings")},
    }


def pack(body: dict) -> bytes:
    data = _gzip(body)
    _check_size(data, "request")
    return data


def _newest_pr(*lists) -> int | None:
    merged = [m for items in lists for m in items or [] if isinstance(m, dict) and m.get("mergedAt")]
    return max(merged, key=lambda m: m["mergedAt"])["number"] if merged else None


def commit_ledger(gh: GitHub, repo: str, head: str | None, ledger: dict, message: str) -> str:
    """One commit holding only problems.json on top of `head`; Conflict when the branch moved."""
    text = json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
    blob = gh.json(f"/repos/{repo}/git/blobs", "POST", {"content": text, "encoding": "utf-8"})["sha"]
    entry = {"path": LEDGER_PATH, "mode": "100644", "type": "blob", "sha": blob}
    tree = gh.json(f"/repos/{repo}/git/trees", "POST", {"tree": [entry]})["sha"]
    payload = {"message": message, "tree": tree, "parents": [head] if head else []}
    commit = gh.json(f"/repos/{repo}/git/commits", "POST", payload)["sha"]
    try:
        if head:
            gh.json(f"/repos/{repo}/git/refs/heads/{LEDGER_BRANCH}", "PATCH", {"sha": commit, "force": False})
        else:
            gh.json(f"/repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{LEDGER_BRANCH}", "sha": commit})
    except HTTPError as e:
        if e.code != 422:
            raise
        message = " ".join(_error_message(e).split())
        # Only a lost race is worth a retry: not a fast-forward, or the branch appeared since it was read.
        if not message or re.search(r"fast.forward|already exists", message, re.I):
            raise Conflict() from e
        raise Skip(
            f"GitHub refused to write {LEDGER_BRANCH}: {message}. A ruleset that covers it, or a branch named"
            f" {LEDGER_BRANCH.split('/')[0]}, stops the update; exempt the branch or rename that one"
        ) from e
    return commit


def update(gh: GitHub, env: dict, now: datetime | None = None) -> str:
    """Collect, ask the service, commit; the notice to print."""
    repo, base = env["REPOSITORY"], env["CLOSED_FUNCTIONS_URL"].rstrip("/")
    now = now or datetime.now(timezone.utc)
    head, ledger = read_ledger(gh, repo)
    facts = collect(gh, env, ledger, now)
    for retried in (False, True):
        data = pack(build_request(env, ledger, facts))
        answer = _post(f"{base}/v1/briefing", data, _bearer(env), timeout=SERVICE_TIMEOUT)
        new = answer.get("ledger") if isinstance(answer, dict) else None
        if not isinstance(new, dict) or new.get("schema") != 1 or str(new.get("repo", "")).lower() != repo.lower():
            raise ValueError("the service returned no ledger for this repository")
        summary = answer.get("summary") or {}
        count = summary.get("problems", len(new.get("problems") or []))
        pr = _newest_pr(new.get("merged"), facts["merged"])
        through = f"#{pr}" if pr else env.get("PUSH_SHA", "")[:7]
        try:
            sha = commit_ledger(gh, repo, head, new, f"Update problem history ({count} problems, through {through})")
        except Conflict:
            if retried:
                raise Skip(f"{LEDGER_BRANCH} moved twice while this run updated it; the next sync carries on")
            # Another run updated it first: fold our facts into its ledger instead. They are a superset
            # of what that ledger lacks, and the service merges what it already holds. Its issues may
            # postdate our list of open ones, so this request lets the service drop none.
            head, ledger = read_ledger(gh, repo)
            facts = {**facts, "openIssues": {"numbers": [], "complete": False}}
            continue
        line = (
            f"Problem history: {count} problems ({summary.get('open', '?')} open, {summary.get('added', '?')} new),"
            f" through {through}; committed {sha[:7]} to {LEDGER_BRANCH}"
        )
        return "; ".join([line, *facts["notes"]])


def _stop() -> None:
    print(f"::warning::Updating the problem history stopped after {int(STEP_LIMIT_SECONDS // 60)} minutes", flush=True)
    os._exit(0)


def main(env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    timer = threading.Timer(STEP_LIMIT_SECONDS, _stop)
    timer.daemon = True
    timer.start()
    try:
        if not env.get("CLOSED_FUNCTIONS_URL", "").strip():
            return 0
        print(f"::notice::{update(GitHub(env), env)}")
    except Skip as e:
        print(f"::notice::Problem history skipped: {e}")
    except Exception as e:  # never fail the sync over the problem history
        hint = ""
        if isinstance(e, HTTPError) and e.code in (403, 404):
            hint = " (the sync job needs contents: write, issues: read, pull-requests: read and actions: read)"
        print(f"::warning::Updating the problem history failed: {type(e).__name__}: {e}{hint}")
    finally:
        timer.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
