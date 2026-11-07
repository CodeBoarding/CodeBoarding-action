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

import gzip
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from closed_features import MAX_BUNDLE_BYTES, Skip, _bearer, _post  # noqa: E402

LEDGER_BRANCH = "codeboarding/briefing"
LEDGER_PATH = "problems.json"
ISSUE_OVERLAP = timedelta(minutes=5)
# A pull request merged moments before the last one folded in may have been missed by that run.
MERGED_OVERLAP = timedelta(hours=1)
FIRST_CLOSED_DAYS = 90
FIRST_MERGED_DAYS = 56
MAX_ISSUES = 1000
MAX_MERGED = 500
MAX_PATHS = 300
MAX_ARTIFACTS = 40
# Successful runs on a head commit whose artifacts are listed before giving up on its review.
MAX_RUNS_PER_PR = 6
MAX_BODY_CHARS = 2000

# The webview's Briefing query (src/lib/github.ts, BRIEFING_ISSUES_QUERY), plus a linked pull
# request's title and url, read from the repository's own issue list rather than search so an issue
# updated a moment ago is never missed.
_BRIEF_PR = "fragment BriefPr on PullRequest{ number title url state isDraft reviewDecision updatedAt author{ login } repository{ nameWithOwner } }"
ISSUES_QUERY = (
    _BRIEF_PR
    + """
query($owner:String!,$name:String!,$states:[IssueState!],$since:DateTime,$dir:OrderDirection!,$c:String){
  repository(owner:$owner, name:$name){
    issues(first:50, after:$c, states:$states, filterBy:{since:$since}, orderBy:{field:UPDATED_AT, direction:$dir}){
      pageInfo{ hasNextPage endCursor }
      nodes{ number title body url state createdAt closedAt updatedAt authorAssociation
        author{ __typename login } repository{ nameWithOwner } labels(first:10){ nodes{ name } } assignees(first:5){ nodes{ login } }
        timelineItems(last:30, itemTypes:[CLOSED_EVENT, CROSS_REFERENCED_EVENT, CONNECTED_EVENT, DISCONNECTED_EVENT]){ nodes{ __typename
          ... on ClosedEvent{ closer{ __typename ... on PullRequest{ number author{ login } repository{ nameWithOwner } } } }
          ... on CrossReferencedEvent{ willCloseTarget source{ __typename ...BriefPr ... on PullRequest{ commits(last:1){ nodes{ commit{ statusCheckRollup{ state } } } } } } }
          ... on ConnectedEvent{ subject{ __typename ...BriefPr } source{ __typename ...BriefPr } }
          ... on DisconnectedEvent{ subject{ __typename ...BriefPr } source{ __typename ...BriefPr } } } } } } } }"""
)
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
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(url, data=data, method=method, headers=self._headers(accept))
        with urlopen(req, timeout=60) as res:  # nosec B310 - GitHub API URL
            return res.read()

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


def _nodes(gh: GitHub, query: str, variables: dict, field: str):
    cursor = None
    while True:
        conn = gh.graphql(query, {**variables, "c": cursor})["repository"][field]
        yield from conn.get("nodes") or []
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return
        cursor = page.get("endCursor")


def _login(actor) -> str | None:
    return (actor or {}).get("login") or None


