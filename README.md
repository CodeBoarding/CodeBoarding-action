# CodeBoarding Action

One GitHub Action with two modes:

- **`review`** (default) compares a pull request's head with its merge base, posts an inline Mermaid architecture diff, and uploads both analyses as a workflow artifact.
- **`sync`** updates the versioned analysis state used by future incremental runs. It can push directly or open one rolling PR for protected branches.

The action is a thin wrapper around the [CodeBoarding](https://github.com/CodeBoarding/CodeBoarding) CLI. Analysis logic and provider defaults live in Core, not in this repository.

[CodeBoarding](https://github.com/CodeBoarding/CodeBoarding) · [Website](https://codeboarding.org) · [Examples](https://codeboarding.org/diagrams) · [VS Code extension](https://marketplace.visualstudio.com/items?itemName=Codeboarding.codeboarding) · [Discord](https://discord.gg/T5zHTJYFuy)

## Review pull requests

Create `.github/workflows/codeboarding.yml`:

```yaml
name: CodeBoarding review

on:
  pull_request:
    types: [opened, reopened, synchronize]
  issue_comment:
    types: [created]

permissions:
  contents: read
  actions: read        # download the analysis an earlier run published
  pull-requests: write
  issues: write
  id-token: write

# One review at a time per pull request. Two pushes in quick succession would
# otherwise analyze concurrently, and both would start from the same older
# analysis instead of the newer one continuing from its predecessor. They also
# share one sticky comment, so whichever finishes last wins — which can be the
# run for the older commit. Queue rather than cancel, so a /codeboarding command
# waits for a running review instead of killing it.
concurrency:
  group: codeboarding-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: false

jobs:
  review:
    if: >
      (github.event_name == 'pull_request' &&
       github.event.pull_request.head.repo.full_name == github.repository) ||
      (github.event_name == 'issue_comment' && github.event.issue.pull_request != null &&
       startsWith(github.event.comment.body, '/codeboarding') &&
       contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association))
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: CodeBoarding/CodeBoarding-action@v1
        with:
          llm: hosted   # or license, or a provider name -- see Authentication
```

Automatic runs review both draft and non-draft pull requests and update one sticky **CodeBoarding review** comment. Opening, reopening, or pushing a commit runs analysis; changing only the draft state does not. A trusted repository owner, member, or collaborator can comment `/codeboarding` to analyze the current PR head again, including on fork PRs; every command creates a new result comment.

`synchronize` re-runs the review on every push to the branch. Each of those runs covers only the commits pushed since the previous one, so a push costs a fraction of a first analysis — and a pushed commit is the only thing that builds the reusable analysis, since GitHub gives comment-triggered runs a read-only cache. Drop `synchronize` from the list if you would rather spend one analysis per pull request than one per push.

Keep the `concurrency` block if you keep `synchronize`: it is what makes a push continue from the push before it, and what stops a slower run for an older commit from overwriting the review comment for a newer one. Set `cancel-in-progress: true` instead to abandon a superseded run rather than queue it, which costs less when branches are pushed to rapidly, at the price of no analysis for the commits in between.

`/codeboarding` analyzes the current head, reusing this pull request's previous analysis when there is one. It takes no arguments.

The action checks out and analyzes the exact PR head SHA, and compares it with the PR's **merge base** — the commit the branch forked from, which is what GitHub's own "Files changed" tab uses. Commits pushed to the base branch after the fork point are therefore not reported as this PR's changes; the comment notes how far behind the branch is instead. It does not commit generated files to either branch.

Automatic fork runs are skipped because the `pull_request` event does not receive hosted OIDC credentials. A trusted `/codeboarding` command runs the released action code from the base repository and checks the fork's source into a separate analysis directory; it never executes an action definition from the fork with privileged credentials.

A review run uploads `codeboarding-review-<run_id>-<attempt>`:

| File | Contents |
|---|---|
| `analysis.json` | the head analysis, at `head_sha` |
| `health_report.json` | the head's health findings, when the engine produced any |
| `metadata.json` | which commits that graph describes, and the name of the base artifact — see [the field list](docs/COMMIT_STRATEGY.md#what-a-run-publishes) |

The graph it was compared against is published separately, named for the commit
it describes, because the merge base rarely changes during a pull request and a
copy per run would store the same bytes over and over. Read
`metadata.base_artifact` and fetch that artifact by name; the graph inside it is
`analysis.json`, and `metadata.base_artifact_id` records exactly which artifact
this review used, since two can share a name and disagree.

Every bundle the action publishes carries a `metadata.json` with a `kind` of
`review`, `base` or `warmstart`. Check it rather than inferring from the
payload: a base bundle is otherwise indistinguishable from a head one.

The action requests 14-day retention for it; a repository or organisation policy can shorten that, so treat an artifact's own `expired` flag as the truth rather than any fixed window. When a pull request outlives its review artifact, `/codeboarding` regenerates it at the cost of one incremental over the pull request — the base graph is kept longer and does not need re-analyzing.

### Reused analysis

Each review seeds the head analysis from this pull request's own previous run, so a run only covers the commits pushed since it. That analysis is published as a workflow artifact named for the pull request, and the next run fetches it by name. With nothing to fetch, the run seeds from the merge base's analysis instead, which `sync` publishes for every commit it processes.

Everything here is best-effort. A missing artifact, an expired one, or a token without `actions: read` all mean the run derives from the base instead — which is what every run did before any of this existed. Stored analyses are read only when the run that produced them worked on this repository's own code, so a fork cannot leave one behind for a later run to load. On GitHub Enterprise Server, where `actions/upload-artifact@v4` is unsupported, nothing is stored or reused and every review derives from the committed baseline.

Because artifacts are readable and writable from every trigger, all paths behave the same: an automatic run, a `/codeboarding` command and a manual dispatch each reuse the previous analysis and publish their own. A stored analysis is discarded, and the head re-derived from the base, whenever the pinned CodeBoarding version, `.codeboardingignore`, the model selection, the analysis depth or the merge base changes — or when the base graph it grew from is not the one this run compares against.

Fork pull requests never carry an analysis forward. They are reviewed on request, each review starts from the base, and nothing they produce is read by a run on this repository's own code: untrusted code must not shape state that a later run loads. The action also accepts `pull_request_target`, which runs on the base branch ref and lets both share one chain; that trigger has its own trade-offs (a PR that adds this workflow will not run it until merged, and the fork gate becomes load-bearing), so `pull_request` remains the recommended default.

### Pull requests that change nothing analysed

A pull request whose changed files are all outside what the engine analyses (docs, configuration, CI, tests the ignore file excludes, or a language the engine does not read) cannot have moved the architecture. The action says so by comparing the two analyses it already holds: the engine records a content hash for every file a component owns, and the review counts the analysed files whose hash differs between the base and the head, including files added or removed. At zero, no byte of analysed code changed, and the comment says `(no analysed file changed)` after the component count. If the count of changed components is not zero at the same time, the analysis grouped the same code differently, and the comment says that too rather than presenting it as a change. The review artifact's `metadata.json` carries the count as `analysed_files_changed` (a string, like every other field there, and `unknown` when either analysis has a file without a hash).

Every review comment ends with a machine-readable HTML comment, `<!-- codeboarding: platform_url=… changed=… analysed_files_changed=… head=… -->`, for readers that should not parse the prose or the diagram. The progress comment carries the platform link from the start, so a pull request can be opened there while the run is still going.

## Authentication and providers

The `llm` input is required and says where analysis credentials come from. There are
three answers, and the action never picks one for you:

```yaml
    with:
      llm: hosted                                        # CodeBoarding's free tier
```
```yaml
    with:
      llm: license                                       # a CodeBoarding plan
      license_key: ${{ secrets.CODEBOARDING_LICENSE }}
```
```yaml
    with:
      llm: anthropic                                     # your own provider key
      anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

`hosted` and `license` run through CodeBoarding's proxy and need `id-token: write`, which
mints short-lived credentials per request and stores no LLM secret in your repository. A
provider key is used directly and needs no OIDC permission.

**An empty value is never a fallback.** If you name a provider and its key is missing --
because the secret does not exist yet, or is misspelt — the run fails in its first
seconds and says which input and which secret to fix. It does not quietly analyze on
CodeBoarding's hosted tier instead. That was the old behaviour, and it meant a repository
could report an Anthropic review that Anthropic never produced.

The same rule makes the combinations explicit rather than order-dependent:

| Workflow says | Result |
|---|---|
| nothing | refused: `llm` is required |
| `llm: hosted` | the free tier |
| `llm: hosted` + any provider key | refused: pick one |
| `llm: hosted` + `license_key` | refused: use `llm: license` |
| `llm: license` without `license_key` | refused: names the secret to add |
| `llm: anthropic` + `anthropic_api_key` | Anthropic, directly |
| `llm: anthropic`, key empty or absent | refused: names the input and the secret |
| `llm: anthropic` + `openai_api_key` | refused: a second provider's key |
| `llm: anthropic` + key + `license_key` | Anthropic, on a CodeBoarding plan |

A licence alongside your own key is deliberately allowed: it says "my CodeBoarding plan,
my own tokens". **Your key always wins.** A direct provider call never reaches
CodeBoarding, so the licence is recorded and reported but not spent, and nothing meters
that combination today. The job summary says so on every run, rather than leaving you to
infer it from the tier name.

### Providers

Each provider has its own inputs, so which key a workflow uses is readable from the file
without knowing any precedence rules.

| `llm` | Provider | Inputs | Needs at least one of |
|---|---|---|---|
| `anthropic` | Anthropic | `anthropic_api_key` | `anthropic_api_key` |
| `aws_bedrock` | AWS Bedrock | `aws_bedrock_api_key`, `aws_bedrock_region` | `aws_bedrock_api_key` |
| `cerebras` | Cerebras | `cerebras_api_key` | `cerebras_api_key` |
| `deepseek` | DeepSeek | `deepseek_api_key`, `deepseek_base_url` | `deepseek_api_key` or `deepseek_base_url` |
| `glm` | GLM | `glm_api_key`, `glm_base_url` | `glm_api_key` or `glm_base_url` |
| `google` | Google Gemini | `google_api_key` | `google_api_key` |
| `kimi` | Kimi | `kimi_api_key`, `kimi_base_url` | `kimi_api_key` or `kimi_base_url` |
| `litellm` | LiteLLM | `litellm_api_key`, `litellm_base_url` | `litellm_base_url` |
| `ollama` | Ollama | `ollama_api_key`, `ollama_base_url` | `ollama_base_url` |
| `openai` | OpenAI | `openai_api_key`, `openai_base_url` | `openai_api_key` or `openai_base_url` |
| `openrouter` | OpenRouter | `openrouter_api_key` | `openrouter_api_key` |
| `orcarouter` | OrcaRouter | `orcarouter_api_key` | `orcarouter_api_key` |
| `requesty` | Requesty | `requesty_api_key`, `requesty_base_url` | `requesty_api_key` |
| `vercel` | Vercel AI Gateway | `vercel_api_key`, `vercel_base_url` | `vercel_api_key` or `vercel_base_url` |

Each provider has exactly one accepted spelling, and its inputs are named after it, so
`llm: X` always pairs with `X_api_key`. There are no aliases: a second spelling is another
thing to document and keep in step, and an unrecognised value is refused with the accepted
list. `ollama` and `litellm` are selected by their endpoint rather than a key, which is why
a key alone does not configure them — that mirrors how Core itself decides.

This table is generated from [`scripts/action/supported-providers.json`](scripts/action/supported-providers.json),
which mirrors the CodeBoarding release this action pins. `tests/test_provider_table_drift.py`
installs that release in CI and fails if the two disagree, so a provider cannot be added
to Core and silently stay unreachable here.

### Reporting

Every run reports what it resolved, so the answer never has to be inferred from behaviour:

- outputs `llm_tier` (`hosted`, `license`, `byok`, `byok+license`), `llm_provider` (empty
  on the hosted tiers, where the upstream is ours rather than yours), and
  `llm_config_error` (empty when configured);
- a job-summary table naming the tier and provider;
- on a configuration failure, an error annotation and — in review mode — a pull request
  comment with the fix, so the person who has to add the secret sees it where they are.
- when the provider rejects the key or its quota runs out during the analysis, the run fails
  with an error annotation saying which, and in review mode the failure comment says it too.
  No partial analysis is published.

## Model selection

All model inputs are optional and are passed directly to Core without action-side validation:

```yaml
        with:
          model: google/gemini-3.7-flash
          agent_model: anthropic/claude-sonnet-4 # optional analysis-only override
          parsing_model: openai/gpt-5-mini       # optional parsing-only override
```

Precedence is intentionally simple:

| Work | Resolution |
|---|---|
| Analysis | `agent_model` → `model` → active provider's Core default |
| Parsing | `parsing_model` → `model` → active provider's Core default |

Set only `model` when both jobs should use the same model. Set either specialized input only when that job needs a different model. Model identifiers are not secrets and can be stored in GitHub repository variables.

## Keep the baseline current

Sync mode commits only Core's persisted incremental-analysis state under `.codeboarding/`:

- `analysis.json`
- `fingerprint.json`
- `static_analysis.pkl`
- `static_analysis.sha`
- `codeboarding_version.json` when emitted by Core
- `health/health_report.json`

It does **not** render or commit architecture Markdown. Existing v1-generated `.codeboarding/*.md` and `docs/development/architecture.md` files carrying CodeBoarding's generated badge are removed on the first v2 sync. Hand-written Markdown and user-authored CodeBoarding configuration, including `health/health_config.json` and `health/.healthignore`, are preserved.

Create `.github/workflows/codeboarding-sync.yml`:

```yaml
name: CodeBoarding sync

on:
  push:
    branches: [main]
    paths-ignore:
      - '.codeboarding/analysis.json'
      - '.codeboarding/fingerprint.json'
      - '.codeboarding/static_analysis.pkl'
      - '.codeboarding/static_analysis.sha'
      - '.codeboarding/codeboarding_version.json'
  workflow_dispatch:
    inputs:
      force_full:
        description: Rebuild without the committed baseline
        type: boolean
        default: false

permissions:
  contents: write
  id-token: write

concurrency:
  group: codeboarding-sync
  cancel-in-progress: false

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: CodeBoarding/CodeBoarding-action@v1
        with:
          mode: sync
          llm: hosted
          synced_branch: main
          force_full: ${{ inputs.force_full || false }}
```

`synced_branch` is the code branch sync keeps an up-to-date analysis of; it defaults to the branch that triggered the run. `save_baseline_to` chooses where that analysis is saved: `synced_branch` (the default, a commit on that branch), `pull_request` (a rolling PR into it) or `baseline_branch` (a branch of its own, see below).

The first run, `force_full: true`, or an incompatible baseline causes a full analysis. Otherwise sync asks Core for an incremental update. If the generated state is unchanged, no commit is created. If the synced branch advances while analysis is running, the stale result is not rebased onto code it did not analyze; the newer push run is allowed to produce the current baseline.

### Protected branches

Set `save_baseline_to: pull_request` and grant `pull-requests: write`:

```yaml
permissions:
  contents: write
  pull-requests: write
  id-token: write

# ...
      - uses: CodeBoarding/CodeBoarding-action@v1
        with:
          mode: sync
          llm: hosted
          synced_branch: main
          save_baseline_to: pull_request
```

Generation is identical to direct push. Only delivery changes: the same commit is force-with-lease pushed to the machine-owned `codeboarding/sync` branch and one rolling PR is opened into `synced_branch`. When there is no longer a generated diff, an obsolete rolling PR is closed.

With the default `github.token`, the repository or organization must allow GitHub Actions to create pull requests. A GitHub App token or PAT can instead be passed as `github_token`. The same input is used for review comments and sync delivery.

### Save the diagram to a branch of its own

Set `save_baseline_to: baseline_branch` to keep the analysis off your code branches entirely:

```yaml
      - uses: CodeBoarding/CodeBoarding-action@v1
        with:
          mode: sync
          llm: hosted
          synced_branch: main
          save_baseline_to: baseline_branch
```

`main` is then only read. Each sync adds one commit to `codeboarding/baseline` in the same repository (set `baseline_branch` to change the name), an orphan branch that shares no history with `main`. It holds the same `.codeboarding/` files sync would otherwise commit to `main`, plus `.codeboarding/source.json` naming the commit they describe and the configuration that made them; the commit message carries both as `CodeBoarding-Source:` and `CodeBoarding-Config:` trailers. Pushes only ever fast-forward, `main` is never written, and no pull request is opened. Reviews read their base from the branch, and the web platform reads the latest diagram from it. The [baseline branch section](docs/COMMIT_STRATEGY.md#the-baseline-branch) covers what happens if the branch is deleted, and a ruleset you should import to protect it: sync and review load a pickle from it.

No permission beyond the `contents: write` every sync already needs: the first sync creates the branch with an ordinary push. If you renamed it with `baseline_branch`, set the same name in your review workflow too, since reviews read it — or keep both jobs in one workflow, below.

**Moving an existing setup.** Nothing changes until you opt in: the default keeps committing to the synced branch. To switch, paste this into your coding agent:

```text
Move this repository's CodeBoarding sync to save_baseline_to: baseline_branch.
1. In the workflow that runs CodeBoarding/CodeBoarding-action with mode: sync, set
   `save_baseline_to: baseline_branch` in its `with:` block, replacing any
   sync_strategy. Rename target_branch to synced_branch if it is set. Keep every
   other input.
2. Delete the generated files under .codeboarding/ from the default branch, keeping
   the user configuration: .codeboarding/.codeboardingignore,
   .codeboarding/health/health_config.json and .codeboarding/health/.healthignore.
   Reviews read the baseline branch first, so this is cleanup, not a requirement.
3. Remove any .gitattributes lines that mark .codeboarding/ files as
   linguist-generated, if nothing else is left under .codeboarding/ for them.
4. Open a pull request with these changes. After it merges, close any open
   pull request from the codeboarding/sync branch and delete that branch.
```

The first sync after the merge creates `codeboarding/baseline`, catching up from a saved analysis when there is one.

### Review and sync in one workflow

The two workflows above can be one file with two jobs. Each job keeps its own permissions, and settings both modes read live in one `env:` block, so review always looks for the baseline where sync saves it:

```yaml
name: CodeBoarding

on:
  pull_request:
    types: [opened, reopened, synchronize]
  issue_comment:
    types: [created]
  push:
    branches: [main]   # the synced branch; `on:` cannot read env
  workflow_dispatch:
    inputs:
      force_full:
        description: Rebuild without the saved baseline
        type: boolean
        default: false

env:
  SYNCED_BRANCH: main
  BASELINE_BRANCH: codeboarding/baseline

permissions: {}

# Reviews queue per pull request, syncs per branch.
concurrency:
  group: codeboarding-${{ github.event.pull_request.number || github.event.issue.number || github.ref_name }}
  cancel-in-progress: false

jobs:
  review:
    if: >
      (github.event_name == 'pull_request' &&
       github.event.pull_request.head.repo.full_name == github.repository) ||
      (github.event_name == 'issue_comment' && github.event.issue.pull_request != null &&
       startsWith(github.event.comment.body, '/codeboarding') &&
       contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association))
    runs-on: ubuntu-latest
    timeout-minutes: 60
    permissions:
      contents: read
      actions: read
      pull-requests: write
      issues: write
      id-token: write
    steps:
      - uses: CodeBoarding/CodeBoarding-action@v1
        with:
          llm: hosted
          baseline_branch: ${{ env.BASELINE_BRANCH }}

  sync:
    if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    timeout-minutes: 60
    permissions:
      contents: write
      id-token: write
    steps:
      - uses: CodeBoarding/CodeBoarding-action@v1
        with:
          mode: sync
          llm: hosted
          synced_branch: ${{ env.SYNCED_BRANCH }}
          save_baseline_to: baseline_branch
          baseline_branch: ${{ env.BASELINE_BRANCH }}
          force_full: ${{ inputs.force_full || false }}
```

With `save_baseline_to: baseline_branch` sync never commits to `main`, so the `push` trigger needs no `paths-ignore`. Existing two-file setups keep working unchanged; this is only a different way to call the same action.

## Inputs

| Input | Mode | Default | Description |
|---|---|---|---|
| `mode` | both | `review` | `review` or `sync`. |
| `llm` | both | **required** | `hosted`, `license`, or a provider name. No default. |
| `<provider>_api_key` | both | empty | That provider's key, e.g. `anthropic_api_key`. See [Providers](#providers). |
| `<provider>_base_url` | both | empty | That provider's endpoint, where it has one. |
| `aws_bedrock_region` | both | empty | Bedrock region. Core defaults to `us-east-1`. |
| `license_key` | both | empty | CodeBoarding license. Required by `llm: license`. |
| `model` | both | empty | Default model for both analysis and parsing. |
| `agent_model` | both | empty | Analysis-only override for `model`. |
| `parsing_model` | both | empty | Parsing-only override for `model`. |
| `depth_cap` | both | `2` | Positive integer maximum analysis depth, including full-analysis fallbacks. Changing it rebuilds incompatible state. |
| `github_token` | both | `${{ github.token }}` | Token for comments and sync delivery. |
| `synced_branch` | sync | event branch | Code branch sync keeps an up-to-date analysis of. |
| `save_baseline_to` | sync | `synced_branch` | Where sync saves the analysis: `synced_branch` (a commit on it), `pull_request` (a rolling PR into it), or `baseline_branch` (a commit on `baseline_branch`; the synced branch is never written). |
| `baseline_branch` | both | `codeboarding/baseline` | Branch `save_baseline_to: baseline_branch` saves the analysis to; reviews read their base from it when it exists. |
| `force_full` | sync | `false` | Ignore the committed baseline for this run. |
| `warmstart_retention_days` | review | `1` | Days to keep the reusable analysis. Only the next run reads it. |

`target_branch` and `sync_strategy` are deprecated names for `synced_branch` and `save_baseline_to` (`push` is `synced_branch`, `pull_request` is `pull_request`). They still work, with a warning; setting a value under both names fails.

The `/codeboarding` command, comment heading, Mermaid direction (`LR`), hosted webview URL, rolling sync branch, commit message, and CodeBoarding 0.14.5 version are intentionally fixed rather than exposed as configuration.

Review mode needs no sync workflow or committed `.codeboarding` directory. If no
usable merge-base analysis exists, it runs full analysis there directly, then
seeds an incremental analysis of the PR head and publishes both states. Later
runs prefer compatible prior PR state for incremental updates, while the review
still compares the merge base with the current head.

Set `depth_cap` in the action's `with:` block (for example, `depth_cap: 4`).
This configuration is authoritative: stored `metadata.depth_cap` is checked for
compatibility, not inherited, and legacy `metadata.depth_level` is not used as a
fallback. Missing or incompatible baseline depth triggers a rebuild.

`metadata.depth_cap` records the configured maximum; `metadata.depth_level`
records the depth actually reached, which can be shallower. Comparing the cap
avoids rejecting valid state or reducing future rebuild depth when a run stops
early. The action input matches the metadata name and the engine receives only
`--depth-cap`. There are no old-name input aliases. Historical workflows using
the removed `depth_level` action input must switch to `depth_cap`.

This action pins Core 0.14.3 for the `--depth-cap` CLI contract. Publish that Core
release before releasing the action. Earlier eShop evidence predates this final
breaking CLI migration.

## Outputs

| Output | Mode | Description |
|---|---|---|
| `llm_tier` | both | `hosted`, `license`, `byok`, or `byok+license`. |
| `llm_provider` | both | Provider the run used. Empty on `hosted` and `license`: which upstream the proxy routes to is CodeBoarding's decision, not your configuration. |
| `llm_config_error` | both | Configuration failure code, empty when configured. |
| `diagram_md` | review | Path to the rendered Mermaid block on the runner. |
| `n_changed` | review | Number of changed components. |
| `truncated` | review | Whether the graph was reduced to fit GitHub limits. |
| `review_artifact_url` | review | URL of the uploaded head analysis. |
| `seed_source` | review | `pr-chain` when the head grew from this PR's previous analysis, `base` otherwise. |
| `merge_base_sha` | review | Merge base used as the comparison baseline. |
| `analysis_mode` | sync | `incremental` or `full`. |
| `files_written` | sync | Number of persisted analysis artifacts produced. |
| `committed` | sync | Whether a baseline commit was delivered. |
| `sync_pr_url` | sync | Rolling PR URL for PR delivery. |
| `sync_pr_number` | sync | Rolling PR number for PR delivery. |

## GitHub Enterprise Server

Repository fetches, comments, pushes, and rolling-PR API calls use `github.server_url`; GitHub.com is not hardcoded for repository operations. The hosted CodeBoarding webview and LLM proxy remain CodeBoarding-operated production services.

## Local test harness

Render a diff without making LLM calls:

```bash
tests/run_local.sh --base-json /tmp/base.json --head-json /tmp/head.json
```

Run the local analysis pipeline:

```bash
export OPENROUTER_API_KEY=sk-or-...
python -m pip install codeboarding==0.14.5
tests/run_local.sh --repo /path/to/repo --base main --head feature
```

## License

MIT. See [LICENSE](LICENSE).
