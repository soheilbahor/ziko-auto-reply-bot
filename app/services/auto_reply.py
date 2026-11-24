"""At-most-once Telegram Business auto-reply engine.

Uses the Module 1 delivery-token contract. No message bodies are persisted or logged.
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from app.models import DeliveryOutcome

logger = logging.getLogger("ziko.auto_reply")


class ReplyOutcome(str, Enum):
    SENT = "sent"
    ALREADY_REPLIED = "already_replied"
    DUPLICATE_EVENT = "duplicate_event"
    IGNORED = "ignored"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class BusinessMessageEngine:
    """Business message processor. sender must expose aiogram Bot.send_message."""

    def __init__(self, *, db: Any, sender: Any, settings: Any) -> None:
        self.db, self.sender, self.settings = db, sender, settings
        self._connection_lock = asyncio.Lock()

    async def handle_business_connection(self, connection: Any) -> None:
        user = getattr(connection, "user", None)
        owner_id = getattr(user, "id", None)
        if owner_id not in self.settings.admin_ids:
            logger.warning("Ignoring business connection from non-owner")
            return
        rights = getattr(connection, "rights", None)
        can_reply = bool(getattr(rights, "can_reply", False))
        await self.db.save_business_connection(
            str(connection.id), bool(connection.is_enabled), can_reply, int(owner_id)
        )

    async def handle_business_message(self, message: Any) -> ReplyOutcome:
        connection_id = getattr(message, "business_connection_id", None)
        chat = getattr(message, "chat", None)
        sender = getattr(message, "from_user", None)
        if not connection_id or chat is None or getattr(chat, "type", None) != "private":
            return ReplyOutcome.IGNORED
        if getattr(message, "edit_date", None) is not None or sender is None or getattr(sender, "is_bot", False):
            return ReplyOutcome.IGNORED

        connection = await self.db.get_business_connection()
        if connection is None or connection.connection_id != connection_id:
            return ReplyOutcome.IGNORED
        if not connection.is_enabled or not connection.can_reply:
            return ReplyOutcome.IGNORED
        if (int(chat.id) == connection.owner_user_id or int(chat.id) in self.settings.admin_ids
                or int(sender.id) == connection.owner_user_id
                or int(sender.id) in self.settings.admin_ids):
            return ReplyOutcome.IGNORED

        state = await self.db.get_app_state()
        if not state.enabled or not state.session_id or not state.active_status_key:
            return ReplyOutcome.IGNORED

        # Deduplicate before claiming; message text is intentionally never retained.
        if not await self.db.claim_inbound_event(connection_id, int(chat.id), int(message.message_id)):
            return ReplyOutcome.DUPLICATE_EVENT
        status = await self.db.get_status(state.active_status_key)
        if status is None or not status.reply_text.strip() or len(status.reply_text) > 4096:
            return ReplyOutcome.IGNORED

        token = await self.db.claim_reply(connection_id, int(chat.id), state.session_id)
        if token is None:
            return ReplyOutcome.ALREADY_REPLIED

        # Re-check state and connection after claim. If sending has not begun, release.
        latest = await self.db.get_app_state()
        latest_conn = await self.db.get_business_connection()
        if (not latest.enabled or latest.session_id != state.session_id or latest_conn is None
                or not latest_conn.is_enabled or not latest_conn.can_reply
                or latest_conn.connection_id != connection_id):
            await self.db.release_reply(
                connection_id=connection_id, chat_id=int(chat.id),
                session_id=state.session_id, claim_token=token,
            )
            return ReplyOutcome.IGNORED

        try:
            sent = await self.sender.send_message(
                chat_id=int(chat.id), text=status.reply_text,
                business_connection_id=connection_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
            await self.db.complete_reply(
                connection_id=connection_id, chat_id=int(chat.id),
                session_id=state.session_id, claim_token=token,
                outcome=DeliveryOutcome.REJECTED, error=type(exc).__name__,
            )
            return ReplyOutcome.FAILED
        except asyncio.CancelledError:
            # Once the send call has started, cancellation is ambiguous.
            await asyncio.shield(self.db.complete_reply(
                connection_id=connection_id, chat_id=int(chat.id),
                session_id=state.session_id, claim_token=token,
                outcome=DeliveryOutcome.UNCERTAIN, error="cancelled_during_send",
            ))
            raise
        except Exception as exc:  # timeout/network/unknown => do not risk duplicate
            await self.db.complete_reply(
                connection_id=connection_id, chat_id=int(chat.id),
                session_id=state.session_id, claim_token=token,
                outcome=DeliveryOutcome.UNCERTAIN, error=type(exc).__name__,
            )
            return ReplyOutcome.UNCERTAIN

        await self.db.complete_reply(
            connection_id=connection_id, chat_id=int(chat.id),
            session_id=state.session_id, claim_token=token,
            outcome=DeliveryOutcome.SENT,
            telegram_message_id=getattr(sent, "message_id", None),
        )
        return ReplyOutcome.SENT


def create_engine(*, db: Any, sender: Any, settings: Any) -> BusinessMessageEngine:
    return BusinessMessageEngine(db=db, sender=sender, settings=settings)
