# QA Report — ZIKO Personal Assistant

Copy this file to `QA_REPORT_<date>.md` and fill every field from **actual** command output.
Leave a field as `NOT RUN` rather than guessing. Do not paste tokens, `.env` contents or
message bodies.

## 1. Run metadata

| Field | Value |
|---|---|
| Date / time (UTC) | |
| Tester | |
| Git commit / build id | |
| Python version (`python --version`) | |
| OS | |
| Host (local / EC2 t3.small) | |
| aiogram version | |
| pytest / pytest-asyncio / aiosqlite versions | |
| `ZIKO_STRICT` set? | yes / no |
| Module overrides used (`ZIKO_*_MODULE`, `ZIKO_*_FACTORY`) | |

## 2. Commands executed

```
pip install -r requirements.txt pytest pytest-asyncio
pytest -ra -v                 # paste the exact command you ran
ZIKO_STRICT=1 pytest -ra      # required before release: skips must be zero
```

## 3. Results summary (copy from the pytest footer)

| Passed | Failed | Errors | Skipped | xfailed |
|---|---|---|---|---|
| | | | | |

Release rule: **no failures, no errors, zero skips under `ZIKO_STRICT=1`.**

## 4. Results by area

| Area | File | Passed | Failed | Skipped | Notes |
|---|---|---|---|---|---|
| Storage contract | `tests/test_storage.py` | | | | |
| Status/session semantics | `tests/test_status_sessions.py` | | | | |
| Business message engine | `tests/test_business_handler.py` | | | | |
| Admin security + static checks | `tests/test_admin_security.py` | | | | |

## 5. Requirement coverage

| Requirement | Test(s) | Result (PASS / FAIL / SKIP / NOT RUN) |
|---|---|---|
| First delivery reserved (claim token returned) | `test_first_delivery_is_reserved_with_a_claim_token` | |
| Second delivery in session rejected (`None`) | `test_second_delivery_in_same_session_is_rejected_*` | |
| New status session allows one new delivery | `test_different_status_session_allows_one_new_delivery` | |
| Disable → enable = fresh session | `test_disable_then_enable_creates_fresh_session` | |
| Same status selection does not reset | `test_selecting_same_active_status_does_not_reset_session` | |
| REJECTED delivery retryable (new token) | `test_rejected_delivery_can_be_retried_with_a_new_token`, `test_repeated_rejections_then_one_success` | |
| UNCERTAIN delivery never retried in session | `test_uncertain_delivery_is_not_retried`, `test_uncertain_delivery_survives_restart` | |
| `release_reply` frees an unsent reservation | `test_release_frees_reservation_when_sending_never_started` | |
| Stale / forged / cross-chat token cannot settle another worker's reservation | `test_stale_*`, `test_token_is_bound_to_its_own_chat_and_session`, `test_unknown_token_cannot_complete_or_release`, `test_concurrent_stale_completions_never_free_the_current_holder` | |
| Expired lease → UNCERTAIN (default policy), not re-granted | `test_expired_lease_with_default_policy_is_not_regranted`, `test_expired_lease_with_explicit_uncertain_policy_*`, `test_expired_uncertain_lease_survives_restart` | |
| Legacy `sent=True/False` API absent | `test_legacy_sent_flag_api_is_gone`, `test_production_code_does_not_use_legacy_sent_flag_api` | |
| Every claim token passed to its completion/release | `test_claim_token_is_passed_to_the_matching_completion`, `assert_every_claim_settled_once` (all engine tests), `test_every_complete_reply_call_in_production_passes_claim_token_and_outcome` | |
| Confirmed API rejection → REJECTED | `test_confirmed_api_rejection_is_recorded_as_rejected_and_retried_later` | |
| Ambiguous network outcome → UNCERTAIN | `test_ambiguous_network_outcome_is_recorded_as_uncertain_and_not_resent`, `test_unexpected_exception_during_send_is_treated_as_uncertain` | |
| Failure before sending → release | `test_failure_before_sending_releases_the_claim_and_allows_retry` | |
| Concurrent claims → one winner | `test_concurrent_claims_never_yield_two_reservations` (+ variants) | |
| Duplicate inbound events rejected | `test_duplicate_inbound_event_is_rejected` | |
| Valid private message replies | `test_valid_private_message_triggers_active_reply` | |
| No reply when automation OFF | `test_no_reply_when_global_automation_disabled`, `test_default_off_state_never_replies` | |
| No reply when connection disabled | `test_no_reply_when_business_connection_disabled` | |
| No reply without `can_reply` | `test_no_reply_when_can_reply_is_false`, `test_connection_update_with_missing_rights_*` | |
| Correct `business_connection_id` sent | `test_business_connection_id_is_passed_to_send_message` | |
| Groups/channels/bots/owner/admin chat ignored | `test_non_private_chats_*`, `test_bot_senders_*`, `test_owner_messages_*`, `test_messages_in_admin_chat_*` | |
| Edited events ignored | `test_edited_events_do_not_trigger_reply` | |
| API failures recorded safely | `test_confirmed_api_rejection_*`, `test_ambiguous_network_outcome_*`, `test_failure_details_do_not_leak_token_or_body` | |
| Admin can operate panel | `test_authorized_admin_can_open_panel_and_activate_status` | |
| Unauthorized users/callbacks rejected | `test_unauthorized_*` | |
| FSM validates identity each step | `test_non_admin_cannot_complete_edit_flow_via_forged_fsm_state` | |
| Empty / long text rejected | `test_empty_or_excessively_long_text_is_rejected` | |
| Secrets not logged | `test_failure_details_*`, `test_bot_token_is_never_logged_*`, `test_settings_repr_*` | |
| `.env` excluded from Git | `test_env_file_is_excluded_from_git` | |
| SQLite protected | `test_database_file_is_not_group_or_world_accessible`, `test_sqlite_files_are_excluded_from_git` | |
| Message content not persisted | `test_schema_has_no_message_content_columns`, `test_private_message_body_is_never_persisted` | |
| No MTProto / personal login | `test_no_mtproto_or_userbot_imports`, `test_no_personal_account_login_material_in_source` | |
| No inbound ports | `test_no_inbound_web_server_or_webhook`, `test_polling_is_the_update_mechanism` | |

