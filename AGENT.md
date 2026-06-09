# AGENT.md — CodeBoarding-action

This repo is a GitHub Action that gives pull requests a visual system-design
review: it analyzes the PR with the CodeBoarding engine and posts a diagram of
the added, modified, and removed components as a PR comment. The engine
(`CodeBoarding/CodeBoarding`) is pinned to a release in `action.yml`; engine
changes reach users only when that pin is bumped *and* a new action release
ships.

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
