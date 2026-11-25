# Module 1 Contract — Core, Configuration & Database

Authoritative reference for modules that integrate with Module 1 (`app/config.py`,
`app/models.py`, `app/storage.py`, `app/database_schema.sql`). Schema version: **2**.

> **Verification status.** The tests were run on Python 3.12.3 and 3.13.18 against a
> local *stand-in* for `aiosqlite` (the real package could not be installed in the
> build sandbox). Real `aiosqlite` compatibility is **not yet verified**. Run
> `python -m unittest discover -s tests -t . -v` on the target server after
> `pip install aiosqlite` before relying on it. The integration examples in section 8
> are illustrative and have never been executed.

> **Delivery guarantee (production default).** Module 1 is configured for **strict
> at-most-once** automated replies per contact per active status session
> (`expired_lease_policy="uncertain"`). Ambiguous outcomes (timeouts, dropped connections,
> HTTP 5xx, crashes or hangs mid-send, shutdown mid-send) are recorded as `uncertain` and are
> **never retried automatically**; the contact is answered again only when a new session starts.
> Only a *confirmed* Telegram API rejection (`REJECTED`) is retried, by a later incoming
> message. Preventing a duplicate personal message takes priority over answering every
> contact. `"reclaim"` is available only as an explicit opt-in (section 6.6).

## 1. Changes from the first draft (breaking, read first)

| Item | Before | Now |
|---|---|---|
| `claim_reply` return | `bool` | `str \| None` — a unique **claim token** on success, `None` if refused. Still truthy/falsy, but the token must be kept. |
| `complete_reply` | `(connection_id, chat_id, session_id, sent: bool, telegram_message_id=None, error=None) -> bool` | `(connection_id, chat_id, session_id, claim_token, outcome: DeliveryOutcome, telegram_message_id=None, error=None) -> bool` |
| New | — | `DeliveryOutcome` enum, `Database.release_reply`, delivery state `uncertain`, constructor option `expired_lease_policy` (**default `"uncertain"`**, `"reclaim"` is opt-in) |
| Schema | v1 | v2 (existing v1 files are migrated automatically on `initialize()`) |

## 2. Dependencies and initialization

* Python 3.12+, runtime dependency: `aiosqlite` (stdlib `sqlite3` with SQLite ≥ 3.24 for upserts; Ubuntu 24.04 ships 3.45).
* Create **one** `Database` per process and share it between all handlers. Several instances
  or processes on the same file are safe (see 6.7) but unnecessary.

```python
from app.config import load_settings
from app.storage import Database

settings = load_settings()                     # raises ConfigError on bad configuration
db = Database(settings.database_path)          # nothing is opened yet
await db.initialize()                          # creates dir/file (0600), schema, seed rows
try:
    ...                                        # run the bot
finally:
    await db.close()                           # idempotent
```

`async with Database(path) as db:` is equivalent. `initialize()` is idempotent, applies the
schema (or migrations) inside one transaction, sets `PRAGMA user_version`, enables
`foreign_keys`, WAL mode and a 5 s busy timeout, and seeds the six statuses and the
`app_state` row **without overwriting** existing data. A database whose `user_version` is
newer than the code raises `SchemaVersionError`. Using a method before `initialize()` raises
`StorageError`.

Environment variables read by `load_settings()` (a `.env` file in the working directory is a
fallback; real environment variables win):

| Variable | Required | Default | Notes |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | format `<digits>:<20+ chars>`; never echoed in errors |
| `ADMIN_IDS` | yes | — | positive integers separated by comma/semicolon/space; at least one |
| `DATABASE_PATH` | no | `data/ziko.sqlite3` | relative to working directory (`/opt/ziko-assistant/data/...` in production) |
| `LOG_LEVEL` | no | `INFO` | `CRITICAL ERROR WARNING INFO DEBUG` (case-insensitive) |

## 3. Public API

### 3.1 `app.config`

```python
class ConfigError(ValueError): ...            # message never contains the token

@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str                             # excluded from repr()
    admin_ids: frozenset[int]
    database_path: Path
    log_level: str                             # upper-case

def load_settings(
    environ: Mapping[str, str] | None = None,  # tests only; None = os.environ (+ .env)
    *,
    env_file: Path | None = None,
) -> Settings
```