def linked_pull_requests(node: dict) -> list[dict]:
    """webview src/lib/briefing/linked.ts `linkedPullRequests`: the pull requests an issue's timeline
    links to, and whether each closes it (a closing keyword, or a Development link not since undone)."""
    prs: dict[str, tuple[dict, str | None]] = {}
    keyword, connected = set(), set()
    for t in (node.get("timelineItems") or {}).get("nodes") or []:
        kind = (t or {}).get("__typename")
        if kind == "CrossReferencedEvent":
            pr = t.get("source")
        elif kind in ("ConnectedEvent", "DisconnectedEvent"):
            pr = next(
                (x for x in (t.get("subject"), t.get("source")) if (x or {}).get("__typename") == "PullRequest"), None
            )
        else:
            pr = None
        if not pr or pr.get("__typename") != "PullRequest" or not pr.get("repository"):
            continue
        if not pr.get("number") or not pr.get("state"):
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
    out = []
    for key, (s, checks) in prs.items():
        decision = s.get("reviewDecision")
        item = {"repo": s["repository"]["nameWithOwner"], "number": s["number"]}
        item.update({k: s[k] for k in ("title", "url") if s.get(k) is not None})
        item.update(
            {
                "state": s["state"],
                "draft": bool(s.get("isDraft")),
                "updatedAt": s.get("updatedAt") or node.get("updatedAt"),
                "review": {"APPROVED": "approved", "CHANGES_REQUESTED": "changes"}.get(decision, "review"),
                "checks": checks,
                "closes": key in keyword or key in connected,
            }
        )
        if _login(s.get("author")):
            item["author"] = _login(s.get("author"))
        out.append(item)
    return out


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
    out = {
        "repo": node["repository"]["nameWithOwner"],
        "number": node["number"],
        "title": node.get("title") or "",
        "body": node.get("body") or "",
        "url": node.get("url"),
        "state": node.get("state"),
        "createdAt": node.get("createdAt"),
        "closedAt": node.get("closedAt"),
        "updatedAt": node.get("updatedAt"),
    }
    if _login(author):
        out["author"] = _login(author)
    out["authorBot"] = author.get("__typename") == "Bot"
    out["association"] = node.get("authorAssociation") or "NONE"
    out["labels"] = [label["name"] for label in (node.get("labels") or {}).get("nodes") or [] if label]
    out["assignees"] = [a["login"] for a in (node.get("assignees") or {}).get("nodes") or [] if a]
    closed_by = None
    if closer and closer.get("repository") and closer.get("number"):
        closed_by = {"repo": closer["repository"]["nameWithOwner"], "number": closer["number"]}
        if _login(closer.get("author")):
            closed_by["author"] = _login(closer.get("author"))
    out["closedBy"] = closed_by
    out["linked"] = linked_pull_requests(node)
    return out


def read_ledger(gh: GitHub, repo: str) -> tuple[str | None, dict | None]:
    """The ledger branch's head and its problems.json; (None, None) before the first update."""
    try:
        head = gh.json(f"/repos/{repo}/git/ref/heads/{LEDGER_BRANCH}")["object"]["sha"]
    except HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    try:
        raw = gh.call("GET", f"/repos/{repo}/contents/{LEDGER_PATH}?ref={head}", accept="application/vnd.github.raw")
    except HTTPError as e:
        if e.code == 404:
            return head, None
        raise
    ledger = json.loads(raw)
    if not isinstance(ledger, dict):
        raise Skip(f"{LEDGER_PATH} on {LEDGER_BRANCH} is not a ledger")
    return head, ledger


def collect_issues(gh: GitHub, repo: str, ledger: dict | None, started: datetime) -> tuple[list[dict], str, bool]:
    """Issues for the request, the `issuesThrough` to record, and whether the cap cut the list.

    A later run reads oldest update first, so when the cap cuts it short `issuesThrough` stops at the
    last issue read and the next run carries on from there."""
    owner, name = repo.split("/", 1)
    since = _parse((ledger or {}).get("issuesThrough"))
    cutoff = None
    if since is None:
        cutoff = started - timedelta(days=FIRST_CLOSED_DAYS)
        passes = [(["OPEN"], None, "DESC"), (["CLOSED"], _iso(cutoff), "DESC")]
    else:
        passes = [(None, _iso(since - ISSUE_OVERLAP), "ASC")]
    out: dict[int, dict] = {}
    through, cut = _iso(started), False
    for states, after, direction in passes:
        variables = {"owner": owner, "name": name, "states": states, "since": after, "dir": direction}
        for n in _nodes(gh, ISSUES_QUERY, variables, "issues"):
            if not n or "number" not in n:
                continue
            if cutoff and n.get("state") == "CLOSED" and (_parse(n.get("closedAt")) or cutoff) <= cutoff:
                continue
            if len(out) >= MAX_ISSUES:
                cut = True
                break
            out[n["number"]] = issue_facts(n)
            if direction == "ASC":
                through = n.get("updatedAt") or through
        if cut:
            break
    if not cut or since is None:
        through = _iso(started)
    return list(out.values()), through, cut


