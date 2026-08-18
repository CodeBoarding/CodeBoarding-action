# Baseline & artifact commit strategy

What CodeBoarding writes into a repo, what sync mode commits, and what review
mode stores as workflow artifacts.

## The artifacts

The engine writes these under `.codeboarding/`:

| File | Type | Size | Purpose |
|---|---|---|---|
| `analysis.json` | JSON (text) | KB–low MB | The component graph — **the diagram source** |
| `health/health_report.json` | JSON (text) | KB | Health findings → **the warnings** |
| `static_analysis.pkl` | binary pickle | MB-scale | LSP/CFG cache → **warm-start** (re-LSP only changed files) |
| `static_analysis.sha` | text (1 line) | bytes | Tag recording the pkl's commit → the warm-start gate |

## Decision

**Commit in sync mode:**
- ✅ `analysis.json` — required for the extension (and later the webview) to **show the diagram instantly without regenerating** — i.e. without spending the user's API key. It's text and diffs meaningfully.
- ✅ `health/health_report.json` — required for warnings in the extension/webview. Small text.
- ✅ `static_analysis.pkl` + `static_analysis.sha` — required for reliable warm-start incremental sync from the committed baseline.

**Upload in review mode:**
- ✅ Both sides of the comparison — the PR-head `analysis.json` and the `base_analysis.json` it was measured against — plus metadata naming the base tip, the merge base, and how the head was seeded. Stored as a GitHub Actions artifact, kept 30 days.

The artifact is the only one of these channels a reader outside the workflow can use: the Actions cache has no download API, so anything the webview needs has to ship here. Carrying the base graph per run duplicates a text file, but it keeps each artifact self-contained; the alternative is a reader walking back through older artifacts for a base that may already have expired.

**Cache between runs (never committed):**
- ✅ The whole analysis state directory, twice: once per pull request (its head state, so the next run only covers new commits) and once per base commit (the merge base's analysis, shared by every pull request that forked from it). Sync mode publishes the base entry as a by-product of the baseline it already computes.

> **Principle:** sync mode is the only git writer. Review mode never commits generated files to PR branches, so generated artifacts cannot conflict with `main` during merge.

**Delivery (`sync_strategy`).** The *set* of committed files above is identical either way; only how it reaches `main` differs. `push` (default) fast-forwards `main` directly. `pull_request` (for protected `main`) commits the same files to a machine-owned `sync_pr_branch` and opens one rolling PR into `main` — the baseline reaches `main` only on merge. Incremental sync always seeds from the baseline committed on `main` (never from the unmerged PR branch — that keeps an untrusted `static_analysis.pkl` off the runner), so under `pull_request` the rolling PR must be merged on a cadence to keep the baseline warm; each run still re-detects **every** change since the last-merged baseline via the whole-tree `fingerprint.json`, so no commits are missed between merges.

## Where to commit — two separate workflows

1. **CI/CD on `main` (the baseline keeper).** On push to `main`, regenerate and commit `analysis.json`, `static_analysis.pkl`, `static_analysis.sha`, `health/health_report.json`, and rendered docs to `main`. Keeps the baseline current so PRs diff against an accurate, up-to-date snapshot and the extension shows a real diagram on the default branch.

2. **The review action (PR).** Comment plus GitHub Actions artifact — no commits to contributors' branches (no churn, no generated-file merge conflicts, and it works on fork PRs where the token is read-only).

## Now vs. later

- **Now — extension-direct.** Committing `analysis.json` + `health_report.json` on `main` means a user who installs the extension and opens the repo sees the committed diagram + warnings **instantly, with no API key**. The PR comment's CTA points straight at the extension (install / open in editor).
- **Later — hosted webview.** The webview can read durable default-branch data from committed sync artifacts and PR-specific data from the uploaded review artifact or a backend copy of that artifact.

## Warm-start tradeoff (the `.pkl`)

The warm-start — and the engine's incremental path itself — needs the pkl **and** its `.sha`: the cluster baseline that drives incremental lives only inside the pkl. Sync mode commits the pair alongside `analysis.json`, and review mode can still fall back to Actions cache or deterministic seeding when needed:

- **Committed sync baseline:** sync copies `static_analysis.pkl` + `.sha` from `.codeboarding/` into the analysis workdir before incremental analysis.
- **No committed pkl or cache miss:** the action can seed the pkl deterministically (`engine_adapter.py seed`: LSP indexing + the same clustering call a full run makes — **no LLM calls**), then save it to `actions/cache`. Seeding is fail-open: if it fails, the head run falls back to a full analysis.

Either way the head analysis is seeded from that directory and runs incrementally when possible.

## Summary

| Artifact | Commit? | Where | Why |
|---|---|---|---|
| `analysis.json` | ✅ | sync commit on `main`; review artifact for PRs | diagram source |
| `health_report.json` | ✅ | sync commit on `main`; computed in review for comments, not uploaded | warnings |
| `static_analysis.pkl` | ✅ | sync commit on `main` only | warm-start incremental baseline |
| `static_analysis.sha` | ✅ | with `static_analysis.pkl` | warm-start gate |
| whole state directory | ❌ | Actions cache, per PR and per base commit | reuse the previous run instead of re-deriving from the base |

## Names

The two stores have different lifetimes, so they are named for different jobs.

**Artifact — one per workflow run, immutable once written.** GitHub scopes an
artifact to its run and offers no way to append to or replace an earlier one, so
the name only has to be unique, and a consumer finds it by listing a pull
request's artifacts and taking the newest:

    codeboarding-review-<run_id>-<attempt>/
      analysis.json        the head analysis, at metadata.head_sha
      base_analysis.json   what it was compared against, at metadata.merge_base_sha
      metadata.json        both SHAs, merge_base_resolved, seed_source, chain_depth

**Cache — shared across runs, found by key.** Here the name *is* the lookup, so
it carries everything that decides whether prior state still applies:

| Key | Scope | The question it answers |
|---|---|---|
| `cb-head-v1-<cfg>-pr<N>-mb<merge_base>-<head_sha>` | one pull request | what did this pull request's last run produce |
| `cb-base-v1-<cfg>-<merge_base>` | one commit | what does this commit's architecture look like |
| `cb-fork-v1-<cfg>-<fork_repo>-pr<N>-mb<merge_base>-<head_sha>` | one fork pull request | the same as `cb-head`, quarantined |

`<cfg>` digests the cache schema version, the pinned CodeBoarding version,
`.codeboardingignore`, and the model selection. A run forced with
`/codeboarding refresh` or `full` appends `-<mode><run_id>.<attempt>`, because
cache entries are immutable and it must not save under the key holding the state
it was told to discard.

The chain is restored **by prefix**, which selects the newest entry for that
pull request. The base is restored **by exact key first**: an exact hit is that
merge base's own analysis and needs no engine run, while a prefix hit is only
another commit's baseline, usable as a warm seed for the catch-up.

## Cache identity

The cache key pins everything that makes prior state meaningful: the pinned
CodeBoarding version, `.codeboardingignore`, and the merge base (which in turn
pins the analysis depth, since depth is read from that commit's baseline).
Anything else changing means no key matches, so the run re-derives from the base
rather than reusing state that describes a different analysis.

State produced while analyzing a fork's code lives under its own key namespace
that trusted runs never restore from, and a fork run never writes a base entry:
`static_analysis.pkl` is a pickle, so restoring one derived from untrusted code
into a privileged run has to be impossible by construction, not by convention.