### 3.2 `app.models`

```python
class DeliveryOutcome(Enum):
    SENT = "sent"             # Telegram confirmed delivery
    REJECTED = "rejected"     # Telegram returned an API error: definitely NOT delivered
    UNCERTAIN = "uncertain"   # unknown: may have been delivered

@dataclass(frozen=True, slots=True)
class AppState:
    enabled: bool
    active_status_key: str | None
    session_id: str | None     # None whenever enabled is False
    updated_at: str            # UTC, "%Y-%m-%dT%H:%M:%S.%fZ"

@dataclass(frozen=True, slots=True)
class Status:
    key: str                   # sleeping, cooking, gaming, away, studying, working
    title: str                 # Persian, e.g. "در حال خواب"
    emoji: str
    reply_text: str

@dataclass(frozen=True, slots=True)
class BusinessConnectionState:
    connection_id: str
    is_enabled: bool
    can_reply: bool
    owner_user_id: int

DEFAULT_STATUSES: tuple[Status, ...]           # the six seed rows
```

### 3.3 `app.storage`

Constants: `SCHEMA_VERSION: int = 2`.

Exceptions (all derive from `StorageError(Exception)`):

| Exception | Raised when |
|---|---|
| `SchemaVersionError` | database newer than code, or no migration path |
| `UnknownStatusError` | status key does not exist (`activate_status`, `save_status_text`) |
| `UnknownConnectionError` | `claim_inbound_event` / `claim_reply` for a connection never passed to `save_business_connection` |
| `InvalidReplyTextError` (also `ValueError`) | reply text empty/whitespace or longer than 4096 characters |

`Database`:

```python
class Database:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,            # UTC clock; tests
        session_id_factory: Callable[[], str] | None = None,    # default uuid4 string
        pending_lease_seconds: int = 300,                       # > 0, else ValueError
        expired_lease_policy: Literal["reclaim", "uncertain"] = "uncertain",  # else ValueError
    ) -> None

    async def __aenter__(self) -> Self                          # = initialize()
    async def __aexit__(self, exc_type, exc, traceback) -> None # = close()
    async def initialize(self) -> None
    async def close(self) -> None

    # app state
    async def get_app_state(self) -> AppState
    async def activate_status(self, status_key: str) -> AppState
    async def enable_auto_reply(self) -> AppState
    async def disable_auto_reply(self) -> AppState

    # statuses
    async def list_statuses(self) -> list[Status]               # fixed seed order
    async def get_status(self, status_key: str) -> Status | None
    async def save_status_text(self, status_key: str, reply_text: str) -> Status

    # business connection
    async def save_business_connection(
        self, connection_id: str, is_enabled: bool, can_reply: bool, owner_user_id: int
    ) -> BusinessConnectionState                                # ValueError if connection_id empty
    async def get_business_connection(self) -> BusinessConnectionState | None

    # deduplication and delivery
    async def claim_inbound_event(self, connection_id: str, chat_id: int, message_id: int) -> bool
    async def claim_reply(self, connection_id: str, chat_id: int, session_id: str) -> str | None
    async def complete_reply(
        self,
        connection_id: str,
        chat_id: int,
        session_id: str,
        claim_token: str,
        outcome: DeliveryOutcome,
        telegram_message_id: int | None = None,
        error: str | None = None,
    ) -> bool                                                    # TypeError / ValueError on bad args
    async def release_reply(
        self, connection_id: str, chat_id: int, session_id: str, claim_token: str
    ) -> bool

    # maintenance
    async def prune_processed_messages(self, older_than_days: int = 30) -> int   # ValueError if < 1
```

Method notes:

* `activate_status` — raises `UnknownStatusError`. Returns the resulting `AppState`.
* `enable_auto_reply` — if no status has ever been selected, auto-reply **stays off**; check `state.enabled`.
* `save_status_text` — strips whitespace; does **not** change the session.
* `get_business_connection` — an enabled connection if there is one, otherwise the latest known; `None` if none saved.
* `claim_inbound_event` — `True` only the first time `(connection_id, chat_id, message_id)` is seen (survives restarts).
* `prune_processed_messages` — deletes dedup records older than the cutoff, returns the count. Call it periodically (e.g. daily).

## 4. Session lifecycle

