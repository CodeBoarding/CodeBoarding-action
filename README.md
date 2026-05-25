<div align="center">
  <img src="assets/icon.svg" alt="CodeBoarding Logo" height="150" />

  # CodeBoarding Architecture Review

  Posts a PR comment with an architecture diagram showing which components changed (added/modified/removed) — green/yellow/red.
</div>

## What it does

On every pull request, this action:

1. Reads the `.codeboarding/analysis.json` committed at the PR base commit (the "before" snapshot).
2. Runs an incremental analysis on the PR head — only LLM-calls components whose code actually changed, so a typical PR costs a handful of LLM calls and a docs-only PR costs none.
3. Diffs the two analyses and renders the architecture diagram with changed components colored:
   - **green** for added components,
   - **yellow** for modified,
   - **red** (dashed + hatched) for removed.
4. Pushes the PNG to an orphan branch (`codeboarding-images`) in your repo and posts a sticky PR comment with the image.

## Usage

```yaml
name: Architecture review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  pull-requests: write
  contents: write           # for pushing the rendered PNG to the image branch

jobs:
  diagram:
    runs-on: ubuntu-latest
    if: github.event.pull_request.draft == false
    timeout-minutes: 60
    steps:
      - uses: codeboarding/codeboarding-action@v1
        with:
          llm_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

You need **one secret**: an LLM API key. OpenRouter is the default; pass your own model via `agent_model` / `parsing_model` inputs if you prefer.

## Inputs

| Input | Default | Description |
|---|---|---|
| `llm_api_key` | (required) | LLM API key. Currently OpenRouter (`OPENROUTER_API_KEY`). |
| `github_token` | `${{ github.token }}` | Token used to post the comment and push the image. |
| `engine_ref` | `main` | Git ref of `CodeBoarding/CodeBoarding`. Pin in production. |
| `vscode_ref` | `main` | Git ref of `CodeBoarding/CodeBoarding-vscode`. Pin in production. |
| `depth_level` | `1` | Diagram depth (1–3). Higher = slower + more detail. |
| `agent_model` | `openrouter/anthropic/claude-sonnet-4` | LLM for analysis. |
| `parsing_model` | `openrouter/anthropic/claude-sonnet-4` | LLM for parsing. |
| `image_branch` | `codeboarding-images` | Orphan branch where rendered PNGs are stored. |
| `comment_header` | `Architecture review` | Header line of the PR comment. |

## Outputs

| Output | Description |
|---|---|
| `diff_png` | Path to the rendered PNG in the runner workspace. |
| `diff_json` | Path to the computed diff JSON. |
| `image_url` | Public raw URL of the uploaded PNG (empty on fork PRs). |

## Establishing the baseline

The action reads the `.codeboarding/analysis.json` that was committed at the PR base commit. If your repo has never been analyzed, the first PR will skip the diff and post a "no baseline yet" comment.

To create the baseline, run the existing CodeBoarding docs workflow once on `main`. It will commit a fresh `analysis.json` to `.codeboarding/`. After that, every PR shows what changed.

We recommend a companion workflow that keeps the baseline fresh on every push to `main`. A pre-canned snippet for this is on the roadmap — see TODOs below.

## Fork PRs

PRs from forks cannot push to your image branch. The action still computes the diff and posts a comment, but without the rendered image. A maintainer can re-run the workflow from the Actions tab once they trust the PR.

## Limitations (V1)

- **Baseline must be committed.** If `.codeboarding/analysis.json` isn't in the repo at the PR base commit, the action posts a "no baseline" message and exits without rendering.
- **Fork PRs get no image** (text-only summary instead).
- **No focus / crop mode** for huge diagrams with tiny changes — the whole graph is rendered. Coming in V2.
- **Re-clones the target repo** inside the analysis engine (legacy `generate_analysis()` API). One extra clone per run; harmless but measurable on huge repos.
- **Image branch grows unbounded.** A scheduled cleanup workflow is on the roadmap.

## TODOs (require changes outside this repo)

The action vendors a few small pieces that should ideally live upstream. Each one is a clean refactor that would let us drop ~250 lines from `scripts/`:

- **Move `diff_component_trees` from `CodeBoarding-wrapper/codeboarding_pro/diff/tree_diff.py` into `CodeBoarding/diagram_analysis/`.** It's ~140 lines of set arithmetic with no LLM logic and no wrapper-side dependencies (only depends on `ComponentJson`/`RelationJson` from core). The wrapper's `DiffService` orchestrator can stay; just move the algorithm.
- **Expose a slim `analyze-only-this-checkout(repo_path, base_ref) -> analysis.json` entry point in core.** Today `generate_analysis()` re-clones the repo from a URL, which forces us to do a redundant clone even when the runner already has the code checked out.
- **Add a `mode: baseline` entry point in this action** that publishes a refresh-on-push workflow snippet (so users get the companion workflow with one input flag instead of copy-paste).
- **Bundle the webview-ui as a pre-built release asset** of the `CodeBoarding-vscode` repo so the action can download a small tarball instead of cloning + `npm ci` + `vite build` (~60s saved per cold run).
- **Move image hosting off the consumer's repo** by adding a CodeBoarding-hosted `/render` endpoint. The action would POST the diff JSON and receive a hosted PNG URL — no orphan branch, no `contents: write` permission needed, fork PRs work.

## License

MIT — see [LICENSE](LICENSE).
