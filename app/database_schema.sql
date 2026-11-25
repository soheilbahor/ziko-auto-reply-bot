-- ZIKO Personal Assistant: database schema, version 2 (full schema for new databases;
-- existing version 1 databases are upgraded by the migration in app/storage.py).
--
-- Applied by app.storage.Database.initialize() inside a single transaction
-- together with `PRAGMA user_version`, so a crash can never leave a half-built
-- schema. Every statement is idempotent. Do not put BEGIN/COMMIT or
-- PRAGMA user_version in this file.
--
-- PRIVACY: no table stores the content of private messages. Inbound events
-- are recorded by identifiers only; deliveries by status and Telegram IDs only.

-- Selectable statuses. Never hard-deleted: deleted_at hides a status.
-- reply_mode: 'plain' sends normally; 'reply' quotes the incoming message.
-- active_from_hour / active_to_hour: optional local window in the owner's timezone
-- (both set or both NULL; the start and end hours must differ).
CREATE TABLE IF NOT EXISTS statuses (
    key                  TEXT PRIMARY KEY
                         CHECK (length(key) BETWEEN 1 AND 32 AND key NOT GLOB '*[^a-z_]*'),
    title                TEXT    NOT NULL CHECK (length(trim(title)) > 0),
    emoji                TEXT    NOT NULL CHECK (length(trim(emoji)) > 0),
    reply_text           TEXT    NOT NULL CHECK (length(trim(reply_text)) > 0 AND length(reply_text) <= 4096),
    sort_order           INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT    NOT NULL,
    is_active            INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    deleted_at           TEXT,
    reply_mode           TEXT    NOT NULL DEFAULT 'reply' CHECK (reply_mode IN ('plain', 'reply')),
    min_interval_seconds INTEGER NOT NULL DEFAULT 0 CHECK (min_interval_seconds BETWEEN 0 AND 86400),
    active_from_hour     INTEGER CHECK (active_from_hour BETWEEN 0 AND 23),
    active_to_hour       INTEGER CHECK (active_to_hour BETWEEN 0 AND 23)
);

-- Ordered messages of a status. Position 1 is sent first. Sticker content is a Telegram file_id.
CREATE TABLE IF NOT EXISTS reply_steps (
    status_key TEXT    NOT NULL REFERENCES statuses (key) ON UPDATE CASCADE ON DELETE CASCADE,
    position   INTEGER NOT NULL CHECK (position BETWEEN 1 AND 10),
    kind       TEXT    NOT NULL CHECK (kind IN ('text', 'emoji', 'sticker')),
    content    TEXT    NOT NULL CHECK (length(content) BETWEEN 1 AND 4096),
    created_at TEXT    NOT NULL,
    PRIMARY KEY (status_key, position)
) WITHOUT ROWID;

-- Singleton owner preferences (row created by the seeding step).
CREATE TABLE IF NOT EXISTS owner_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    timezone   TEXT NOT NULL DEFAULT 'Asia/Tehran' CHECK (length(timezone) BETWEEN 1 AND 64),
    language   TEXT NOT NULL DEFAULT 'fa' CHECK (language IN ('fa', 'en')),
    owner_name TEXT NOT NULL DEFAULT '' CHECK (length(owner_name) <= 64),
    updated_at TEXT NOT NULL
);

-- Contacts that never receive an auto-reply. Only numeric chat IDs are stored.
CREATE TABLE IF NOT EXISTS reply_exceptions (
    chat_id    INTEGER PRIMARY KEY,
    created_at TEXT    NOT NULL
) WITHOUT ROWID;

-- Singleton row holding the global auto-reply state.
-- Invariant: enabled = 1 requires an active status and a session id;
-- enabled = 0 never has a session id (a stale session can never be used).
CREATE TABLE IF NOT EXISTS app_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    enabled           INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    active_status_key TEXT REFERENCES statuses (key) ON UPDATE CASCADE ON DELETE RESTRICT,
    session_id        TEXT,
    updated_at        TEXT NOT NULL,
    CHECK (
        (enabled = 1 AND active_status_key IS NOT NULL AND session_id IS NOT NULL)
        OR (enabled = 0 AND session_id IS NULL)
    )
);

-- Business connections as last reported by Telegram. Rows are never deleted;
-- a revoked connection is stored with is_enabled = 0.
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT    PRIMARY KEY CHECK (length(connection_id) > 0),
    is_enabled    INTEGER NOT NULL CHECK (is_enabled IN (0, 1)),
    can_reply     INTEGER NOT NULL CHECK (can_reply IN (0, 1)),
    owner_user_id INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_business_connections_enabled
    ON business_connections (is_enabled, updated_at);

-- Inbound event deduplication: identifiers only, never message content.
CREATE TABLE IF NOT EXISTS processed_messages (
    connection_id TEXT    NOT NULL
                  REFERENCES business_connections (connection_id)
                  ON UPDATE CASCADE ON DELETE CASCADE,
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (connection_id, chat_id, message_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_processed_messages_created_at
    ON processed_messages (created_at);

-- One row per (connection, chat, status session): at most one successful
-- auto-reply per contact per session. The primary key is the uniqueness guard.
--   pending:   reserved by one worker that is sending right now (lease-limited)
--   sent:      delivery confirmed; final, never retried
--   failed:    Telegram confirmed it rejected the send; a later incoming message may retry
--   uncertain: outcome unknown, the message may have been delivered; final, never
--              retried automatically (at-most-once). A new status session starts fresh.
-- claim_token identifies the current owner of a pending reservation. It is replaced
-- on every successful claim, so a worker whose lease was reclaimed can no longer
-- complete or release the row.
CREATE TABLE IF NOT EXISTS reply_deliveries (
    connection_id       TEXT    NOT NULL
                        REFERENCES business_connections (connection_id)
                        ON UPDATE CASCADE ON DELETE CASCADE,
    chat_id             INTEGER NOT NULL,
    session_id          TEXT    NOT NULL CHECK (length(session_id) > 0),
    state               TEXT    NOT NULL
                        CHECK (state IN ('pending', 'sent', 'failed', 'uncertain', 'partial')),
    attempts            INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    parts_sent          INTEGER NOT NULL DEFAULT 0 CHECK (parts_sent >= 0),
    telegram_message_id INTEGER,
    last_error          TEXT,
    claim_token         TEXT    NOT NULL CHECK (length(claim_token) > 0),
    claimed_at          TEXT    NOT NULL,
    completed_at        TEXT,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (connection_id, chat_id, session_id),
    CHECK (state NOT IN ('sent', 'partial') OR completed_at IS NOT NULL)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_reply_deliveries_state_claimed
    ON reply_deliveries (state, claimed_at);

CREATE INDEX IF NOT EXISTS idx_reply_deliveries_session
    ON reply_deliveries (session_id);

CREATE INDEX IF NOT EXISTS idx_reply_deliveries_contact
    ON reply_deliveries (connection_id, chat_id, state, completed_at);
