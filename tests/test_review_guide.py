"""Tests for the review-guide step: the bundle it sends, the credentials, and that it never fails a run."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "review_guide.py"
_SPEC = importlib.util.spec_from_file_location("review_guide", _SCRIPT)
assert _SPEC and _SPEC.loader
review_guide = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = review_guide
_SPEC.loader.exec_module(review_guide)


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
        "REVIEW_GUIDE_URL": "http://unused",
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
                mock.patch.object(review_guide, "_github_json", side_effect=_github_json) as gh,
                mock.patch.object(review_guide, "_contents", return_value="def f():\n    return 1\n") as base,
                mock.patch.object(review_guide, "call_graph", return_value={"nodes": {}, "edges": []}),
            ):
                b = review_guide.bundle(env)
            # the diff is between the two analysed commits, the merge base not the branch tip
            self.assertIn("/repos/o/r/compare/B...H", [c.args[0] for c in gh.call_args_list])
            files = json.loads(b["files"]["files.json"])
            self.assertEqual(files["H:app.py"], "def f():\n    return 2\n")
            self.assertEqual(files["B:app.py"], "def f():\n    return 1\n")
            self.assertNotIn("B:new.py", files)  # an added file has no base side to fetch
            base.assert_called_once_with("o/r", "B", "app.py")
            meta = json.loads(b["files"]["metadata.json"])
            self.assertEqual((meta["base_sha"], meta["head_sha"]), ("B", "H"))
            self.assertEqual(json.loads(b["files"]["pr.json"])["commits"], ["Return two"])
            self.assertEqual(sorted(b["files"]), sorted(review_guide_inputs()))

    def test_a_missing_call_graph_skips_the_guide_without_failing(self):
        with tempfile.TemporaryDirectory() as t:
            env = _workspace(Path(t))
            (Path(env["HEAD_ANALYSIS_PATH"]).parent / "static_analysis.pkl").unlink()
            with mock.patch.object(review_guide, "_github_json", side_effect=_github_json):
                with mock.patch("builtins.print") as out:
                    self.assertEqual(review_guide.main(env), 0)
            self.assertIn("No review guide", out.call_args.args[0])
            self.assertFalse((Path(env["ARTIFACT_DIR"]) / "review_guide.json").exists())


def review_guide_inputs():
    return [
        "analysis.json",
        "base/analysis.json",
        "metadata.json",
        "pr.json",
        "pr-files.json",
        "head-cg.json",
        "base-cg.json",
        "files.json",
    ]


class CredentialTests(unittest.TestCase):
    def test_without_oidc_the_runs_own_token_proves_the_repository(self):
        self.assertEqual(review_guide.credentials({"GITHUB_TOKEN": "ghs_run"}), {"Authorization": "Bearer ghs_run"})

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
                headers = review_guide.credentials(env)
                self.assertEqual(list(Path(t).iterdir()), [])  # the license file does not outlive the call
            self.assertEqual(headers, {"Authorization": "Bearer jwt.token.sig~codeboarding-license~LIC"})
            self.assertIn("audience=codeboarding-proxy", mint.requests[0]["path"])
        finally:
            mint.close()

    def test_the_runs_own_provider_key_goes_along_and_the_caller_is_still_proved(self):
        headers = review_guide.credentials({"GITHUB_TOKEN": "ghs_run", "OPENROUTER_API_KEY": " sk-or "})
        self.assertEqual(
            headers,
            {"Authorization": "Bearer ghs_run", "x-llm-api-key": "sk-or", "x-llm-provider": "openrouter"},
        )


class RequestTests(unittest.TestCase):
    def test_the_guide_lands_in_the_artifact_beside_the_analysis(self):
        guide = {"version": 1, "repo": "o/r", "pr": 7, "themes": [], "changes": [{"id": "u1"}]}
        service = _Server(lambda path, body: (200, {"guide": guide, "usage": {"provider": "ollama", "hosted": True}}))
        try:
            with tempfile.TemporaryDirectory() as t:
                env = {**_workspace(Path(t)), "REVIEW_GUIDE_URL": service.url}
                with (
                    mock.patch.object(review_guide, "_github_json", side_effect=_github_json),
                    mock.patch.object(review_guide, "_contents", return_value="x"),
                    mock.patch.object(review_guide, "call_graph", return_value={"nodes": {}, "edges": []}),
                ):
                    self.assertEqual(review_guide.main(env), 0)
                written = json.loads((Path(env["ARTIFACT_DIR"]) / "review_guide.json").read_text())
            self.assertEqual(written, guide)
            sent = service.requests[0]
            self.assertEqual(sent["path"], "/v1/review-guide")
            self.assertEqual(sent["headers"]["Authorization"], "Bearer ghs_run")
            self.assertEqual(json.loads(gzip.decompress(sent["body"]))["repo"], "o/r")
        finally:
            service.close()

    def test_a_refusal_is_a_notice_and_the_run_goes_on(self):
        service = _Server(
            lambda path, body: (402, {"error": "quota", "message": "Resource exhausted: token limit reached"})
        )
        try:
            with tempfile.TemporaryDirectory() as t:
                env = {**_workspace(Path(t)), "REVIEW_GUIDE_URL": service.url}
                with (
                    mock.patch.object(review_guide, "_github_json", side_effect=_github_json),
                    mock.patch.object(review_guide, "_contents", return_value="x"),
                    mock.patch.object(review_guide, "call_graph", return_value={"nodes": {}, "edges": []}),
                    mock.patch("builtins.print") as out,
                ):
                    self.assertEqual(review_guide.main(env), 0)
                self.assertFalse((Path(env["ARTIFACT_DIR"]) / "review_guide.json").exists())
            self.assertIn("402", out.call_args.args[0])
            self.assertIn("token limit", out.call_args.args[0])
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
