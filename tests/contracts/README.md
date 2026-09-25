# Paywall contracts

JSON Schemas (draft 2020-12) for the wire shapes licensing-aws answers and accepts. The webview, the VS Code extension and the Action each keep a copy of this directory and validate their own fixtures against it in their test suites; `tests/unit/test_contracts/test_schemas.py` validates the examples here.

| Schema | Route |
| --- | --- |
| `session.schema.json` | answer of `POST /session/github` and `POST /session/extension` |
| `me.schema.json` | answer of `GET /me` |
| `usage.schema.json` | answer of `GET /me/usage`: this week's counted runs and opened private reviews, item by item |
| `workspace-usage.schema.json` | answer of `GET /workspaces/{workspace}/usage` (admins of a Team or Enterprise organisation; 403 `reason: not_admin` or `no_plan` otherwise) |
| `run-start.request.schema.json`, `run-start.response.schema.json` | `POST /run/start` on gha_proxy (OIDC) and license_proxy (extension token) |
| `run-finish.request.schema.json`, `run-finish.response.schema.json` | `POST /run/finish` on both |
| `meter-consume.request.schema.json`, `meter-consume.response.schema.json` | `POST /meter/consume` |
| `billing-checkout.request.schema.json`, `billing-checkout.response.schema.json` | `POST /billing/checkout` |
| `billing-portal.request.schema.json`, `billing-portal.response.schema.json` | `POST /billing/portal` |
| `legacy-link.request.schema.json`, `legacy-link.response.schema.json` | `POST /legacy/link` (always 200; only `linked: true` is a link) |
| `session-extension.request.schema.json` | body of `POST /session/extension` (its answer is `session.schema.json`) |
| `wall.schema.json` | the refusal inside the answers above, and the body of a 402 from either proxy (`{"error": {"message", "type"}, "wall": …}`) |
| `meter.schema.json` | one weekly allowance, used by the others |

Schemas reference each other by `$id` (`https://codeboarding.org/schemas/licensing/v1/<file>`), so load the whole directory into one registry. Examples are named `<schema stem>.<variant>.json`, and `.request.` examples validate against the request schema.

A change here is a change to four repositories: bump the copies in the same series of pull requests.
