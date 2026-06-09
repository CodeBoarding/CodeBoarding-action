# AGENT.md — CodeBoarding-action

Composite GitHub Action (`action.yml`) that posts an architecture-diff Mermaid
diagram on PRs. The analysis engine is `CodeBoarding/CodeBoarding`, checked out
at the `engine_ref` input's pinned default in `action.yml` — engine changes
reach users only when that default is bumped *and* a new action release ships.

## Releases — read before committing

Consumers install via `uses: CodeBoarding/CodeBoarding-action@v1`, a moving
major tag resolved fresh on every workflow run. Shipping = cutting a GitHub
release; merging to `main` alone ships nothing.

Flow: release-please (`.github/workflows/release-please.yml`) maintains a
"chore: release X.Y.Z" PR from the Conventional Commits on `main`. Merging that
PR tags `vX.Y.Z`, creates the GitHub Release, and force-moves `v1`. A manually
published `vX.Y.Z` release also moves `v1` (`release-major-tag.yml`).

## Commit message rules (enforced by the release flow, not by lint)

release-please derives version bumps and the changelog ONLY from Conventional
Commit messages:

- `feat: ...` → minor bump (1.1.0 → 1.2.0)
- `fix: ...` → patch bump
- `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer → major bump. Avoid unless
  intended: `@v1` consumers never receive v2 automatically.
- `chore:` / `docs:` / `ci:` / `refactor:` / `test:` → included in the next
  release but do not trigger one on their own.

Consequences:

- A change adopters should receive MUST be committed as `feat:` or `fix:` —
  with any other prefix release-please silently never proposes a release for it.
- Use the conventional format in individual commits AND PR titles: merge
  commits are skipped as unparseable (the underlying PR commits are read), and
  a squash-merge keeps only the PR title.
- Bumping the default `engine_ref` in `action.yml` is a `feat:` (or `fix:` for
  an engine hotfix) — it changes what users run.
- Never hand-edit `.release-please-manifest.json`, `version.txt`, or
  `CHANGELOG.md`, with one exception: after a *manual* release, re-sync the
  manifest to the released version, or the next release PR will propose an
  already-existing tag and fail.
