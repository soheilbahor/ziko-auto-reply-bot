# ZIKO integration report

## Inputs inspected
- `ziko-module1.zip`: Module 1 contract, config, models, SQLite schema/storage and original storage/config tests.
- `files.zip`: Module 2 engine, business router, package init and original engine tests.
- `ziko-module3-admin.zip`: admin router and keyboard package.
- `ziko-module4-qa.zip`: QA fixtures/tests, security requirements and QA report template.

All four ZIPs were extracted and inventoried. Module 2's loose files were inspected as well as all Python modules, the Module 1 contract, schema, QA tests, and security documentation.

## Canonical architecture
- `app.storage.Database` and `app.models.DeliveryOutcome` remain authoritative Module 1 implementations.
- `app.handlers.admin.create_admin_router` and `app.keyboards.admin` are retained from Module 3.
- Module 2 was replaced at the integration boundary by `app.services.auto_reply.BusinessMessageEngine`, and `app.handlers.business.create_business_router` wires Telegram's `business_connection` and `business_message` updates to it.
- `app.main` owns process lifecycle, shared DB, Dispatcher, memory FSM storage and long polling.
- The Module 1 API is re-exported via `app.database` for annotation/import compatibility.

## Contract reconciliation
The Module 1 contract and implementation define:
- `claim_reply(connection_id, chat_id, session_id) -> str | None`.
- `complete_reply(connection_id, chat_id, session_id, claim_token, outcome, telegram_message_id=None, error=None) -> bool`.
- `release_reply(connection_id, chat_id, session_id, claim_token) -> bool`.
- `DeliveryOutcome.SENT`, `REJECTED`, `UNCERTAIN`.

The integrated engine keeps the returned claim token and passes it to exactly one settlement call. No production code uses the legacy `sent=True/False` API.

## Key incompatibility found and resolved
The original Module 2 engine still used `complete_reply(..., sent: bool, ...)`, treated ambiguous network errors as retryable failures, and did not consume `claim_token` or `DeliveryOutcome`. That directly violated the later Module 1 breaking contract. It was not safe to adapt the database backwards; instead the Module 2 integration service was rewritten against the Module 1 contract. The old Module 2 source and test were retained as text snapshots under `docs/archive/module2_original_*.py.txt` for traceability, not imported by production.

The Module 4 QA fixture's assumptions A10-A14 were checked against Module 1's actual contract. The actual lease keyword is `pending_lease_seconds`, not one of the fixture's four guessed names. The QA fixture's owner/admin identities were also reconciled so the account owner is in ADMIN_IDS, as required by the Module 2 connection trust check. The integration does not change Module 1's constructor/API to satisfy an assumption that the contract does not make.

## Lease policy
The actual Module 1 constructor default is `expired_lease_policy="uncertain"` (not `reclaim`). It was not changed. This is strict at-most-once: an abandoned pending claim becomes UNCERTAIN when its lease expires, and is not automatically granted again. Explicit `"reclaim"` remains an opt-in.

## Telegram API verification
The current official Bot API documents:
- `Update.business_connection` and `Update.business_message`.
- `BusinessConnection.id`, `.user`, `.rights`, `.is_enabled`.
- `BusinessBotRights.can_reply`.
- `sendMessage`'s optional `business_connection_id`, used to send on behalf of the connected business account.
The router explicitly registers business updates and polling explicitly requests them. The engine checks the persisted connection's enabled state and reply permission before sending, and uses `send_message(..., business_connection_id=...)`.

Official reference: https://core.telegram.org/bots/api

## Important remaining gaps / deployment risks
- No live Telegram Business account was available, so connection permissions and actual personal-account delivery have not been smoke-tested.
- `aiogram` and `aiosqlite` were not installed in the build sandbox; the complete pytest suite and real async SQLite initialization could not be executed here.
- Module 4 QA tests assume the engine and sender interface; static review was used to align the interface, but test outcomes remain NOT RUN until dependencies are installed.
- `app.main` uses aiogram MemoryStorage for admin FSM. Editing state is lost on process restart; persisted business delivery/session state remains in SQLite.
- The integration intentionally does not auto-discover or persist multiple concurrent business connections beyond the Module 1 `get_business_connection()` API, which returns one preferred connection. If multiple owners/business connections are in scope, Module 1's data/API contract needs extension.
- SQLite WAL sidecar files should be included in consistent backups; the provided backup script uses SQLite's online backup API to produce a consistent standalone DB snapshot.
