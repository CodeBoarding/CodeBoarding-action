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
    types: [opened, reopened, ready_for_review, synchronize]
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
      (github.event_name == 'pull_request' && github.event.pull_request.draft == false &&
       github.event.pull_request.head.repo.full_name == github.repository) ||
      (github.event_name == 'issue_comment' && github.event.issue.pull_request != null &&
       startsWith(github.event.comment.body, '/codeboarding') &&
       contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association))
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: CodeBoarding/CodeBoarding-action@v2
```

Automatic runs update one sticky **CodeBoarding review** comment. A trusted repository owner, member, or collaborator can comment `/codeboarding` to analyze the current PR head again, including on fork PRs; every command creates a new result comment.

`synchronize` re-runs the review on every push to the branch. Each of those runs covers only the commits pushed since the previous one, so a push costs a fraction of a first analysis — and a pushed commit is the only thing that builds the reusable analysis, since GitHub gives comment-triggered runs a read-only cache. Drop `synchronize` from the list if you would rather spend one analysis per pull request than one per push.

Keep the `concurrency` block if you keep `synchronize`: it is what makes a push continue from the push before it, and what stops a slower run for an older commit from overwriting the review comment for a newer one. Set `cancel-in-progress: true` instead to abandon a superseded run rather than queue it, which costs less when branches are pushed to rapidly, at the price of no analysis for the commits in between.

| Command | What it does |
|---|---|
| `/codeboarding` | Analyzes the current head, reusing this PR's previous analysis when one is available. |
| `/codeboarding refresh` | Ignores that previous analysis and re-derives the head from the merge base. |
| `/codeboarding full` | Forces a from-scratch full analysis of the head. |

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

## Authentication and providers

With no LLM inputs, the action uses CodeBoarding's hosted OpenRouter tier. It mints short-lived GitHub OIDC credentials per request, so the job needs `id-token: write` and no stored LLM secret.

For a direct provider, pass its name and key:

```yaml
      - uses: CodeBoarding/CodeBoarding-action@v2
        with:
          llm_provider: anthropic
          llm_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

The action maps a provider name to the environment variable Core uses (`anthropic` → `ANTHROPIC_API_KEY`, `openai` → `OPENAI_API_KEY`, and so on). `aws`/`aws_bedrock` maps to `AWS_BEARER_TOKEN_BEDROCK`. Provider names following the standard convention are not restricted by an action-side allowlist; the pinned Core release remains the source of truth for which providers it implements.

CodeBoarding 0.13.8 supports OpenRouter, OpenAI-compatible endpoints, Anthropic, Google, Vercel AI Gateway, AWS Bedrock, Cerebras, DeepSeek, GLM, Kimi, Ollama, and LiteLLM. See Core's [`agents/llm_config.py`](https://github.com/CodeBoarding/CodeBoarding/blob/main/agents/llm_config.py) for current defaults and endpoint variables. In particular, Ollama needs `OLLAMA_BASE_URL` or `OLLAMA_HOST`, and LiteLLM needs `LITELLM_BASE_URL` on the action step.

A CodeBoarding license keeps the hosted OIDC path but removes hosted quota limits:

```yaml
        with:
          license_key: ${{ secrets.CODEBOARDING_LICENSE }}
```

`llm_api_key` takes precedence over `license_key`. A direct provider key does not require `id-token: write`; hosted free and licensed usage does.

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
      - uses: CodeBoarding/CodeBoarding-action@v2
        with:
          mode: sync
          target_branch: main
          force_full: ${{ inputs.force_full || false }}
```

The first run, `force_full: true`, or an incompatible baseline causes a full analysis. Otherwise sync asks Core for an incremental update. If the generated state is unchanged, no commit is created. If the target advances while analysis is running, the stale result is not rebased onto code it did not analyze; the newer push run is allowed to produce the current baseline.

### Protected branches

Set `sync_strategy: pull_request` and grant `pull-requests: write`:

```yaml
permissions:
  contents: write
  pull-requests: write
  id-token: write

# ...
      - uses: CodeBoarding/CodeBoarding-action@v2
        with:
          mode: sync
          target_branch: main
          sync_strategy: pull_request
```

Generation is identical to direct push. Only delivery changes: the same commit is force-with-lease pushed to the machine-owned `codeboarding/sync` branch and one rolling PR is opened into `target_branch`. When there is no longer a generated diff, an obsolete rolling PR is closed.

With the default `github.token`, the repository or organization must allow GitHub Actions to create pull requests. A GitHub App token or PAT can instead be passed as `github_token`. The same input is used for review comments and sync delivery.

## Inputs

| Input | Mode | Default | Description |
|---|---|---|---|
| `mode` | both | `review` | `review` or `sync`. |
| `llm_api_key` | both | empty | Direct-provider key. With the default provider, empty selects hosted OIDC usage. |
| `llm_provider` | both | `openrouter` | Provider for `llm_api_key`. |
| `license_key` | both | empty | License for unmetered hosted usage. |
| `model` | both | empty | Default model for both analysis and parsing. |
| `agent_model` | both | empty | Analysis-only override for `model`. |
| `parsing_model` | both | empty | Parsing-only override for `model`. |
| `github_token` | both | `${{ github.token }}` | Token for comments and sync delivery. |
| `sync_strategy` | sync | `push` | `push` or `pull_request`. |
| `target_branch` | sync | event branch | Branch receiving the baseline or rolling PR. |
| `force_full` | sync | `false` | Ignore the committed baseline for this run. |
| `warmstart_retention_days` | review | `1` | Days to keep the reusable analysis. Only the next run reads it. |

The `/codeboarding` command, comment heading, Mermaid direction (`LR`), hosted webview URL, rolling sync branch, commit message, and CodeBoarding 0.13.8 version are intentionally fixed in v2 rather than exposed as configuration.

## Outputs

| Output | Mode | Description |
|---|---|---|
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
python -m pip install codeboarding==0.13.8
tests/run_local.sh --repo /path/to/repo --base main --head feature
```

## License

MIT. See [LICENSE](LICENSE).
