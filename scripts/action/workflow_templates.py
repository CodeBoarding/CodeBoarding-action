#!/usr/bin/env python3
"""Render a workflow template, or recognise one that is already committed.

Both directions from one file, because they are the same knowledge read two ways. A hole is
substituted to render and captured to match, so a template can never be renderable but
unmatchable, which is exactly how a generator and a detector drift apart when they are
written separately.

Recognising beats parsing. If a committed workflow matches the v3 template, we wrote v3, so
its triggers, permissions and credential wiring are already known and none of them has to be
inferred from the file. A file that matches nothing has been edited, which is a fact rather
than the heuristic guess that "no extra steps and no extra inputs" gives.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = ROOT / "templates"
PROVIDERS = ROOT / "scripts" / "action" / "supported-providers.json"

HOLE = re.compile(r"\{\{(\w+)\}\}")


def _same(fill: str, captured: str) -> bool:
    """Normalised, and forgiving only about the trailing newline a hole cannot capture."""
    return normalise(fill).rstrip("\n") == normalise(captured).rstrip("\n")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalise(text: str) -> str:
    """Line endings and trailing whitespace, and nothing cleverer.

    Anything more forgiving starts matching files we did not write, which turns "this is
    yours, we will not touch it" into a silent overwrite.
    """
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def credential_fills(version: int | None = None) -> dict[str, str]:
    """Every credential block a generated workflow can carry, keyed by tier or provider.

    `byok` is one authored fill expanded across the provider table, so adding a provider is
    a change to that table and nowhere else.
    """
    root = fills_root(version)
    fills = {
        "hosted": _read(root / "credentials.hosted.yml"),
        "license": _read(root / "credentials.license.yml"),
    }
    byok = _read(root / "credentials.byok.yml")
    endpoint = _read(root / "credentials.byok-endpoint.yml")
    table = json.loads(_read(providers_path(version)))
    for name, provider in table["providers"].items():
        # The input that SELECTS the provider, not merely the one that looks like a key.
        # ollama and litellm are selected by their base URL, so a workflow wiring
        # `ollama_api_key` is refused by the action's own contract with
        # `missing_provider_key`, after telling the user to create a secret that could
        # never have worked. The contract already draws this line; the fill has to as well.
        selectors = [i for i, var in provider["inputs"].items() if var in provider["selection_envs"]]
        keys = [i for i in selectors if i.endswith("_api_key")]
        if keys:
            fills[f"byok:{name}"] = (
                byok.replace("{{LABEL}}", provider["label"])
                .replace("{{SECRET}}", provider["inputs"][keys[0]])
                .replace("{{KEY_INPUT}}", keys[0])
                .replace("{{LLM}}", name)
            )
        else:
            fills[f"byok:{name}"] = (
                endpoint.replace("{{LABEL}}", provider["label"])
                .replace("{{SELECTOR}}", selectors[0])
                .replace("{{LLM}}", name)
            )
    return fills


def extra_permissions(tier: str, delivery: str, version: int | None = None) -> str:
    """The optional lines in the sync job's `permissions:` block, in file order."""
    root = fills_root(version)
    oidc = "byok" if tier.startswith("byok") else "hosted"
    return _read(root / f"delivery.permission.{delivery}.yml") + _read(root / f"oidc.sync.{oidc}.yml")


def fills_for(kind: str, tier: str, delivery: str, version: int | None = None) -> dict[str, str]:
    """What each hole in `kind` takes, for one configuration."""
    root = fills_root(version)
    creds = credential_fills(version)[tier]
    # Least privilege, and it is the credentials that decide it: only the hosted tiers mint
    # an OIDC token, so a workflow running on the user's own provider key has no reason to
    # let a moving third-party action identify their repository.
    oidc = "byok" if tier.startswith("byok") else "hosted"
    if kind == "review":
        return {
            "CREDENTIALS": creds,
            "OIDC_PERMISSION": _read(root / f"oidc.review.{oidc}.yml"),
            "SYNC_PR_GUARD": _read(root / f"sync_pr_guard.{delivery}.yml"),
        }
    return {
        "CREDENTIALS": creds,
        # One hole, not two. The delivery permission and the OIDC permission are adjacent
        # lines in the same block, and two adjacent holes cannot be told apart: the regex
        # would let the first capture nothing and the second capture both.
        "EXTRA_PERMISSIONS": extra_permissions(tier, delivery, version),
        "DELIVERY_INPUT": _read(root / f"delivery.input.{delivery}.yml"),
    }


def render(kind: str, *, branch: str, tier: str, delivery: str, version: int | None = None) -> str:
    """The file we would write for this configuration."""
    template = _read(template_path(kind, version))
    values = {"BRANCH": yaml_scalar(branch), **fills_for(kind, tier, delivery, version)}
    return HOLE.sub(lambda m: values[m.group(1)], template)


def yaml_scalar(value: str) -> str:
    """A value safe to drop inside single quotes.

    Branch names may contain an apostrophe: `release/o'neil` is a valid ref, and
    interpolating it raw produced `branches: ['release/o'neil']`, which GitHub cannot
    parse. Doubling is how a single-quoted YAML scalar escapes one.
    """
    return value.replace("'", "''")


