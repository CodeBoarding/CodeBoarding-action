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
- ✅ PR-head `analysis.json` and metadata containing the PR base SHA plus the committed baseline SHA when one was found — stored as a GitHub Actions artifact.

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
