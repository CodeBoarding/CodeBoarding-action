# Baseline, cache, and artifact strategy

Where CodeBoarding keeps analysis state, and how a review run finds the two
graphs it compares.

## Three stores

| Store | Holds | Lifetime | Who can read it |
|---|---|---|---|
| **Git** (`sync` commits) | `.codeboarding/` on the target branch | forever | anyone with repo read |
| **Workflow artifacts** | every analysis this action reuses or publishes | a retention window | any run with `actions: read`, plus humans |
| ~~Actions cache~~ | — | — | not used |

The cache is deliberately not used. GitHub gives comment-triggered runs a
read-only cache token, so a `/codeboarding` run — which is what a webview refresh
posts — could never save what it computed, and cache entries written by a pull
request run are invisible to every other ref. Artifacts are readable and writable
from every trigger, so one store serves all of them.

The cost of that choice: reading another run's artifact needs `actions: read` on
the consumer's token, and artifact storage is billed past a plan allowance while
cache was free. Retention is the lever — see below.

## What a run publishes

**A review run**

| Artifact | Contents | Retention | Read by |
|---|---|---|---|
| `codeboarding-review-<run>-<attempt>` | `analysis.json`, `health_report.json`, `metadata.json` | 30 days | the webview, humans |
| `codeboarding-base-<cfg>-<merge_base>` | the merge base's own analysis | repository default | any later review forking from that commit |
| `codeboarding-warmstart-<cfg>-pr<N>` | the working directory: graph, pickle, fingerprint, gate | **1 day**, configurable | only the next run of that pull request |

The base graph is published only by the run that *computed* it, so it is written
about once per merge base rather than once per run — ten runs on one pull request
would otherwise store ten identical copies, which measured at exactly half the
artifact.

`metadata.json` names the base artifact so a reader can fetch it without
reconstructing the name:

| Field | Meaning |
|---|---|
| `head_sha` | the commit `analysis.json` describes |
| `pr_base_sha` | the merge base, under the name the webview resolves |
| `merge_base_sha` | the same value under this action's own name |
| `base_artifact` | the artifact holding the graph that was compared against |
| `merge_base_resolved` | `false` means the merge base could not be resolved, so the comparison is against `base_sha` |
| `base_sha` | the base branch tip when the event fired — *not* what was compared against |
| `pr_number`, `mode`, `seed_source`, `chain_depth` | provenance; nothing rendering a diagram needs them |

**A sync run** publishes the base graph under both the commit it analyzed and the
baseline commit it writes on top, because a pull request opened either side of
that commit has a different merge base.

## Retention is what costs

Artifacts are charged by size × time, so the three windows are set by what reads
them:

- **30 days** for the review artifact — a reader may come back to a pull request.
- **Repository default** for a base graph — it must outlive every review artifact
  that names it.
- **1 day** for the warm-start bundle, since only the next run reads it. This is
  `warmstart_retention_days` if a repository wants longer. It behaves like the
  old cache eviction: a pull request left alone longer than the window
  re-derives from the base.

## How a review resolves its two graphs

**Base**, first match wins:

| Source | Engine cost |
|---|---|
| the published `codeboarding-base-<cfg>-<merge_base>` artifact | none |
| no artifact — check out the merge base, seed from the baseline committed there, catch up | one incremental |
| no committed baseline either | full analysis |

A trusted run that computed the base publishes it, so the next pull request
forking from that commit gets the first row.

**Head**, first match wins:

| Source | Covers |
|---|---|
| this pull request's warm-start bundle | only the commits pushed since that run |
| nothing to fetch: first run, moved merge base, changed config, `refresh`, or a fork | the whole pull request |

A restored bundle is used only when it grew from the very base graph this run
diffs against, recorded as a digest in `origin.json`. Two runs of the engine over
one commit need not name components identically, so a head descended from one
base and a diagram drawn against another would report changes nobody made.

## Trust boundary

`static_analysis.pkl` is a Python pickle, so state derived from code the
repository does not control must never be loaded by a privileged run. With the
cache, GitHub enforced that. With artifacts there is no platform boundary, so the
rule is simply that **a fork pull request has no lineage**: it is reviewed on
request, every review starts from the base, and it publishes no warm-start bundle
and no base graph. Nothing it produces is ever read.

Reuse is best effort throughout. A missing or expired artifact, or a token
without `actions: read`, falls back to deriving the base directly — which is what
every run did before any of this existed.