def fills_root(version: int | None = None) -> Path:
    """Fills belong to the version that shipped them.

    A historical template with today's fills is not that historical template. If a fill's
    wording or the provider table changed since, every repository on that version would
    stop matching and be reported as hand-edited, which is exactly the failure the history
    exists to prevent.
    """
    return TEMPLATES / "fills" if version is None else TEMPLATES / "history" / f"v{version}" / "fills"


def providers_path(version: int | None = None) -> Path:
    if version is None:
        return PROVIDERS
    return TEMPLATES / "history" / f"v{version}" / "supported-providers.json"


def template_path(kind: str, version: int | None = None) -> Path:
    name = "codeboarding.yml" if kind == "review" else "codeboarding-sync.yml"
    if version is None:
        return TEMPLATES / name
    return TEMPLATES / "history" / f"v{version}" / name


def to_pattern(template: str) -> re.Pattern[str]:
    """The same template as a matcher, each hole a capture group.

    Built from the template rather than written alongside it, so the two cannot disagree
    about what is fixed and what is configurable.
    """
    parts, holes, last = [], [], 0
    for hole in HOLE.finditer(template):
        parts.append(re.escape(normalise(template[last : hole.start()])))
        holes.append(hole.group(1))
        parts.append(f"(?P<{hole.group(1)}_{len(holes)}>[\\s\\S]*?)")
        last = hole.end()
    parts.append(re.escape(normalise(template[last:])))
    # `\\Z`, not `$`: `$` also matches just before a trailing newline, so a hole at the end
    # of a template captures one character less than the fill that produced it.
    return re.compile("\\A" + "".join(parts) + "\\Z")


def match(kind: str, text: str, version: int | None = None) -> dict[str, str] | None:
    """What produced this file, or None when nothing we shipped did.

    Every capture is checked against what we could have written there. The holes accept
    arbitrary text by construction, so without that check an edited delivery block or a
    credential block we never authored would still "match", and the update path would then
    claim ownership of a file it did not write and overwrite the user's edits.
    """
    template = _read(template_path(kind, version))
    found = to_pattern(template).match(normalise(text))
    if not found:
        return None

    # A hole that appears twice must capture the same value both times. `branches:` and
    # `target_branch:` are one setting written in two places; editing only one of them is
    # an edit, not a configuration we generated.
    captured: dict[str, str] = {}
    for group, value in found.groupdict().items():
        name = group.rsplit("_", 1)[0]
        if name in captured and captured[name] != value:
            return None
        captured[name] = value

    result: dict[str, str] = {}
    if "BRANCH" in captured:
        result["branch"] = captured["BRANCH"]

    fills = credential_fills(version)
    tier = next(
        (t for t, body in fills.items() if _same(body, captured.get("CREDENTIALS", ""))),
        None,
    )
    if tier is None:
        return None  # a credential block we never wrote: the file has been edited
    result["tier"] = tier

    # Delivery is two or three separate holes that have to describe the SAME mode. Reading
    # one of them and inferring the rest would accept a file with a pull-request guard and
    # a push permission, which is not something we ever generate.
    for delivery in ("push", "pull_request"):
        if all(
            _same(expected, captured[key])
            for key, expected in (
                ("SYNC_PR_GUARD", _read(fills_root(version) / f"sync_pr_guard.{delivery}.yml")),
                ("EXTRA_PERMISSIONS", extra_permissions(tier, delivery, version)),
                ("DELIVERY_INPUT", _read(fills_root(version) / f"delivery.input.{delivery}.yml")),
            )
            if key in captured
        ):
            result["delivery"] = delivery
            return result
    return None  # the delivery holes disagree, or none of them is a fill we authored


def bundle() -> dict:
    """Everything a consumer needs, in one file it can import.

    The webview bundles its generator into a browser build, so it cannot read these files
    from disk. Rather than have it keep a second, hand-maintained copy of the text, the
    action publishes the templates as data and the webview vendors that one artifact.
    `tests/test_workflow_templates.py` asserts this matches the .yml files it is built from,
    so the authored template stays the thing under review.
    """
    log = json.loads(_read(TEMPLATES / "CHANGELOG.json"))
    kinds = {"review": "codeboarding.yml", "sync": "codeboarding-sync.yml"}
    return {
        "current": log["current"],
        "changelog": log["versions"],
        "templates": {k: _read(TEMPLATES / name) for k, name in kinds.items()},
        "credentials": credential_fills(),
        "oidc": {
            kind: {t: _read(TEMPLATES / "fills" / f"oidc.{kind}.{t}.yml") for t in ("hosted", "byok")}
            for kind in ("review", "sync")
        },
        "delivery": {
            d: {
                "permission": _read(TEMPLATES / "fills" / f"delivery.permission.{d}.yml"),
                "input": _read(TEMPLATES / "fills" / f"delivery.input.{d}.yml"),
                "sync_pr_guard": _read(TEMPLATES / "fills" / f"sync_pr_guard.{d}.yml"),
            }
            for d in ("push", "pull_request")
        },
    }


def write_bundle() -> Path:
    path = TEMPLATES / "bundle.json"
    path.write_text(json.dumps(bundle(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(write_bundle())
