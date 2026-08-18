# Baseline, cache, and artifact strategy

Where CodeBoarding keeps analysis state, and how a review run finds the two
graphs it compares.

## Three stores

| Store | Holds | Lifetime | Who can read it |
|---|---|---|---|
| **Git** (`sync` mode commits) | `.codeboarding/` on the target branch | forever | anyone — extension, webview, humans |
| **Actions cache** | whole analysis state dirs, pickle included | 7-day idle, 10 GB LRU | only a workflow run |
| **Workflow artifact** | the two graphs a review compared, plus metadata | 30 days | anyone with repo read, via the API |

The split matters: the cache is a run's **input** (warm start) and has no download
API, so nothing outside a run can ever read it. The artifact is a run's
**output**, and is the only channel the webview has.

## What sync commits

Sync is the only git writer. Review mode never commits to a contributor's
branch, so generated files cannot conflict during a merge.

| File | Why it is committed |
|---|---|
| `analysis.json` | the diagram, shown instantly with no API key |
| `fingerprint.json` | whole-tree file hashes — how incremental detects change |
| `static_analysis.pkl` | LSP/CFG cache and the cluster baseline incremental needs |
| `static_analysis.sha` | the warm-start gate for the pickle |
| `codeboarding_version.json` | when Core emits it |

Everything else generated is removed on sync: v1 architecture Markdown and
`health/health_report.json`. Hand-written Markdown and user configuration
(`.codeboardingignore`, health config) are preserved.

**Delivery (`sync_strategy`).** The committed set is identical either way; only
how it reaches the branch differs. `push` fast-forwards it directly.
`pull_request` commits the same files to the machine-owned `codeboarding/sync`
branch and keeps one rolling PR open — the baseline lands only on merge, so that
PR must be merged on a cadence to keep the baseline warm. Sync always seeds from
the baseline committed on the target branch, never from the unmerged PR branch,
which keeps an untrusted pickle off the runner. Nothing is missed between
merges: `fingerprint.json` re-detects every change since the last merged
baseline.

## How a review resolves its two graphs

Every review builds **base** (at the merge base) and **head** (at the PR head),
diffs them, and posts the result. Each side takes the first source that applies.

**Base**

| Source | Engine cost |
|---|---|
| `cb-base-…-<merge_base>` exact cache hit | none |
| `cb-base-…` prefix hit — another commit's baseline, used as a warm seed | one catch-up incremental |
| No cache — seed from `.codeboarding/` committed at the merge base | one catch-up incremental, nothing to do if that baseline is current |
| No committed baseline either | full analysis |

If the base was computed rather than restored, a trusted run saves it under its
exact key. How far that reaches depends on where the run happened, because a
cache entry is only visible to the ref that wrote it and to the default branch:

- **sync**, on the base branch, writes an entry every pull request can restore.
  This is what makes the first row common.
- **`/codeboarding`**, which runs on the default branch, also writes a shared
  entry.
- **an automatic `pull_request` run** writes into `refs/pull/<n>/merge`, so its
  entry serves only later runs of that same pull request. Another pull request
  with the identical merge base still computes its own.

So review runs warm themselves, and sync is what warms everybody.

**Head**

| Source | Covers |
|---|---|
| `cb-head-…-pr<N>-mb<merge_base>-` prefix hit — this pull request's last analysis | only the commits pushed since that run |
| No hit: first run, moved merge base, changed config, or `/codeboarding refresh` — copy the base state | the whole pull request |

Then the head state is saved under `cb-head-…-<head_sha>`.

**First run on a new pull request.** Nothing is cached. Base seeds from the
committed baseline and catches up; head starts from that base and covers every
file the pull request touches. Two engine passes, and both cache entries are
written.

**Every run after it.** Base is an exact hit and costs no engine work at all;
head continues from the previous run and covers only the new commits. One
engine pass.

## Names

**Artifact — one per run, immutable.** GitHub scopes an artifact to its run and
cannot append to an earlier one, so the name only has to be unique; a consumer
lists a pull request's artifacts and takes the newest.

    codeboarding-review-<run_id>-<attempt>/
      analysis.json        the head analysis, at metadata.head_sha
      base_analysis.json   what it was compared against, at metadata.merge_base_sha
      metadata.json        which commits those graphs describe

| `metadata.json` field | Meaning |
|---|---|
| `head_sha` | the commit `analysis.json` describes |
| `pr_base_sha` | the merge base, under the name the webview resolves |
| `merge_base_sha` | the same value under this action's own name |
| `merge_base_resolved` | `false` means the merge base could not be resolved, so the comparison is against `base_sha` and may include commits this pull request never made |
| `base_sha` | the base branch tip when the event fired — *not* what was compared against |
| `pr_number` | the pull request |
| `mode` | `incremental` or `full`, how the head graph was produced |
| `seed_source` | `pr-chain` or `base`, which state the head analysis grew from |
| `chain_depth` | how many incremental runs are stacked on the base |

The merge base is reported twice on purpose. The webview resolves a pull
request's base as `base_commit_sha || pr_base_sha || base_sha`, so a value
published only as `merge_base_sha` never reaches it and the chain falls through
to the branch tip — the drift this action stopped making, made again one layer
up.

The last three fields are diagnostics. They explain how a graph was produced,
not what it means, and nothing rendering a diagram needs them.

Shipping the base graph too keeps the artifact self-contained: a reader can
reproduce the comparison without resolving the merge base itself, and without
falling back to the default branch's committed baseline, which is the branch tip
rather than the fork point and would drift exactly as the review used to.

**Cache — shared across runs, found by key.** Here the name *is* the lookup, so
it carries everything that decides whether prior state still applies.

| Key | Scope | The question it answers |
|---|---|---|
| `cb-head-v1-<cfg>-pr<N>-mb<merge_base>-<head_sha>` | one pull request | what did its last run produce |
| `cb-base-v1-<cfg>-<merge_base>` | one commit | what does this commit's architecture look like |
| `cb-fork-v1-<cfg>-<fork_repo>-pr<N>-mb<merge_base>-<head_sha>` | one fork pull request | the same as `cb-head`, quarantined |

`<cfg>` digests the cache schema version, the pinned CodeBoarding version,
`.codeboardingignore`, and the model selection — everything that changes what an
analysis says. The merge base in the key pins the analysis depth too, since
depth is read from that commit's baseline. A run forced with `/codeboarding
refresh` or `full` appends `-<mode><run_id>.<attempt>`: cache entries are
immutable, so it must not save under the key holding the state it was told to
discard.

A restored chain is used only when it grew from the very base graph this run
diffs against, which `origin.json` records as a digest. Two runs of the engine
over one commit need not name components identically, so a head descended from
one base and a diagram drawn against another would report additions and
removals for code nobody touched.

The head chain is restored **by prefix**, which selects the newest entry for the
pull request — an exact head-sha lookup would outrank a newer generation and
undo a refresh. The base is restored **by exact key first**, because there an
exact hit is that merge base's own analysis while a prefix hit is only a warm
seed.

## Trust boundary

`static_analysis.pkl` is a Python pickle, so state derived from code the
repository does not control must never be restored into a privileged run. Fork
analyses therefore live under their own `cb-fork-` namespace that no trusted run
restores from, and a fork run never writes a base entry. That is enforced by key
construction, not by convention.

Caching is best effort throughout. A miss, an unavailable cache service, or a
GitHub Enterprise Server without one falls back to deriving the base directly.
