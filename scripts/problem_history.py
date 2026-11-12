#!/usr/bin/env python3
"""Keep this repository's problem history on codeboarding/briefing, when the workflow opts in.

Collects facts only: the issues and merged pull requests since the last update, and each merged pull
request's architecture findings from its review artifact. POST /v1/briefing folds them into the current
ledger and returns the new one, which is committed as problems.json on an orphan branch that is never
merged. Every rule about what counts as a problem, how urgent it is and whether it is fixed lives in the
service. Nothing here fails the run.

The service is sent the run's OIDC token, never GITHUB_TOKEN, which can write to this repository: a job
without `id-token: write` skips the update.

Environment: CLOSED_FUNCTIONS_URL, REPOSITORY, BRANCH, PUSH_SHA, PUSH_BEFORE, GITHUB_TOKEN, GITHUB_API_URL,
GITHUB_GRAPHQL_URL, ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN, and optionally LICENSE_KEY.
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
from remote_features import (
    MAX_BRIEFING_JSON_BYTES,
    Skip,
    TooLarge,
    _bearer,
    _encode,
    _error_message,
    _github,
    _post,
)  # noqa: E402

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
# metadata.json and closed/architecture.json are read from an artifact only up to this size each.
MAX_MEMBER_BYTES = 1024 * 1024
# The review artifact's retention-days in action.yml; an unread merge older than this has nothing left to find.
REVIEW_RETENTION_DAYS = 14
MAX_BODY_CHARS = 2000
# Compressions of one request at most: the first, and retries aimed by the ratio the one before measured.
MAX_COMPRESSIONS = 3
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
# The ledger branch's head and how many updates it holds, which says whose turn it is to be read again.
LEDGER_REF_QUERY = """
query($owner:String!,$name:String!,$ref:String!){
  repository(owner:$owner, name:$name){ ref(qualifiedName:$ref){ target{ oid ... on Commit{ history(first:1){ totalCount } } } } } }"""
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


PERMISSIONS = "contents: write, issues: read, pull-requests: read, actions: read and id-token: write"
# GraphQL error types that mean the token was refused, rather than that a node does not exist.
DENIED = {"FORBIDDEN", "INSUFFICIENT_SCOPES"}


class GraphQLError(RuntimeError):
    """A GraphQL answer without what was asked; `types` holds its errors' types."""

    def __init__(self, errors: list):
        errors = [e for e in errors if isinstance(e, dict)]
        self.types = {e.get("type") for e in errors}
        super().__init__(f"GitHub GraphQL: {'; '.join(e.get('message', '') for e in errors) or 'no data'}")


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
        data, errors = out.get("data"), [e for e in out.get("errors") or [] if isinstance(e, dict)]
        # A node the token cannot read (a pull request in another private repository) comes back null
        # beside an error, and the rest stands. A missing answer, or the repository or one of its lists
        # refused, is a failure.
        refused = [e for e in errors if e.get("type") in DENIED and len(e.get("path") or []) <= 2]
        if not data or any(v is None for v in data.values()) or refused:
            raise GraphQLError(refused or errors)
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
                conn = (conn or {}).get(key)
            if conn is None:
                raise GraphQLError([{"message": f"no repository.{'.'.join(path)}"}])
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


def read_ledger(gh: GitHub, repo: str) -> tuple[str | None, dict | None, int]:
    """The ledger branch's head, its problems.json and how many commits it holds; (None, None, 0) before
    the first update."""
    owner, name = repo.split("/", 1)
    variables = {"owner": owner, "name": name, "ref": f"refs/heads/{LEDGER_BRANCH}"}
    target = (gh.graphql(LEDGER_REF_QUERY, variables)["repository"].get("ref") or {}).get("target") or {}
    head = target.get("oid")
    if not head:
        return None, None, 0
    updates = (target.get("history") or {}).get("totalCount") or 0
    try:
        raw = gh.call("GET", f"/repos/{repo}/contents/{LEDGER_PATH}?ref={head}", accept="application/vnd.github.raw")
    except HTTPError as e:
        if e.code == 404:
            return head, None, updates
        raise
    ledger = json.loads(raw)
    if not isinstance(ledger, dict):
        raise Skip(f"{LEDGER_PATH} on {LEDGER_BRANCH} is not a ledger")
    return head, ledger, updates


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

    `issuesThrough` never claims an issue a cap skipped, so the next run reads it, except ties a bulk edit
    left past the cap. A later run reads oldest update first and stops at the last issue read, or past the
    overlap when that would read the same page again. A first run reads newest first: a cut open pass stops
    at the oldest open issue's update, and a cut closed pass at the start of its 90 days."""
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
                    # A bulk edit can give more issues than the cap one update time, and stopping at it would
                    # start every later run on the same page. Moving past the overlap loses ties past the cap.
                    stop = max(last or since, since + ISSUE_OVERLAP + timedelta(seconds=1))
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


