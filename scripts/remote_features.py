#!/usr/bin/env python3
"""Ask the CodeBoarding service for a pull request's review guide and architecture checks.

Runs after the review analysis. The two requests go out in parallel and independently: either one
failing leaves the other alone, and nothing here fails the run. The guide is stored as
review_guide.json and the architecture envelope as closed/architecture.json in the review artifact,
including partial, skipped and failed results.

What a request sends
--------------------
One JSON object of named files, compressed with xz. Both requests send the same files:

- head.json and base.json: one side document per commit (see side_document). It holds what the
  service reads from that commit's analysis.json and call graph (static_analysis.pkl), each fact once:
  the methods and graph nodes as one table per file, the calls, the component tree, and the few
  analysis edges that are no call.
- metadata.json: the review artifact's metadata, with the merge base as the base.
- pr.json: the pull request's title, body, head commit and commit subjects.
- pr-files.json: the files changed between the two analysed commits, as GitHub's compare API lists them.
- files.json: those files' contents at both commits, keyed `<sha>:<path>`.

Architecture also sends source.json, the repository's tracked source, unless the workflow turns it off
or it does not fit.

Why it is packed this way
-------------------------
On a large repository such as crate, the two analyses and two call graphs are about 680 MB of JSON.
Most of it repeats itself: the analysis edges are the calls again, the analysis and the graph each list
every method, and the head is nearly the base. A side document writes each fact once (crate: 7 MB per
side). xz then sees the base repeat the head, because its window is 64 MB where gzip's is 32 KB:
crate's whole request is 1.4 MB.

Size limits
-----------
- MAX_BUNDLE_BYTES (4.3 MB compressed): the most a request may carry. Past it the request is skipped,
  after first dropping source.json if architecture carried it.
- MAX_FILES: how many changed files have their contents sent; past it a feature reads diffs only.
- MAX_FILE_BYTES: a larger file is sent neither in files.json nor in source.json.
- MAX_SOURCE_BYTES: the most source.json holds; past it source is not sent.

Credentials
-----------
Credentials follow the analysis. A run that chose its own provider uses its own key and model for every
feature, and a hosted or licensed run uses CodeBoarding's hosted model. A run whose provider the service
cannot call (a self-hosted endpoint) makes no model call at all: its results report what they can
without one, and it never falls back to the hosted model. Either way the service needs to know the
caller is this repository's run: a fresh GitHub Actions OIDC token (plus the license, the same bearer the
hosted proxy gets), or the run's GITHUB_TOKEN when the workflow cannot mint one.

Environment: CLOSED_FUNCTIONS_URL, REPOSITORY, PR_NUMBER, ARTIFACT_DIR, HEAD_ANALYSIS_PATH,
BASE_ANALYSIS_PATH, CHECKOUT_DIR, GITHUB_TOKEN, GITHUB_API_URL, and optionally LICENSE_KEY,
CLOSED_FEATURES_SOURCE, LLM, MODEL, AGENT_MODEL, <PROVIDER>_API_KEY, <PROVIDER>_BASE_URL,
ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN.
"""

from __future__ import annotations

import importlib.util
import json
import lzma
import os
import pickle
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

# A Function URL takes 6 MB, measured after base64, and a compressed body goes through base64 on the
# way in: 4.5 MB of bundle arrives, and 5 MB is refused with a 413.
MAX_BUNDLE_BYTES = 4_300_000
# Changed files whose contents are sent at both commits; past this a feature reads diffs only.
MAX_FILES = 300
# A file larger than this is sent neither as a changed file's contents nor as source.
MAX_FILE_BYTES = 300_000
# Fixed hosts matching closed_functions/llm.py; never supplied by a remote service.
PROVIDER_HOSTS = {
    "ollama": "ollama.com",
    "openrouter": "openrouter.ai",
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "aws_bedrock": "bedrock-runtime.{region}.amazonaws.com",
    "google": "generativelanguage.googleapis.com",
    "cerebras": "api.cerebras.ai",
    "deepseek": "api.deepseek.com",
    "glm": "open.bigmodel.cn",
    "kimi": "api.moonshot.cn",
    "vercel": "ai-gateway.vercel.sh",
    "orcarouter": "api.orcarouter.ai",
    "requesty": "router.requesty.ai",
    "opper": "api.opper.ai",
}
SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".cs", ".csproj")
SOURCE_EXCLUDE = re.compile(r"(^|/)(node_modules|dist|build|vendor|__fixtures__|docs|public)/|\.min\.js$|\.d\.ts$")
MAX_SOURCE_BYTES = 16_000_000


