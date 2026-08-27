#!/usr/bin/env python3
"""The run's LLM credentials: what they resolve to, or what to tell the user instead.

Both halves live here on purpose. The `message` on every ConfigError is the sentence the
user actually reads -- verify-credentials.sh puts it in the step output, and action.yml posts
that same string as the pull request comment, the error annotation and the job summary --
so the rule and its explanation are written together and cannot drift apart.

One provider, chosen explicitly by the `llm` input, and nothing is ever reached by an
empty string falling through to a default. A misconfigured run fails here -- before the
checkout and the engine install -- naming the input and the secret to fix, rather than
succeeding on someone else's credentials or failing later inside the engine.

Reads the action's inputs from CB_IN_* environment variables (prefixed so that wiring an
input can never itself set a provider selection variable), and writes the resolved
environment to an auth directory the analysis steps read. Prints a JSON summary that
carries no secret values.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

DOCS = "https://github.com/CodeBoarding/CodeBoarding-action#authentication-and-providers"
SETTINGS_HINT = "Settings -> Secrets and variables -> Actions"
TABLE = Path(__file__).resolve().parent / "supported-providers.json"


#: Every reason this module can refuse a configuration.
#:
#: Declared rather than left implicit in the raise sites, for two reasons. These codes are
#: an interface: the action emits them as `llm_config_error` and the webview keys on them,
#: so adding one silently is a contract change nobody reviewed. And a code with no test is
#: invisible -- `license_with_provider_key` shipped untested precisely because nothing
#: enumerated the set. `test_llm_contract.py` asserts every entry here is exercised.
ERROR_CODES = frozenset(
    {
        "missing_llm",
        "unknown_llm",
        "missing_provider_key",
        "missing_license_key",
        "missing_id_token",
        "hosted_with_provider_key",
        "hosted_with_license",
        "license_with_provider_key",
        "foreign_provider_key",
    }
)


class ConfigError(Exception):
    """A configuration the action refuses to run, with the code the webview keys on.

    Two renderings, because they go to surfaces with different rules. ``message`` is one
    plain line, for the ``::error::`` annotation, which cannot carry newlines. ``details``
    is markdown for the pull request comment and the job summary, where a link and a
    snippet the reader can copy are worth far more than a sentence describing them.
    """

    def __init__(self, code: str, message: str, details: str | None = None) -> None:
        super().__init__(message)
        assert code in ERROR_CODES, f"undeclared error code: {code}"
        self.code = code
        self.message = message
        self.details = details or message


def secrets_url(environ: dict[str, str]) -> str | None:
    """This repository's "new secret" page, when the runner tells us which repo we are in."""
    repo = environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return None
    server = environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return f"{server}/{repo}/settings/secrets/actions/new"


def workflow_path(environ: dict[str, str]) -> str:
    """The workflow file to edit, named rather than left for the reader to find.

    GITHUB_WORKFLOW_REF is ``owner/repo/.github/workflows/x.yml@refs/...``; a repository
    can have several workflows calling this action, so "add it to your workflow" is not
    an instruction someone can follow without guessing.
    """
    ref = environ.get("GITHUB_WORKFLOW_REF", "")
    path = ref.split("@", 1)[0]
    _, _, tail = path.partition("/")
    _, _, tail = tail.partition("/")
    return tail or "your CodeBoarding workflow"


def _add_secret(environ: dict[str, str], secret: str, what: str) -> str:
    """Step one of every credential remedy: put the value in the repository."""
    url = secrets_url(environ)
    where = f"[Add a repository secret]({url})" if url else "Add a repository secret"
    return f"{where} named `{secret}`, with {what} as the value."


def _wire_it(environ: dict[str, str], llm: str, lines: list[str]) -> str:
    """Step two: the exact YAML, in the exact file, indented as it will sit there."""
    body = "\n".join(f"          {line}" for line in lines)
    return (
        f"In `{workflow_path(environ)}`, the CodeBoarding step's `with:` block "
        f"needs to read:\n\n```yaml\n        with:\n          llm: {llm}\n{body}\n```"
    )


