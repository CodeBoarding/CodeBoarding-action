#!/usr/bin/env python3
"""Call the review-guide and architecture endpoints independently after the review analysis.

Both requests carry the same two sides, PR metadata and changed files. Only architecture carries
optional source. A side is one document holding what the service reads from that commit's analysis and
call graph (see side_document), and the bundle is xz: its window spans the whole bundle, so the base
side costs little next to a head that repeats most of it. Store the legacy guide in review_guide.json and the unchanged architecture
envelope in closed/architecture.json, including partial, skipped and failed results.

Credentials follow the analysis: a run that chose its own provider uses its own key and model for
every feature, and a hosted or licensed run uses CodeBoarding's hosted model. A run whose provider the
service cannot call (a self-hosted endpoint) makes no model call at all: its results report
what they can without one, and it never falls back to the hosted model. Either way the service needs
to know the caller is this repository's run: a fresh GitHub Actions OIDC token (plus the license, the
same bearer the hosted proxy gets), or the run's GITHUB_TOKEN when the workflow cannot mint one.
Nothing here fails the run.

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
    req = Request(
        path if path.startswith("http") else f"{api}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {token or os.environ['GITHUB_TOKEN']}",
            "Accept": accept,
            "User-Agent": "codeboarding-action",
        },
    )
    with urlopen(req, timeout=timeout) as res:  # nosec B310 - GitHub API URL
        return res.read()


def _error_message(e: HTTPError) -> str:
    """The `message` of an error answer's JSON body, or "" when it has none."""
    try:
        return str(json.loads(e.read()).get("message") or "")
    except (ValueError, AttributeError, OSError):
        return ""


def _github_json(path: str):
    return json.loads(_github(path))


def call_graph(pkl: Path) -> dict:
    """A static_analysis.pkl's call graph as the service reads it: nodes and edges with call sites.
    The pickle holds CodeBoarding's own classes, which the installed engine provides."""
    with pkl.open("rb") as f:
        results = pickle.load(f)  # nosec B301 - written by this run's own analysis
    nodes, edges = {}, []
    for lang in results.results.values():
        graph = lang.cfg.graph
        for name, n in graph.nodes.items():
            nodes[name] = {
                "file": n.file_path,
                "start": n.line_start,
                "end": n.line_end,
                "callable": bool(n.is_callable()),
                "cb": bool(n.is_callback_or_anonymous()),
            }
        for e in graph.edges:
            sites = []
            for s in vars(e).get("_call_sites") or []:
                v = s if isinstance(s, (tuple, list, dict)) else vars(s) if hasattr(s, "__dict__") else str(s)
                sites.append(list(v) if isinstance(v, tuple) else v)
            edges.append({"s": e.get_source(), "d": e.get_destination(), "sites": sites})
    return {"nodes": nodes, "edges": edges}


def _short(q: str, path: str) -> str:
    """A qualified name as a row writes it: `~.rest` when it is the file's dotted path plus `.rest`, and
    a leading `~` doubled so a name of its own that starts with one reads back unchanged."""
    stem = path.rsplit(".", 1)[0].replace("/", ".")
    if q.startswith(stem + "."):
        return "~" + q[len(stem) :]
    return "~" + q if q.startswith("~") else q