class Skip(Exception):
    """A step that did not happen, for a reason worth one line in the log."""


# --- GitHub ---------------------------------------------------------------------------------------


def _github(
    path: str,
    accept: str = "application/vnd.github+json",
    *,
    method: str = "GET",
    payload=None,
    api: str | None = None,
    token: str | None = None,
    timeout: int = 30,
) -> bytes:
    """A GitHub API call: `path` under `api` (default GITHUB_API_URL), or a full URL."""
    api = (api or os.environ.get("GITHUB_API_URL", "https://api.github.com")).rstrip("/")
    request = Request(
        path if path.startswith("http") else f"{api}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {token or os.environ['GITHUB_TOKEN']}",
            "Accept": accept,
            "User-Agent": "codeboarding-action",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - GitHub API URL
        return response.read()


def _error_message(error: HTTPError) -> str:
    """The `message` of an error answer's JSON body, or "" when it has none."""
    try:
        return str(json.loads(error.read()).get("message") or "")
    except (ValueError, AttributeError, OSError):
        return ""


def _github_json(path: str):
    return json.loads(_github(path))


def _diff(repo: str, base_sha: str, head_sha: str) -> list[dict]:
    """The files changed between the two analysed commits, in the shape of a pull request's files
    (the compare API returns the same fields), so a feature reads exactly what was analysed."""
    compared = _github_json(f"/repos/{repo}/compare/{base_sha}...{head_sha}")
    fields = ("filename", "status", "additions", "deletions", "changes", "patch", "previous_filename")
    return [{field: changed[field] for field in fields if field in changed} for changed in compared.get("files") or []]


def _contents(repo: str, sha: str, path: str) -> str | None:
    """A file's text at a commit, or None when GitHub has no such file or it is not UTF-8."""
    try:
        raw = _github(f"/repos/{repo}/contents/{quote(path)}?ref={sha}", accept="application/vnd.github.raw")
    except HTTPError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --- Side documents -------------------------------------------------------------------------------


def call_graph(pickle_path: Path) -> dict:
    """A static_analysis.pkl's call graph as the service reads it: each node's file, span and whether
    it is a callback, and each call's site lines. A site's file is always its caller's and no reader
    takes the column, so neither is kept. The pickle holds CodeBoarding's own classes, which the
    installed engine provides."""
    with pickle_path.open("rb") as f:
        results = pickle.load(f)  # nosec B301 - written by this run's own analysis
    nodes, edges = {}, []
    for language in results.results.values():
        graph = language.cfg.graph
        for name, node in graph.nodes.items():
            nodes[name] = {
                "file": node.file_path,
                "start": node.line_start,
                "end": node.line_end,
                "cb": bool(node.is_callback_or_anonymous()),
            }
        for edge in graph.edges:
            edges.append({"s": edge.get_source(), "d": edge.get_destination(), "lines": _site_lines(edge)})
    return {"nodes": nodes, "edges": edges}


def _site_lines(edge) -> list:
    """The line of each place the call is made; a site may be a dict or an object."""
    sites = vars(edge).get("_call_sites") or []
    return [site.get("line") if isinstance(site, dict) else getattr(site, "line", None) for site in sites]


@dataclass(eq=False)
class _Row:
    """One row of a side document's file table: a methods_index entry, a call-graph node the index
    lacks, or a method that is also a node."""

    name: str
    start: int | None
    end: int | None
    method: tuple | None  # (type, content_hash) for a methods_index entry
    callback: int = -1  # 1 or 0 once a graph node shares the row; -1 for a method that is no node
    number: int = -1  # the row's position across all files, set by _Table.number_rows

    def written(self, path: str) -> list:
        name = _short_name(self.name, path)
        if self.method is None:
            return [name, self.start, self.end, self.callback]
        return [name, self.start, self.end, *self.method, self.callback]


def _short_name(name: str, path: str) -> str:
    """A qualified name as a row writes it: `~.rest` when it is the file's dotted path plus `.rest`,
    and a leading `~` doubled so a name of its own that starts with one reads back unchanged."""
    stem = path.rsplit(".", 1)[0].replace("/", ".")
    if name.startswith(stem + "."):
        return "~" + name[len(stem) :]
    return "~" + name if name.startswith("~") else name


class _Table:
    """The rows of one side, per file: every methods_index entry, then every call-graph node, which
    shares the row of a method with its name."""

    def __init__(self, methods_index: dict):
        self.rows_by_file: dict[str, list[_Row]] = {}
        self.row_of_key: dict[str, _Row] = {}
        self.methods_named: dict[str, list[tuple[str, _Row]]] = {}  # qualified name -> (file, row)
        self.row_of_node: dict[str, _Row] = {}
        self.row_count = 0
        for key, method in methods_index.items():
            self._add_method(key, method)

    def _add_method(self, key: str, method: dict) -> None:
        path, name = method.get("file_path") or "", method.get("qualified_name") or ""
        if key != f"{path}|{name}":
            raise ValueError(f"methods_index key {key!r} is not its file and qualified name")
        row = _Row(
            name,
            start=method.get("start_line"),
            end=method.get("end_line"),
            method=(method.get("type"), method.get("content_hash")),
        )
        self.rows_by_file.setdefault(path, []).append(row)
        self.row_of_key[key] = row
        self.methods_named.setdefault(name, []).append((path, row))

    def add_node(self, name: str, node: dict) -> _Row:
        """The row of the method with the node's name, preferring the one in the node's file, or a new
        row for a node that is no method."""
        methods = self.methods_named.get(name) or []
        row = next((row for path, row in methods if path == node.get("file")), methods[0][1] if methods else None)
        if row is None:
            row = _Row(name, start=node.get("start"), end=node.get("end"), method=None)
            self.rows_by_file.setdefault(node.get("file") or "", []).append(row)
        row.callback = int(node["cb"])
        self.row_of_node[name] = row
        return row

    def number_rows(self) -> list:
        """Number the rows, files in path order and each file's rows by start line, and return the
        files as the document writes them: `[path, [row, ...]]`."""
        written = []
        for path in sorted(self.rows_by_file):
            rows = sorted(self.rows_by_file[path], key=lambda row: (row.start is None, row.start or 0, row.name))
            for row in rows:
                row.number = self.row_count
                self.row_count += 1
            written.append([path, [row.written(path) for row in rows]])
        return written

    def member(self, key: str) -> int | str:
        """A methods_index key as a component member or edge end: its row number, or the key itself
        when the index lacks it."""
        row = self.row_of_key.get(key)
        return key if row is None else row.number


def side_document(analysis: dict, graph: dict) -> dict:
    """One commit's analysis and call graph as the service reads them, each fact once. The service's
    bundle.expand_side turns it back into both, in every field a feature reads.

    - files: per file, rows for the methods_index entries `[name, start, end, type, content_hash, cb]`
      and for call-graph nodes the index lacks `[name, start, end, cb]`. A method and the graph node of
      the same name share a row; cb is -1 for a method that is no graph node. A name under its file's
      dotted path is written `~.rest`. Rows are numbered in order across files; `unplaced` continues the
      numbering for call ends that are no node.
    - graph_spans: `[row, start, end]` for a node whose span is not its method's.
    - calls: per caller, `[callee, line, ...]`, one line per call site.
    - components: `[id, name, description, parent, members, relations]` in tree order. Members are rows
      (a key where the index lacks the method), or null for a parent holding exactly its children's.
    - extra_edges: the analysis's edges that are no call. The rest repeat the calls: on crate one of
      149,316 is not a call."""
    table = _Table(analysis.get("methods_index") or {})
    spans = []
    for name, node in (graph.get("nodes") or {}).items():
        row = table.add_node(name, node)
        if (row.start, row.end) != (node.get("start"), node.get("end")):
            spans.append((row, node.get("start"), node.get("end")))
    files = table.number_rows()
    calls, unplaced = _calls(graph.get("edges") or [], table)
    called = {(caller, callee) for caller, callees in calls for callee, *_ in callees}
    edges = {(table.member(source), table.member(target)) for source, target in _analysis_edges(analysis)}
    return {
        "version": 1,
        "files": files,
        "unplaced": unplaced,
        "graph_spans": [[row.number, start, end] for row, start, end in spans],
        "components": _components(analysis.get("components"), table),
        "relations": _relations(analysis.get("components_relations")),
        "calls": calls,
        "extra_edges": sorted(edges - called, key=repr),
    }


def _calls(edges: list[dict], table: _Table) -> tuple[list, list[str]]:
    """The calls grouped by caller, `[caller, [[callee, line, ...], ...]]` in row order, and the call
    ends that are no node, numbered after the last row."""
    number_of = {name: row.number for name, row in table.row_of_node.items()}
    unplaced: list[str] = []
    by_caller: dict[int, list] = {}
    for edge in edges:
        for name in (edge["s"], edge["d"]):
            if name not in number_of:
                number_of[name] = table.row_count + len(unplaced)
                unplaced.append(name)
        by_caller.setdefault(number_of[edge["s"]], []).append([number_of[edge["d"]], *edge["lines"]])
    return [[caller, by_caller[caller]] for caller in sorted(by_caller)], unplaced


def _components(components: list | None, table: _Table) -> list:
    """The component tree in pre-order, each `[id, name, description, parent, members, relations]`.
    A parent's members are null when they are exactly its children's, which the service rebuilds."""
    flat = _flatten(components)
    children: list[list[int]] = [[] for _ in flat]
    for position, (_, parent) in enumerate(flat):
        if parent >= 0:
            children[parent].append(position)
    members = [_members(component, table) for component, _ in flat]
    written = []
    for position, (component, parent) in enumerate(flat):
        from_children = set().union(*(members[child] for child in children[position]))
        implied = children[position] and set(members[position]) == from_children
        written.append(
            [
                component.get("component_id"),
                component.get("name"),
                component.get("description"),
                parent,
                None if implied else members[position],
                _relations(component.get("components_relations")),
            ]
        )
    return written


def _flatten(components: list | None) -> list[tuple[dict, int]]:
    """The component tree in pre-order, each component with its parent's position (-1 at the top)."""
    flat: list[tuple[dict, int]] = []

    def visit(level: list | None, parent: int) -> None:
        for component in level or []:
            flat.append((component, parent))
            visit(component.get("components"), len(flat) - 1)

    visit(components, -1)
    return flat


def _members(component: dict, table: _Table) -> list:
    return [
        table.member(f"{group['file_path']}|{name}")
        for group in component.get("file_methods") or []
        for name in group.get("methods") or []
    ]


def _relations(relations: list | None) -> list:
    """Each relation as `[src_id, dst_id, label]`."""
    return [[relation.get("src_id"), relation.get("dst_id"), relation.get("relation")] for relation in relations or []]


def _analysis_edges(scope: dict):
    """Every method edge of every relation in the analysis, top level and nested."""
    for relation in scope.get("components_relations") or []:
        for edge in relation.get("all_edges") or []:
            if edge.get("source") and edge.get("target"):
                yield edge["source"], edge["target"]
    for component in scope.get("components") or []:
        yield from _analysis_edges(component)


# --- Collecting the inputs ------------------------------------------------------------------------


def collect(env: dict) -> dict:
    """The files both requests send, named as the service reads them."""
    repo, pr = env["REPOSITORY"], int(env["PR_NUMBER"])
    head_analysis, base_analysis = Path(env["HEAD_ANALYSIS_PATH"]), Path(env["BASE_ANALYSIS_PATH"])
    metadata = json.loads((Path(env["ARTIFACT_DIR"]) / "metadata.json").read_text())
    head_sha = metadata["head_sha"]
    # The analysis compared against the merge base, so that is the base every feature reads too.
    base_sha = metadata.get("merge_base_sha") or metadata.get("base_sha")
    for analysis_path in (head_analysis, base_analysis):
        pickle_path = analysis_path.parent / "static_analysis.pkl"
        if not pickle_path.is_file():
            raise Skip(f"no call graph at {pickle_path}")

    pull = _github_json(f"/repos/{repo}/pulls/{pr}")
    commits = _github_json(f"/repos/{repo}/pulls/{pr}/commits?per_page=100")
    changed = _diff(repo, base_sha, head_sha)
    contents = _changed_contents(repo, changed, Path(env["CHECKOUT_DIR"]), head_sha, base_sha)
    return {
        "head.json": _side(head_analysis),
        "base.json": _side(base_analysis),
        "metadata.json": json.dumps({**metadata, "base_sha": base_sha, "head_sha": head_sha, "pr_number": str(pr)}),
        "pr.json": json.dumps(
            {
                "title": pull.get("title") or "",
                "body": pull.get("body") or "",
                "headRefOid": head_sha,
                "commits": [(commit.get("commit") or {}).get("message", "").split("\n")[0] for commit in commits],
            }
        ),
        "pr-files.json": json.dumps(changed),
        "files.json": json.dumps(contents),
    }


def _side(analysis_path: Path) -> str:
    """The side document of the commit whose analysis this is; its call graph lies beside it."""
    analysis = json.loads(analysis_path.read_text())
    graph = call_graph(analysis_path.parent / "static_analysis.pkl")
    return json.dumps(side_document(analysis, graph), separators=(",", ":"))


def _changed_contents(repo: str, changed: list[dict], checkout: Path, head_sha: str, base_sha: str) -> dict:
    """The first MAX_FILES changed files at both commits, keyed `<sha>:<path>`: the head from the
    checkout, the base from GitHub. A file over MAX_FILE_BYTES or not UTF-8 is left out."""
    contents: dict[str, str] = {}
    for change in changed[:MAX_FILES]:
        if change.get("status") != "removed":
            text = _checkout_text(checkout, change["filename"])
            if text is not None:
                contents[f"{head_sha}:{change['filename']}"] = text
        if change.get("status") != "added":
            base_name = change.get("previous_filename") or change["filename"]
            text = _contents(repo, base_sha, base_name)
            if text is not None and len(text.encode()) <= MAX_FILE_BYTES:
                contents[f"{base_sha}:{base_name}"] = text
    return contents


def _checkout_text(checkout: Path, name: str) -> str | None:
    """A checked-out file's text, or None when it is unsafe to read, over MAX_FILE_BYTES or not UTF-8."""
    try:
        path = _checkout_file(checkout, name)
        if path.stat().st_size <= MAX_FILE_BYTES:
            return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _checkout_file(checkout: Path, name: str) -> Path:
    """Reject traversal and symlinks (including parent directories) before reading source."""
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise OSError("path outside checkout")
    path = checkout
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise OSError("symlink in checkout")
    return path


def collect_source(env: dict) -> tuple[str | None, str | None]:
    """Tracked architecture source, within fixed size and extension limits, or an omission reason."""
    if env.get("CLOSED_FEATURES_SOURCE", "true").strip().lower() == "false":
        return None, "turned off by the workflow"
    checkout = Path(env["CHECKOUT_DIR"])
    listed = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z"], capture_output=True, check=True).stdout
    total = 0
    files: dict[str, str] = {}
    for name in listed.decode("utf-8", "replace").split("\0"):
        if not name or not name.endswith(SOURCE_EXTENSIONS) or SOURCE_EXCLUDE.search(name):
            continue
        try:
            path = _checkout_file(checkout, name)
            size = path.stat().st_size
            if size > MAX_FILE_BYTES or not path.is_file():
                continue
            files[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        total += size
        if total > MAX_SOURCE_BYTES:
            return None, f"its source is over the {MAX_SOURCE_BYTES / 1e6:.0f} MB limit"
    return json.dumps(files), None


# --- Credentials ----------------------------------------------------------------------------------


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)  # its dataclass looks its own module up while it is defined
    spec.loader.exec_module(sys.modules[spec.name])
    return sys.modules[spec.name]


def _relay():
    return _load("oidc_relay", Path(__file__).with_name("oidc_relay.py"))


def _api_key(env: dict, llm: str) -> str:
    """The key as the analysis read it, so a pasted `VAR="..."` that worked there works here too."""
    check = _load("credential_check", Path(__file__).parent / "action" / "credential_check.py")
    inputs = check.load_table()["providers"].get(llm, {}).get("inputs", {})
    env_var = inputs.get(f"{llm}_api_key", f"{llm.upper()}_API_KEY")
    return check._clean_key(env.get(f"{llm.upper()}_API_KEY", ""), env_var)


def model_headers(env: dict) -> dict[str, str]:
    """What tells the service which model this run uses. Nothing for a hosted or licensed run, which
    use CodeBoarding's model and settings as they stand. A run's own provider sends its key and model;
    one the service cannot reach sends its name alone, which the service reads as "no model here", so a
    run that chose its own provider is never sent to ours. A base URL elsewhere is a provider the
    service cannot reach: it calls fixed hosts only."""
    llm = env.get("LLM", "").strip().lower()
    if llm in ("", "hosted", "license"):
        return {}
    key = _api_key(env, llm)
    base_url = env.get(f"{llm.upper()}_BASE_URL", "").strip()
    host = PROVIDER_HOSTS.get(llm)
    if host and llm == "aws_bedrock":
        host = host.format(region=env.get("AWS_BEDROCK_REGION", "").strip())
    if not key or not host or (base_url and urlparse(base_url).hostname != host):
        return {"x-llm-provider": llm}
    headers = {"x-llm-api-key": key, "x-llm-provider": llm}
    if env.get(f"{llm.upper()}_REGION", "").strip():
        headers["x-llm-region"] = env[f"{llm.upper()}_REGION"].strip()
    # The analysis's own order, so a run's remote features use its analysis model.
    model = env.get("AGENT_MODEL", "").strip() or env.get("MODEL", "").strip()
    if model:
        headers["x-llm-model"] = model
    return headers


def _bearer(env: dict) -> dict[str, str]:
    url, token = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not token:
        # Without `id-token: write` the run's own token proves the same thing: it can read this
        # repository. It carries no license, so the owner's free bucket pays.
        return {"Authorization": f"Bearer {env['GITHUB_TOKEN']}"}
    relay = _relay()
    license_file = None
    if env.get("LICENSE_KEY", "").strip():
        license_file = Path(env.get("RUNNER_TEMP", "/tmp")) / "codeboarding-guide-license.txt"
        license_file.write_text(env["LICENSE_KEY"].strip())
    try:
        config = relay.RelayConfig(
            upstream_base_url="", id_token_request_url=url, id_token_request_token=token, license_file=license_file
        )
        return {"Authorization": relay._authorization(config)}
    finally:
        if license_file is not None:
            license_file.unlink(missing_ok=True)


# --- Requests -------------------------------------------------------------------------------------


def _post(url: str, body: bytes, headers: dict[str, str], timeout: int = 600) -> dict:
    request = Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/x-xz", "User-Agent": "codeboarding-action"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - service URL from workflow configuration
            return json.loads(response.read())
    except HTTPError as error:
        reason = f"the service answered {error.code}: {_error_message(error) or error.reason}"
        if error.code == 402:
            raise Skip(reason) from error
        raise RuntimeError(reason) from error


def _compress(body: dict) -> bytes:
    return lzma.compress(json.dumps(body).encode(), preset=9 | lzma.PRESET_EXTREME)


def _pack(ctx: dict, files: dict[str, str], omitted: dict[str, str]) -> bytes:
    """A request body: the named files, and why any optional one is missing."""
    body = {"version": 1, "repo": ctx["repo"], "pr": ctx["pr"], "files": files}
    if omitted:
        body["omitted"] = omitted
    return _compress(body)


def _check_size(data: bytes, what: str = "bundle") -> None:
    if len(data) > MAX_BUNDLE_BYTES:
        raise Skip(f"its {what} is {len(data) / 1e6:.1f} MB, over the service's limit")


def _no_architecture(ctx: dict, status: str, reason: str) -> dict:
    """Preserve the architecture envelope even when the request cannot complete."""
    return {
        "feature": "architecture",
        "schema": 1,
        "status": status,
        "repo": ctx["repo"],
        "pr": ctx["pr"],
        "headSha": ctx["head"],
        "generator": None,
        "coverage": None,
        "data": None,
        "reason": reason,
    }


def run_review_guide(service_url: str, ctx: dict, inputs: dict, headers: dict, artifact: Path) -> None:
    try:
        if "x-llm-provider" in headers and "x-llm-api-key" not in headers:
            raise Skip("the service cannot call this run's provider")
        data = _pack(ctx, inputs, {})
        _check_size(data)
        answer = _post(f"{service_url.rstrip('/')}/v1/review-guide", data, headers)
        guide = answer.get("guide")
        if not isinstance(guide, dict):
            raise ValueError("the service returned no guide")
        (artifact / "review_guide.json").write_text(json.dumps(guide, ensure_ascii=False, indent=1))
    except Exception as error:
        print(f"::notice::review-guide {'skipped' if isinstance(error, Skip) else 'failed'}: {error}")


def run_architecture(service_url: str, ctx: dict, inputs: dict, headers: dict, env: dict) -> dict:
    try:
        try:
            source, why = collect_source(env)
        except Exception as error:
            source, why = None, f"reading it failed: {type(error).__name__}"
        files, omitted = dict(inputs), {}
        if source is None:
            omitted["source.json"] = why or "the source was not collected"
        else:
            files["source.json"] = source
        data = _pack(ctx, files, omitted)
        if "source.json" in files and len(data) > MAX_BUNDLE_BYTES:
            del files["source.json"]
            omitted["source.json"] = "the repository is too large to send"
            data = _pack(ctx, files, omitted)
        _check_size(data)
        answer = _post(f"{service_url.rstrip('/')}/v1/architecture", data, headers)
        envelope = answer.get("envelope")
        if not isinstance(envelope, dict) or envelope.get("status") not in ("ok", "partial", "skipped", "failed"):
            raise ValueError("the service returned no architecture result")
        return envelope
    except Exception as error:
        return _no_architecture(ctx, "skipped" if isinstance(error, Skip) else "failed", str(error))


def main(env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        artifact = Path(env["ARTIFACT_DIR"])
        metadata = json.loads((artifact / "metadata.json").read_text())
        ctx = {"repo": env["REPOSITORY"], "pr": int(env["PR_NUMBER"]), "head": metadata.get("head_sha")}
        service_url = env["CLOSED_FUNCTIONS_URL"]
        if not service_url.strip():
            return 0
        try:
            headers = {**_bearer(env), **model_headers(env)}
            inputs = collect(env)
        except Exception as error:
            reason = f"collecting inputs or credentials failed: {type(error).__name__}: {error}"
            print(f"::notice::review-guide failed: {reason}")
            architecture = _no_architecture(ctx, "failed", reason)
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                guide = pool.submit(run_review_guide, service_url, ctx, inputs, headers, artifact)
                result = pool.submit(run_architecture, service_url, ctx, inputs, headers, env)
                architecture = result.result()
                guide.result()
        path = artifact / "closed" / "architecture.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(architecture, ensure_ascii=False, indent=1))
        if architecture["status"] not in ("ok", "partial"):
            print(f"::notice::architecture {architecture['status']}: {architecture.get('reason')}")
    except Exception as error:  # never fail the review over a remote feature
        print(f"::warning::Running the remote features failed: {type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
