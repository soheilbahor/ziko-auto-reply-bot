# SECURITY — ZIKO Personal Assistant

Scope: the Telegram Connected Business Bot auto-reply assistant described in the shared
engineering specification (Python 3.12, aiogram 3.x, SQLite/aiosqlite, long polling, Ubuntu 24.04
on a single EC2 host).

This document states the security **requirements and controls the project must satisfy**. It is
not evidence that they are satisfied. Evidence is the executed test run recorded in
`QA_REPORT_TEMPLATE.md`; anything not covered by an executed test is listed under
"Manual verification".

## 1. Assets and trust boundaries

| Asset | Why it matters | Where it lives |
|---|---|---|
| Bot token | Full control of the bot | `.env` (production), process memory |
| Admin ID list | Defines who can control auto-replies | `ADMIN_IDS` in `.env` |
| Business connection (id, owner id, rights) | Allows sending as the owner's account | SQLite |
| Delivery metadata (ids, timestamps, error class) | Prevents duplicate replies | SQLite |
| Contacts' message content | Private third-party data | **Must never be stored or logged** |

Trust boundaries: Telegram Bot API (long polling, outbound HTTPS only) ↔ application ↔ local
SQLite file. There is no inbound application interface.

## 2. Hard rules (from the spec)

1. No Telethon, Pyrogram, MTProto or userbot login; no phone number, login code, password,
   2FA password, `api_id` or `api_hash`; no stored personal-account sessions.
2. Official Bot API only; connection permissions (`is_enabled`, `rights.can_reply`) are honoured
   and never bypassed.
3. Only numeric IDs in `ADMIN_IDS` may use the management interface.
4. Default state is OFF; nothing is sent until an admin activates a status.
5. Only message IDs and delivery metadata are persisted; no message bodies.
6. Bot tokens, message bodies and full connection details are never logged.

## 3. Threats and required controls

| # | Threat | Required control | Automated check |
|---|---|---|---|
| T1 | Non-admin operates the panel (commands or callbacks) | Admin check on every message and callback; no inline controls shown to non-admins | `test_admin_security.py` (unauthorized tests) |
| T2 | Forged or stale callback data / FSM state used by a non-admin | Re-validate admin identity on **every** FSM step, not only on entry | `test_non_admin_cannot_complete_edit_flow_via_forged_fsm_state` |
| T3 | Oversized or empty reply texts | Reject empty/whitespace-only text and anything above the limit (Telegram max 4096 chars) | `test_empty_or_excessively_long_text_is_rejected` |
| T4 | Duplicate or concurrent replies to one contact | Atomic `claim_reply` returning a `claim_token` or `None` per `(connection_id, chat_id, session_id)`; dedup of inbound events | `test_storage.py` concurrency + dedup tests; `test_business_handler.py` concurrent handlers |
| T4a | Stale worker completes or releases another worker's reservation | `complete_reply` / `release_reply` accept only the current holder's `claim_token`; stale, forged, wrong-chat and wrong-session tokens change nothing | `test_stale_*`, `test_token_is_bound_to_its_own_chat_and_session`, `test_unknown_token_*`, `test_concurrent_stale_completions_*` |
| T4b | Duplicate message after an ambiguous Telegram outcome or a crash | Ambiguous outcomes are recorded as `DeliveryOutcome.UNCERTAIN` and never retried in the same session; the production default `expired_lease_policy` is `"uncertain"`, so an expired un-completed reservation is not re-granted | `test_uncertain_*`, `test_expired_lease_*`, engine `test_ambiguous_network_outcome_*` |
| T4c | Leaked reservation (claim taken, Telegram never contacted, slot stuck) | `release_reply(claim_token)` when sending has not started; every granted token is settled exactly once | `test_release_*`, `test_failure_before_sending_releases_the_claim_and_allows_retry`, `assert_every_claim_settled_once` |
| T4d | Legacy `sent=True/False` API reintroduced | Removed from `Database`; no call sites in production code | `test_legacy_sent_flag_api_is_gone`, `test_production_code_does_not_use_legacy_sent_flag_api`, `test_every_complete_reply_call_in_production_passes_claim_token_and_outcome` |
| T5 | Replying when not allowed (OFF, disabled connection, no `can_reply`) | Gate every reply on global switch, connection `is_enabled` and `can_reply` | `test_business_handler.py` gate tests |
| T6 | Replying to the wrong audience (groups, channels, bots, owner, admin chat, edits) | Filter before claiming any slot | `test_business_handler.py` ignore tests |
| T7 | Secret disclosure through logs, errors or `repr()` | Redaction filter; never format raw exceptions that may contain request URLs; `Settings` repr must not expose the token | `test_failure_details_do_not_leak_token_or_body`, `test_settings_repr_does_not_leak_bot_token` |
| T8 | Private content retained at rest | No content columns; store only ids, timestamps, status, error class | `test_schema_has_no_message_content_columns`, `test_private_message_body_is_never_persisted` |
| T9 | SQLite file readable by other local users | DB file and directory created with owner-only permissions (0600 / 0700) | `test_database_file_is_not_group_or_world_accessible` |
| T10 | Secrets committed to Git | `.env` and SQLite files in `.gitignore`; `.env.example` holds placeholders only | `test_env_file_is_excluded_from_git`, `test_sqlite_files_are_excluded_from_git`, `test_no_real_secret_committed_in_env_example` |
| T11 | Personal-account takeover paths | No MTProto/userbot libraries or login material anywhere in source or dependencies | `test_no_mtproto_or_userbot_imports`, `test_no_personal_account_login_material_in_source`, `test_dependency_files_do_not_list_mtproto_clients` |
| T12 | Inbound attack surface | Long polling only; no webhook, no listening socket | `test_no_inbound_web_server_or_webhook`, `test_polling_is_the_update_mechanism` |
| T13 | Telegram rate limits (FloodWait / RetryAfter) and API errors causing loops or duplicate sends | Confirmed API rejections (`TelegramBadRequest`, `TelegramForbiddenError`, `TelegramRetryAfter`) are recorded as `REJECTED` so a later message may retry; network timeouts/resets and unknown errors are `UNCERTAIN`; never retry concurrently | `test_confirmed_api_rejection_*`, `test_ambiguous_network_outcome_*`, `test_unexpected_exception_during_send_is_treated_as_uncertain`, `test_rejected_is_never_used_for_ambiguous_errors` |
| T14 | Missing business updates because `allowed_updates` omits them | Request `business_connection` and `business_message` explicitly | `test_business_updates_are_requested_from_telegram` (source reference check only) |

