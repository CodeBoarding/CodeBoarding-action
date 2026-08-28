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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalise(text: str) -> str:
    """Line endings and trailing whitespace, and nothing cleverer.

    Anything more forgiving starts matching files we did not write, which turns "this is
    yours, we will not touch it" into a silent overwrite.
    """
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def credential_fills() -> dict[str, str]:
    """Every credential block a generated workflow can carry, keyed by tier or provider.

    `byok` is one authored fill expanded across the provider table, so adding a provider is
    a change to that table and nowhere else.
    """
    fills = {
        "hosted": _read(TEMPLATES / "fills" / "credentials.hosted.yml"),
        "license": _read(TEMPLATES / "fills" / "credentials.license.yml"),
    }
    byok = _read(TEMPLATES / "fills" / "credentials.byok.yml")
    table = json.loads(_read(PROVIDERS))
    for name, provider in table["providers"].items():
        key_input = next(i for i in provider["inputs"] if i.endswith("_api_key"))
        fills[f"byok:{name}"] = (
            byok.replace("{{LABEL}}", provider["label"])
            .replace("{{SECRET}}", provider["inputs"][key_input])
            .replace("{{KEY_INPUT}}", key_input)
            .replace("{{LLM}}", name)
        )
    return fills


def fills_for(kind: str, tier: str, delivery: str) -> dict[str, str]:
    """What each hole in `kind` takes, for one configuration."""
    creds = credential_fills()[tier]
    if kind == "review":
        return {
            "CREDENTIALS": creds,
            "SYNC_PR_GUARD": _read(TEMPLATES / "fills" / f"sync_pr_guard.{delivery}.yml"),
        }
    return {
        "CREDENTIALS": creds,
        "DELIVERY_PERMISSION": _read(TEMPLATES / "fills" / f"delivery.{delivery}.permission.yml"),
        "DELIVERY_INPUT": _read(TEMPLATES / "fills" / f"delivery.{delivery}.input.yml"),
    }


def render(kind: str, *, branch: str, tier: str, delivery: str, version: int | None = None) -> str:
    """The file we would write for this configuration."""
    template = _read(template_path(kind, version))
    values = {"BRANCH": branch, **fills_for(kind, tier, delivery)}
    return HOLE.sub(lambda m: values[m.group(1)], template)


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

    Returns the configuration read straight out of the captures: no YAML parsing, no
    inference, and no way for the answer to be half-known.
    """
    found = to_pattern(_read(template_path(kind, version))).match(normalise(text))
    if not found:
        return None
    captured = {k.rsplit("_", 1)[0]: v for k, v in found.groupdict().items()}
    result: dict[str, str] = {}
    if "BRANCH" in captured:
        result["branch"] = captured["BRANCH"]
    for tier, body in credential_fills().items():
        if normalise(body).rstrip("\n") == captured.get("CREDENTIALS", "").rstrip("\n"):
            result["tier"] = tier
            break
    else:
        return None  # a credential block we never wrote: the file has been edited
    guard = captured.get("SYNC_PR_GUARD", captured.get("DELIVERY_INPUT", ""))
    result["delivery"] = "pull_request" if guard.strip() else "push"
    return result


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
        "delivery": {
            d: {
                "permission": _read(TEMPLATES / "fills" / f"delivery.{d}.permission.yml"),
                "input": _read(TEMPLATES / "fills" / f"delivery.{d}.input.yml"),
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
