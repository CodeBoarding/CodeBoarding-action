#!/usr/bin/env python3
"""Loopback relay that refreshes a GitHub Actions OIDC JWT for every request.

The hosted CodeBoarding proxy authenticates each OpenRouter request with a
GitHub OIDC JWT.  Those JWTs are deliberately short lived, whereas an analysis
can make requests for longer than a single token's lifetime.  This tiny,
stdlib-only relay is started by ``action.yml`` on the runner's loopback
interface.  The engine talks to it with a harmless placeholder API key; the
relay obtains a fresh JWT from GitHub and swaps it into the request sent to the
real proxy.

It is intentionally not a general-purpose proxy: it only binds to 127.0.0.1,
only uses the upstream URL supplied by the action, and never logs credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


_HOP_BY_HOP = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True)
class RelayConfig:
    upstream_base_url: str
    id_token_request_url: str
    id_token_request_token: str
    license_file: Path | None = None


def _with_audience(url: str) -> str:
    """Add (or replace) the audience without assuming GitHub's query layout."""
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "audience"]
    query.append(("audience", "codeboarding-proxy"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _mint_oidc_token(config: RelayConfig) -> str:
    request = Request(
        _with_audience(config.id_token_request_url),
        headers={"Authorization": f"Bearer {config.id_token_request_token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:  # nosec B310 - GitHub runner URL
            payload = json.loads(response.read())
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise RuntimeError("could not mint a GitHub OIDC token") from exc

    token = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("GitHub returned an empty OIDC token")
    return token.strip()


def _authorization(config: RelayConfig) -> str:
    token = _mint_oidc_token(config)
    if config.license_file is not None:
        try:
            license_key = config.license_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("could not read CodeBoarding license") from exc
        if not license_key:
            raise RuntimeError("CodeBoarding license is empty")
        token = f"{token}~codeboarding-license~{license_key}"
    return f"Bearer {token}"


def _upstream_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + (path if path.startswith("/") else f"/{path}")


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Request headers contain bearer material. Keep Action logs credential-free.
        return

    @property
    def config(self) -> RelayConfig:
        return self.server.config  # type: ignore[attr-defined]

    def _request_body(self) -> bytes:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        return self.rfile.read(max(0, length))

    def _send(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in _HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle(self) -> None:
        try:
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in _HOP_BY_HOP and key.lower() != "authorization"
            }
            headers["Authorization"] = _authorization(self.config)
            request = Request(
                _upstream_url(self.config.upstream_base_url, self.path),
                data=self._request_body(),
                headers=headers,
                method=self.command,
            )
            try:
                with urlopen(request, timeout=310) as response:  # nosec B310 - configured proxy URL
                    self._send(response.status, dict(response.headers.items()), response.read())
            except HTTPError as response:
                self._send(response.code, dict(response.headers.items()), response.read())
        except (RuntimeError, URLError, OSError) as exc:
            body = json.dumps({"error": {"message": "CodeBoarding OIDC relay request failed."}}).encode()
            self._send(502, {"Content-Type": "application/json"}, body)
            print(f"OIDC relay request failed: {exc}", file=sys.stderr)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: RelayConfig):
        super().__init__(("127.0.0.1", 0), _RelayHandler)
        self.config = config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--license-file", type=Path)
    args = parser.parse_args(argv)

    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        print("A GitHub OIDC request URL/token is unavailable.", file=sys.stderr)
        return 2

    server = RelayServer(RelayConfig(args.upstream_base_url, request_url, request_token, args.license_file))
    args.ready_file.write_text(str(server.server_port), encoding="utf-8")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
