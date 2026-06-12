# Mockup: per-component file dropdowns in the sticky PR comment

The sticky PR comment currently shows the Mermaid diff diagram, the color legend,
and the CTA footer — but never lists *which files* made a component change color.
The data is already there: every component in `analysis.json` carries
`file_methods[]` (file paths + methods), which the diff logic uses internally but
never renders.

This doc mocks up two candidate formats, both using collapsed-by-default
`<details>` blocks below the legend so the diagram stays the hero. The same two
variants are posted as comments on the mockup PR so they can be compared exactly
as reviewers would see them. All component/file/method names below are invented.

---

## Variant A — component → changed files

One dropdown per changed component; expanding it lists the files in that
component that the PR actually touched (intersection of the component's
`file_methods[]` with the git diff — not the component's full file list).

---

### Architecture review · 3 components changed

```mermaid
graph LR
    API["API Gateway"]
    AUTH["AuthService"]
    RL["RateLimiter"]
    LSS["LegacySessionStore"]
    DB["UserStore"]
    API -- "authenticates via" --> AUTH
    API -- "throttled by" --> RL
    AUTH -- "reads/writes" --> DB
    AUTH -- "persisted sessions in" --> LSS
    classDef added fill:#1f883d,stroke:#0b5d23,color:#ffffff;
    classDef modified fill:#bf8700,stroke:#7d4e00,color:#ffffff;
    classDef deleted fill:#cf222e,stroke:#82071e,color:#ffffff,stroke-dasharray:5 3;
    class RL added;
    class AUTH modified;
    class LSS deleted;
    linkStyle 1 stroke:#0b5d23,stroke-width:2px;
    linkStyle 3 stroke:#82071e,stroke-width:2px,stroke-dasharray:5 3;
```

Colors indicate component changes compared to `main`: 🟩 Added · 🟨 Modified · 🟥 Removed

<details>
<summary>🟨 <b>AuthService</b> — 3 files changed</summary>

- `src/auth/handler.py`
- `src/auth/session.py`
- `src/auth/middleware.py`

</details>
<details>
<summary>🟩 <b>RateLimiter</b> — 2 files added</summary>

- `src/ratelimit/bucket.py`
- `src/ratelimit/config.py`

</details>
<details>
<summary>🟥 <b>LegacySessionStore</b> — 2 files removed</summary>

- `src/auth/legacy/store.py`
- `src/auth/legacy/migrations.py`

</details>

---

See this architecture in your editor: [**Open in VS Code →**](https://marketplace.visualstudio.com/items?itemName=Codeboarding.codeboarding)

<sub>codeboarding-action · run 0000000001 · **mockup — Variant A: component → changed files**</sub>

---

## Variant B — component → files → methods

Same as Variant A, but each file is itself a dropdown that reveals the changed
methods inside it, each tagged added / modified / removed. The inner dropdowns
sit in a `<blockquote>` so they indent under their component.

---

### Architecture review · 3 components changed

```mermaid
graph LR
    API["API Gateway"]
    AUTH["AuthService"]
    RL["RateLimiter"]
    LSS["LegacySessionStore"]
    DB["UserStore"]
    API -- "authenticates via" --> AUTH
    API -- "throttled by" --> RL
    AUTH -- "reads/writes" --> DB
    AUTH -- "persisted sessions in" --> LSS
    classDef added fill:#1f883d,stroke:#0b5d23,color:#ffffff;
    classDef modified fill:#bf8700,stroke:#7d4e00,color:#ffffff;
    classDef deleted fill:#cf222e,stroke:#82071e,color:#ffffff,stroke-dasharray:5 3;
    class RL added;
    class AUTH modified;
    class LSS deleted;
    linkStyle 1 stroke:#0b5d23,stroke-width:2px;
    linkStyle 3 stroke:#82071e,stroke-width:2px,stroke-dasharray:5 3;
```

Colors indicate component changes compared to `main`: 🟩 Added · 🟨 Modified · 🟥 Removed

<details>
<summary>🟨 <b>AuthService</b> — 3 files · 6 methods changed</summary>
<blockquote>

<details>
<summary>🟨 <code>src/auth/handler.py</code> · 3 methods</summary>

- 🟩 `validate_token` <sub>added</sub>
- 🟨 `login` <sub>modified</sub>
- 🟥 `legacy_login` <sub>removed</sub>

</details>
<details>
<summary>🟨 <code>src/auth/session.py</code> · 1 method</summary>

- 🟨 `refresh` <sub>modified</sub>

</details>
<details>
<summary>🟩 <code>src/auth/middleware.py</code> · 2 methods</summary>

- 🟩 `require_auth` <sub>added</sub>
- 🟩 `inject_claims` <sub>added</sub>

</details>

</blockquote>
</details>
<details>
<summary>🟩 <b>RateLimiter</b> — 2 files · 3 methods added</summary>
<blockquote>

<details>
<summary>🟩 <code>src/ratelimit/bucket.py</code> · 2 methods</summary>

- 🟩 `acquire` <sub>added</sub>
- 🟩 `refill` <sub>added</sub>

</details>
<details>
<summary>🟩 <code>src/ratelimit/config.py</code> · 1 method</summary>

- 🟩 `load_limits` <sub>added</sub>

</details>

</blockquote>
</details>
<details>
<summary>🟥 <b>LegacySessionStore</b> — 2 files · 4 methods removed</summary>
<blockquote>

<details>
<summary>🟥 <code>src/auth/legacy/store.py</code> · 3 methods</summary>

- 🟥 `get_session` <sub>removed</sub>
- 🟥 `put_session` <sub>removed</sub>
- 🟥 `evict_expired` <sub>removed</sub>

</details>
<details>
<summary>🟥 <code>src/auth/legacy/migrations.py</code> · 1 method</summary>

- 🟥 `migrate_v1_sessions` <sub>removed</sub>

</details>

</blockquote>
</details>

---

See this architecture in your editor: [**Open in VS Code →**](https://marketplace.visualstudio.com/items?itemName=Codeboarding.codeboarding)

<sub>codeboarding-action · run 0000000002 · **mockup — Variant B: component → files → methods**</sub>

---

## Implementation notes (if either variant is adopted)

- **List changed files only.** Intersect each component's `file_methods[]` paths
  with the PR's git diff. A component can own 40 files while the PR touched 2 —
  listing all of them answers the wrong question.
- **Method statuses** (Variant B) come from comparing base vs head
  `file_methods[].methods` per file — the same comparison
  `_has_method_changes()` already does, surfaced instead of discarded.
- **Comment size cap.** GitHub caps comment bodies at 65,536 chars and the
  Mermaid block alone may use ~45k. Cap files per component (e.g. 15 +
  "…and N more") and drop the dropdowns before dropping the diagram.
- **Rendering gotchas.** Markdown inside `<details>` needs a blank line after
  `</summary>`; nested dropdowns need the `<blockquote>` wrapper for indentation.
  Build the block in a Python helper next to `build_cta.py` rather than inline
  bash in `action.yml`.
- **Deleted components/files** don't exist on the head ref — render paths as
  plain code spans, not links.
