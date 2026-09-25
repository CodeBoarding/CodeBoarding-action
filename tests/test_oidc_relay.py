"""Tests for the stdlib loopback relay used by the hosted OIDC tier."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "oidc_relay.py"
_SPEC = importlib.util.spec_from_file_location("oidc_relay", _SCRIPT)
assert _SPEC and _SPEC.loader
oidc_relay = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = oidc_relay
_SPEC.loader.exec_module(oidc_relay)


class _Server:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class TestOidcRelay(unittest.TestCase):
    def test_each_forwarded_request_mints_a_fresh_token(self):
        issued_tokens = []
        received = []

        class OidcIssuer(BaseHTTPRequestHandler):
            def do_GET(self):
                issued_tokens.append(self.path)
                payload = json.dumps({"value": f"jwt-{len(issued_tokens)}"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(
                    (self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"])))
                )
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        issuer = _Server(OidcIssuer)
        upstream = _Server(Upstream)
        issuer.start()
        upstream.start()
        relay = oidc_relay.RelayServer(
            oidc_relay.RelayConfig(
                upstream_base_url=f"{upstream.url}/api/v1",
                id_token_request_url=f"{issuer.url}/token?existing=value",
                id_token_request_token="request-token",
            )
        )
        relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
        relay_thread.start()
        try:
            relay_url = f"http://127.0.0.1:{relay.server_port}/chat/completions?model=test"
            for _ in range(2):
                with urlopen(Request(relay_url, data=b'{"prompt":"hello"}', method="POST")) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'{"ok":true}')
        finally:
            relay.shutdown()
            relay.server_close()
            relay_thread.join(timeout=2)
            issuer.close()
            upstream.close()

        self.assertEqual(issued_tokens, ["/token?existing=value&audience=codeboarding-proxy"] * 2)
        self.assertEqual([auth for _, auth, _ in received], ["Bearer jwt-1", "Bearer jwt-2"])
        self.assertEqual([path for path, _, _ in received], ["/api/v1/chat/completions?model=test"] * 2)

    def test_the_run_id_rides_last_in_the_bearer_with_or_without_a_licence(self):
        """The proxy splits `~codeboarding-run~` off from the right, then the licence."""

        class OidcIssuer(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b'{"value": "jwt"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        issuer = _Server(OidcIssuer)
        issuer.start()
        self.addCleanup(issuer.close)
        with tempfile.TemporaryDirectory() as temp:
            license_file, run_id_file = Path(temp) / "license.txt", Path(temp) / "run-id"
            license_file.write_text("LIC\n")

            def bearer(**files):
                config = oidc_relay.RelayConfig(
                    "https://proxy.example", f"{issuer.url}/token", "request-token", **files
                )
                return oidc_relay.authorization(config)

            self.assertEqual(bearer(run_id_file=run_id_file), "Bearer jwt", "no run id until the preflight wrote one")
            run_id_file.write_text("github:o/r#42\n")
            self.assertEqual(bearer(run_id_file=run_id_file), "Bearer jwt~codeboarding-run~github:o/r#42")
            self.assertEqual(
                bearer(license_file=license_file, run_id_file=run_id_file),
                "Bearer jwt~codeboarding-license~LIC~codeboarding-run~github:o/r#42",
            )
            self.assertEqual(bearer(license_file=license_file), "Bearer jwt~codeboarding-license~LIC")

    def test_a_402_wall_is_kept_for_the_action_and_still_relayed(self):
        answers = [
            {
                "error": {"message": "Weekly token ceiling reached", "type": "quota"},
                "wall": {"reason": "token_ceiling"},
            },
            {"error": {"message": "Resource exhausted: token limit reached"}},
        ]

        class Issuer(BaseHTTPRequestHandler):
            def do_GET(self):
                self._reply(200, {"value": "jwt"})

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self._reply(402, answers.pop(0))

            def _reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = _Server(Issuer)
        server.start()
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as temp:
            wall_file = Path(temp) / "codeboarding-wall" / "wall.json"
            relay = oidc_relay.RelayServer(
                oidc_relay.RelayConfig(server.url, f"{server.url}/token", "request-token", wall_file=wall_file)
            )
            thread = threading.Thread(target=relay.serve_forever, daemon=True)
            thread.start()
            try:
                statuses = []
                for _ in range(2):
                    wall_file.unlink(missing_ok=True)
                    request = Request(f"http://127.0.0.1:{relay.server_port}/chat/completions", data=b"{}")
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request)
                    statuses.append((caught.exception.code, wall_file.exists()))
                    caught.exception.close()
                    if wall_file.exists():
                        self.assertEqual(json.loads(wall_file.read_text()), {"reason": "token_ceiling"})
            finally:
                relay.shutdown()
                relay.server_close()
                thread.join(timeout=2)
        self.assertEqual(statuses, [(402, True), (402, False)], "a 402 without a wall is today's quota, not a wall")

    def test_audience_replaces_an_existing_value(self):
        self.assertEqual(
            oidc_relay._with_audience("https://issuer.example/token?audience=old&x=1"),
            "https://issuer.example/token?x=1&audience=codeboarding-proxy",
        )
