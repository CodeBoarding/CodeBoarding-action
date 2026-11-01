"""Tests for the closed-features step: the bundle it sends, the credentials, one result per feature, and
that it never fails a run."""

from __future__ import annotations

import gzip
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "closed_features.py"
_SPEC = importlib.util.spec_from_file_location("closed_features", _SCRIPT)
assert _SPEC and _SPEC.loader
closed_features = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = closed_features
_SPEC.loader.exec_module(closed_features)


class _Server:
    """A local HTTP server answering every request with one handler."""

    def __init__(self, respond):
        outer = self
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, payload = respond(self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()


def _workspace(tmp: Path) -> dict:
    """A run's state as the step finds it: the artifact folder, both analyses, a head checkout."""
    artifact, head, base, checkout = (tmp / n for n in ("artifact", "head", "base", "checkout"))
    for d in (artifact, head, base, checkout):
        d.mkdir()
    (artifact / "metadata.json").write_text(
        json.dumps({"kind": "review", "head_sha": "H", "base_sha": "TIP", "merge_base_sha": "B", "pr_number": "7"})
    )
    for d in (head, base):
        (d / "analysis.json").write_text(json.dumps({"components": [], "methods_index": {}}))
        (d / "static_analysis.pkl").write_bytes(b"")
    (checkout / "app.py").write_text("def f():\n    return 2\n")
    return {
        "CLOSED_FUNCTIONS_URL": "http://127.0.0.1:9",
        "REPOSITORY": "o/r",
        "PR_NUMBER": "7",
        "ARTIFACT_DIR": str(artifact),
        "HEAD_ANALYSIS_PATH": str(head / "analysis.json"),
        "BASE_ANALYSIS_PATH": str(base / "analysis.json"),
        "CHECKOUT_DIR": str(checkout),
        "GITHUB_TOKEN": "ghs_run",
        "RUNNER_TEMP": str(tmp),
    }


_COMPARE = {
    "files": [
        {"filename": "app.py", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@ -1,2 +1,2 @@"},
        {"filename": "new.py", "status": "added", "additions": 1, "deletions": 0, "patch": "@@ -0,0 +1 @@"},
    ]
}


def _github_json(path: str):
    if path.startswith("/repos/o/r/compare/"):
        return _COMPARE
    if path.endswith("/commits?per_page=100"):
        return [{"commit": {"message": "Return two\n\nbody"}}]
    return {"title": "Two", "body": "Returns two"}


class BundleTests(unittest.TestCase):
    def test_the_bundle_reads_the_analysed_commits_and_both_sides_of_each_file(self):
        with tempfile.TemporaryDirectory() as t:
            env = _workspace(Path(t))
            with (
                mock.patch.object(closed_features, "_github_json", side_effect=_github_json) as gh,
                mock.patch.object(closed_features, "_contents", return_value="def f():\n    return 1\n") as base,
                mock.patch.object(closed_features, "call_graph", return_value={"nodes": {}, "edges": []}),
            ):
                b = closed_features.collect(env)
            # the diff is between the two analysed commits, the merge base not the branch tip
            self.assertIn("/repos/o/r/compare/B...H", [c.args[0] for c in gh.call_args_list])
            files = json.loads(b["files.json"])
            self.assertEqual(files["H:app.py"], "def f():\n    return 2\n")
            self.assertEqual(files["B:app.py"], "def f():\n    return 1\n")
            self.assertNotIn("B:new.py", files)  # an added file has no base side to fetch
            base.assert_called_once_with("o/r", "B", "app.py")
            meta = json.loads(b["metadata.json"])
            self.assertEqual((meta["base_sha"], meta["head_sha"]), ("B", "H"))
            self.assertEqual(json.loads(b["pr.json"])["commits"], ["Return two"])
            self.assertEqual(sorted(b), sorted(closed_features.FALLBACK["needs"]))


class CredentialTests(unittest.TestCase):
    HOSTS = {"openrouter": "openrouter.ai", "ollama": "ollama.com", "anthropic": "api.anthropic.com"}

    def test_without_oidc_the_runs_own_token_proves_the_repository(self):
        self.assertEqual(closed_features._bearer({"GITHUB_TOKEN": "ghs_run"}), {"Authorization": "Bearer ghs_run"})

    def test_with_oidc_the_bearer_is_the_proxys_own_with_the_license(self):
        mint = _Server(lambda path, body: (200, {"value": "jwt.token.sig"}))
        try:
            env = {
                "ACTIONS_ID_TOKEN_REQUEST_URL": f"{mint.url}/token?x=1",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req",
                "LICENSE_KEY": "LIC",
                "GITHUB_TOKEN": "ghs_run",
            }
            with tempfile.TemporaryDirectory() as t:
                env["RUNNER_TEMP"] = t
                headers = closed_features._bearer(env)
                self.assertEqual(list(Path(t).iterdir()), [])  # the license file does not outlive the call
            self.assertEqual(headers, {"Authorization": "Bearer jwt.token.sig~codeboarding-license~LIC"})
            self.assertIn("audience=codeboarding-proxy", mint.requests[0]["path"])
        finally:
            mint.close()

    def test_a_hosted_or_licensed_run_sends_no_model_at_all(self):
        for llm in ("hosted", "license", ""):
            env = {"LLM": llm, "OPENROUTER_API_KEY": "left over"}
            self.assertEqual(closed_features.model_headers(env, self.HOSTS), {})

    def test_a_runs_own_provider_key_and_model_are_used(self):
        env = {"LLM": "anthropic", "ANTHROPIC_API_KEY": " sk-a ", "MODEL": "claude-sonnet-5"}
        self.assertEqual(
            closed_features.model_headers(env, self.HOSTS),
            {"x-llm-api-key": "sk-a", "x-llm-provider": "anthropic", "x-llm-model": "claude-sonnet-5"},
        )

    def test_the_analysis_model_wins_as_it_does_in_the_analysis(self):
        env = {"LLM": "anthropic", "ANTHROPIC_API_KEY": "k", "MODEL": "general", "AGENT_MODEL": "analysis"}
        self.assertEqual(closed_features.model_headers(env, self.HOSTS)["x-llm-model"], "analysis")

    def test_ollama_on_its_cloud_is_reachable_and_a_self_hosted_one_is_not(self):
        cloud = {"LLM": "ollama", "OLLAMA_API_KEY": "k", "OLLAMA_BASE_URL": "https://ollama.com"}
        self.assertEqual(closed_features.model_headers(cloud, self.HOSTS)["x-llm-api-key"], "k")
        own = {**cloud, "OLLAMA_BASE_URL": "http://10.0.0.5:11434"}
        self.assertEqual(closed_features.model_headers(own, self.HOSTS), {"x-llm-provider": "ollama"})

    def test_a_provider_the_service_cannot_call_never_sends_its_key(self):
        env = {"LLM": "litellm", "LITELLM_API_KEY": "k", "LITELLM_BASE_URL": "https://proxy.internal"}
        self.assertEqual(closed_features.model_headers(env, self.HOSTS), {"x-llm-provider": "litellm"})

    def test_a_base_url_elsewhere_keeps_the_key_at_home(self):
        env = {"LLM": "openai", "OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "https://gateway.corp/v1"}
        hosts = {"openai": "api.openai.com"}
        self.assertEqual(closed_features.model_headers(env, hosts), {"x-llm-provider": "openai"})

    def test_bedrock_sends_its_region(self):
        env = {"LLM": "aws_bedrock", "AWS_BEDROCK_API_KEY": "k", "AWS_BEDROCK_REGION": "eu-west-1"}
        hosts = {"aws_bedrock": "bedrock-runtime.{region}.amazonaws.com"}
        self.assertEqual(
            closed_features.model_headers(env, hosts),
            {"x-llm-api-key": "k", "x-llm-provider": "aws_bedrock", "x-llm-region": "eu-west-1"},
        )


GUIDE = {
    "name": "review-guide",
    "schema": 1,
    "url": "/v1/features/review-guide",
    "needs": ["pr.json"],
    "output": "closed/review-guide.json",
    "dataOutput": "review_guide.json",
    "enabled": True,
}
OTHER = {
    "name": "other",
    "schema": 2,
    "url": "/v1/features/other",
    "needs": ["analysis.json", "pr-files.json"],
    "output": "closed/other.json",
    "dataOutput": None,
    "enabled": True,
}


def _envelope(name, data, status="ok"):
    return {
        "feature": name,
        "schema": 1,
        "status": status,
        "repo": "o/r",
        "pr": 7,
        "headSha": "H",
        "generator": {"model": "m", "hosted": True, "version": "v"},
        "coverage": {"languages": ["Python"], "files": {"seen": 2, "total": 2}},
        "data": data,
    }


def _run(env):
    with (
        mock.patch.object(closed_features, "_github_json", side_effect=_github_json),
        mock.patch.object(closed_features, "_contents", return_value="x"),
        mock.patch.object(closed_features, "call_graph", return_value={"nodes": {}, "edges": []}),
        mock.patch("builtins.print") as out,
    ):
        code = closed_features.main(env)
    return code, [" ".join(map(str, c.args)) for c in out.call_args_list]


def _read(env, name):
    return json.loads((Path(env["ARTIFACT_DIR"]) / name).read_text())


class RunnerTests(unittest.TestCase):
    def _serve(self, features, answer):
        def respond(path, body):
            if path.startswith("/v1/features?"):
                return features if isinstance(features, tuple) else (200, {"version": 1, "features": features})
            return answer(path, body)

        service = _Server(respond)
        self.addCleanup(service.close)
        return service

    def test_each_feature_gets_its_own_request_with_only_the_inputs_it_names(self):
        guide = {"version": 1, "changes": [{"id": "u1"}]}

        def answer(path, body):
            name = path.rsplit("/", 1)[-1]
            data = guide if name == "review-guide" else {"findings": []}
            return 200, {"envelope": _envelope(name, data), "usage": {"model": "m"}}

        service = self._serve([GUIDE, OTHER], answer)
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            self.assertEqual(_run(env)[0], 0)
            self.assertEqual(_read(env, "review_guide.json"), guide)
            self.assertEqual(_read(env, "closed/review-guide.json")["data"], guide)
            self.assertEqual(_read(env, "closed/other.json")["status"], "ok")
            self.assertFalse((Path(env["ARTIFACT_DIR"]) / "other.json").exists())
        posts = {r["path"]: json.loads(gzip.decompress(r["body"])) for r in service.requests if r["body"]}
        self.assertEqual(sorted(posts["/v1/features/review-guide"]["files"]), ["pr.json"])
        self.assertEqual(sorted(posts["/v1/features/other"]["files"]), ["analysis.json", "pr-files.json"])
        self.assertEqual(posts["/v1/features/other"]["pr"], 7)

    def test_a_feature_that_fails_is_written_as_failed_and_costs_its_neighbour_nothing(self):
        guide = {"version": 1, "changes": []}

        def answer(path, body):
            if path.endswith("/other"):
                return 500, {"error": "failed", "message": "KeyError: x"}
            return 200, {"envelope": _envelope("review-guide", guide), "usage": {}}

        service = self._serve([GUIDE, OTHER], answer)
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            code, printed = _run(env)
            self.assertEqual(code, 0)
            self.assertEqual(_read(env, "review_guide.json"), guide)
            failed = _read(env, "closed/other.json")
        self.assertEqual((failed["status"], failed["data"], failed["headSha"]), ("failed", None, "H"))
        self.assertIn("500", failed["reason"])
        self.assertTrue(any("other failed" in line for line in printed))

    def test_a_spent_bucket_is_skipped_not_failed(self):
        service = self._serve(
            [OTHER], lambda p, b: (402, {"error": "quota", "message": "Resource exhausted: token limit reached"})
        )
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            _run(env)
            out = _read(env, "closed/other.json")
        self.assertEqual(out["status"], "skipped")
        self.assertIn("token limit", out["reason"])

    def test_a_turned_off_feature_is_skipped_and_never_requested(self):
        service = self._serve([{**OTHER, "enabled": False}], lambda p, b: (500, {}))
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            _run(env)
            self.assertEqual(_read(env, "closed/other.json")["status"], "skipped")
        self.assertEqual([r["path"] for r in service.requests], ["/v1/features?repo=o/r"])
        self.assertEqual(service.requests[0]["headers"]["Authorization"], "Bearer ghs_run")

    def test_a_feature_needing_what_this_action_does_not_collect_is_skipped(self):
        wants = {**OTHER, "needs": ["whole-repo.tar"]}
        service = self._serve([wants], lambda p, b: (500, {}))
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            _run(env)
            out = _read(env, "closed/other.json")
        self.assertEqual(out["status"], "skipped")
        self.assertIn("whole-repo.tar", out["reason"])

    def test_a_service_with_no_manifest_still_gets_asked_for_the_guide_alone(self):
        guide = {"version": 1, "repo": "o/r", "pr": 7, "themes": [], "changes": [{"id": "u1"}]}
        usage = {"provider": "ollama", "model": "m", "hosted": True}
        service = self._serve((404, {"error": "not_found"}), lambda p, b: (200, {"guide": guide, "usage": usage}))
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            _run(env)
            written = (Path(env["ARTIFACT_DIR"]) / "review_guide.json").read_text()
            wrapped = _read(env, "closed/review-guide.json")
        # byte for byte what the step has always written
        self.assertEqual(written, json.dumps(guide, ensure_ascii=False, indent=1))
        self.assertEqual((wrapped["status"], wrapped["data"]), ("ok", guide))
        sent = service.requests[-1]
        self.assertEqual(sent["path"], "/v1/review-guide")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer ghs_run")
        self.assertEqual(sorted(json.loads(gzip.decompress(sent["body"]))["files"]), sorted(GUIDE_INPUTS))

    def test_a_missing_call_graph_is_recorded_for_every_feature_and_never_fails_the_run(self):
        service = self._serve([GUIDE, OTHER], lambda p, b: (500, {}))
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            (Path(env["HEAD_ANALYSIS_PATH"]).parent / "static_analysis.pkl").unlink()
            self.assertEqual(_run(env)[0], 0)
            self.assertFalse((Path(env["ARTIFACT_DIR"]) / "review_guide.json").exists())
            for name in ("closed/review-guide.json", "closed/other.json"):
                self.assertEqual(_read(env, name)["status"], "failed")

    def test_a_path_outside_the_artifact_is_refused(self):
        escape = {**OTHER, "output": "../outside.json"}
        answer = lambda p, b: (200, {"envelope": _envelope("other", {}), "usage": {}})  # noqa: E731
        service = self._serve([escape], answer)
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), "CLOSED_FUNCTIONS_URL": service.url}
            self.assertEqual(_run(env)[0], 0)
            self.assertFalse((Path(t) / "outside.json").exists())


class OwnProviderTests(RunnerTests):
    def test_a_provider_the_service_cannot_call_gets_no_guide_and_no_hosted_model(self):
        env_keys = {"LLM": "litellm", "LITELLM_API_KEY": "k", "LITELLM_BASE_URL": "http://proxy:4000"}
        guide = {**GUIDE, "needsModel": True}
        other = {**OTHER, "needsModel": False}
        service = self._serve([guide, other], lambda p, b: (200, {"envelope": _envelope("other", {}), "usage": {}}))
        with tempfile.TemporaryDirectory() as t:
            env = {**_workspace(Path(t)), **env_keys, "CLOSED_FUNCTIONS_URL": service.url}
            _, printed = _run(env)
            self.assertEqual(_read(env, "closed/review-guide.json")["status"], "skipped")
            self.assertFalse((Path(env["ARTIFACT_DIR"]) / "review_guide.json").exists())
            self.assertEqual(_read(env, "closed/other.json")["status"], "ok")
        posts = [r for r in service.requests if r["body"]]
        self.assertEqual([r["path"] for r in posts], ["/v1/features/other"])
        lower = {k.lower(): v for k, v in posts[0]["headers"].items()}
        self.assertEqual(lower["x-llm-provider"], "litellm")
        self.assertNotIn("x-llm-api-key", lower)
        self.assertEqual(sum("cannot call this run's provider" in line for line in printed), 2)

    def test_with_no_manifest_a_run_on_its_own_provider_still_never_reaches_the_hosted_model(self):
        service = self._serve((404, {}), lambda p, b: (200, {"guide": {"version": 1}, "usage": {}}))
        with tempfile.TemporaryDirectory() as t:
            env = {
                **_workspace(Path(t)),
                "LLM": "anthropic",
                "ANTHROPIC_API_KEY": "k",
                "CLOSED_FUNCTIONS_URL": service.url,
            }
            _run(env)
            self.assertEqual(_read(env, "closed/review-guide.json")["status"], "skipped")
        self.assertEqual([r for r in service.requests if r["body"]], [])


class SourceTests(unittest.TestCase):
    SPEC = {
        "input": "source.json",
        "extensions": [".py"],
        "exclude": r"(^|/)tests/",
        "maxFileBytes": 1000,
        "maxBytes": 5000,
    }

    def _repo(self, tmp: Path) -> Path:
        (tmp / "tests").mkdir()
        (tmp / "a.py").write_text("x = 1\n")
        (tmp / "big.py").write_text("x" * 2000)
        (tmp / "b.txt").write_text("not asked for")
        (tmp / "tests" / "t.py").write_text("excluded")
        (tmp / "untracked.py").write_text("not tracked")
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        subprocess.run(["git", "-C", str(tmp), "add", "a.py", "big.py", "b.txt", "tests"], check=True)
        return tmp

    def test_only_the_tracked_files_a_feature_asked_for_are_sent(self):
        with tempfile.TemporaryDirectory() as t:
            text, why = closed_features.collect_source(self.SPEC, {"CHECKOUT_DIR": str(self._repo(Path(t)))})
        self.assertEqual((json.loads(text), why), ({"a.py": "x = 1\n"}, None))

    def test_the_workflow_can_turn_it_off(self):
        out = closed_features.collect_source(self.SPEC, {"CLOSED_FEATURES_SOURCE": "false", "CHECKOUT_DIR": "/nowhere"})
        self.assertEqual(out, (None, "turned off by the workflow"))

    def test_past_the_cap_nothing_is_sent_and_it_says_why(self):
        with tempfile.TemporaryDirectory() as t:
            text, why = closed_features.collect_source(
                {**self.SPEC, "maxBytes": 3}, {"CHECKOUT_DIR": str(self._repo(Path(t)))}
            )
        self.assertIsNone(text)
        self.assertIn("limit", why)

    def _sent(self, source, inputs=None):
        feature = {**OTHER, "source": self.SPEC, "needs": ["pr.json"]}
        service = _Server(lambda p, b: (200, {"envelope": _envelope("other", {}), "usage": {}}))
        self.addCleanup(service.close)
        ctx = {"repo": "o/r", "pr": 7, "head": "H"}
        with mock.patch("builtins.print"):
            closed_features.run_feature(feature, service.url, ctx, inputs or {"pr.json": "{}"}, {}, source)
        return json.loads(gzip.decompress(service.requests[0]["body"]))

    def test_the_source_goes_only_in_the_request_of_the_feature_that_asked(self):
        sent = self._sent(('{"a.py": "x"}', None))
        self.assertEqual(sent["files"]["source.json"], '{"a.py": "x"}')
        service = _Server(lambda p, b: (200, {"envelope": _envelope("other", {}), "usage": {}}))
        self.addCleanup(service.close)
        with mock.patch("builtins.print"):
            closed_features.run_feature(
                {**GUIDE, "url": "/g"}, service.url, {"repo": "o/r", "pr": 7, "head": "H"}, {"pr.json": "{}"}, {}, None
            )
        self.assertNotIn("source.json", json.loads(gzip.decompress(service.requests[0]["body"]))["files"])

    def test_a_feature_without_its_source_is_told_why(self):
        sent = self._sent((None, "turned off by the workflow"))
        self.assertNotIn("source.json", sent["files"])
        self.assertEqual(sent["omitted"], {"source.json": "turned off by the workflow"})

    def test_a_bundle_too_large_to_send_drops_the_source_and_keeps_the_feature(self):
        import os

        huge = os.urandom(5_000_000).hex()
        sent = self._sent((json.dumps({"a.py": huge}), None))
        self.assertNotIn("source.json", sent["files"])
        self.assertIn("too large", sent["omitted"]["source.json"])


GUIDE_INPUTS = closed_features.FALLBACK["needs"]


if __name__ == "__main__":
    unittest.main()
