#!/usr/bin/env python3
"""Explain a run the engine refused to finish, in the pull request and the job summary.

analyze_repository.py records the engine's refusal (quota used up, credentials rejected) in
$RUNNER_TEMP/codeboarding-engine-error.json. This renders it for a person: a sticky comment
body in review mode, and the job summary in both modes. The run itself stays failed; this
only replaces "see the workflow logs" with the reason and what to change.

Writes `reason` (the engine's `kind`, empty when there is nothing to report) and `body_path`
to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ERROR_FILE = "codeboarding-engine-error.json"
DOCS = "https://github.com/CodeBoarding/CodeBoarding-action#authentication-and-providers"
HEADINGS = {
    "llm_quota_exhausted": "stopped: LLM quota used up",
    "llm_auth": "stopped: LLM credentials rejected",
}
OWN_KEY = "use your own LLM key: set `llm` to your provider and pass its key input, for example `llm: anthropic` with `anthropic_api_key`"
LICENSE = "use a CodeBoarding license: `llm: license` with `license_key`"


def _quota(llm: str) -> list[str]:
    lines = ["The LLM provider refused the analysis because the token quota is used up."]
    if llm == "hosted":
        lines += [
            "",
            "On `llm: hosted` that is CodeBoarding's free tier, whose allowance is per GitHub owner per week and resets Monday 00:00 UTC. To analyze before then, either:",
            "",
            f"- {OWN_KEY};",
            f"- or {LICENSE}.",
        ]
    elif llm == "license":
        lines += [
            "",
            f"On `llm: license` that is your CodeBoarding plan's allowance. To analyze before it renews, {OWN_KEY}.",
        ]
    else:
        lines += [
            "",
            f"On `llm: {llm}` that is the quota of your own provider account. Raise it with the provider, or {LICENSE}.",
        ]
    return lines


def _auth(llm: str) -> list[str]:
    return [
        "The LLM provider rejected this run's credentials.",
        "",
        f"Check the key or license this workflow passes for `llm: {llm}`; see [Authentication and providers]({DOCS}).",
    ]


def render(error: dict, env: dict[str, str]) -> str:
    kind = error["kind"]
    mode = env.get("MODE", "review")
    # credential_check.py accepts any case, so `Hosted` must read as hosted here too.
    llm = env.get("LLM", "").strip().lower() or "hosted"
    lines = [f"### CodeBoarding {mode} · {HEADINGS[kind]}", ""]
    lines.append("CodeBoarding stopped instead of publishing a map without AI naming.")
    lines += _quota(llm) if kind == "llm_quota_exhausted" else _auth(llm)
    lines.append("")
    if mode == "sync":
        lines.append("The baseline was not updated, and no base analysis was published.")
    else:
        lines.append("No diagram was posted for this run.")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    lines += [
        "",
        f"<sub>run [{run_id}]({server}/{repository}/actions/runs/{run_id}) · attempt {env.get('GITHUB_RUN_ATTEMPT', '1')}</sub>",
    ]
    if mode == "review":
        # The review comment's machine-readable line, with `failure` in place of the counts a
        # stopped run does not have. One line, `key=value`, no spaces in values.
        platform = f"https://app.codeboarding.org/{repository}/pull/{env.get('PR_NUMBER', '')}"
        lines.append(f"<!-- codeboarding: platform_url={platform} head={env.get('HEAD_SHA', '')} failure={kind} -->")
    return "\n".join(lines) + "\n"


def main() -> int:
    env = dict(os.environ)
    runner_temp = Path(env["RUNNER_TEMP"])
    outputs = []
    try:
        error = json.loads((runner_temp / ERROR_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        error = None
    if isinstance(error, dict) and error.get("kind") in HEADINGS:
        body = render(error, env)
        path = runner_temp / "codeboarding-engine-failure.md"
        path.write_text(body, encoding="utf-8")
        summary = env.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(body)
        outputs = [f"reason={error['kind']}", f"body_path={path}"]
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.writelines(line + "\n" for line in outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