def relinked(ledger: dict | None, repo: str, read: set[int], turn: int) -> list[int]:
    """Stored open issues with an open linked pull request, which can move without updating the issue.

    Past MAX_RELINKED they take turns in number order, and update number `turn` starts where the one
    before it stopped, so the least recently read come first and each is read again at least every
    ceil(n / MAX_RELINKED) updates. Issues read this run already are passed over."""
    candidates = set()
    for i in (ledger or {}).get("issues") or []:
        if not isinstance(i, dict) or i.get("state") != "OPEN" or str(i.get("repo", "")).lower() != repo.lower():
            continue
        number = i.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if any((p or {}).get("state") == "OPEN" for p in i.get("linked") or []):
            candidates.add(number)
    if not candidates:
        return []
    ring = sorted(candidates)
    start = turn * MAX_RELINKED % len(ring)
    return [n for n in ring[start:] + ring[:start] if n not in read][:MAX_RELINKED]


def _by_number(gh: GitHub, repo: str, numbers: list[int], field: str, selection: str) -> list[dict]:
    """repository.<field>(number:) for each of `numbers` in one query, in order; {} for one not found."""
    owner, name = repo.split("/", 1)
    params = "".join(f",$n{k}:Int!" for k in range(len(numbers)))
    fields = " ".join(f"a{k}: {field}(number:$n{k}){{ {selection} }}" for k in range(len(numbers)))
    query = f"query($owner:String!,$name:String!{params}){{ repository(owner:$owner, name:$name){{ {fields} }} }}"
    if "...IssueFields" in selection:
        query = _BRIEF_PR + _ISSUE_FIELDS + "\n" + query
    variables = {"owner": owner, "name": name, **{f"n{k}": n for k, n in enumerate(numbers)}}
    found = gh.graphql(query, variables)["repository"]
    return [found.get(f"a{k}") or {} for k in range(len(numbers))]


def issues_by_number(gh: GitHub, repo: str, numbers: list[int]) -> list[dict]:
    """IssueFacts for these issue numbers; one deleted, transferred or never an issue is left out."""
    out = []
    for start in range(0, len(numbers), ISSUES_PER_QUERY):
        for node in _by_number(gh, repo, numbers[start : start + ISSUES_PER_QUERY], "issue", "...IssueFields"):
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
) -> tuple[list[dict], dict[int, str], list[str]]:
    """Pull requests merged into `branch` since the ledger's last one, each one's head commit, and a note
    per cap that cut the list short.

    The service records the newest merge it is sent as where the next run starts, so past MAX_MERGED this
    sends the oldest ones and the next run carries on from there."""
    owner, name = repo.split("/", 1)
    since = _merged_since(ledger, now)
    found, notes = [], []
    variables = {"owner": owner, "name": name, "base": branch}
    for n in _nodes(gh, MERGED_QUERY, variables, "pullRequests", pages=MAX_MERGED_PAGES):
        if n is MORE:
            notes.append(f"scanned the {MAX_MERGED_PAGES * 50} latest updated pull requests only")
            break
        if not n:
            continue
        # Merging updates a pull request, so past this point everything merged earlier.
        if (_parse(n.get("updatedAt")) or since) < since:
            break
        merged_at = _parse(n.get("mergedAt"))
        if merged_at and merged_at >= since:
            found.append((merged_at, n))
    if len(found) > MAX_MERGED:
        notes.append(f"read the {MAX_MERGED} oldest of {len(found)} merged pull requests; the next runs read the rest")
        oldest = {id(n) for _, n in sorted(found, key=lambda m: m[0])[:MAX_MERGED]}
        found = [m for m in found if id(m[1]) in oldest]
    out, heads = [], {}
    for _, n in found:
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
    return out, heads, notes