def _merged_since(ledger: dict | None, now: datetime) -> datetime:
    if ledger:
        last = _parse((ledger.get("through") or {}).get("mergedAt")) or _parse(ledger.get("updatedAt"))
        if last:
            return last - MERGED_OVERLAP
    return now - timedelta(days=FIRST_MERGED_DAYS)


def _paths(gh: GitHub, owner: str, name: str, number: int, files: dict) -> list[str]:
    paths = [f["path"] for f in files.get("nodes") or [] if f]
    page = files.get("pageInfo") or {}
    while page.get("hasNextPage") and len(paths) < MAX_PATHS:
        variables = {"owner": owner, "name": name, "n": number, "c": page.get("endCursor")}
        more = gh.graphql(FILES_QUERY, variables)["repository"]["pullRequest"]["files"]
        paths += [f["path"] for f in more.get("nodes") or [] if f]
        page = more.get("pageInfo") or {}
    return paths[:MAX_PATHS]


def collect_merged(
    gh: GitHub, repo: str, branch: str, ledger: dict | None, now: datetime
) -> tuple[list[dict], dict[int, str]]:
    """Pull requests merged into `branch` since the ledger's last one, and each one's head commit."""
    owner, name = repo.split("/", 1)
    since = _merged_since(ledger, now)
    out, heads = [], {}
    for n in _nodes(gh, MERGED_QUERY, {"owner": owner, "name": name, "base": branch}, "pullRequests"):
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
        approvers = []
        for review in (n.get("reviews") or {}).get("nodes") or []:
            login = _login((review or {}).get("author"))
            if login and login not in approvers:
                approvers.append(login)
        pr = {"number": n["number"], "title": n.get("title") or "", "url": n.get("url")}
        if _login(n.get("author")):
            pr["author"] = _login(n.get("author"))
        pr["mergedAt"] = n["mergedAt"]
        pr["approvers"] = approvers
        pr["paths"] = _paths(gh, owner, name, n["number"], n.get("files") or {})
        out.append(pr)
        heads[n["number"]] = n.get("headRefOid")
    return out, heads


