# Baseline, cache, and artifact strategy

Where CodeBoarding keeps analysis state, and how a review run finds the two
graphs it compares.

## Where analysis is stored

| Store | Holds | Lifetime | Who can read it |
|---|---|---|---|
| **Git, analysis branch** (`codeboarding_analysis_location: codeboarding_branch`, the default) | one commit per sync on `codeboarding/baseline` | forever | anyone with repo read |
| **Git, code branch** (`codeboarding_analysis_location: in_place`) | `.codeboarding/` on the synced branch | forever | anyone with repo read |
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
| `codeboarding-review-<run>-<attempt>` | `analysis.json`, `health_report.json`, `metadata.json` | 14 days | the webview, humans |
| `codeboarding-base-<cfg>-<merge_base>` | the merge base's own analysis | 30 days, renewed while still referenced | any later review forking from that commit |
| `codeboarding-warmstart-<cfg>-pr<N>` | the working directory: graph, pickle, fingerprint, gate | **1 day**, configurable | only the next run of that pull request |

Bundles carry analysis state, not the engine's scratch: run logs and lock files
are stripped before publication, since no reader inflates them and every fetch
pays for them. Health configuration stays, because a run seeded from a bundle
reads it.

The base graph is published only by the run that *computed* it, so it is written
about once per merge base rather than once per run — with two exceptions. A
review artifact references a base by id for its whole retention, so a base about
to expire inside that window is republished rather than left dangling under a
review that outlives it. And when no artifact holds the base at all, because the
upload failed or because a fork review publishes nothing, the review artifact
carries the graph inline and leaves `base_artifact` empty: a reader should prefer
an inline `base_analysis.json` and fall back to the named artifact — ten runs on one pull request
would otherwise store ten identical copies, which measured at exactly half the
artifact.

**Every bundle carries a `metadata.json` naming its `kind`** — `review`, `base`
or `warmstart`. Without it a base bundle is an `analysis.json` and nothing else,
which unpacks exactly like a head artifact and would be rendered as one by a
reader that resolved the wrong name. Assert on `kind` rather than inferring from
the payload.

`metadata.json` in the review artifact names the base artifact so a reader can
fetch it without reconstructing the name:

Types are part of the contract, not an accident of how the file is written:
`merge_base_resolved` is a JSON **boolean**, everything else is a string. A
string `"false"` is truthy in most consumers, so a caveat keyed on it silently
never fires.

| Field | Type | Meaning |
|---|---|---|
| `head_sha` | string | the commit `analysis.json` describes |
| `pr_base_sha` | string | the merge base, under the name the webview resolves |
| `merge_base_sha` | string | the same value under this action's own name |
| `base_artifact` | string | the artifact holding the graph that was compared against |
| `base_artifact_id` | string | **which one**, since two artifacts can share that name and disagree: the engine is not deterministic, and a sync run publishes bases for the same commit |
| `merge_base_resolved` | **boolean** | `false` means the merge base could not be resolved, so the comparison is against `base_sha` |
| `base_sha` | string | the base branch tip when the event fired — *not* what was compared against |
| `kind` | string | always `review`, so a reader can tell this artifact from a base or warm-start bundle |
| `analysed_files_changed` | string | analysed files whose content hash differs between base and head; `unknown` when the analyses cannot say |
| `pr_number`, `mode`, `seed_source`, `chain_depth` | string | provenance; nothing rendering a diagram needs them. `mode` is the head's engine mode |

**A sync run** publishes the base graph under both the commit it analyzed and the
baseline commit it writes on top, because a pull request opened either side of
that commit has a different merge base.

## Retention is what costs

Artifacts are charged by size × time, so the three windows are set by what reads
them:

- **14 days** for the review artifact — the dominant cost, since it is the one
  kept for weeks. A pull request open longer loses its rendered analysis until
  someone asks for it again, which costs one incremental over the pull request:
  the base graph is still published, so nothing re-analyzes the base.
- **30 days** for a base graph, which must outlive every review that names it.
  The renewal threshold is the review's own retention, so the surplus — 16 days
  here — is how long a base is reused before being republished.