`app_state` is a single row. A *session* is the period during which one status is continuously
active and auto-reply is on; `session_id` is a fresh UUID4 string for each one.

| Action | Resulting state | Session |
|---|---|---|
| (initial) | disabled, no status | none |
| `activate_status(k)` while disabled | enabled, status `k` | **new** |
| `activate_status(k)` while enabled with a *different* status | enabled, status `k` | **new** |
| `activate_status(k)` while enabled with the *same* status | unchanged (including `updated_at`) | **kept** |
| `disable_auto_reply()` | disabled, status remembered | discarded (`None`) |
| `enable_auto_reply()` while disabled with a remembered status | enabled | **new** |
| `enable_auto_reply()` while enabled | unchanged | kept |
| `enable_auto_reply()` with no remembered status | stays disabled | none |
| `save_status_text(...)` | unchanged | kept |

A database constraint forbids `enabled = 1` without a status and session, and `enabled = 0`
with a session, so a stale session id can never be reused after disabling.

## 5. Delivery state machine

One row per `(connection_id, chat_id, session_id)`; the primary key guarantees at most one
delivery record per contact per session.

```
                claim_reply                       complete_reply(SENT)
   (no row) ───────────────► pending ───────────────────────────────► sent        (final)
                              │  ▲  │
   complete_reply(REJECTED)   │  │  │ complete_reply(UNCERTAIN)
   or release_reply           ▼  │  └──────────────────────────────► uncertain   (final)
                            failed    claim_reply (new token)
                              └─────────────────┘
   pending + lease expired:  policy "uncertain" (DEFAULT) -> becomes uncertain, claim_reply returns None
                             policy "reclaim"   (opt-in)  -> claim_reply gives a NEW token (old token dead)
```

## 6. Claim / release / complete semantics

### 6.1 `claim_reply`
Returns a random `claim_token` (32 hex chars) **or `None`**. On `None`, send nothing. It
returns `None` when: the delivery is `sent`, `uncertain`, or `pending` with a live lease;
`session_id` is not the current active session; auto-reply is disabled. It succeeds for a new
row and for `failed` (retry after a confirmed rejection). Under the default policy an expired
`pending` lease is **not** handed out: it becomes `uncertain` and `None` is returned. Only with
the explicit opt-in `"reclaim"` policy does an expired `pending` lease succeed. Every success writes a **new** token, which invalidates the previous
holder's token. The whole claim is one atomic SQL statement inside a `BEGIN IMMEDIATE`
transaction. The caller must pass `session_id` read from `get_app_state()` immediately before.

### 6.2 `complete_reply`
One atomic `UPDATE ... WHERE connection_id, chat_id, session_id, claim_token AND state = 'pending'`.
Returns `True` only if *this call* made the transition. It returns `False` (and changes nothing)
when the token is not the current owner's — because the lease was reclaimed, the reservation was
already completed or released, or the token belongs to another chat/session/connection. A `False`
result on a stale token means another worker owns the reservation now; do not retry, just log.

| `outcome` | New state | Later incoming messages from that contact, same session |
|---|---|---|
| `SENT` | `sent` (+ `telegram_message_id`) | never answered again |
| `REJECTED` | `failed` (+ redacted `error`) | next message may re-claim and retry |
| `UNCERTAIN` | `uncertain` (+ redacted `error`) | **never answered again in this session** |

### 6.3 `release_reply`
For a worker that holds a token but **never called Telegram** (shutdown, or the connection lost
`can_reply` between claim and send). Same ownership rule as `complete_reply`; makes the delivery
retryable (`failed`, error `"released without sending"`). Never call it after a send attempt began.

### 6.4 Classifying send results (important for Module 3)
At-most-once has priority: when in doubt, report `UNCERTAIN`.

* `SENT`: `sendMessage` returned a `Message` (store `message.message_id`).
* `REJECTED`: Telegram returned an error *response* proving it did not process the request:
  `ok=false` with HTTP 400/403/404/429 (bad request, bot blocked, no `can_reply` right,
  flood control / `retry_after`).
* `UNCERTAIN`: anything else — timeouts, connection reset or broken pipe after the request was
  sent, HTTP 5xx, undecodable response, `CancelledError`/shutdown mid-send, any unexpected
  exception. A failure strictly *before* the request left the machine (e.g. DNS failure) is safe
  to treat as `REJECTED`, but classifying it `UNCERTAIN` is always allowed.