def _member(z: zipfile.ZipFile, name: str):
    """A JSON file in the archive, or None when it has none. A zip entry reads no further than the size
    it declares, so checking that size bounds what is read."""
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"its {name} is over {MAX_MEMBER_BYTES >> 20} MB, not read")
    return json.loads(z.read(info))


def read_architecture(archive: bytes, pr: int) -> dict | None:
    """closed/architecture.json from a review artifact, when its metadata.json says it answers for this
    pull request. A skipped one carries no findings, and still tells the service the pull request was read."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        meta = _member(z, "metadata.json")
        if not isinstance(meta, dict) or str(meta.get("pr_number")) != str(pr):
            return None
        envelope = _member(z, "closed/architecture.json")
    if not isinstance(envelope, dict) or envelope.get("status") not in ("ok", "partial", "skipped"):
        return None
    return envelope


def _own_review(run: dict, repo: str) -> bool:
    """A CodeBoarding run on this repository's own branch. A fork's pull request runs the fork's workflow,
    which can upload anything under the review artifact's name."""
    head_repo = str((run.get("head_repository") or {}).get("full_name") or "")
    return bool(re.search("codeboarding", run.get("name") or "", re.I)) and head_repo.lower() == repo.lower()


def review_findings(gh: GitHub, repo: str, pr: int, sha: str) -> dict | None:
    """The architecture envelope from the newest successful CodeBoarding review of this head commit."""
    listed = gh.json(f"/repos/{repo}/actions/runs?head_sha={quote(sha)}&event=pull_request&status=success&per_page=30")
    runs = [r for r in listed.get("workflow_runs") or [] if _own_review(r, repo)]
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
    gh: GitHub, repo: str, merged: list[dict], heads: dict[int, str], ledger: dict | None, now: datetime
) -> tuple[dict[str, dict], list[str]]:
    """Findings per merged pull request the ledger has not folded in yet, and again for a few it holds
    unread that merged inside the review artifact's retention, newest first, each capped. Nothing records
    a look that found nothing, so the retention is what ends the retries."""
    known = {m.get("number") for m in (ledger or {}).get("merged") or [] if isinstance(m, dict)}
    retained = now - timedelta(days=REVIEW_RETENTION_DAYS)

    def newest(items):
        return sorted(items, key=lambda m: str(m.get("mergedAt") or ""), reverse=True)

    todo = newest(m for m in merged if m["number"] not in known)[:MAX_ARTIFACTS]
    retries = newest(m for m in _unread(ledger) if (_parse(m.get("mergedAt")) or retained) > retained)
    retries = retries[:MAX_FINDINGS_RETRIES]
    out, errors, heads = {}, [], dict(heads)
    # A merge read from the ledger has no head commit stored; its pull request names it.
    missing = [m["number"] for m in retries if not heads.get(m["number"])]
    if missing:
        try:
            found = _by_number(gh, repo, missing, "pullRequest", "headRefOid")
            heads.update((n, p.get("headRefOid")) for n, p in zip(missing, found))
        except Exception as e:  # their findings are looked for again next run
            errors += [f"#{n}: {type(e).__name__}: {e}" for n in missing]
    for m in todo + retries:
        try:
            head = heads.get(m["number"])
            if not head:
                continue
            envelope = review_findings(gh, repo, m["number"], head)
        except Exception as e:  # a missing or unreadable artifact only means no findings for it
            errors.append(f"#{m['number']}: {type(e).__name__}: {e}")
            continue
        if envelope:
            out[str(m["number"])] = envelope
    return out, errors