## 4. Deployment hardening (manual verification)

These cannot be proven by unit tests and must be checked on the server and recorded in the QA report.

**Network**
- EC2 security group: no inbound rules except SSH (port 22) restricted to known admin IP ranges.
  The application needs outbound HTTPS to `api.telegram.org` only.
- Confirm with `ss -tlnp` that the service process listens on nothing.

**Host and process**
- Run as a dedicated unprivileged user (e.g. `ziko`), not root.
- `/opt/ziko-assistant` owned by that user; `.env` mode `0600`; data directory `0700`;
  DB file `0600` (set `umask 077` in the service unit or chmod after creation).
- systemd hardening suggestions: `NoNewPrivileges=yes`, `ProtectSystem=strict`,
  `ReadWritePaths=/opt/ziko-assistant/data`, `PrivateTmp=yes`, `ProtectHome=yes`,
  `Restart=on-failure`.
- Keep Ubuntu security updates enabled (`unattended-upgrades`); restrict SSH to key auth.

**Secrets**
- Generate the token in BotFather; rotate immediately (`/revoke`) if it appears in any log,
  ticket, chat or repository history.
- Never paste the token or `.env` into issue trackers or the QA report.
- Backups of the SQLite file contain connection metadata: encrypt and restrict them.

**Telegram configuration**
- Business Mode enabled in BotFather; the owner grants only the permissions required
  (reply to messages). The bot must still verify `can_reply` itself.
- Verify the owner connection with the official Chat Automation flow only. Never request the
  owner's phone number, codes or passwords.

**Logging**
- Log level INFO in production. Journald retention should be bounded.
- Review a sample of production logs for tokens, URLs containing `/bot<token>/`, and message text.

## 5. Known limits of the automated tests

- The authoritative `MODULE1_CONTRACT.md` is included in this project. Its signatures and semantics are implemented by `app.storage.Database`; assumptions A10-A14 in the QA fixture were reconciled against that contract.

- Tests mock Telegram; they cannot prove behaviour against the live Bot API (for example the
  exact shape of `business_connection` rights in a given Bot API release). Verify the integration
  against the current official documentation and a real connected test account before go-live.
- Static checks use pattern matching and can miss obfuscated code; they complement code review.
- File-permission checks reflect the test machine's umask and filesystem; confirm on the server.
- Concurrency tests exercise one event loop (plus a second DB handle); they do not simulate two
  separate OS processes.

## 6. Reporting and incident response

1. Suspected token leak: revoke in BotFather, update `.env`, restart the service, review logs and
   Git history, and record the incident.
2. Unexpected replies sent: disable auto-reply from the admin panel (or stop the service), then
   inspect delivery metadata by connection id, chat id and session id.
3. Suspected unauthorized admin access: verify `ADMIN_IDS`, rotate the token, review logs.
