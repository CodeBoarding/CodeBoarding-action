#!/usr/bin/env python3
"""Ask CodeBoarding's closed functions for this pull request's review guide, and keep it in the
review artifact as ``review_guide.json``.

Runs after the review analysis, from what the run already has: both analyses and both call graphs
(the ``static_analysis.pkl`` beside each ``analysis.json``), the head checkout, and the diff between
the two analysed commits. It packs them into one gzipped bundle, posts it, and writes the guide the
service returns next to ``analysis.json``, where the webview reads it. Nothing here builds the guide
itself; that code is CodeBoarding's and stays on its side.

Credentials follow the analysis: the run's own Ollama, OpenRouter or OpenAI key when it has one,
otherwise CodeBoarding's hosted model. Either way the service needs to know the caller is this
repository's run: a fresh GitHub Actions OIDC token (plus the license, the same bearer the hosted
proxy gets), or the run's GITHUB_TOKEN when the workflow cannot mint one. A failure leaves the
artifact without a guide and never fails the run.

Environment: REVIEW_GUIDE_URL, REPOSITORY, PR_NUMBER, ARTIFACT_DIR, HEAD_ANALYSIS_PATH,
BASE_ANALYSIS_PATH, CHECKOUT_DIR, GITHUB_TOKEN, GITHUB_API_URL, and optionally LICENSE_KEY,
OLLAMA_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY, ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import pickle
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# The service's Function URL takes at most 6 MB; the bundle is gzipped JSON, sent as is.
MAX_BUNDLE_BYTES = 5_800_000
# Changed files whose contents are sent at both commits; past this the guide reads diffs only.
MAX_FILES = 300
OWN_KEYS = (("OLLAMA_API_KEY", "ollama"), ("OPENROUTER_API_KEY", "openrouter"), ("OPENAI_API_KEY", "openai"))


class Skip(Exception):
    """This run gets no guide, for a reason worth one line in the log."""


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
    (the compare API returns the same fields), so the guide reads exactly what was analysed."""
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


def bundle(env: dict) -> dict:
    """Everything the guide is built from, as the service's bundle format names it."""
    repo, pr = env["REPOSITORY"], int(env["PR_NUMBER"])
    artifact = Path(env["ARTIFACT_DIR"])
    head_dir, base_dir = Path(env["HEAD_ANALYSIS_PATH"]).parent, Path(env["BASE_ANALYSIS_PATH"]).parent
    meta = json.loads((artifact / "metadata.json").read_text())
    head_sha = meta["head_sha"]
    # The analysis compared against the merge base, so that is the base the guide reads too.
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
            p = checkout / f["filename"]
            try:
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
    return {"version": 1, "repo": repo, "pr": pr, "files": inputs}


def _relay():
    spec = importlib.util.spec_from_file_location("oidc_relay", Path(__file__).with_name("oidc_relay.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)  # its dataclass looks its own module up while it is defined
    spec.loader.exec_module(sys.modules[spec.name])
    return sys.modules[spec.name]


def credentials(env: dict) -> dict[str, str]:
    """The request headers: the run's own key for a provider the service speaks, else the hosted
    model's bearer, the same one the hosted proxy gets (a fresh OIDC token, plus the license)."""
    for name, provider in OWN_KEYS:
        if env.get(name, "").strip():
            # The service still checks the caller is this repository's run.
            return {**_bearer(env), "x-llm-api-key": env[name].strip(), "x-llm-provider": provider}
    return _bearer(env)


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


def request_guide(url: str, body: bytes, headers: dict[str, str]) -> dict:
    req = Request(
        f"{url.rstrip('/')}/v1/review-guide",
        data=body,
        headers={**headers, "Content-Type": "application/gzip", "User-Agent": "codeboarding-action"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=600) as res:  # nosec B310 - the service URL the workflow names
            return json.loads(res.read())
    except HTTPError as e:
        try:
            message = json.loads(e.read()).get("message") or e.reason
        except ValueError:
            message = e.reason
        raise Skip(f"the guide service answered {e.code}: {message}") from e
    except (URLError, TimeoutError) as e:
        raise Skip(f"the guide service did not answer: {type(e).__name__}") from e


def main(env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        data = gzip.compress(json.dumps(bundle(env)).encode(), compresslevel=9)
        if len(data) > MAX_BUNDLE_BYTES:
            raise Skip(f"the bundle is {len(data) / 1e6:.1f} MB, over the service's limit")
        answer = request_guide(env["REVIEW_GUIDE_URL"], data, credentials(env))
        guide = answer.get("guide")
        if not isinstance(guide, dict):
            raise Skip("the guide service returned no guide")
        out = Path(env["ARTIFACT_DIR"]) / "review_guide.json"
        out.write_text(json.dumps(guide, ensure_ascii=False, indent=1))
        usage = answer.get("usage") or {}
        print(
            f"Review guide: {len(guide.get('changes') or [])} changes in {len(guide.get('themes') or [])} main changes; "
            f"{usage.get('provider')} {usage.get('model')}, {usage.get('promptTokens', 0) + usage.get('outputTokens', 0)} tokens "
            f"({'hosted' if usage.get('hosted') else 'own key'}); bundle {len(data) / 1e6:.2f} MB"
        )
    except Skip as e:
        print(f"::notice::No review guide for this run: {e}")
    except Exception as e:  # never fail the review over its guide
        print(f"::warning::Building the review guide failed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
