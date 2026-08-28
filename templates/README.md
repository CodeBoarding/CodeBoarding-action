# Workflow templates

The workflows CodeBoarding writes into a repository, and the only place their text lives.

Three things read these files:

- the **webview**, which renders them when it opens a setup pull request;
- the **webview again**, which matches a repository's committed workflow against every
  version here to work out which one it is running and what has changed since;
- the **action's README**, whose copy-and-paste examples are generated from them, so the
  file someone copies by hand and the file the button writes cannot drift apart. They had
  already drifted before this existed: the README's review workflow lacked the `closed`
  trigger, so closing a pull request left its analysis running to completion.

## Shape

A template is the finished file with named holes:

    {{BRANCH}}                target branch, in the push filter and in `target_branch`
    {{CREDENTIALS}}           the `llm:` block, one of the fills below
    {{DELIVERY_PERMISSION}}   `pull-requests: write`, or nothing
    {{DELIVERY_INPUT}}        `sync_strategy: pull_request`, or nothing
    {{SYNC_PR_GUARD}}         skips the rolling baseline PR, or nothing

Holes are what makes a template both renderable and matchable. Rendering substitutes them.
Matching turns the same file into a regular expression, with each hole a capture group, so
one pass over a committed workflow answers *which version* and *configured how* together.
Nothing has to be parsed, and nothing has to be inferred.

## Fills

`fills/` holds every value a hole can take, verbatim. `credentials.byok.yml` is itself a
template, expanded across the provider table in `scripts/action/supported-providers.json`,
so adding a provider stays a one-line change to that table.

## History

`history/` holds the exact bytes of every template version we have shipped. It must be
frozen, never regenerated: if an old version were re-rendered by today's code, one cosmetic
change would invalidate every repository on that version at once and they would all read as
edited-by-hand.

## Changelog

`CHANGELOG.json` carries one sentence per version, typed `update` or `replace`. It is the
copy the webview shows, not a developer changelog someone later paraphrases. Adding a
template version means adding an entry; there is no separate detector to write.