* On `retry_after`, do **not** sleep while holding the claim; complete as `REJECTED` and let a
  later message retry.

### 6.5 Handling of later messages after an uncertain outcome
Their inbound events are still deduplicated (`claim_inbound_event` returns `True`), but
`claim_reply` returns `None`, so the contact gets no further auto-reply in that session. This
is deliberate: the first reply may already have reached them. The contact is answered again
only when a **new session** starts (different status, or disable then enable). There is no
automatic or API-level way to clear an `uncertain` row; if the owner wants to retry, switching
status or toggling auto-reply is the supported way.

### 6.6 Crash, hang and lease behaviour (`expired_lease_policy`)
A worker that dies or hangs while holding a reservation leaves a `pending` row; whether it
delivered is unknown. Within `pending_lease_seconds` (default 300) nobody else may claim it.
When the lease has expired, the next `claim_reply` for that contact applies the policy:

* **`"uncertain"` (default, production):** the row becomes `uncertain`, `claim_reply` returns
  `None`, and the dead worker's token is dead too. Strict at-most-once: no duplicate can result,
  at the cost that this contact stays unanswered for the rest of the session.
* **`"reclaim"` (explicit opt-in, `Database(path, expired_lease_policy="reclaim")`):** the next
  incoming message claims the reservation under a new token. If the dead or hung worker had in
  fact delivered, that contact receives one duplicate. The old token is dead, so a hung worker
  waking up later cannot overwrite the new owner's reservation. Use only if answering every
  contact matters more than avoiding a duplicate.

Policy comparison (same session, same contact):

| Situation | `"uncertain"` (default) | `"reclaim"` (opt-in) |
|---|---|---|
| `REJECTED` (confirmed API rejection) | `failed`, retried by a later message | same |
| `complete_reply(UNCERTAIN)` | `uncertain`, never retried | same |
| `pending`, lease expired | becomes `uncertain`, never retried | re-claimed under a new token |
| `pending`, lease still live | refused (`None`) | refused (`None`) |

`pending_lease_seconds` must exceed the worst-case duration of one send attempt (set an HTTP
timeout well below it, e.g. 30 s). A caller cancelled *after* its claim committed loses the
token; the row then follows the lease rules above (so by default that one contact is not
answered again in the session).

### 6.7 Transaction safety
* Every write runs in `BEGIN IMMEDIATE … COMMIT` (rolled back on any error or cancellation;
  `BEGIN` is inside the guarded block so a cancelled caller cannot leave a transaction open).
* One connection per `Database`, guarded by an `asyncio.Lock`; reads take the same lock.
* `claim_reply`, `complete_reply`, `release_reply` and `claim_inbound_event` are each a single
  SQL statement conditional on current row state, so they are atomic across `Database` instances
  and processes sharing the file (covered by tests with two instances).
* WAL mode, `synchronous = NORMAL`, `busy_timeout = 5000`, `foreign_keys = ON`; file mode `0600`.

## 7. Privacy

No column holds message content. `processed_messages` stores only
`(connection_id, chat_id, message_id, created_at)`; `reply_deliveries` stores state, attempts,
Telegram message id, a token-redacted error string (≤ 500 chars) and timestamps. The storage layer
does not log connection ids, chat ids, texts or tokens.

## 8. Integration examples (illustrative, not executed)

### Module 2 — admin interface

```python
async def on_status_chosen(callback: CallbackQuery, db: Database) -> None:
    if callback.from_user.id not in settings.admin_ids:
        return
    key = callback.data.removeprefix("status:")
    try:
        state = await db.activate_status(key)         # same status -> session kept, no reset
    except UnknownStatusError:
        await callback.answer("وضعیت نامعتبر است", show_alert=True)
        return
    status = await db.get_status(key)
    await callback.answer(f"فعال شد: {status.emoji} {status.title}")

async def on_toggle(callback: CallbackQuery, db: Database) -> None:
    state = await db.get_app_state()
    state = await (db.disable_auto_reply() if state.enabled else db.enable_auto_reply())
    if not state.enabled and state.active_status_key is None:
        ...  # nothing was ever selected: ask the owner to pick a status first

async def on_edit_text(message: Message, db: Database, key: str) -> None:
    try:
        await db.save_status_text(key, message.text)
    except InvalidReplyTextError:
        await message.answer("متن نامعتبر است (خالی یا بیش از ۴۰۹۶ نویسه)")

# Business connection updates (Module 2 or 3, whichever owns the update type):
async def on_business_connection(bc: BusinessConnection, db: Database) -> None:
    await db.save_business_connection(
        bc.id, bc.is_enabled, bool(bc.rights and bc.rights.can_reply), bc.user.id
    )
```

