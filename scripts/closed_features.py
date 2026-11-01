#!/usr/bin/env python3
"""Run CodeBoarding's closed features on this pull request and keep what each one answers in the
review artifact.

Runs after the review analysis. It asks the service which features exist (``GET /v1/features``),
packs what the run already has once (both analyses, both call graphs from the ``static_analysis.pkl``
beside each ``analysis.json``, the head checkout, the diff between the two analysed commits), and
posts one request per feature in parallel, each with the inputs that feature names. Every feature
gets a file in the artifact whatever happens to it: its answer, or an envelope saying it was skipped
or failed, so a reader can tell "not checked" from "nothing found".

Nothing here knows what a feature does. A feature's code is CodeBoarding's and stays on its side, and
a new one needs no release of this Action unless it asks for an input this script does not collect.
The one exception is ``FALLBACK``: a service that cannot name its features still gets asked for the
review guide, because that is what every service since this Action's first release can answer.

A feature can ask for the repository's source. Only that feature's request carries it, and past the
size the service names, or with ``closed_features_source: false`` in the workflow, the request goes
without it and says why, so the feature reports what it could not check. This script reads the files
and sends them; it computes nothing from them.

Credentials follow the analysis: a run that chose its own provider uses its own key and model for
every feature, and a hosted or licensed run uses CodeBoarding's hosted model. A run whose provider the
service cannot call (a self-hosted endpoint, Bedrock) makes no model call at all: its features report
what they can without one, and it never falls back to the hosted model. Either way the service needs
to know the caller is this repository's run: a fresh GitHub Actions OIDC token (plus the license, the
same bearer the hosted proxy gets), or the run's GITHUB_TOKEN when the workflow cannot mint one.
Nothing here fails the run.

Environment: CLOSED_FUNCTIONS_URL, REPOSITORY, PR_NUMBER, ARTIFACT_DIR, HEAD_ANALYSIS_PATH,
BASE_ANALYSIS_PATH, CHECKOUT_DIR, GITHUB_TOKEN, GITHUB_API_URL, and optionally LICENSE_KEY,
CLOSED_FEATURES_SOURCE, LLM, MODEL, <PROVIDER>_API_KEY, <PROVIDER>_BASE_URL,
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
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

# A Function URL takes 6 MB, measured after base64, and a gzipped body goes through base64 on the way
# in: 4.5 MB of bundle arrives, and 5 MB is refused with a 413.
MAX_BUNDLE_BYTES = 4_300_000
# Changed files whose contents are sent at both commits; past this a feature reads diffs only.
MAX_FILES = 300
# The providers a service from before the manifest can call, each with the host it calls.
FALLBACK_HOSTS = {"ollama": "ollama.com", "openrouter": "openrouter.ai", "openai": "api.openai.com"}
# What the review guide has always been: asked for when the service lists no features.
FALLBACK = {
    "name": "review-guide",
    "schema": 1,
    "url": "/v1/review-guide",
    "needs": [
        "analysis.json",
        "base/analysis.json",
        "metadata.json",
        "pr.json",
        "pr-files.json",
        "head-cg.json",
        "base-cg.json",
        "files.json",
    ],
    "output": "closed/review-guide.json",
    "dataOutput": "review_guide.json",
    "enabled": True,
}


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
    """Everything a feature can ask for, as the service's bundle format names it."""
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
    return inputs


