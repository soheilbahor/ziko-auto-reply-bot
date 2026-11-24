"""Shared data contracts for ZIKO Personal Assistant.

These immutable dataclasses are the only types exchanged between the storage
layer and the other modules (admin interface, business-message handlers).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeliveryOutcome(Enum):
    """How an auto-reply send attempt ended, from the sender's point of view.

    SENT:       Telegram confirmed delivery (sendMessage returned a Message).
    REJECTED:   Telegram returned an API error response (ok=false, e.g. 400/403/429),
                so the message was definitely NOT delivered. A later incoming message
                may retry.
    UNCERTAIN:  The outcome is unknown (timeout, connection reset after the request was
                sent, HTTP 5xx, process shutdown mid-send ...). The message MAY have been
                delivered, so it is never retried automatically (at-most-once priority).
    """

    SENT = "sent"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class AppState:
    """Global auto-reply state (a single row in the database)."""

    enabled: bool
    active_status_key: str | None
    session_id: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class Status:
    """A selectable status with its editable Persian reply text."""

    key: str
    title: str
    emoji: str
    reply_text: str


@dataclass(frozen=True, slots=True)
class BusinessConnectionState:
    """The state of a Telegram Business connection as last reported by Telegram."""

    connection_id: str
    is_enabled: bool
    can_reply: bool
    owner_user_id: int


# Initial statuses seeded on first start. Existing rows (and any text the owner
# has edited since) are never overwritten by seeding.
DEFAULT_STATUSES: tuple[Status, ...] = (
    Status(
        key="sleeping",
        title="در حال خواب",
        emoji="😴",
        reply_text="سلام! الان خوابم و نمی‌تونم جواب بدم. وقتی بیدار شدم حتماً بهت پیام می‌دم. 😴",
    ),
    Status(
        key="cooking",
        title="در حال آشپزی",
        emoji="🍳",
        reply_text="سلام! الان مشغول آشپزی‌ام و دستم بنده. بعد از اینکه کارم تموم شد جواب می‌دم. 🍳",
    ),
    Status(
        key="gaming",
        title="در حال بازی",
        emoji="🎮",
        reply_text="سلام! الان وسط بازی‌ام و نمی‌تونم جواب بدم. به محض اینکه بازی تموم شد بهت پیام می‌دم. 🎮",
    ),
    Status(
        key="away",
        title="بیرون از خانه",
        emoji="🚶",
        reply_text="سلام! الان بیرون از خونه‌ام و دسترسی‌م محدوده. وقتی برگشتم جواب می‌دم. 🚶",
    ),
    Status(
        key="studying",
        title="در حال مطالعه",
        emoji="📚",
        reply_text="سلام! الان مشغول مطالعه‌ام و تمرکز دارم. بعد از اینکه تموم کردم بهت پیام می‌دم. 📚",
    ),
    Status(
        key="working",
        title="مشغول کار",
        emoji="🛠",
        reply_text="سلام! الان سرم شلوغه و مشغول کارم. به محض اینکه وقتم آزاد شد جواب می‌دم. 🛠",
    ),
)
