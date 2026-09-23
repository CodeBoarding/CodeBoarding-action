"""The run's start and finish against CodeBoarding's proxy, checked against the shared contract.

tests/contracts is a copy of licensing-aws's contracts/ directory (CodeBoarding/licensing-aws#17,
cf8ae92). The proxy answers in those shapes, so the preflight's request must validate against
them and every example answer must turn into sensible step outputs.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError:  # CI installs it; a bare local checkout skips the contract checks
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "action" / "run_meter.py"
CONTRACTS = Path(__file__).resolve().parent / "contracts"
NEEDS_JSONSCHEMA = unittest.skipIf(
    Draft202012Validator is None, "jsonschema is not installed: `pip install jsonschema` to check the paywall contracts"
)

_SPEC = importlib.util.spec_from_file_location("run_meter", SCRIPT)
assert _SPEC and _SPEC.loader
run_meter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_meter)


def example(name: str) -> dict:
    return json.loads((CONTRACTS / "examples" / name).read_text(encoding="utf-8"))


def validate(document: dict, schema_name: str) -> list[str]:
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in CONTRACTS.glob("*.schema.json")]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    schema = json.loads((CONTRACTS / schema_name).read_text(encoding="utf-8"))
    return [error.message for error in Draft202012Validator(schema, registry=registry).iter_errors(document)]


class FakeProxy:
    """GitHub's OIDC issuer and CodeBoarding's proxy on one loopback server."""

    def __init__(self, answer: dict | None = None, status: int = 200) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._reply(200, {"value": "oidc-jwt"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                proxy.requests.append((self.path, self.headers["Authorization"], body))
                self._reply(status, answer or {})

            def _reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RunMeterTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.runner_temp = self.root / "runner"
        self.auth_dir = self.runner_temp / "codeboarding-auth"
        self.auth_dir.mkdir(parents=True)
        self.checkout = self.root / "checkout"
        (self.checkout / ".codeboarding").mkdir(parents=True)

    def _environ(self, proxy: FakeProxy | None, tier: str = "hosted", **extra: str) -> dict[str, str]:
        (self.auth_dir / "tier").write_text(tier, encoding="utf-8")
        environ = {"RUNNER_TEMP": str(self.runner_temp), "CHECKOUT_DIR": str(self.checkout), "DEPTH_CAP": "5"}
        if proxy is not None:
            environ |= {
                "CODEBOARDING_PROXY_URL": proxy.url,
                "ACTIONS_ID_TOKEN_REQUEST_URL": f"{proxy.url}/token",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
            }
        return environ | extra

    def _start(self, answer: dict | None, tier: str = "hosted", **extra: str):
        proxy = FakeProxy(answer)
        self.addCleanup(proxy.close)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            outputs, code = run_meter.start(self._environ(proxy, tier, **extra))
        self.assertEqual(code, 0, log.getvalue())
        return outputs, proxy.requests, log.getvalue()

    # -- the request -------------------------------------------------------

    @NEEDS_JSONSCHEMA
    def test_the_preflight_request_matches_the_contract_in_every_credential_mode(self) -> None:
        (self.checkout / ".codeboarding" / "analysis.json").write_text('{"metadata": {"depth_cap": 2}}')
        for tier, credential in (("hosted", "hosted"), ("license", "hosted"), ("byok", "own_key")):
            with self.subTest(tier=tier):
                _, requests, _ = self._start(example("run-start.allowed.json"), tier)
                path, _, body = requests[0]
                self.assertEqual(path, "/run/start")
                self.assertEqual(validate(body, "run-start.request.schema.json"), [])
                self.assertEqual(body["credential"], credential)
                self.assertEqual((body["depth"], body["baseline_depth"]), (5, 2))
                self.assertEqual(body["client"]["surface"], "action")
                manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
                self.assertEqual(body["client"]["version"], manifest["."])

    def test_an_unknown_baseline_depth_is_sent_as_null(self) -> None:
        _, requests, _ = self._start(example("run-start.allowed.json"))
        self.assertIsNone(requests[0][2]["baseline_depth"])

    def test_a_licence_rides_in_the_bearer_only_on_the_license_tier(self) -> None:
        (self.auth_dir / "license.txt").write_text("LIC-123", encoding="utf-8")
        _, licensed, _ = self._start(example("run-start.allowed.json"), "license")
        _, own_key, _ = self._start(example("run-start.allowed.json"), "byok+license")
        self.assertEqual(licensed[0][1], "Bearer oidc-jwt~codeboarding-license~LIC-123")
        self.assertEqual(own_key[0][1], "Bearer oidc-jwt")

    # -- the answer --------------------------------------------------------

    @NEEDS_JSONSCHEMA
    def test_every_contract_example_validates(self) -> None:
        for path in sorted((CONTRACTS / "examples").glob("run-*.json")):
            stem, _, _ = path.name.partition(".")
            kind = "request" if ".request." in path.name else "response"
            with self.subTest(example=path.name):
                self.assertEqual(validate(example(path.name), f"{stem}.{kind}.schema.json"), [])

    def test_every_run_start_answer_becomes_step_outputs(self) -> None:
        for path in sorted((CONTRACTS / "examples").glob("run-start.*.json")):
            if ".request." in path.name:
                continue
            answer = example(path.name)
            with self.subTest(example=path.name):
                outputs, _, _ = self._start(answer)
                self.assertEqual(outputs["allowed"], str(answer["allowed"]).lower())
                self.assertEqual(outputs["depth_cap"], str(min(5, answer["depth_cap"])))
                self.assertEqual(outputs["run_id"], answer["run_id"] or "")
                self.assertEqual(outputs["full_analysis"], str(answer["full_analysis"]).lower())
                self.assertEqual(outputs["mode"], answer["mode"])
                self.assertEqual(outputs["wall_message"], (answer["wall"] or {}).get("message", ""))
                run_id_file = self.auth_dir / "run-id"
                self.assertEqual(run_id_file.read_text() if run_id_file.exists() else None, answer["run_id"])

    def test_the_proxy_can_only_lower_the_depth(self) -> None:
        outputs, _, _ = self._start(example("run-start.legacy.json") | {"depth_cap": 10}, DEPTH_CAP="4")
        self.assertEqual(outputs["depth_cap"], "4")

    def test_an_unreachable_proxy_fails_open_at_three_levels(self) -> None:
        environ = self._environ(None) | {
            "CODEBOARDING_PROXY_URL": "http://127.0.0.1:9",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "http://127.0.0.1:9/token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "PATH": os.environ["PATH"],
        }
        (self.auth_dir / "run-id").write_text("from-an-earlier-invocation")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "start"], env=environ, capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning", result.stdout)
        outputs = dict(line.split("=", 1) for line in (self.root / "github-output").read_text().splitlines())
        self.assertEqual((outputs["allowed"], outputs["depth_cap"], outputs["run_id"]), ("true", "3", ""))
        self.assertFalse((self.auth_dir / "run-id").exists(), "no hosted call may carry a stale run id")

    def test_an_answer_that_breaks_the_contract_fails_open(self) -> None:
        outputs, _, log = self._start({"error": {"message": "Internal"}})
        self.assertEqual((outputs["allowed"], outputs["depth_cap"]), ("true", "3"))
        self.assertIn("::warning", log)

    def test_a_job_without_id_token_permission_skips_the_check_and_keeps_its_depth(self) -> None:
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            outputs, code = run_meter.start(self._environ(None, "byok"))
        self.assertEqual(code, 0)
        self.assertIn("id-token: write", log.getvalue())
        self.assertEqual((outputs["allowed"], outputs["depth_cap"], outputs["run_id"]), ("true", "5", ""))

    def test_a_depth_that_is_not_a_positive_integer_stops_the_run(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            _, code = run_meter.start(self._environ(None, DEPTH_CAP="0"))
        self.assertEqual(code, 1)

    # -- the finish --------------------------------------------------------

    def _finish(self, **environ: str) -> tuple[dict, str]:
        proxy = FakeProxy(example("run-finish.charged.json"))
        self.addCleanup(proxy.close)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            code = run_meter.finish(self._environ(proxy, RUN_ID="github:o/r#1", **environ))
        self.assertEqual(code, 0)
        return proxy.requests[0][2], log.getvalue()

    def _marked(self, *, rewritten: bool) -> dict[str, str]:
        """A map seeded before analyze.sh's marker, then rewritten by the engine or not."""
        analysis = self.root / "head-state" / "analysis.json"
        analysis.parent.mkdir()
        analysis.write_text("{}")
        time.sleep(0.01)
        marker = self.runner_temp / "codeboarding-map"
        marker.write_text(str(analysis))
        if rewritten:
            time.sleep(0.01)
            analysis.write_text('{"components": []}')
        return {"MAP_MARKER": str(marker)}

    @NEEDS_JSONSCHEMA
    def test_the_finish_request_matches_the_contract(self) -> None:
        body, _ = self._finish(JOB_STATUS="failure")
        self.assertEqual(validate(body, "run-finish.request.schema.json"), [])

    def test_a_written_map_is_produced(self) -> None:
        analysis = self.root / "analysis.json"
        analysis.write_text("{}")
        body, log = self._finish(ANALYSIS_PATH=str(analysis), JOB_STATUS="success")
        self.assertEqual(body["outcome"], "produced")
        self.assertIn("charged=True", log)

    def test_a_crash_after_the_map_was_written_is_still_produced(self) -> None:
        body, _ = self._finish(JOB_STATUS="failure", **self._marked(rewritten=True))
        self.assertEqual(body["outcome"], "produced")

    def test_the_seed_a_run_started_from_is_not_its_map(self) -> None:
        body, _ = self._finish(JOB_STATUS="failure", **self._marked(rewritten=False))
        self.assertEqual(body["outcome"], "failed")

    def test_a_cancelled_job_without_a_map_is_cancelled(self) -> None:
        body, _ = self._finish(JOB_STATUS="cancelled", ANALYSIS_PATH="")
        self.assertEqual(body["outcome"], "cancelled")

    def test_an_unreachable_proxy_never_fails_the_finish(self) -> None:
        environ = self._environ(None) | {
            "RUN_ID": "github:o/r#1",
            "CODEBOARDING_PROXY_URL": "http://127.0.0.1:9",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "http://127.0.0.1:9/token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
        }
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            self.assertEqual(run_meter.finish(environ), 0)
        self.assertIn("::warning", log.getvalue())


if __name__ == "__main__":
    unittest.main()