def _relay():
    spec = importlib.util.spec_from_file_location("oidc_relay", Path(__file__).with_name("oidc_relay.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)  # its dataclass looks its own module up while it is defined
    spec.loader.exec_module(sys.modules[spec.name])
    return sys.modules[spec.name]


def model_headers(env: dict, hosts: dict[str, str]) -> dict[str, str]:
    """What tells the service which model this run uses. Nothing for a hosted or licensed run. A run's
    own provider sends its key and model; one the service cannot reach sends its name alone, which
    the service reads as "no model here", so a run that chose its own provider is never sent to ours.
    A base URL elsewhere is a provider the service cannot reach: it calls fixed hosts only."""
    llm = env.get("LLM", "").strip().lower()
    if llm in ("", "hosted", "license"):
        return {}
    key = env.get(f"{llm.upper()}_API_KEY", "").strip()
    base = env.get(f"{llm.upper()}_BASE_URL", "").strip()
    host = hosts.get(llm)
    if not key or not host or (base and urlparse(base).hostname != host):
        return {"x-llm-provider": llm}
    out = {"x-llm-api-key": key, "x-llm-provider": llm}
    if env.get(f"{llm.upper()}_REGION", "").strip():
        out["x-llm-region"] = env[f"{llm.upper()}_REGION"].strip()
    if env.get("MODEL", "").strip():
        out["x-llm-model"] = env["MODEL"].strip()
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


def collect_source(spec: dict, env: dict) -> tuple[str | None, str | None]:
    """The tracked files a feature asked for, as one JSON document, or why there is none."""
    if env.get("CLOSED_FEATURES_SOURCE", "true").strip().lower() == "false":
        return None, "turned off by the workflow"
    checkout = Path(env["CHECKOUT_DIR"])
    listed = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z"], capture_output=True, check=True).stdout
    extensions = tuple(spec.get("extensions") or ())
    exclude = re.compile(spec["exclude"]) if spec.get("exclude") else None
    limit, total = spec.get("maxBytes") or 0, 0
    files: dict[str, str] = {}
    for name in listed.decode("utf-8", "replace").split("\0"):
        if not name or not name.endswith(extensions) or (exclude and exclude.search(name)):
            continue
        path = checkout / name
        try:
            size = path.stat().st_size
            if size > (spec.get("maxFileBytes") or size) or not path.is_file():
                continue
            files[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        total += size
        if limit and total > limit:
            return None, f"its source is over the {limit / 1e6:.0f} MB limit"
    return json.dumps(files), None


def read_manifest(url: str, headers: dict[str, str], repo: str) -> tuple[list[dict], dict[str, str]]:
    """What the service runs and which providers it can call, or the guide alone when it cannot say.
    A feature that cannot be read is left out rather than guessed at; a service that names none is
    treated as one that failed."""
    address = f"{url.rstrip('/')}/v1/features?repo={quote(repo, safe='/')}"
    try:
        req = Request(address, headers={**headers, "User-Agent": "codeboarding-action"})
        with urlopen(req, timeout=15) as res:  # nosec B310 - the service URL the workflow names
            manifest = json.loads(res.read())
        features = manifest.get("features")
    except (OSError, ValueError, AttributeError) as e:
        print(f"::notice::The service listed no features ({type(e).__name__}); asking for the review guide alone.")
        return [FALLBACK], FALLBACK_HOSTS
    usable = [
        f
        for f in features or []
        if isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("output"), str)
    ]
    hosts = manifest.get("providers")
    return (usable, hosts if isinstance(hosts, dict) else {}) if usable else ([FALLBACK], FALLBACK_HOSTS)


def _in_artifact(artifact: Path, relative: str) -> Path:
    """A path the service named, held inside the artifact."""
    path = (artifact / relative).resolve()
    if not path.is_relative_to(artifact.resolve()):
        raise Skip(f"the service named a path outside the artifact: {relative}")
    return path


def _write(artifact: Path, feature: dict, env_out: dict) -> None:
    path = _in_artifact(artifact, feature["output"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(env_out, ensure_ascii=False, indent=1))
    # A reader from before the envelope takes the bare data, and only a good result.
    plain = feature.get("dataOutput")
    if plain and env_out.get("status") in ("ok", "partial") and env_out.get("data") is not None:
        _in_artifact(artifact, plain).write_text(json.dumps(env_out["data"], ensure_ascii=False, indent=1))


def _no_result(feature: dict, ctx: dict, status: str, reason: str) -> dict:
    """The envelope of a feature that never answered."""
    return {
        "feature": feature["name"],
        "schema": feature.get("schema"),
        "status": status,
        "repo": ctx["repo"],
        "pr": ctx["pr"],
        "headSha": ctx["head"],
        "generator": None,
        "coverage": None,
        "data": None,
        "reason": reason,
    }


def _answered(feature: dict, ctx: dict, answer: dict) -> dict:
    env_out = answer.get("envelope")
    if isinstance(env_out, dict) and env_out.get("status"):
        return env_out
    # A service from before the manifest answers the bare guide.
    usage = answer.get("usage") or {}
    if isinstance(answer.get("guide"), dict):
        return {
            **_no_result(feature, ctx, "ok", ""),
            "generator": {"model": usage.get("model"), "hosted": bool(usage.get("hosted")), "version": None},
            "data": answer["guide"],
        }
    return _no_result(feature, ctx, "failed", "the service returned no result")


def _post(url: str, body: bytes, headers: dict[str, str]) -> dict:
    req = Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/gzip", "User-Agent": "codeboarding-action"},
        method="POST",
    )
    with urlopen(req, timeout=600) as res:  # nosec B310 - a URL the workflow's service named
        return json.loads(res.read())


def _pack(ctx: dict, files: dict[str, str], omitted: dict[str, str]) -> bytes:
    body = {"version": 1, "repo": ctx["repo"], "pr": ctx["pr"], "files": files}
    if omitted:
        body["omitted"] = omitted
    return gzip.compress(json.dumps(body).encode(), compresslevel=9)


def run_feature(
    feature: dict,
    base: str,
    ctx: dict,
    inputs: dict[str, str],
    headers: dict[str, str],
    source: tuple[str | None, str | None] | None = None,
) -> dict:
    """One feature, start to finish: never raises, always an envelope."""
    if "x-llm-provider" in headers and "x-llm-api-key" not in headers and feature.get("needsModel", True):
        return _no_result(feature, ctx, "skipped", "it needs a model, and the service cannot call this run's provider")
    needs = feature.get("needs") or []
    missing = [n for n in needs if n not in inputs]
    if missing:
        return _no_result(feature, ctx, "skipped", f"this Action does not collect {', '.join(missing)}")
    url = urljoin(f"{base.rstrip('/')}/", (feature.get("url") or "").lstrip("/"))
    if not url.startswith((base.rstrip("/"), "https://")):
        return _no_result(feature, ctx, "failed", "the service named an address that is not https")
    files = {n: inputs[n] for n in needs}
    spec = feature.get("source") or None
    omitted: dict[str, str] = {}
    if spec:
        text, why = source if source else (None, "the source was not collected")
        if text is None:
            omitted[spec["input"]] = why or "the source was not collected"
        else:
            files[spec["input"]] = text
    data = _pack(ctx, files, omitted)
    if spec and spec["input"] in files and len(data) > MAX_BUNDLE_BYTES:
        # The source is the one optional part: past the limit the feature runs without it.
        del files[spec["input"]]
        omitted[spec["input"]] = "the repository is too large to send"
        data = _pack(ctx, files, omitted)
    if len(data) > MAX_BUNDLE_BYTES:
        return _no_result(feature, ctx, "skipped", f"its bundle is {len(data) / 1e6:.1f} MB, over the service's limit")
    try:
        answer = _post(url, data, headers)
    except HTTPError as e:
        try:
            message = json.loads(e.read()).get("message") or e.reason
        except ValueError:
            message = e.reason
        # 402 is the owner's weekly bucket: nothing went wrong, and nothing was checked.
        return _no_result(
            feature, ctx, "skipped" if e.code == 402 else "failed", f"the service answered {e.code}: {message}"
        )
    except (OSError, ValueError) as e:
        return _no_result(feature, ctx, "failed", f"the service did not answer: {type(e).__name__}")
    out = _answered(feature, ctx, answer)
    usage = answer.get("usage") or {}
    print(
        f"{feature['name']}: {out.get('status')}; {usage.get('provider')} {usage.get('model')}, "
        f"{usage.get('promptTokens', 0) + usage.get('outputTokens', 0)} tokens "
        f"({'hosted' if usage.get('hosted') else 'own key'}); bundle {len(data) / 1e6:.2f} MB"
    )
    return out


def _source_of(feature: dict, env: dict) -> tuple[str | None, str | None]:
    try:
        return collect_source(feature["source"], env)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, f"reading it failed: {type(e).__name__}"


def main(env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        artifact = Path(env["ARTIFACT_DIR"])
        meta = json.loads((artifact / "metadata.json").read_text())
        ctx = {"repo": env["REPOSITORY"], "pr": int(env["PR_NUMBER"]), "head": meta.get("head_sha")}
        base = env["CLOSED_FUNCTIONS_URL"]
        try:
            bearer = _bearer(env)
            features, hosts = read_manifest(base, bearer, ctx["repo"])
        except Exception as e:  # the caller could not be proved, so nothing can be asked for
            print(f"::notice::Could not identify this run to the service: {type(e).__name__}")
            bearer, features, hosts = None, [FALLBACK], FALLBACK_HOSTS
        results: dict[str, dict] = {
            f["name"]: _no_result(f, ctx, "skipped", "turned off by the service")
            for f in features
            if not f.get("enabled") or not f.get("url")
        }
        enabled = [f for f in features if f["name"] not in results]
        if enabled:
            try:
                if bearer is None:
                    raise Skip("this run could not be identified to the service")
                inputs = collect(env)
                headers = {**bearer, **model_headers(env, hosts)}
                if "x-llm-provider" in headers and "x-llm-api-key" not in headers:
                    print(
                        f"::notice::The service cannot call this run's provider ({headers['x-llm-provider']}), "
                        "so it makes no model call: measured findings only, and no review guide."
                    )
            except Exception as e:  # every feature reads this, so every feature is told why
                reason = str(e) if isinstance(e, Skip) else f"collecting its inputs failed: {type(e).__name__}: {e}"
                results.update({f["name"]: _no_result(f, ctx, "failed", reason) for f in enabled})
                enabled = []
            sources = {f["name"]: _source_of(f, env) for f in enabled if f.get("source")}
            with ThreadPoolExecutor(max_workers=max(1, len(enabled))) as pool:
                done = pool.map(lambda f: run_feature(f, base, ctx, inputs, headers, sources.get(f["name"])), enabled)
                results.update({f["name"]: r for f, r in zip(enabled, done)})
        for f in features:
            out = results[f["name"]]
            _write(artifact, f, out)
            if out["status"] not in ("ok", "partial"):
                print(f"::notice::{f['name']} {out['status']}: {out.get('reason')}")
    except Exception as e:  # never fail the review over a closed feature
        print(f"::warning::Running the closed features failed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