def side_document(analysis: dict, cg: dict) -> dict:
    """One commit's analysis and call graph as the service reads them, each fact once. The service's
    bundle.expand_side turns it back into both, in every field a feature reads.

    - files: per file, rows for the methods_index entries `[name, start, end, type, content_hash, cb]`
      and for call-graph nodes the index lacks `[name, start, end, cb]`. A method and the graph node of
      the same name share a row; cb is -1 for a method that is no graph node. A name under its file's
      dotted path is written `~.rest`. Rows are numbered in order across files; `unplaced` continues the
      numbering for call ends that are no node.
    - calls: per caller, `[callee, line, ...]`, one line per call site. No reader takes the column, and a
      site's file is always its caller's.
    - components: `[id, name, description, parent, members, relations]` in tree order. Members are rows
      (a key where the index lacks the method), or null for a parent holding exactly its children's.
    - The analysis's edges are the calls between two different leaves (a row's leaf is the first one
      listing it), less `hidden_edges`, plus `extra_edges`. On crate that leaves one extra edge of
      149,316: the analysis and the graph repeat the same edges, and only their set is read."""
    index = analysis.get("methods_index") or {}
    files: dict[str, list[list]] = {}
    row_of: dict[str, list] = {}  # methods_index key -> its row
    by_name: dict[str, list[tuple[str, list]]] = {}
    for key, m in index.items():
        path, q = m.get("file_path") or "", m.get("qualified_name") or ""
        if key != f"{path}|{q}":
            raise ValueError(f"methods_index key {key!r} is not its file and qualified name")
        row = [q, m.get("start_line"), m.get("end_line"), m.get("type"), m.get("content_hash"), -1]
        files.setdefault(path, []).append(row)
        row_of[key] = row
        by_name.setdefault(q, []).append((path, row))
    node_row: dict[str, list] = {}
    spans: list[tuple[list, int, int]] = []
    for q, n in (cg.get("nodes") or {}).items():
        same = by_name.get(q) or []
        hit = next((r for p, r in same if p == n.get("file")), same[0][1] if same else None)
        if hit is None:
            hit = [q, n.get("start"), n.get("end"), int(bool(n.get("cb")))]
            files.setdefault(n.get("file") or "", []).append(hit)
        else:
            hit[5] = int(bool(n.get("cb")))
            if (hit[1], hit[2]) != (n.get("start"), n.get("end")):
                spans.append((hit, n.get("start"), n.get("end")))
        node_row[q] = hit
    position: dict[int, int] = {}
    out_files = []
    for path in sorted(files):
        rows = sorted(files[path], key=lambda r: (r[1] is None, r[1] or 0, r[0]))
        for r in rows:
            position[id(r)] = len(position)
        out_files.append([path, [[_short(r[0], path), *r[1:]] for r in rows]])
    at = {q: position[id(r)] for q, r in node_row.items()}
    unplaced = []
    calls: dict[int, list] = {}
    for e in cg.get("edges") or []:
        for q in (e["s"], e["d"]):
            if q not in at:
                at[q] = len(position) + len(unplaced)
                unplaced.append(q)
        lines = [s.get("line") if isinstance(s, dict) else None for s in e.get("sites") or []]
        calls.setdefault(at[e["s"]], []).append([at[e["d"]], *lines])

    def token(key):
        return position[id(row_of[key])] if key in row_of else key

    comps: list[tuple[dict, int]] = []

    def walk(cs, parent):
        for c in cs or []:
            comps.append((c, parent))
            walk(c.get("components"), len(comps) - 1)

    walk(analysis.get("components"), -1)
    children: list[list[int]] = [[] for _ in comps]
    for i, (_, parent) in enumerate(comps):
        if parent >= 0:
            children[parent].append(i)
    members: list[list] = [
        [token(f"{g['file_path']}|{q}") for g in c.get("file_methods") or [] for q in g.get("methods") or []]
        for c, _ in comps
    ]
    implied: list[set] = [set() for _ in comps]
    for i in reversed(range(len(comps))):
        implied[i] = set(members[i]) if not children[i] else set().union(*(implied[k] for k in children[i]))
    relations = lambda rows: [[r.get("src_id"), r.get("dst_id"), r.get("relation")] for r in rows or []]  # noqa: E731
    out_comps = [
        [
            c.get("component_id"),
            c.get("name"),
            c.get("description"),
            parent,
            None if children[i] and set(members[i]) == implied[i] else members[i],
            relations(c.get("components_relations")),
        ]
        for i, (c, parent) in enumerate(comps)
    ]

    leaf: dict[int, int] = {}
    for i, own in enumerate(members):
        if not children[i]:
            for m in own:
                if isinstance(m, int):
                    leaf.setdefault(m, i)
    implied_edges = {
        (s, d) for s, ds in calls.items() for d, *_ in ds if s in leaf and d in leaf and leaf[s] != leaf[d]
    }
    edges: set[tuple] = set()

    def collect_edges(o):
        for r in o.get("components_relations") or []:
            for e in r.get("all_edges") or []:
                if e.get("source") and e.get("target"):
                    edges.add((token(e["source"]), token(e["target"])))
        for c in o.get("components") or []:
            collect_edges(c)

    collect_edges(analysis)
    return {
        "version": 1,
        "files": out_files,
        "unplaced": unplaced,
        "graph_spans": [[position[id(r)], start, end] for r, start, end in spans],
        "components": out_comps,
        "relations": relations(analysis.get("components_relations")),
        "calls": [[s, calls[s]] for s in sorted(calls)],
        "extra_edges": sorted(edges - implied_edges, key=repr),
        "hidden_edges": sorted(implied_edges - edges),
    }