def collect(gh: GitHub, env: dict, ledger: dict | None, now: datetime, turn: int = 0) -> dict:
    repo = env["REPOSITORY"]
    notes = []
    # Without the list the service drops nothing, which is only slower, so it is read by the first update of
    # each UTC day: 20 requests a run for 2000 open issues bought little on a busy branch.
    open_issues = {"numbers": [], "complete": False}
    last = _parse((ledger or {}).get("updatedAt"))
    if not last or last.astimezone(timezone.utc).date() != now.astimezone(timezone.utc).date():
        # Read before the issues, so an issue closed in between is still listed rather than dropped.
        try:
            open_issues = open_issue_numbers(gh, repo)
            if not open_issues["complete"]:
                notes.append(f"over {MAX_OPEN_NUMBERS} open issues, so none was checked for removal")
        except Exception as e:
            notes.append(f"open issues not listed ({type(e).__name__}: {e})")
    issues, through, capped = collect_issues(gh, repo, ledger, now)
    notes += capped
    if capped:
        # An issue the cap skipped may have closed since the ledger stored it open; it must not be dropped.
        open_issues = {"numbers": [], "complete": False}
    if ledger:
        again = relinked(ledger, repo, {i["number"] for i in issues}, turn)
        try:
            issues += issues_by_number(gh, repo, again)
        except Exception as e:  # their pull requests are read again next time
            notes.append(f"{len(again)} issues with open pull requests not read again ({type(e).__name__}: {e})")
    merged, heads, cut_merged = collect_merged(gh, repo, env["BRANCH"], ledger, now)
    notes += cut_merged
    findings, errors = collect_findings(gh, repo, merged, heads, ledger, now)
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


def pack(body: dict) -> tuple[bytes, list[str]]:
    """The compressed request and the pull requests whose findings were left out of it, largest first;
    their merges stay unread for a later run. Findings are left out until the JSON is under the service's
    limit, counted per entry rather than serialised again, and then it is compressed once. Only when that
    is over the limit on what is sent is it compressed again, down to a little under what the ratio just
    measured says fits, at most MAX_COMPRESSIONS times in all."""
    findings = body.get("findings") or {}
    # An entry's bytes in the request: `"pr": {...}` and its separator, exact while one entry is left.
    cost = {pr: len(json.dumps({pr: envelope})) for pr, envelope in findings.items()}
    kept, dropped = sorted(findings, key=cost.get), []
    size, budget = len(json.dumps({**body, "findings": findings})), MAX_BRIEFING_JSON_BYTES
    for compressions in range(1, MAX_COMPRESSIONS + 1):
        while kept and size > budget:
            dropped.append(kept.pop())
            size -= cost[dropped[-1]]
        request = {**body, "findings": {pr: findings[pr] for pr in findings if pr not in dropped}}
        try:
            return _encode(request, MAX_BRIEFING_JSON_BYTES, "request"), dropped
        except TooLarge as e:
            if not kept or e.fits is None or compressions == MAX_COMPRESSIONS:
                raise
            budget = e.fits * 9 // 10


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
    if not env.get("ACTIONS_ID_TOKEN_REQUEST_URL") or not env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        # _bearer would fall back to GITHUB_TOKEN, which here can write to the repository.
        raise Skip(
            "the service is sent only an OIDC token, and this job cannot mint one."
            " Add `id-token: write` to the sync job's permissions"
        )
    now = now or datetime.now(timezone.utc)
    head, ledger, updates = read_ledger(gh, repo)
    facts = collect(gh, env, ledger, now, updates)
    for retried in (False, True):
        data, dropped = pack(build_request(env, ledger, facts))
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
            head, ledger, _ = read_ledger(gh, repo)
            facts = {**facts, "openIssues": {"numbers": [], "complete": False}}
            continue
        line = (
            f"Problem history: {count} problems ({summary.get('open', '?')} open, {summary.get('added', '?')} new),"
            f" through {through}; committed {sha[:7]} to {LEDGER_BRANCH}"
        )
        if dropped:
            line += f"; left out the findings of #{', #'.join(dropped)} to stay under the request limit"
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
        if (isinstance(e, HTTPError) and e.code in (403, 404)) or (isinstance(e, GraphQLError) and e.types & DENIED):
            hint = f" (the sync job needs {PERMISSIONS})"
        print(f"::warning::Updating the problem history failed: {type(e).__name__}: {e}{hint}")
    finally:
        timer.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
