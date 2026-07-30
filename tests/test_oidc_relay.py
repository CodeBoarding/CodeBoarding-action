"""Tests for the stdlib loopback relay used by the hosted OIDC tier."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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

    def test_audience_replaces_an_existing_value(self):
        self.assertEqual(
            oidc_relay._with_audience("https://issuer.example/token?audience=old&x=1"),
            "https://issuer.example/token?x=1&audience=codeboarding-proxy",
        )
