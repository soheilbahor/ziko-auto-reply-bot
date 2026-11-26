"""Child-bot lifecycle: explicit states, startup verification and error classification.

Why this module exists (root cause of "child bots are created but never answer /start"):

aiogram's polling loop (Dispatcher._listen_updates, aiogram 3.34) catches EVERY exception
from getUpdates, logs it and retries forever with backoff. It never raises. So:

* a token revoked in @BotFather          -> TelegramUnauthorizedError retried forever
* the same token polled elsewhere         -> TelegramConflictError (409) retried forever
* a webhook left on the bot               -> getUpdates refused (409) retried forever

In every case ZIKO's supervisor (which only handles exceptions that *escape*
start_polling) believed the bot was "running": it held a polling slot, was reported as
running in /platform, and the owner's /start was simply never received.

The fix has three parts, all here so they are testable without aiogram:

1. ``PollingWatchdog``: the session middleware counts consecutive getUpdates failures by
   class. A fatal class (unauthorized / conflict / not found) or N consecutive failures of
   any class trips it; the supervisor then stops that poller deliberately and handles the
   cause instead of letting aiogram spin.
2. ``State``: a bot is RUNNING only after getMe succeeded, the webhook was removed AND the
   first getUpdates call returned. Before that it is STARTING; never ACTIVE by assumption.
3. ``classify``: maps every startup/polling failure to an English code + an operator
   message (logs, CLI) and a Persian explanation (Telegram, owner-facing).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class State(str, Enum):
    QUEUED = "queued"          # enabled, waiting for a polling slot
    STARTING = "starting"      # slot held: getMe / deleteWebhook / first getUpdates pending
    RUNNING = "running"        # first getUpdates returned: updates ARE being received
    BACKOFF = "backoff"        # a run ended with a retryable error; restart scheduled
    FAILED = "failed"          # stopped for a non-retryable reason (needs a human)
    STOPPED = "stopped"        # disabled, or the platform is shutting down


@dataclass(frozen=True, slots=True)
class Diagnosis:
    code: str            # stable machine code, English
    retryable: bool
    disable: bool        # stop for good and mark the tenant disabled
    operator: str        # English, for logs/CLI/admin notification
    owner_title: str     # Persian, for the child-bot owner in Telegram
    owner_action: str    # Persian


# Exception class name -> diagnosis. Names are matched along the class hierarchy, so the
# module never imports aiogram and tests can use stand-in classes.
_TABLE: dict[str, Diagnosis] = {
    "TelegramUnauthorizedError": Diagnosis(
        "token_rejected", retryable=False, disable=True,
        operator="Telegram rejected the bot token (revoked or regenerated in @BotFather).",
        owner_title="توکن ربات شما از طرف تلگرام رد شد",
        owner_action="اگر توکن را در ‎@BotFather عوض کرده‌اید، در ربات اصلی ‎/connect_bot را بزنید و توکن جدید را بفرستید."),
    "TelegramConflictError": Diagnosis(
        "polling_conflict", retryable=False, disable=False,
        operator="Another process is polling this token (HTTP 409). Stop the other instance or bot host.",
        owner_title="این ربات هم‌زمان جای دیگری هم اجرا می‌شود",
        owner_action="اگر این ربات را روی سرور یا سرویس دیگری هم راه‌انداخته‌اید، آن را خاموش کنید؛ ZIKO دوباره تلاش می‌کند."),
    "TelegramNotFound": Diagnosis(
        "bot_not_found", retryable=False, disable=True,
        operator="Telegram reports the bot does not exist (deleted in @BotFather).",
        owner_title="این ربات در تلگرام پیدا نشد",
        owner_action="احتمالاً ربات در ‎@BotFather حذف شده است. یک ربات جدید بسازید و با ‎/create_bot وصل کنید."),
    "TelegramRetryAfter": Diagnosis(
        "flood_wait", retryable=True, disable=False,
        operator="Telegram rate limit (FloodWait); retrying after the requested delay.",
        owner_title="تلگرام موقتاً سرعت را محدود کرده است",
        owner_action="نیازی به کاری نیست؛ ربات خودش صبر می‌کند و ادامه می‌دهد."),
    "TelegramServerError": Diagnosis(
        "telegram_unavailable", retryable=True, disable=False,
        operator="Telegram API server error (5xx); retrying with backoff.",
        owner_title="سرورهای تلگرام موقتاً در دسترس نیستند",
        owner_action="نیازی به کاری نیست؛ ربات به‌صورت خودکار دوباره وصل می‌شود."),
    "TelegramNetworkError": Diagnosis(
        "network", retryable=True, disable=False,
        operator="Network error talking to api.telegram.org; check DNS, firewall and routing.",
        owner_title="ارتباط سرور با تلگرام قطع شده است",
        owner_action="نیازی به کاری نیست؛ پس از برقراری ارتباط، ربات خودکار ادامه می‌دهد."),
    "TimeoutError": Diagnosis(
        "timeout", retryable=True, disable=False,
        operator="Request to Telegram timed out; retrying with backoff.",
        owner_title="پاسخ تلگرام دیر رسید",
        owner_action="نیازی به کاری نیست؛ ربات دوباره تلاش می‌کند."),
    "TokenMismatch": Diagnosis(
        "token_mismatch", retryable=False, disable=True,
        operator="The stored token belongs to a different bot than the registered bot_id.",
        owner_title="توکن ذخیره‌شده متعلق به ربات دیگری است",
        owner_action="در ربات اصلی ‎/connect_bot را بزنید و توکن همین ربات را دوباره بفرستید."),
    "StorageError": Diagnosis(
        "database", retryable=True, disable=False,
        operator="The child bot's database could not be opened or migrated; see the traceback in the log.",
        owner_title="مشکل موقت در ذخیره‌سازی تنظیمات ربات",
        owner_action="مدیر پلتفرم مطلع شده است. چند دقیقه بعد دوباره امتحان کنید."),
}
_UNKNOWN = Diagnosis(
    "unknown", retryable=True, disable=False,
    operator="Unexpected error while starting or polling; see the log for the exception type.",
    owner_title="خطای پیش‌بینی‌نشده در اجرای ربات",
    owner_action="ZIKO به‌صورت خودکار دوباره تلاش می‌کند. اگر ادامه داشت، با پشتیبانی تماس بگیرید.")


def classify(exc: BaseException | str | None) -> Diagnosis:
    if exc is None:
        return _UNKNOWN
    if isinstance(exc, str):
        return _TABLE.get(exc, _UNKNOWN)
    for cls in type(exc).mro():
        found = _TABLE.get(cls.__name__)
        if found is not None:
            return found
    return _UNKNOWN


FATAL_CODES = frozenset({"token_rejected", "polling_conflict", "bot_not_found", "token_mismatch"})


class TokenMismatch(Exception):
    """getMe answered with a different bot id than the one registered for this token."""


class WatchdogTripped(Exception):
    """Raised by the supervisor to end a poller that aiogram would otherwise retry forever."""

    def __init__(self, error_name: str, consecutive: int, error: BaseException | None = None) -> None:
        super().__init__(f"{error_name} x{consecutive}")
        self.error_name = error_name
        self.consecutive = consecutive
        self.error = error  # the last underlying exception (most specific class seen)


@dataclass(slots=True)
class PollingWatchdog:
    """Observes getUpdates results for one poller. Pure; driven by the session middleware.

    * first successful getUpdates -> ``first_poll`` is set (the bot is genuinely RUNNING)
    * a FATAL error class          -> trips after ``fatal_after`` consecutive occurrences
      (2 by default, so one transient 409 while a previous instance shuts down is tolerated)
    * any error class              -> trips after ``max_consecutive`` in a row
    """

    fatal_after: int = 2
    max_consecutive: int = 8
    consecutive: int = 0
    last_error: str | None = None
    tripped: WatchdogTripped | None = None
    first_poll_at: float | None = None
    last_ok_at: float | None = None
    _callbacks: list[Any] = field(default_factory=list)

    @property
    def first_poll(self) -> bool:
        return self.first_poll_at is not None

    @property
    def healthy(self) -> bool:
        """True while the most recent getUpdates succeeded (aiogram may be retrying otherwise)."""
        return self.first_poll and self.consecutive == 0

    def on_trip(self, callback: Any) -> None:
        self._callbacks.append(callback)

    def ok(self, now: float | None = None) -> None:
        now = now or time.time()
        if self.first_poll_at is None:
            self.first_poll_at = now
        self.last_ok_at = now
        self.consecutive = 0
        self.last_error = None

    def failed(self, exc: BaseException) -> WatchdogTripped | None:
        if self.tripped is not None:
            return self.tripped
        name = type(exc).__name__
        self.consecutive = self.consecutive + 1 if name == self.last_error else 1
        self.last_error = name
        fatal = classify(exc).code in FATAL_CODES
        if (fatal and self.consecutive >= self.fatal_after) or self.consecutive >= self.max_consecutive:
            # Remember the most specific class seen, so the supervisor can classify it.
            self.tripped = WatchdogTripped(name, self.consecutive, exc)
            for callback in self._callbacks:
                callback(self.tripped)
        return self.tripped


@dataclass(slots=True)
class BotStatus:
    """What the platform knows about one child bot, for /platform, CLI and logs."""

    bot_id: int
    state: State = State.QUEUED
    since: float = field(default_factory=time.time)
    starts: int = 0                  # start attempts since the process started
    restarts: int = 0                # attempts after the first
    last_error_code: str | None = None
    last_error_at: float | None = None
    running_since: float | None = None

    def move(self, state: State, *, error: Diagnosis | None = None, now: float | None = None) -> None:
        now = now or time.time()
        if state is State.STARTING:
            self.starts += 1
            if self.starts > 1:
                self.restarts += 1
        if state is State.RUNNING:
            self.running_since = now
        elif state is not State.RUNNING:
            self.running_since = None
        if error is not None:
            self.last_error_code, self.last_error_at = error.code, now
        self.state, self.since = state, now

    def as_dict(self) -> dict[str, Any]:
        return {"bot_id": self.bot_id, "state": self.state.value, "since": self.since,
                "starts": self.starts, "restarts": self.restarts,
                "last_error_code": self.last_error_code, "last_error_at": self.last_error_at,
                "running_since": self.running_since}