def load_table(path: Path = TABLE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _clean_key(raw: str) -> str:
    """Undo the ways a pasted key arrives wrapped: whitespace, quotes, a VAR= prefix."""
    value = re.sub(r"\s+", "", raw)
    for _ in range(2):
        value = re.sub(r'^"(.*)"$', r"\1", value)
        value = re.sub(r"^'(.*)'$", r"\1", value)
    value = re.sub(r"^[A-Z0-9_]+=", "", value)
    for _ in range(2):
        value = re.sub(r'^"(.*)"$', r"\1", value)
        value = re.sub(r"^'(.*)'$", r"\1", value)
    return value


def read_inputs(table: dict, environ: dict[str, str]) -> dict[str, str]:
    """Every provider input that carries a value, keyed by input name."""
    values: dict[str, str] = {}
    for provider in table["providers"].values():
        for input_name in provider["inputs"]:
            raw = environ.get(f"CB_IN_{input_name.upper()}", "")
            value = _clean_key(raw) if input_name.endswith("_api_key") else raw.strip()
            if value:
                values[input_name] = value
    return values


def owner_of(table: dict, input_name: str) -> str:
    for name, provider in table["providers"].items():
        if input_name in provider["inputs"]:
            return name
    raise KeyError(input_name)


def _provider_list(table: dict) -> str:
    return ", ".join(sorted(table["providers"]))


def _reject_provider_inputs(table: dict, given: dict[str, str], llm: str, tier: str) -> None:
    """`llm: hosted`/`license` run on CodeBoarding's credentials; a provider key means the
    workflow is asking for two different things at once."""
    if not given:
        return
    input_name = sorted(given)[0]
    provider = owner_of(table, input_name)
    raise ConfigError(
        f"{tier}_with_provider_key",
        f"`llm: {llm}` runs on CodeBoarding's hosted tier, but `{input_name}` is set. "
        f"Remove it, or set `llm: {provider}` to run on that key instead.",
    )


def _require_id_token(llm: str, environ: dict[str, str]) -> None:
    # Both, because the relay needs both (oidc_relay.py refuses to start without either)
    # and a runner can expose one without the other. Checking only the URL let that case
    # through preflight and turned it into a generic failure after the engine install,
    # which is the whole thing this check exists to prevent.
    if environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        return
    raise ConfigError(
        "missing_id_token",
        f"`llm: {llm}` authenticates with a GitHub OIDC token, which this job cannot mint. "
        "Add `permissions:` with `id-token: write` to the job that uses this action.",
        "\n\n".join(
            [
                f"`llm: {llm}` authenticates with a GitHub OIDC token, which this job cannot mint.",
                f"In `{workflow_path(environ)}`, the job running this action needs:",
                "```yaml\n    permissions:\n      id-token: write\n```",
                "No secret is involved: the token is minted per request and never stored.",
            ]
        ),
    )


def _resolve_byok(table: dict, name: str, given: dict[str, str], environ: dict[str, str]) -> dict:
    provider = table["providers"][name]
    foreign = sorted(i for i in given if owner_of(table, i) != name)
    if foreign:
        other = owner_of(table, foreign[0])
        raise ConfigError(
            "foreign_provider_key",
            f"`llm: {name}` is selected, but `{foreign[0]}` is set, which configures "
            f"`{other}`. Set only {provider['label']}'s inputs, or change `llm` to `{other}`.",
        )

    env = {var: given[i] for i, var in provider["inputs"].items() if i in given}
    # Core selects a provider when one of its selection_envs is set. Anything else -- an
    # API key that is not a selection variable, a region -- cannot make it usable, which
    # is why a key alone does not configure ollama or litellm.
    if not any(env.get(var) for var in provider["selection_envs"]):
        wanted = [i for i, var in provider["inputs"].items() if var in provider["selection_envs"]]
        keys = [i for i in wanted if i.endswith("_api_key")]
        needed = " or ".join(f"`{i}`" for i in wanted)
        # A base URL is configuration, not a credential; only send people to the secrets
        # page for the inputs that actually belong there.
        if keys:
            secret = provider["inputs"][keys[0]]
            fix = f"Add the {secret} repository secret ({SETTINGS_HINT}) and wire it as `{keys[0]}`."
            details = "\n\n".join(
                [
                    f"`llm: {name}` needs {needed}, and none is set.",
                    "**1.** " + _add_secret(environ, secret, f"your {provider['label']} API key"),
                    "**2.** " + _wire_it(environ, name, [f"{keys[0]}: ${{{{ secrets.{secret} }}}}"]),
                ]
            )
        else:
            fix = f"Set `{wanted[0]}` on the action step to your {provider['label']} endpoint."
            details = "\n\n".join(
                [
                    f"`llm: {name}` needs {needed}, and none is set.",
                    _wire_it(environ, name, [f"{wanted[0]}: https://your-{name}-host"]),
                ]
            )
        raise ConfigError(
            "missing_provider_key",
            f"`llm: {name}` needs {needed}, and none is set. {fix}",
            details,
        )
    return env


def resolve(table: dict, environ: dict[str, str]) -> dict:
    """The whole contract. Returns a plan; raises ConfigError with the reason otherwise."""
    llm = environ.get("CB_IN_LLM", "").strip().lower()
    license_key = environ.get("CB_IN_LICENSE_KEY", "").strip()
    given = read_inputs(table, environ)

    if not llm:
        raise ConfigError(
            "missing_llm",
            "The `llm` input is required and has no default. Set it to `hosted` "
            "(CodeBoarding's free tier), `license` (a CodeBoarding plan), or one of: "
            f"{_provider_list(table)}. See {DOCS}.",
        )

    if llm == "hosted":
        _reject_provider_inputs(table, given, llm, "hosted")
        if license_key:
            raise ConfigError(
                "hosted_with_license",
                "`llm: hosted` is the free tier and never spends a licence. Set `llm: license` "
                "to run your CodeBoarding plan, or remove `license_key`.",
            )
        _require_id_token(llm, environ)
        return {"tier": "hosted", "provider": table["hosted_provider"], "env": {}}

    if llm == "license":
        _reject_provider_inputs(table, given, llm, "license")
        if not license_key:
            raise ConfigError(
                "missing_license_key",
                "`llm: license` needs `license_key`, which is empty. Add the "
                f"CODEBOARDING_LICENSE repository secret ({SETTINGS_HINT}) and wire it.",
                "\n\n".join(
                    [
                        "`llm: license` needs `license_key`, which is empty.",
                        "**1.** " + _add_secret(environ, "CODEBOARDING_LICENSE", "your CodeBoarding licence key"),
                        "**2.** " + _wire_it(environ, "license", ["license_key: ${{ secrets.CODEBOARDING_LICENSE }}"]),
                    ]
                ),
            )
        _require_id_token(llm, environ)
        return {
            "tier": "license",
            "provider": table["hosted_provider"],
            "license": license_key,
            "env": {},
        }

    name = llm
    if name not in table["providers"]:
        raise ConfigError(
            "unknown_llm",
            f"`llm: {environ.get('CB_IN_LLM', '').strip()}` is not a value this action "
            f"understands. Use `hosted`, `license`, or one of: {_provider_list(table)}. "
            f"See {DOCS}.",
        )

    env = _resolve_byok(table, name, given, environ)
    # A licence alongside a provider key is deliberately allowed, not an error: it says
    # "my CodeBoarding plan, my own tokens". Nothing enforces it yet -- direct provider
    # calls never reach our proxy -- so it is recorded for the surfaces that read it.
    return {
        "tier": "byok+license" if license_key else "byok",
        "provider": name,
        "env": env,
    }


def _is_endpoint(var: str) -> bool:
    """Configuration rather than credential: safe to name in an artifact, worth hashing."""
    return var.endswith(("_BASE_URL", "_HOST")) or var == "AWS_DEFAULT_REGION"


def _pays(table: dict, plan: dict) -> str:
    """What is actually paying for this run's tokens, in one phrase.

    Endpoint-only runs are the reason this is not just the tier name. `llm: openai` with a
    base URL and no key, and every keyless ollama or litellm run, resolve with no API key
    at all -- with-auth.sh strips any inherited one -- so "your own OpenAI key" would name
    a credential that is not there.
    """
    tier, provider = plan["tier"], plan["provider"]
    if tier == "hosted":
        return "CodeBoarding's hosted free tier"
    if tier == "license":
        return "CodeBoarding's hosted tier, on your plan"
    entry = table["providers"].get(provider, {})
    label = entry.get("label", provider)
    key_envs = {var for i, var in entry.get("inputs", {}).items() if i.endswith("_api_key")}
    if any(plan["env"].get(var) for var in key_envs):
        return f"your own {label} key, called directly"
    return f"your own {label} endpoint, called directly with no API key"


def plan_headline(table: dict, plan: dict) -> str:
    """The one line the log opens with. Same source as the summary, so they cannot drift."""
    sentence = f"CodeBoarding is running on {_pays(table, plan)}."
    if plan["tier"] == "byok+license":
        return sentence + " The wired CodeBoarding plan is not spent on a direct call."
    return sentence


def plan_summary(table: dict, plan: dict) -> list[tuple[str, str]]:
    """What this run is actually about to do, for the job summary.

    "Tier: byok+license" names the configuration without answering the question someone
    reads a summary to answer, which is *which credential pays*. A direct provider call
    never reaches CodeBoarding, so a licence wired beside your own key is recorded and
    not spent; saying only "byok+license" leaves that ambiguous, so it is spelled out.
    """
    tier, provider = plan["tier"], plan["provider"]
    rows = [("Tier", f"`{tier}`"), ("Provider", f"`{provider}`")]
    rows.append(("Credentials", _pays(table, plan)))
    if tier == "byok+license":
        rows.append(
            (
                "Licence",
                "wired, and not spent: a direct provider call never reaches CodeBoarding",
            )
        )
    # Only where the run was pointed somewhere other than the default, since that is the
    # setting most likely to be wrong and least likely to be noticed.
    for var, value in sorted(plan["env"].items()):
        if _is_endpoint(var):
            rows.append((f"`{var}`", f"`{value}`"))
    return rows


def write_auth_dir(table: dict, plan: dict, auth_dir: Path) -> None:
    """Lay the plan out as files the later steps read, readable only by this user."""
    os.umask(0o077)
    env_dir = auth_dir / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "tier").write_text(plan["tier"], encoding="utf-8")
    (auth_dir / "provider-name").write_text(plan["provider"], encoding="utf-8")
    for var, value in plan["env"].items():
        (env_dir / var).write_text(value, encoding="utf-8")
    if plan.get("license"):
        (auth_dir / "license.txt").write_text(plan["license"], encoding="utf-8")
    # Every variable core knows about, minus the ones this run actually resolved, so
    # with-auth.sh can strip the rest without carrying its own copy of the list to fall
    # behind on.
    #
    # Keyed on what was RESOLVED, not on which provider was selected. Sparing every
    # variable the selected provider could use would leave an inherited value in place
    # for the ones it did not: `llm: openai` with only `openai_base_url` set would let a
    # stray OPENAI_API_KEY from the job environment supply the credentials, which is the
    # same silent substitution this contract exists to prevent, one provider narrower.
    keep = set(plan["env"])
    everything = {
        var
        for provider in table["providers"].values()
        for var in list(provider["selection_envs"]) + list(provider["inputs"].values())
    }
    # Trailing newline, because `read` returns non-zero on an unterminated final line and
    # a `while read` loop therefore skips it. Without it the alphabetically last variable
    # was never unset: an inherited VERCEL_BASE_URL survived an Anthropic run and let core
    # see two providers configured. with-auth.sh guards the same case from its side.
    # What the run talks to, minus anything secret. The reusable-analysis name is built
    # from this: pointing `openai_base_url` at a different backend, or moving Bedrock to
    # another region, produces different analysis, so it must not restore a bundle built
    # against the old one. Keys are deliberately excluded -- they do not change what the
    # model says, and rotating one should not throw away a warm start.
    backend = [f"{plan['tier']}:{plan['provider']}"]
    backend += [f"{var}={value}" for var, value in sorted(plan["env"].items()) if _is_endpoint(var)]
    (auth_dir / "backend-id").write_text("\n".join(backend), encoding="utf-8")

    foreign = sorted(everything - keep)
    (auth_dir / "foreign-envs").write_text("".join(f"{var}\n" for var in foreign), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, help="Write the resolved plan here.")
    args = parser.parse_args(argv)

    table = load_table()
    try:
        plan = resolve(table, dict(os.environ))
    except ConfigError as error:
        json.dump(
            {
                "ok": False,
                "error": error.code,
                "message": error.message,
                "details": error.details,
            },
            sys.stdout,
        )
        print()
        return 1

    if args.auth_dir:
        write_auth_dir(table, plan, args.auth_dir)
    json.dump(
        {
            "ok": True,
            "error": "",
            "message": "",
            "details": "",
            "tier": plan["tier"],
            "provider": plan["provider"],
            "summary": "\n".join(f"| {k} | {v} |" for k, v in plan_summary(table, plan)),
            "headline": plan_headline(table, plan),
        },
        sys.stdout,
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