- **1 day** for the warm-start bundle, since only the next run reads it. This is
  `warmstart_retention_days` if a repository wants longer. It behaves like the
  old cache eviction: a pull request left alone longer than the window
  re-derives from the base.

## How a review resolves its two graphs

**Base**, first match wins. This is `review-generate-baseline.sh`, in this order:

| Source | Engine cost |
|---|---|
| the published `codeboarding-base-<cfg>-<merge_base>` artifact with a compatible depth cap | none |
| the baseline branch: its entry for the merge base, else for the nearest of the merge base's last 100 first-parent ancestors, made under this configuration | none for the merge base itself, one incremental otherwise |
| the baseline committed on the branch at the merge base, with a compatible depth cap | one incremental |
| the nearest `codeboarding-base-<cfg>-<sha>` artifact among the merge base's last 100 first-parent ancestors | one incremental |
| nothing | full analysis, at the configured `depth_cap` |

The baseline branch comes before the committed baseline because its entries are
pinned to this configuration and a committed baseline is not: one left behind
after a repository moved to the baseline branch must not win. A seed is only
checked before the run (depth cap, and configuration where the source records
it); if Core then asks for a full analysis, the base is analyzed in full rather
than retried from the next source. In practice only a committed baseline can be
refused that way, as it is the one source not pinned to the engine version.

A trusted run that computed the base publishes it, so the next pull request
forking from that commit gets the first row; that includes a base caught up from
an ancestor. Which source won is logged as a notice and not recorded anywhere
else. While a base is built from scratch, the progress comment says so in two
steps: building the base, then analysing the pull request.

**Telemetry.** Every engine run carries `CODEBOARDING_RUN_ID=gh-<run id>-<attempt>-<role>`,
the role being `base`, `head` or `sync`, so the engine's own `analysis_started` /
`analysis_completed` events (mode, duration, tokens) can be read per run. A review
with no `base` events reused its base; `base` events say whether it was caught up
or built in full.

The ancestor lookup walks the merge base's first-parent history, deepening the
shallow checkout to 101 commits, then pages through the repository's artifacts
newest first, keeping those named for this configuration and produced by a run on
the repository's own code. It stops at the first page holding one of the walked
commits (usually the first; at most 50 pages) and takes the nearest commit seen.
The merge base's own `.codeboardingignore` and health configuration replace the
seed's. Sync uses the same sources, in the same order, without the exact artifact, so the first sync after the setup pull request
merges catches up from the base that pull request's review saved, instead of
analyzing from scratch.

The configuration hash includes `depth_cap`. The workflow input controls depth
for both fresh and fallback analyses; stored legacy depth values never override it.

**Head**, first match wins:

| Source | Covers |
|---|---|
| this pull request's warm-start bundle | only the commits pushed since that run |
| nothing to fetch: first run, moved merge base, changed config, or a fork | the whole pull request |

A restored bundle is used only when it grew from the very base graph this run
diffs against, recorded as a digest in `origin.json`. Two runs of the engine over
one commit need not name components identically, so a head descended from one
base and a diagram drawn against another would report changes nobody made.

## The baseline branch

By default sync saves the analysis to a branch of its own in the same
repository, `codeboarding/baseline`. The synced branch is then only read.
`codeboarding_analysis_location: in_place` commits the analysis to the synced
branch instead, and skips everything below.

**What lives where.** The branch is an orphan: it shares no history with the code.
Each sync adds one commit holding the same `.codeboarding/` files
sync would otherwise commit to the synced branch, plus
`.codeboarding/source.json`:

```json
{"schema": 1, "synced_branch": "main", "source_sha": "<sha analysed>", "generated_at": "<iso>", "engine_version": "<v>", "config": "<cfg hash>"}
```

The commit is `chore(codeboarding): diagram of main @<sha7>` with three trailers:
`CodeBoarding-Source: <sha>`, `CodeBoarding-Branch: main` and
`CodeBoarding-Config: <cfg hash>`, the same
configuration hash that names the base artifacts (engine version, provider, model,
depth cap). Engine output is never edited; which commit it describes and how it was
made live only in `source.json` and the trailers. The synced branch is never
written, not even `.gitattributes`. The base artifacts are still published, named
for the analysed commit.

