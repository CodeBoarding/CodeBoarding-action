# Paywall contracts

JSON Schemas (draft 2020-12) for the wire shapes licensing-aws answers and accepts. The webview, the VS Code extension and the Action each keep a copy of this directory and validate their own fixtures against it in their test suites; `tests/unit/test_contracts/test_schemas.py` validates the examples here.

| Schema | Route |
| --- | --- |
| `session.schema.json` | answer of `POST /session/github` and `POST /session/extension` |
| `me.schema.json` | answer of `GET /me` |
| `run-start.request.schema.json`, `run-start.response.schema.json` | `POST /run/start` on gha_proxy (OIDC) and license_proxy (extension token) |
| `run-finish.request.schema.json`, `run-finish.response.schema.json` | `POST /run/finish` on both |
| `meter-consume.request.schema.json`, `meter-consume.response.schema.json` | `POST /meter/consume` |
| `wall.schema.json` | the refusal inside the answers above, and the body of a 402 from either proxy (`{"error": {"message", "type"}, "wall": …}`) |
| `meter.schema.json` | one weekly allowance, used by the others |

Schemas reference each other by `$id` (`https://codeboarding.org/schemas/licensing/v1/<file>`), so load the whole directory into one registry. Examples are named `<schema stem>.<variant>.json`, and `.request.` examples validate against the request schema.

A change here is a change to four repositories: bump the copies in the same series of pull requests.
