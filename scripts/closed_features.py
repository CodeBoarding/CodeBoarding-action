#!/usr/bin/env python3
"""Call the review-guide and architecture endpoints independently after the review analysis.

Both requests carry the same analyses, call graphs, PR metadata and changed files. Only architecture
carries optional source. Store the legacy guide in review_guide.json and the unchanged architecture
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

import gzip
import importlib.util
import json
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

# A Function URL takes 6 MB, measured after base64, and a gzipped body goes through base64 on the way
# in: 4.5 MB of bundle arrives, and 5 MB is refused with a 413.
MAX_BUNDLE_BYTES = 4_300_000
# Changed files whose contents are sent at both commits; past this a feature reads diffs only.
MAX_FILES = 300
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
MAX_SOURCE_FILE_BYTES = 300_000
MAX_SOURCE_BYTES = 16_000_000


class Skip(Exception):
    """A step that did not happen, for a reason worth one line in the log."""


def _github(path: str, accept: str = "application/vnd.github+json") -> bytes:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    req = Request(
        f"{api}{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": accept,
            "User-Agent": "codeboarding-action",
        },
    )
    with urlopen(req, timeout=30) as res:  # nosec B310 - GitHub API URL
        return res.read()


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
                files[f"{head_sha}:{f['filename']}"] = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        if f.get("status") != "added":
            was = f.get("previous_filename") or f["filename"]
            text = _contents(repo, base_sha, was)
            if text is not None:
                files[f"{base_sha}:{was}"] = text

    bundle_meta = {**meta, "base_sha": base_sha, "head_sha": head_sha, "pr_number": str(pr)}
    inputs = {
        "analysis.json": Path(env["HEAD_ANALYSIS_PATH"]).read_text(),
        "base/analysis.json": Path(env["BASE_ANALYSIS_PATH"]).read_text(),
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
        "head-cg.json": json.dumps(call_graph(head_dir / "static_analysis.pkl")),
        "base-cg.json": json.dumps(call_graph(base_dir / "static_analysis.pkl")),
        "files.json": json.dumps(files),
    }
    return inputs


def _relay():
    spec = importlib.util.spec_from_file_location("oidc_relay", Path(__file__).with_name("oidc_relay.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)  # its dataclass looks its own module up while it is defined
    spec.loader.exec_module(sys.modules[spec.name])
    return sys.modules[spec.name]


def model_headers(env: dict) -> dict[str, str]:
    """What tells the service which model this run uses. Nothing for a hosted or licensed run, which
    use CodeBoarding's model and settings as they stand. A run's own provider sends its key and model; one the service cannot reach sends its name alone, which
    the service reads as "no model here", so a run that chose its own provider is never sent to ours.
    A base URL elsewhere is a provider the service cannot reach: it calls fixed hosts only."""
    llm = env.get("LLM", "").strip().lower()
    if llm in ("", "hosted", "license"):
        return {}
    key = env.get(f"{llm.upper()}_API_KEY", "").strip()
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
            if size > MAX_SOURCE_FILE_BYTES or not path.is_file():
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


def _post(url: str, body: bytes, headers: dict[str, str]) -> dict:
    req = Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/gzip", "User-Agent": "codeboarding-action"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=600) as res:  # nosec B310 - service URL from workflow configuration
            return json.loads(res.read())
    except HTTPError as e:
        try:
            message = json.loads(e.read()).get("message") or e.reason
        except (ValueError, AttributeError):
            message = e.reason
        reason = f"the service answered {e.code}: {message}"
        if e.code == 402:
            raise Skip(reason) from e
        raise RuntimeError(reason) from e


def _pack(ctx: dict, files: dict[str, str], omitted: dict[str, str]) -> bytes:
    body = {"version": 1, "repo": ctx["repo"], "pr": ctx["pr"], "files": files}
    if omitted:
        body["omitted"] = omitted
    return gzip.compress(json.dumps(body).encode(), compresslevel=9)


def _check_size(data: bytes) -> None:
    if len(data) > MAX_BUNDLE_BYTES:
        raise Skip(f"its bundle is {len(data) / 1e6:.1f} MB, over the service's limit")


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
