"""Submit explicit user feedback (/codeboarding-feedback) to PostHog.

Standard-library only, on purpose: this runs in the action's guard phase, before
the engine checkout and any dependency install, so it must not import third-party
packages. Unlike Core's anonymous telemetry, this event intentionally carries the
user-written feedback text and PR context — that difference is documented in the
README. All sending failures are swallowed; feedback must never break a PR.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Public PostHog ingest key — the same write-only project key Core ships.
DEFAULT_POSTHOG_KEY = "phc_BQWpoXuPYQhW7mPWQcRv4yzSfuoAmh48EmXuUpeXPUB2"
DEFAULT_POSTHOG_HOST = "https://us.i.posthog.com"
DEFAULT_COMMAND = "/codeboarding-feedback"
DEFAULT_MAX_CHARS = 4000
EVENT_NAME = "codeboarding_feedback_submitted"
SOURCE = "github_action_feedback"


def telemetry_disabled(env: dict) -> bool:
    """Mirror Core's opt-out: DO_NOT_TRACK or CODEBOARDING_TELEMETRY=false."""
    if env.get("DO_NOT_TRACK", "").strip().lower() in ("1", "true", "yes"):
        return True
    return env.get("CODEBOARDING_TELEMETRY", "true").strip().lower() == "false"


def resolve_key(env: dict) -> str:
    return (env.get("CODEBOARDING_POSTHOG_KEY") or env.get("POSTHOG_KEY") or DEFAULT_POSTHOG_KEY).strip()


def resolve_host(env: dict) -> str:
    host = (env.get("CODEBOARDING_POSTHOG_HOST") or env.get("POSTHOG_HOST") or DEFAULT_POSTHOG_HOST).strip()
    return host.rstrip("/") or DEFAULT_POSTHOG_HOST


def resolve_command(env: dict) -> str:
    return (env.get("FEEDBACK_COMMAND") or "").strip() or DEFAULT_COMMAND


def resolve_max_chars(env: dict) -> int:
    try:
        n = int((env.get("FEEDBACK_MAX_CHARS") or "").strip())
    except ValueError:
        return DEFAULT_MAX_CHARS
    return n if n > 0 else DEFAULT_MAX_CHARS


def extract_feedback(comment_body: str, command: str) -> str:
    """Return everything after the leading command token, newlines preserved.

    The command is the first whitespace-delimited token of the comment. Only that
    one token is removed; the remainder (including any later lines) is kept
    verbatim, then outer whitespace is trimmed. Returns "" when the comment does
    not actually start with the command, or carries no text after it.
    """
    body = (comment_body or "").replace("\r\n", "\n").replace("\r", "\n").lstrip()
    if not body:
        return ""
    parts = body.split(None, 1)  # split once on the first run of whitespace
    if parts[0] != command:
        return ""
    return parts[1].strip() if len(parts) > 1 else ""


def cap_feedback(text: str, max_chars: int) -> tuple[str, int, bool]:
    """Return (capped_text, original_length, truncated)."""
    original_length = len(text)
    truncated = original_length > max_chars
    return (text[:max_chars] if truncated else text), original_length, truncated


def _first(env: dict, *names: str) -> str:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def distinct_id(env: dict) -> str:
    sender_id = _first(env, "SENDER_ID")
    if sender_id:
        return f"github-user:{sender_id}"
    return f"github-run:{_first(env, 'RUN_ID', 'GITHUB_RUN_ID')}"


def build_properties(env: dict, command: str, feedback_text: str, feedback_length: int, truncated: bool) -> dict:
    props: dict = {
        "source": SOURCE,
        "command": command,
        "feedback_text": feedback_text,
        "feedback_length": feedback_length,
        "feedback_truncated": truncated,
    }
    optional = {
        "repository": _first(env, "REPOSITORY"),
        "repository_id": _first(env, "REPOSITORY_ID"),
        "pr_number": _first(env, "PR_NUMBER", "ISSUE_NUMBER"),
        "comment_id": _first(env, "COMMENT_ID"),
        "comment_url": _first(env, "COMMENT_URL"),
        "author_association": _first(env, "AUTHOR_ASSOC", "AUTHOR_ASSOCIATION"),
        "sender_login": _first(env, "SENDER_LOGIN"),
        "sender_id": _first(env, "SENDER_ID"),
        "run_id": _first(env, "RUN_ID", "GITHUB_RUN_ID"),
        "run_attempt": _first(env, "RUN_ATTEMPT", "GITHUB_RUN_ATTEMPT"),
        "action_ref": _first(env, "ACTION_REF", "GITHUB_ACTION_REF", "GITHUB_SHA"),
    }
    props.update({key: value for key, value in optional.items() if value})
    return props


def build_payload(env: dict) -> dict | None:
    """Build the PostHog event payload, or None when there is nothing to send."""
    command = resolve_command(env)
    feedback_text, feedback_length, truncated = cap_feedback(
        extract_feedback(env.get("COMMENT_BODY", ""), command), resolve_max_chars(env)
    )
    if not feedback_text:
        return None
    return {
        "api_key": resolve_key(env),
        "event": EVENT_NAME,
        "distinct_id": distinct_id(env),
        "properties": build_properties(env, command, feedback_text, feedback_length, truncated),
    }


def post(payload: dict, host: str, timeout: int = 10) -> int:
    """POST one event to PostHog's ingest endpoint; return the HTTP status."""
    request = urllib.request.Request(
        f"{host}/i/v0/e/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def main(env: dict | None = None) -> int:
    env = os.environ if env is None else env

    if telemetry_disabled(env):
        print("Feedback disabled via DO_NOT_TRACK / CODEBOARDING_TELEMETRY; not sending.")
        return 0

    payload = build_payload(env)
    if payload is None:
        print("No feedback text after the command; nothing to send.")
        return 0
    if not payload["api_key"]:
        print("No PostHog key configured; skipping feedback send.")
        return 0

    truncated = payload["properties"].get("feedback_truncated")
    try:
        status = post(payload, resolve_host(env))
        print(f"Feedback submitted (HTTP {status}, truncated={truncated}).")
    except urllib.error.HTTPError as exc:
        print(f"Feedback endpoint returned HTTP {exc.code}; ignoring.")
    except urllib.error.URLError as exc:
        print(f"Feedback endpoint unreachable ({type(exc.reason).__name__}); ignoring.")
    except Exception as exc:  # never let feedback break the action
        print(f"Feedback send failed ({type(exc).__name__}); ignoring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