def _diff(repo: str, base: str, head: str) -> list[dict]:
    """The files changed between the two analysed commits, in the shape of a pull request's files
    (the compare API returns the same fields), so a feature reads exactly what was analysed."""
    out = _github_json(f"/repos/{repo}/compare/{base}...{head}")
    return [
        {
            k: f.get(k)
            for k in ("filename", "status", "additions", "deletions", "changes", "patch", "previous_filename")
            if k in f
        }
        for f in out.get("files") or []
    ]


def _contents(repo: str, sha: str, path: str) -> str | None:
    try:
        raw = _github(f"/repos/{repo}/contents/{quote(path)}?ref={sha}", accept="application/vnd.github.raw")
    except HTTPError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def collect(env: dict) -> dict:
    """The required inputs shared by both endpoints."""
    repo, pr = env["REPOSITORY"], int(env["PR_NUMBER"])
    artifact = Path(env["ARTIFACT_DIR"])
    head_dir, base_dir = Path(env["HEAD_ANALYSIS_PATH"]).parent, Path(env["BASE_ANALYSIS_PATH"]).parent
    meta = json.loads((artifact / "metadata.json").read_text())
    head_sha = meta["head_sha"]
    # The analysis compared against the merge base, so that is the base every feature reads too.
    base_sha = meta.get("merge_base_sha") or meta.get("base_sha")
    for p in (head_dir / "static_analysis.pkl", base_dir / "static_analysis.pkl"):
        if not p.is_file():
            raise Skip(f"no call graph at {p}")

    pull = _github_json(f"/repos/{repo}/pulls/{pr}")
    commits = _github_json(f"/repos/{repo}/pulls/{pr}/commits?per_page=100")
    pr_files = _diff(repo, base_sha, head_sha)
    checkout = Path(env["CHECKOUT_DIR"])
    files: dict[str, str] = {}
    for f in pr_files[:MAX_FILES]:
        if f.get("status") != "removed":
            try:
                p = _checkout_file(checkout, f["filename"])
                if p.stat().st_size <= MAX_FILE_BYTES:
                    files[f"{head_sha}:{f['filename']}"] = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        if f.get("status") != "added":
            was = f.get("previous_filename") or f["filename"]
            text = _contents(repo, base_sha, was)
            if text is not None and len(text.encode()) <= MAX_FILE_BYTES:
                files[f"{base_sha}:{was}"] = text

    bundle_meta = {**meta, "base_sha": base_sha, "head_sha": head_sha, "pr_number": str(pr)}

    def side(analysis_path: str, directory: Path) -> str:
        analysis = json.loads(Path(analysis_path).read_text())
        return json.dumps(side_document(analysis, call_graph(directory / "static_analysis.pkl")), separators=(",", ":"))

    inputs = {
        "head.json": side(env["HEAD_ANALYSIS_PATH"], head_dir),
        "base.json": side(env["BASE_ANALYSIS_PATH"], base_dir),
        "metadata.json": json.dumps(bundle_meta),
        "pr.json": json.dumps(
            {
                "title": pull.get("title") or "",
                "body": pull.get("body") or "",
                "headRefOid": head_sha,
                "commits": [(c.get("commit") or {}).get("message", "").split("\n")[0] for c in commits],
            }
        ),
        "pr-files.json": json.dumps(pr_files),
        "files.json": json.dumps(files),
    }
    return inputs


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)  # its dataclass looks its own module up while it is defined
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
    use CodeBoarding's model and settings as they stand. A run's own provider sends its key and model; one the service cannot reach sends its name alone, which
    the service reads as "no model here", so a run that chose its own provider is never sent to ours.
    A base URL elsewhere is a provider the service cannot reach: it calls fixed hosts only."""
    llm = env.get("LLM", "").strip().lower()
    if llm in ("", "hosted", "license"):
        return {}
    key = _api_key(env, llm)
    base = env.get(f"{llm.upper()}_BASE_URL", "").strip()
    host = PROVIDER_HOSTS.get(llm)
    if host and llm == "aws_bedrock":
        host = host.format(region=env.get("AWS_BEDROCK_REGION", "").strip())
    if not key or not host or (base and urlparse(base).hostname != host):
        return {"x-llm-provider": llm}
    out = {"x-llm-api-key": key, "x-llm-provider": llm}
    if env.get(f"{llm.upper()}_REGION", "").strip():
        out["x-llm-region"] = env[f"{llm.upper()}_REGION"].strip()
    # The analysis's own order, so a run's closed features use its analysis model.
    model = env.get("AGENT_MODEL", "").strip() or env.get("MODEL", "").strip()
    if model:
        out["x-llm-model"] = model
    return out


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


def _post(url: str, body: bytes, headers: dict[str, str], timeout: int = 600) -> dict:
    req = Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/x-xz", "User-Agent": "codeboarding-action"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as res:  # nosec B310 - service URL from workflow configuration
            return json.loads(res.read())
    except HTTPError as e:
        reason = f"the service answered {e.code}: {_error_message(e) or e.reason}"
        if e.code == 402:
            raise Skip(reason) from e
        raise RuntimeError(reason) from e


def _compress(body: dict) -> bytes:
    return lzma.compress(json.dumps(body).encode(), preset=9 | lzma.PRESET_EXTREME)


def _pack(ctx: dict, files: dict[str, str], omitted: dict[str, str]) -> bytes:
    body = {"version": 1, "repo": ctx["repo"], "pr": ctx["pr"], "files": files}
    if omitted:
        body["omitted"] = omitted
    return _compress(body)


def _check_size(data: bytes, what: str = "bundle") -> None:
    if len(data) > MAX_BUNDLE_BYTES:
        raise Skip(f"its {what} is {len(data) / 1e6:.1f} MB, over the service's limit")


def run_review_guide(base: str, ctx: dict, inputs: dict, headers: dict, artifact: Path) -> None:
    try:
        if "x-llm-provider" in headers and "x-llm-api-key" not in headers:
            raise Skip("the service cannot call this run's provider")
        data = _pack(ctx, inputs, {})
        _check_size(data)
        answer = _post(f"{base.rstrip('/')}/v1/review-guide", data, headers)
        guide = answer.get("guide")
        if not isinstance(guide, dict):
            raise ValueError("the service returned no guide")
        (artifact / "review_guide.json").write_text(json.dumps(guide, ensure_ascii=False, indent=1))
    except Exception as e:
        print(f"::notice::review-guide {'skipped' if isinstance(e, Skip) else 'failed'}: {e}")


def run_architecture(base: str, ctx: dict, inputs: dict, headers: dict, env: dict) -> dict:
    try:
        try:
            source, why = collect_source(env)
        except Exception as e:
            source, why = None, f"reading it failed: {type(e).__name__}"
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
        answer = _post(f"{base.rstrip('/')}/v1/architecture", data, headers)
        envelope = answer.get("envelope")
        if not isinstance(envelope, dict) or envelope.get("status") not in ("ok", "partial", "skipped", "failed"):
            raise ValueError("the service returned no architecture result")
        return envelope
    except Exception as e:
        return _no_architecture(ctx, "skipped" if isinstance(e, Skip) else "failed", str(e))


def main(env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        artifact = Path(env["ARTIFACT_DIR"])
        meta = json.loads((artifact / "metadata.json").read_text())
        ctx = {"repo": env["REPOSITORY"], "pr": int(env["PR_NUMBER"]), "head": meta.get("head_sha")}
        base = env["CLOSED_FUNCTIONS_URL"]
        if not base.strip():
            return 0
        try:
            headers = {**_bearer(env), **model_headers(env)}
            inputs = collect(env)
        except Exception as e:
            reason = f"collecting inputs or credentials failed: {type(e).__name__}: {e}"
            print(f"::notice::review-guide failed: {reason}")
            architecture = _no_architecture(ctx, "failed", reason)
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                guide = pool.submit(run_review_guide, base, ctx, inputs, headers, artifact)
                result = pool.submit(run_architecture, base, ctx, inputs, headers, env)
                architecture = result.result()
                guide.result()
        path = artifact / "closed" / "architecture.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(architecture, ensure_ascii=False, indent=1))
        if architecture["status"] not in ("ok", "partial"):
            print(f"::notice::architecture {architecture['status']}: {architecture.get('reason')}")
    except Exception as e:  # never fail the review over a closed feature
        print(f"::warning::Running the closed features failed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
