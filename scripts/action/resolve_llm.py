#!/usr/bin/env python3
"""Resolves the run's LLM credentials from the action's inputs, or explains why it cannot.

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
TABLE = Path(__file__).resolve().parent / "llm-providers.json"


class ConfigError(Exception):
    """A configuration the action refuses to run, with the code the webview keys on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
    if environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
        return
    raise ConfigError(
        "missing_id_token",
        f"`llm: {llm}` authenticates with a GitHub OIDC token, which this job cannot mint. "
        "Add `permissions:` with `id-token: write` to the job that uses this action.",
    )


def _resolve_byok(table: dict, name: str, given: dict[str, str]) -> dict:
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
            fix = (
                f"Add the {secret} repository secret ({SETTINGS_HINT}) and wire it as "
                f"`{keys[0]}: ${{{{ secrets.{secret} }}}}`."
            )
        else:
            fix = f"Set `{wanted[0]}` on the action step to your {provider['label']} endpoint."
        raise ConfigError(
            "missing_provider_key",
            f"`llm: {name}` needs {needed}, and none is set. {fix}",
        )
    return env


def resolve(table: dict, environ: dict[str, str]) -> dict:
    """The whole contract. Returns a plan; raises ConfigError with the reason otherwise."""
    llm = re.sub(r"[\s-]+", "_", environ.get("CB_IN_LLM", "").strip().lower())
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
        return {"tier": "hosted", "provider": table["hosted_provider"], "hosted": True, "env": {}}

    if llm == "license":
        _reject_provider_inputs(table, given, llm, "license")
        if not license_key:
            raise ConfigError(
                "missing_license_key",
                "`llm: license` needs `license_key`, which is empty. Add the "
                f"CODEBOARDING_LICENSE repository secret ({SETTINGS_HINT}) and wire it as "
                "`license_key: ${{ secrets.CODEBOARDING_LICENSE }}`.",
            )
        _require_id_token(llm, environ)
        return {
            "tier": "license",
            "provider": table["hosted_provider"],
            "hosted": True,
            "license": license_key,
            "env": {},
        }

    name = table["aliases"].get(llm, llm)
    if name not in table["providers"]:
        raise ConfigError(
            "unknown_llm",
            f"`llm: {environ.get('CB_IN_LLM', '').strip()}` is not a value this action "
            f"understands. Use `hosted`, `license`, or one of: {_provider_list(table)}. "
            f"See {DOCS}.",
        )

    env = _resolve_byok(table, name, given)
    # A licence alongside a provider key is deliberately allowed, not an error: it says
    # "my CodeBoarding plan, my own tokens". Nothing enforces it yet -- direct provider
    # calls never reach our proxy -- so it is recorded for the surfaces that read it.
    return {
        "tier": "byok+license" if license_key else "byok",
        "provider": name,
        "hosted": False,
        "env": env,
    }


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
    # Every selection variable core knows about, so with-auth.sh can strip the ones this
    # run did not ask for without carrying its own copy of the list to fall behind on.
    keep = set(table["providers"][plan["provider"]]["selection_envs"])
    keep |= set(table["providers"][plan["provider"]]["inputs"].values())
    everything = {
        var
        for provider in table["providers"].values()
        for var in list(provider["selection_envs"]) + list(provider["inputs"].values())
    }
    (auth_dir / "foreign-envs").write_text("\n".join(sorted(everything - keep)), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, help="Write the resolved plan here.")
    args = parser.parse_args(argv)

    table = load_table()
    try:
        plan = resolve(table, dict(os.environ))
    except ConfigError as error:
        json.dump({"ok": False, "error": error.code, "message": error.message}, sys.stdout)
        print()
        return 1

    if args.auth_dir:
        write_auth_dir(table, plan, args.auth_dir)
    json.dump(
        {
            "ok": True,
            "error": "",
            "message": "",
            "tier": plan["tier"],
            "provider": plan["provider"],
            "hosted": plan["hosted"],
        },
        sys.stdout,
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
