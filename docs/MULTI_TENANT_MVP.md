# ZIKO multi-tenant platform

## Product behavior

- A regular Telegram user may register one child bot.
- The preferred onboarding path is Telegram Managed Bots: ZIKO generates an official creation link, the customer confirms the creation in Telegram, and Telegram sends a `managed_bot` update to ZIKO.
- ZIKO retrieves the new bot token using `getManagedBotToken`, encrypts it with Fernet, and starts an isolated polling runtime. The customer never copies or sends the token in this flow.
- Existing bots can still be connected through `/connect_bot`; ZIKO validates the submitted token with `getMe` before encrypting it.
- Each child bot gets its own SQLite database and admin allow-list.
- Child bots reuse the existing rule/status-based Telegram Business auto-reply engine. No external AI API is required.
- The platform owner can list bots with `/tenants` and enable/disable a specific bot using `/tenant_enable BOT_ID` and `/tenant_disable BOT_ID`.
- Bot Management Mode must be enabled for the master bot. The `getMe` response should report `can_manage_bots: true`.

## User flow

1. User opens the ZIKO master bot and sends `/create_bot`.
2. ZIKO offers automatic managed-bot creation and existing-token connection.
3. For automatic creation, the user opens the official `https://t.me/newbot/<manager_username>/<suggested_username>?name=...` link and confirms the bot creation in Telegram.
4. Telegram delivers a `managed_bot` update to ZIKO. ZIKO calls `getManagedBotToken`, encrypts the token, registers the child bot, and starts its polling task.
5. For an existing bot, the user selects the connection option or sends `/connect_bot`, then submits the token privately. ZIKO validates it with `getMe`.
6. The user opens the child bot and configures reply statuses. For Business auto-replies, the user connects the child bot in Telegram Settings → Telegram Business → Chatbots and grants `can_reply`.

## Runtime and storage

- Control plane: `tenants.sqlite3` beside the configured main database.
- Encryption key: `tenant-token.key`, mode `0600`, generated on first startup.
- Per-bot data: legacy primary bot keeps `tenants/<telegram_user_id>.sqlite3`; additional bots use `tenants/bot-<telegram_bot_id>.sqlite3`.
- Existing installations keep their original primary-bot database path to make rollback non-destructive; additional child bots receive bot-ID-specific database files.
- Each child bot runs in its own aiogram Dispatcher, in-memory FSM storage, polling task, and SQLite database.
- The runtime supervisor retries a stopped child polling loop with exponential backoff.
- Bot tokens are never displayed by `ziko bots list` or stored in plaintext in the registry.

## Operations

- `ziko bots list` shows owner ID, bot ID, username, enabled flag, and registration time. The enabled flag is registry state, not a live process health probe.
- `/tenants` lists registered bots and their Bot IDs.
- `/tenant_enable BOT_ID` and `/tenant_disable BOT_ID` operate on an individual bot.
- Disabled bots remain registered and their database is retained. A destructive delete command is intentionally not provided until an export/archive workflow can guarantee safe retention.
- `ziko backup` creates a standalone main-database backup and a full-platform archive containing the main database, child-bot registry, encryption key, and all tenant databases.
- `ziko restore /path/to/ziko-platform-....tar.gz` verifies the checksum, validates databases and token decryption, creates a pre-restore platform backup, then restores the complete platform as a set.

## Operational limits

This remains a single-process, single-server polling architecture. Each child bot consumes a polling task and an HTTP session. Before supporting a large number of tenants or multiple application servers, add explicit tenant quotas/rate limits and migrate to a shared control plane with distributed webhook/queue workers. Monitor resource use as the number of registered child bots grows.