## 6. Failures (one block per failure)

```
Test id:
Observed:
Expected:
Suspected owner module:
Severity (blocker / major / minor):
Evidence (traceback excerpt, redacted):
```

## 7. Contract mismatches (every skip starting with `CONTRACT MISMATCH:`)

| Test | Mismatch message | Owner module | Resolution (code change / test adapter / spec clarification) |
|---|---|---|---|
| | | | |

Open assumptions that the spec does not fix (confirm or correct each):

| # | Assumption in the tests | Confirmed? | Actual contract |
|---|---|---|---|
| A1 | `Database(database_path)` constructor | | |
| A2 | `claim_inbound_event` truthy/falsy; `claim_reply` returns a `claim_token` or `None` | | |
| A3 | Status keys discovered via `list_statuses()` | | |
| A4 | Engine factory `(db=, sender=, settings=)`; `handle_business_message(message)`; sender call `send_message(..., business_connection_id=...)` | | |
| A5 | Admin factory `(db=, settings=)` → Router/Dispatcher; panel opened by `/start`; inline keyboards | | |
| A6 | Env vars `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_PATH`, `LOG_LEVEL` | | |
| A7 | Unknown connection id ⇒ no reply (QA expectation) | | |
| A8 | Enabling an already-enabled app does not reset the session (QA expectation) | | |
| A9 | DB file/dir permissions 0600/0700 are set by the application | | |
| A10 | `complete_reply` / `release_reply` called with keywords `connection_id, chat_id, session_id, claim_token` (+ `outcome`, `telegram_message_id`, `error`) | | |
| A11 | `DeliveryOutcome` importable from the database module; SENT and UNCERTAIN are terminal for the session, REJECTED and release free the slot | | |
| A12 | A stale-token completion/release raises or returns falsy and never alters the current holder | | |
| A13 | Lease keyword is one of `lease_seconds`, `claim_lease_seconds`, `lease_timeout_seconds`, `lease_ttl_seconds`; `expired_lease_policy` keyword exists; default is `"uncertain"` | | |
| A14 | Engine maps aiogram `TelegramBadRequest`/`TelegramForbiddenError`/`TelegramRetryAfter` → REJECTED; timeouts, resets, `TelegramNetworkError` and unexpected exceptions → UNCERTAIN | | |

Note: `MODULE1_CONTRACT.md` was not available to QA when the delivery-API tests were updated;
confirm A10-A14 against it before trusting a green run.

## 8. Manual verification (from SECURITY.md §4)

| Check | Command / evidence | Result |
|---|---|---|
| No listening sockets | `ss -tlnp` | |
| Security group has no inbound app ports | AWS console / CLI export | |
| Service runs as unprivileged user | `systemctl show ... -p User` | |
| `.env` is `0600`, data dir `0700`, DB `0600` | `stat` output | |
| Bot token not present in logs | `journalctl` search (redacted) | |
| Business Mode enabled; owner connected via Settings → Chat Automation | screenshot (no personal data) | |
| Live smoke test with a dedicated test account: one reply per status session | | |
| Bot API docs re-checked for the version in use | link + date | |

## 9. Known limitations

-

## 10. Sign-off

| Decision | GO / NO-GO |
|---|---|
| Reason | |
| Signed by / date | |