### Module 3 — business message handler

```python
async def on_business_message(message: Message, bot: Bot, db: Database) -> None:
    # 1. Filters (Module 3's job): private chat only, not a bot, not the owner, not an admin
    #    chat, incoming only, not an edit.
    conn = await db.get_business_connection()
    if conn is None or not conn.is_enabled or not conn.can_reply:
        return
    if message.business_connection_id != conn.connection_id:
        return
    state = await db.get_app_state()
    if not state.enabled or state.session_id is None or state.active_status_key is None:
        return
    chat_id = message.chat.id

    # 2. Deduplicate the update (survives restarts and duplicate deliveries).
    if not await db.claim_inbound_event(conn.connection_id, chat_id, message.message_id):
        return

    # 3. Reserve the single reply for this contact in this session.
    token = await db.claim_reply(conn.connection_id, chat_id, state.session_id)
    if token is None:
        return                                   # already sent / uncertain / in flight / stale session

    status = await db.get_status(state.active_status_key)
    if status is None:
        await db.release_reply(conn.connection_id, chat_id, state.session_id, token)
        return

    # 4. Send, then record the outcome with the SAME token.
    async def finish(outcome, **kw):
        # shield: the outcome must be recorded even if this task is being cancelled
        return await asyncio.shield(
            db.complete_reply(conn.connection_id, chat_id, state.session_id, token, outcome, **kw)
        )

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=status.reply_text,
            business_connection_id=conn.connection_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
        await finish(DeliveryOutcome.REJECTED, error=type(exc).__name__)   # confirmed API rejection
    except asyncio.CancelledError:               # shutdown mid-send: the message may have gone out
        await finish(DeliveryOutcome.UNCERTAIN, error="CancelledError")
        raise
    except Exception as exc:                     # timeouts, network, 5xx, anything unexpected
        await finish(DeliveryOutcome.UNCERTAIN, error=type(exc).__name__)
        logger.exception("send outcome uncertain")   # never log message text or the token
    else:
        await finish(DeliveryOutcome.SENT, telegram_message_id=sent.message_id)

# Periodic maintenance (Module 5 / main): await db.prune_processed_messages(30)
```

The aiogram exception class names above must be checked against the installed aiogram
version; the contract only requires that the caller maps "Telegram returned an error
response" to `REJECTED` and everything else to `UNCERTAIN`. In aiogram 3 the network and
5xx errors (`TelegramNetworkError`, `TelegramServerError`) fall into the generic `Exception`
branch and are therefore `UNCERTAIN`, as intended.

## 9. Known limitations

* With the default policy `"uncertain"`, a worker crash/hang during a send, a network timeout or
  a cancelled send leaves that contact unanswered for the rest of the session, even if the message
  was never delivered. This is the intended price of strict at-most-once.
* Only the explicit opt-in policy `"reclaim"` can duplicate a message (a crash or hang during a
  send that actually delivered, followed by a lease expiry).
* A caller cancelled between claim commit and receiving the token leaves a `pending` row until
  the lease expires.
* Only one business connection is used by `get_business_connection()`.
* No API lists or clears `uncertain` deliveries; a new session is the way out.
* Real `aiosqlite` compatibility is untested (see the note at the top).


## Integration verification note (2026-11-23)

This contract remains authoritative for the integrated Module 2 delivery engine. The current
integration uses `claim_reply`'s returned token and settles each reservation with
`complete_reply(..., claim_token=..., outcome=DeliveryOutcome.*)` or `release_reply(...)`.
The database constructor's actual default remains `expired_lease_policy="uncertain"` and
has not been changed by the integration. See `docs/reports/INTEGRATION_REPORT.md` and `docs/reports/QA_REPORT.md`
for the integration reconciliation and the exact tests that could and could not be run.
