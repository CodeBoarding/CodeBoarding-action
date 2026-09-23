#!/usr/bin/env python3
"""A run's start and finish on CodeBoarding's proxy, in every credential mode.

``start`` runs before the engine install and asks ``POST /run/start`` whether this run may
go ahead, and how deep: the plan of whoever the run is charged to caps ``depth_cap``, so
no workflow can raise it. Own-key runs ask too, although their model calls never reach
CodeBoarding; that is the honour-system half of the paywall. The proxy's answer becomes
this step's outputs, and its run id is written where the relay packs it into every hosted
call.

``finish`` runs last, on every outcome, and reports ``POST /run/finish``: ``produced``
charges the run, anything else releases its hold. Success is the map, not the exit code.

Both fail open. The proxy being down or wrong never stops or fails a run: ``start`` then
allows it at up to three levels, and ``finish`` only warns.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from oidc_relay import RelayConfig, authorization  # noqa: E402

ACTION_ROOT = Path(__file__).resolve().parent.parent.parent
#: The cap a run gets when the proxy cannot say: Free's, so an outage lifts nobody past it.
FAIL_OPEN_DEPTH = 3


def proxy_url(environ: dict[str, str]) -> str:
    return environ.get("CODEBOARDING_PROXY_URL") or (Path(__file__).parent / "hosted-proxy-url").read_text().strip()


def post(environ: dict[str, str], path: str, body: dict, license_file: Path | None = None) -> dict:
    config = RelayConfig(
        proxy_url(environ),
        environ["ACTIONS_ID_TOKEN_REQUEST_URL"],
        environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"],
        license_file,
    )
    request = Request(
        proxy_url(environ).rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": authorization(config), "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # nosec B310 - CodeBoarding's proxy
        return json.loads(response.read())


def baseline_depth(checkout: Path) -> int | None:
    """The committed baseline's cap, so the proxy can tell a PR that goes deeper to run in full."""
    try:
        cap = json.loads((checkout / ".codeboarding" / "analysis.json").read_text(encoding="utf-8"))["metadata"][
            "depth_cap"
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else None


def start_request(environ: dict[str, str], depth: int, tier: str) -> dict:
    manifest = json.loads((ACTION_ROOT / ".release-please-manifest.json").read_text(encoding="utf-8"))
    return {
        "depth": depth,
        "credential": "hosted" if tier in ("hosted", "license") else "own_key",
        "baseline_depth": baseline_depth(Path(environ.get("CHECKOUT_DIR", ""))),
        "client": {"surface": "action", "version": manifest["."]},
    }


def start(environ: dict[str, str]) -> tuple[dict[str, str], int]:
    """The step outputs, and the exit code: non-zero only for a depth_cap that is not a number."""
    raw = environ.get("DEPTH_CAP", "2")
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        print("::error::depth_cap must be a positive integer.")
        return {}, 1
    depth = int(raw)
    auth_dir = Path(environ["RUNNER_TEMP"]) / "codeboarding-auth"
    for stale in (Path(environ["RUNNER_TEMP"]) / "codeboarding-map", auth_dir / "run-id"):
        stale.unlink(missing_ok=True)
    outputs = {
        "allowed": "true",
        "depth_cap": str(depth),
        "run_id": "",
        "full_analysis": "false",
        "wall_message": "",
        "mode": "",
    }

    # Only own-key workflows get here without one: the hosted tiers refuse to start without it.
    if not (environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")):
        print(
            "::warning title=CodeBoarding run check::This job cannot mint a GitHub OIDC token, so the run "
            "was not checked against your CodeBoarding plan. Add `id-token: write` to the job's permissions."
        )
        return outputs, 0

    tier = (auth_dir / "tier").read_text(encoding="utf-8").strip()
    license_file = auth_dir / "license.txt"
    try:
        answer = post(
            environ,
            "/run/start",
            start_request(environ, depth, tier),
            license_file if license_file.is_file() else None,
        )
        allowed, cap = answer["allowed"], answer["depth_cap"]
        if not isinstance(allowed, bool) or not isinstance(cap, int) or cap < 1:
            raise ValueError("unexpected /run/start answer")
    except Exception as exc:  # noqa: BLE001 - our outage must never block a run
        capped = min(depth, FAIL_OPEN_DEPTH)
        print(
            f"::warning title=CodeBoarding run check::CodeBoarding could not check this run ({type(exc).__name__}), "
            f"so it runs at up to {capped} levels."
        )
        outputs["depth_cap"] = str(capped)
        return outputs, 0

    run_id = answer.get("run_id") or ""
    wall = answer.get("wall") or {}
    outputs.update(
        {
            "allowed": str(allowed).lower(),
            "depth_cap": str(min(depth, cap)),
            "run_id": run_id,
            "full_analysis": str(answer.get("full_analysis") is True).lower(),
            "wall_message": " ".join(str(wall.get("message", "")).split()),
            "mode": str(answer.get("mode") or ""),
        }
    )
    if run_id:
        (auth_dir / "run-id").write_text(run_id, encoding="utf-8")
    print(
        f"CodeBoarding run check: allowed={outputs['allowed']} depth_cap={outputs['depth_cap']} "
        f"plan={answer.get('plan')} mode={outputs['mode']} charged_to={(answer.get('charged_to') or {}).get('login')}."
    )
    if answer.get("depth_reason"):
        print(f"::notice title=CodeBoarding depth::{answer['depth_reason']}")
    if not allowed:
        print(f"::notice title=CodeBoarding::{outputs['wall_message'] or 'This run is over the plan allowance.'}")
    return outputs, 0


def map_written(environ: dict[str, str]) -> bool:
    """Whether the analysis this run set out to write exists.

    The step output is the normal answer. It is lost when the engine crashes on shutdown
    after writing, so the marker analyze.sh leaves just before the final engine call is the
    fallback: the file it names, rewritten since, is this run's map. A file older than the
    marker is the seed the run started from, not something it produced.
    """
    path = environ.get("ANALYSIS_PATH", "")
    if path and Path(path).is_file():
        return True
    marker = Path(environ.get("MAP_MARKER") or "/nonexistent")
    try:
        expected = Path(marker.read_text(encoding="utf-8").strip())
        return expected.is_file() and expected.stat().st_mtime_ns >= marker.stat().st_mtime_ns
    except OSError:
        return False


def outcome(environ: dict[str, str]) -> str:
    if map_written(environ):
        return "produced"
    if environ.get("JOB_STATUS") == "cancelled":
        return "cancelled"
    return "failed"


def finish(environ: dict[str, str]) -> int:
    """Always 0: reporting the outcome must never be what fails the job."""
    body = {"run_id": environ["RUN_ID"], "outcome": outcome(environ), "error": None}
    try:
        answer = post(environ, "/run/finish", body)
    except Exception as exc:  # noqa: BLE001 - an unreported run is released after the stale-hold timeout
        print(f"::warning title=CodeBoarding run check::Could not report this run's outcome ({type(exc).__name__}).")
        return 0
    print(f"Reported {body['outcome']} to CodeBoarding: charged={answer.get('charged')} ({answer.get('cause')}).")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "finish"])
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    if args.command == "finish":
        return finish(environ)
    outputs, code = start(environ)
    with open(environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in outputs.items())
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
