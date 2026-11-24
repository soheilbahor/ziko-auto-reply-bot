# ZIKO multi-tenant MVP

## Selected product behavior

- One Telegram user may register one child bot.
- The child bot is created by the user through the official `@BotFather` flow. The master bot cannot create Telegram bots directly.
- The user submits the BotFather token to the master bot in a private chat with `/create_bot`.
- ZIKO validates the token with `getMe`, encrypts it with Fernet, and starts a dedicated long-polling runtime.
- Each child bot gets its own SQLite database and admin allow-list.
- The child bot supports the existing rule/status-based Business auto-reply engine. No AI API is required.
- The user may connect their Telegram Business account to their child bot and grant reply permission.
- The platform owner can list, disable, and re-enable child bots from the master bot.

## User flow

1. User opens the master bot.
2. User creates a bot with `@BotFather`.
3. User sends `/create_bot` to the master bot and submits the token in private chat.
4. ZIKO validates the token and registers one bot for that Telegram user ID.
5. User opens the child bot and configures its reply statuses.
6. For Business auto-replies, user connects the child bot in Telegram Settings → Telegram Business → Chatbots and grants `can_reply`.

## Runtime and storage

- Control plane: `tenants.sqlite3` beside the configured main database.
- Encryption key: `tenant-token.key`, mode `0600`, generated on first startup.
- Per-user data: `tenants/<telegram_user_id>.sqlite3`.
- Each child bot runs in its own aiogram Dispatcher and polling task.
- The runtime supervisor retries a stopped child polling loop with exponential backoff.

## Operational limitations

This is an initial single-process/single-server implementation. Each child bot consumes a long-polling task and an HTTP session. Before supporting a large number of tenants or multiple application servers, migrate tenant metadata and tenant data to a shared production database, move bot updates to a distributed webhook/queue worker architecture, add subscription quotas/rate limits, and provide tested full-platform backup/restore for the registry, encryption key, and all tenant databases.

The existing `scripts/backup.sh` backs up the original primary database only; it does not yet produce a complete multi-tenant backup. Until a full-platform backup/restore workflow is added, separately back up `tenants.sqlite3`, `tenant-token.key`, and the entire `tenants/` directory securely.