**How a sync writes it.** It seeds from the branch tip when the tip was made under
this configuration, and runs incrementally. The generated files are replaced
wholesale; only the checkout's own `.codeboardingignore` and health configuration
are kept. The push is a fast-forward onto the tip it fetched, never forced. Sync
refuses to write to an existing branch that is not a baseline branch (its tip has
no `CodeBoarding-Source` trailer, or holds anything besides `.codeboarding/`), so
a code branch that happens to be named `codeboarding/baseline` fails instead of being emptied. The branch belongs to the first branch that saved there: before installing anything, a sync from another branch fails, naming the branch it keeps the analysis of. If the
synced branch moved during the analysis, the result is dropped, as when committing
to it. If another sync moved the baseline branch, it builds on that tip once. A push the
remote refuses while the tip did not move is a branch rule, and the run fails
saying so.

The synced branch is checked just before the push, not in the same transaction:
if it moves in that window, the branch can end on an analysis of the older commit.
Its trailer still names that commit, so no reader takes it for newer, and the run
queued for the newer commit replaces it.

**How a review reads it.** Right after an exact artifact, a review lists the newest 100 commits of the branch (fetched without
file contents, so the listing costs commit messages only) and matches their
trailers against the merge base's first-parent history, up to 100 commits deep.
Only entries made under this run's configuration count: an entry for the merge
base itself is reused as is, so nothing else would catch a different engine or
model. An entry for an ancestor is caught up incrementally. Only without a usable
entry does it fall back to a committed baseline, then to ancestor artifacts.

**If the branch is deleted**, the next sync creates it again as a new orphan,
seeding from a saved ancestor artifact when there is one and analyzing in full
otherwise. The history is lost; the current diagram is not.

**Protecting it.** Sync and review load `static_analysis.pkl` from this branch, and
a pickle runs code when loaded, so whoever can write the branch can run code in
the sync and review workflows. Import
[`baseline-branch-ruleset.json`](baseline-branch-ruleset.json) under Settings,
Rules, Rulesets, New ruleset, Import a ruleset. It blocks creating, updating,
deleting and force-pushing `codeboarding/baseline` for everyone except the
CodeBoarding Review app (`4021464`), so only the workflows you give the app's key
can write it; sync must then push with the app's token as `github_token`.

If sync can only use the default `github.token`, import
[`baseline-branch-ruleset-actions.json`](baseline-branch-ruleset-actions.json)
instead, whose bypass actor is GitHub Actions (integration `15368`). **It only stops
people pushing by hand:** any workflow with `contents: write`, including one run
from a same-repository pull request branch, can still push to the branch and plant
the pickle sync loads.

Creation is covered too, so the bypass actor must be the identity sync pushes as
before the first sync, or that sync fails on the rule. Rulesets on a private repository need a paid GitHub plan
(Pro, Team or Enterprise); on Free they apply to public repositories only.

## Trust boundary

`static_analysis.pkl` is a Python pickle, so state derived from code the
repository does not control must never be loaded by a privileged run. With the
cache, GitHub enforced that. With artifacts there is no platform boundary, so the
rule is simply that **a fork pull request has no lineage**: it is reviewed on
request, every review starts from the base, and it publishes no warm-start bundle
and no base graph. Nothing it produces is ever read.

Reading is guarded too, not just publishing. These artifact names are
predictable, and a pull request from a fork can add a workflow that uploads one:
its run is hosted here, so the artifact lands in this repository's store. A
bundle is therefore only read when the run that produced it had the same head
repository as the repository it ran in. Anything else is ignored, with a warning.

One consequence for readers: a fork review that had to compute the base itself
publishes nothing, so it carries `base_analysis.json` inside its own review
artifact and leaves `base_artifact` empty. Prefer the inline copy when it is
there, and fall back to the named artifact otherwise.

Reuse is best effort throughout. A missing or expired artifact, or a token
without `actions: read`, falls back to deriving the base directly — which is what
every run did before any of this existed.

**GitHub Enterprise Server.** `actions/upload-artifact@v4` is not supported
there, which is why the review artifact has always been restricted to
github.com. The stored analyses are restricted the same way, so a GHES review
derives the base from the committed baseline on every run. That is the same work
it did before, just without the reuse.
