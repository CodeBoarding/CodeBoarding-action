# AGENTS.md — CodeBoarding-action

This repo is a GitHub Action with two modes, selected by the `mode` input:

- **`mode: review`** (default): analyzes a PR with the CodeBoarding engine and
  posts a Mermaid diagram of the added, modified, and removed components as a PR
  comment (runs on `pull_request` / `issue_comment`).
- **`mode: sync`**: on push to a branch, regenerates the architecture and commits
  the versioned baseline (`.codeboarding/analysis.json` + rendered markdown) back
  to the branch, so review mode always diffs against a current baseline.

The action is a thin orchestration wrapper, not the analysis engine: the engine
(`CodeBoarding/CodeBoarding`) is a separate repo checked out at runtime and
pinned to a release in `action.yml`. `scripts/engine_adapter.py` is the CLI
adapter into it (no analysis logic lives there). Engine changes reach users only
when that pin is bumped *and* a new action release ships.

## Bumping the engine pin

`scripts/action/llm-providers.json` mirrors the pinned release's `LLM_PROVIDERS`.
Credentials are validated before the engine is installed — that is what makes a
misconfigured run fail in seconds instead of a minute — so the action cannot ask
the engine at run time and keeps this copy instead.

When you change the `codeboarding==` pin in `action.yml`, update that file in the
same commit: its `engine` field, and any provider whose selection variables
changed. `tests/test_provider_table_drift.py` runs in the `core-compatibility` CI
job and fails when they disagree. Adding a provider also means adding its
`<name>_api_key` input to `action.yml`; `tests/test_action_inputs.py` checks that.

## Protected tests

Some tests encode a behavioural contract that is expensive to rediscover once
lost. They are marked with a `PROTECTED TEST` header naming what they protect.

**No agent, assistant, or automated tool may edit, weaken, skip, rename or
delete a protected test — not even to make a build pass. Only a human may
change one, and only after explicitly saying so in that conversation.** A
request to "fix the failing tests" is not that consent.

When a protected test fails, the behaviour it describes regressed. Fix the code.
If you believe the test itself is wrong, stop and say so, then wait for a human
to decide.

Protected tests:

- `tests/test_merge_base_contract.py` — a review compares a pull request against
  its merge base, never against the base branch tip. Comparing against the tip
  attributes other people's commits to the pull request and reports them
  backwards, as removals.
- `tests/test_action_auth.py::test_a_named_provider_never_falls_back_to_codeboarding_credentials`
  — a workflow that names a provider runs on that provider or fails. The action
  used to read an empty key as "no preference" and resolve it to CodeBoarding's
  hosted tier, so a repository that had not added its secret yet went green
  while running on another vendor's model and CodeBoarding's money, silently.
  Any change that reintroduces a credential fallback breaks this test.

## Releases

Consumers reference a moving major tag (`uses: CodeBoarding/CodeBoarding-action@v1`),
which GitHub resolves fresh on every workflow run. Shipping = publishing a
GitHub release, which re-points the major tag. Merging to `main` alone ships
nothing.

Releases are automated with release-please: it maintains a release PR from the
Conventional Commits on `main`; merging that PR tags the new version, publishes
the release, and moves the major tag.

## Commit messages: always Conventional Commits

release-please derives version bumps and the changelog ONLY from commit
messages, so every commit and PR title must follow Conventional Commits:

- `feat:` → minor bump
- `fix:` → patch bump
- `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer → major bump. Avoid unless
  intended: consumers pinned to the old major tag never receive it automatically.

A deliberate exception exists. The explicit-credentials change (`llm` required,
no fallback) is a breaking change that shipped as `feat:`, not `feat!:`. A major
bump moves adopters to `v2` and freezes `v1`, which would have left every
existing workflow on the old silent-fallback behaviour forever — the opposite of
the intent. Shipping it as a minor bump on the moving `v1` tag is what makes
adopters actually receive it. Do not "correct" this to `feat!:` after the fact.
- `chore:` / `docs:` / `ci:` / `refactor:` / `test:` → ride along in the next
  release but do not trigger one.

Consequences:

- Any change adopters should receive MUST be `feat:` or `fix:` — with other
  prefixes, release-please silently never proposes a release for it.
- Use the conventional format in individual commits AND PR titles: merge
  commits are skipped as unparseable, and a squash-merge keeps only the PR title.
- Don't hand-edit release-please's bookkeeping files (manifest, version,
  changelog). One exception: after a *manual* release, re-sync the manifest to
  the released version, or the next release PR will propose an
  already-existing tag and fail.