def read_architecture(archive: bytes, pr: int) -> dict | None:
    """closed/architecture.json from a review artifact, when it holds findings for this pull request."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        names = set(z.namelist())
        if "metadata.json" in names:
            meta = json.loads(z.read("metadata.json"))
            if str(meta.get("pr_number", pr)) != str(pr):
                return None
        if "closed/architecture.json" not in names:
            return None
        envelope = json.loads(z.read("closed/architecture.json"))
    if not isinstance(envelope, dict) or envelope.get("status") not in ("ok", "partial"):
        return None
    return envelope


def review_findings(gh: GitHub, repo: str, pr: int, sha: str) -> dict | None:
    """The architecture envelope from the newest successful CodeBoarding review of this head commit."""
    runs = gh.json(f"/repos/{repo}/actions/runs?head_sha={quote(sha)}&status=success&per_page=30")
    runs = sorted(
        runs.get("workflow_runs") or [], key=lambda r: not re.search("codeboarding", r.get("name") or "", re.I)
    )
    for run in runs[:MAX_RUNS_PER_PR]:
        want = f"codeboarding-review-{run['id']}-{run.get('run_attempt') or 1}"
        listed = gh.json(f"/repos/{repo}/actions/runs/{run['id']}/artifacts?name={quote(want)}")
        for artifact in listed.get("artifacts") or []:
            if artifact.get("name") != want or artifact.get("expired"):
                continue
            envelope = read_architecture(gh.download(artifact["archive_download_url"]), pr)
            if envelope:
                return envelope
    return None


def collect_findings(
    gh: GitHub, repo: str, merged: list[dict], heads: dict[int, str], ledger: dict | None
) -> tuple[dict[str, dict], list[str]]:
    """Findings per merged pull request the ledger has not folded in yet, newest first, capped."""
    known = {m.get("number") for m in (ledger or {}).get("merged") or [] if isinstance(m, dict)}
    todo = sorted((m for m in merged if m["number"] not in known), key=lambda m: m["mergedAt"], reverse=True)
    out, errors = {}, []
    for m in todo[:MAX_ARTIFACTS]:
        if not heads.get(m["number"]):
            continue
        try:
            envelope = review_findings(gh, repo, m["number"], heads[m["number"]])
        except Exception as e:  # a missing or unreadable artifact only means no findings for it
            errors.append(f"#{m['number']}: {type(e).__name__}: {e}")
            continue
        if envelope:
            out[str(m["number"])] = envelope
    return out, errors


def collect(gh: GitHub, env: dict, ledger: dict | None, now: datetime) -> dict:
    repo = env["REPOSITORY"]
    issues, through, cut = collect_issues(gh, repo, ledger, now)
    merged, heads = collect_merged(gh, repo, env["BRANCH"], ledger, now)
    findings, errors = collect_findings(gh, repo, merged, heads, ledger)
    notes = []
    if cut:
        notes.append(f"read the first {MAX_ISSUES} issues only")
    if errors:
        notes.append(f"no findings for {len(errors)} merged pull requests ({errors[0]})")
    return {
        "now": _iso(now),
        "issues": issues,
        "issuesThrough": through,
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
        "issues": facts["issues"],
        "issuesThrough": facts["issuesThrough"],
        "merged": facts["merged"],
        "findings": facts["findings"],
    }


def pack(body: dict) -> bytes:
    data = gzip.compress(json.dumps(body).encode(), compresslevel=9)
    if len(data) > MAX_BUNDLE_BYTES:
        # The service reads a body for its first sentence and two marker lines, all near the top.
        body = {**body, "issues": [{**i, "body": i["body"][:MAX_BODY_CHARS]} for i in body["issues"]]}
        data = gzip.compress(json.dumps(body).encode(), compresslevel=9)
    if len(data) > MAX_BUNDLE_BYTES:
        raise Skip(f"its request is {len(data) / 1e6:.1f} MB, over the service's limit")
    return data


def _newest_pr(*lists) -> int | None:
    merged = [m for items in lists for m in items or [] if isinstance(m, dict) and m.get("mergedAt")]
    return max(merged, key=lambda m: m["mergedAt"])["number"] if merged else None


def commit_ledger(gh: GitHub, repo: str, head: str | None, ledger: dict, message: str) -> str:
    """One commit holding only problems.json on top of `head`; Conflict when the branch moved."""
    text = json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
    blob = gh.json(f"/repos/{repo}/git/blobs", "POST", {"content": text, "encoding": "utf-8"})["sha"]
    tree = gh.json(
        f"/repos/{repo}/git/trees",
        "POST",
        {"tree": [{"path": LEDGER_PATH, "mode": "100644", "type": "blob", "sha": blob}]},
    )["sha"]
    parents = [head] if head else []
    commit = gh.json(f"/repos/{repo}/git/commits", "POST", {"message": message, "tree": tree, "parents": parents})[
        "sha"
    ]
    try:
        if head:
            gh.json(f"/repos/{repo}/git/refs/heads/{LEDGER_BRANCH}", "PATCH", {"sha": commit, "force": False})
        else:
            gh.json(f"/repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{LEDGER_BRANCH}", "sha": commit})
    except HTTPError as e:
        if e.code == 422:  # not a fast-forward, or the branch appeared since we read it
            raise Conflict() from e
        raise
    return commit


def update(gh: GitHub, env: dict, now: datetime | None = None) -> str:
    """Collect, ask the service, commit; the notice to print."""
    repo, base = env["REPOSITORY"], env["CLOSED_FUNCTIONS_URL"].rstrip("/")
    now = now or datetime.now(timezone.utc)
    head, ledger = read_ledger(gh, repo)
    facts = collect(gh, env, ledger, now)
    for attempt in range(2):
        answer = _post(f"{base}/v1/briefing", pack(build_request(env, ledger, facts)), _bearer(env))
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
            if attempt:
                raise
            # Another run updated it first: fold our facts into its ledger instead. They are a superset
            # of what that ledger lacks, and the service merges what it already holds.
            head, ledger = read_ledger(gh, repo)
            continue
        line = (
            f"Problem history: {count} problems ({summary.get('open', '?')} open, {summary.get('added', '?')} new),"
            f" through {through}; committed {sha[:7]} to {LEDGER_BRANCH}"
        )
        return "; ".join([line, *facts["notes"]])
    raise AssertionError("unreachable")


def main(env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
