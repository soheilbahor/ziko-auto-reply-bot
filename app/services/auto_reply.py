"""At-most-once Telegram Business auto-reply engine.

Uses the Module 1 delivery-token contract. No message bodies are persisted or logged.
"""
from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from typing import Any

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram.types import ReplyParameters

from app import templates
from app.logging_setup import set_stage
from app.models import DeliveryOutcome, OwnerSettings, ReplyStep

MAX_MESSAGE_LENGTH = 4096  # Telegram sendMessage text limit
DEFAULT_CONTACT_NAME = "دوست عزیز"  # used when the contact has no first name
DEFAULT_STEP_DELAY = 1.0  # seconds between the parts of one reply
_PLACEHOLDER = re.compile(r"\{(name|time|date|owner)\}")

logger = logging.getLogger("ziko.auto_reply")


def _error_detail(exc: BaseException) -> str:
    """Only the exception type. Telegram's description echoes the message text, so it is never logged."""
    return type(exc).__name__


class ReplyOutcome(str, Enum):
    SENT = "sent"
    ALREADY_REPLIED = "already_replied"
    DUPLICATE_EVENT = "duplicate_event"
    IGNORED = "ignored"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    PARTIAL = "partial"  # some parts were delivered, a later part failed; never retried


class BusinessMessageEngine:
    """Business message processor. sender must expose aiogram Bot.send_message."""

    def __init__(self, *, db: Any, sender: Any, settings: Any) -> None:
        self.db, self.sender, self.settings = db, sender, settings
        self.step_delay = DEFAULT_STEP_DELAY
        self._connection_lock = asyncio.Lock()

    async def handle_business_connection(self, connection: Any) -> None:
        """Persist only a connection whose owner is explicitly allow-listed.

        Telegram's ``BusinessConnection.user.id`` is the canonical owner ID.
        ``user_chat_id`` is used only as a compatibility fallback when a
        reduced/test update object omits ``user.id``; it identifies the private
        chat of the user who connected the bot. Missing or malformed identity
        always fails closed.
        """
        set_stage("VALIDATE_UPDATE")
        user = getattr(connection, "user", None)
        raw_owner_id = getattr(user, "id", None)
        if raw_owner_id is None:
            raw_owner_id = getattr(connection, "user_chat_id", None)
        if isinstance(raw_owner_id, bool):
            logger.warning("Ignoring business connection with invalid owner identity")
            return
        set_stage("PERMISSION_CHECK")
        configured_owner_id = getattr(self.settings, "business_owner_id", None)
        if isinstance(raw_owner_id, bool) or isinstance(configured_owner_id, bool):
            logger.warning("Ignoring business connection with invalid owner identity")
            return
        try:
            owner_id = int(raw_owner_id)
            expected_owner_id = int(configured_owner_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring business connection: BUSINESS_OWNER_ID is missing or invalid")
            return
        if owner_id <= 0 or expected_owner_id <= 0 or owner_id != expected_owner_id:
            logger.warning("Ignoring business connection from non-owner")
            return

        connection_id = getattr(connection, "id", None)
        if not isinstance(connection_id, str) or not connection_id.strip():
            logger.warning("Ignoring business connection with invalid connection ID")
            return

        rights = getattr(connection, "rights", None)
        # Telegram's optional/missing rights object must never inherit an old
        # grant. Only an explicit True authorizes replying.
        can_reply = getattr(rights, "can_reply", False) is True
        is_enabled = getattr(connection, "is_enabled", False) is True
        set_stage("DATABASE_SAVE")
        await self.db.save_business_connection(
            connection_id, is_enabled, can_reply, owner_id
        )
        logger.info("Business connection saved enabled=%s can_reply=%s", is_enabled, can_reply)

    async def _send_step(
        self, step: ReplyStep, *, chat_id: int, connection_id: str,
        reply_to: int | None, context: dict[str, str],
    ) -> Any:
        """Send one reply part. Only the first part quotes the incoming message."""
        reply_parameters = ReplyParameters(message_id=reply_to) if reply_to is not None else None
        if step.kind == "sticker":
            return await self.sender.send_sticker(
                chat_id=chat_id, sticker=step.content,
                business_connection_id=connection_id, reply_parameters=reply_parameters,
            )
        text = render_template(step.content, context)[:MAX_MESSAGE_LENGTH]
        return await self.sender.send_message(
            chat_id=chat_id, text=text,
            business_connection_id=connection_id, reply_parameters=reply_parameters,
        )

    async def _finish(
        self, connection_id: str, chat_id: int, session_id: str, token: str,
        outcome: DeliveryOutcome, parts: int, *, message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        set_stage("DATABASE_SAVE")
        await self.db.complete_reply(
            connection_id=connection_id, chat_id=chat_id, session_id=session_id,
            claim_token=token, outcome=outcome, telegram_message_id=message_id,
            error=error, parts_sent=parts,
        )

    async def handle_business_message(self, message: Any) -> ReplyOutcome:
        outcome = await self._handle_business_message(message)
        if outcome is ReplyOutcome.SENT:
            logger.info("Business auto-reply finished outcome=%s", outcome.value)
        else:
            logger.debug("Business message finished outcome=%s", outcome.value)
        return outcome

    async def _handle_business_message(self, message: Any) -> ReplyOutcome:
        set_stage("VALIDATE_UPDATE")
        connection_id = getattr(message, "business_connection_id", None)
        chat = getattr(message, "chat", None)
        sender = getattr(message, "from_user", None)
        if not connection_id or chat is None or getattr(chat, "type", None) != "private":
            return ReplyOutcome.IGNORED
        if (
            getattr(message, "edit_date", None) is not None
            or sender is None
            or getattr(sender, "is_bot", False)
            or getattr(sender, "id", None) is None
            or getattr(chat, "id", None) is None
            or getattr(message, "message_id", None) is None
        ):
            return ReplyOutcome.IGNORED

        set_stage("PERMISSION_CHECK")
        # The incoming update's connection ID is not proof of authorization.
        # A persisted, matching Telegram Business connection is mandatory.
        connection = await self.db.get_business_connection()
        if connection is None:
            return ReplyOutcome.IGNORED
        if connection.connection_id != connection_id:
            return ReplyOutcome.IGNORED
        if not connection.is_enabled or not connection.can_reply:
            return ReplyOutcome.IGNORED
        if (int(chat.id) == connection.owner_user_id or int(chat.id) in self.settings.admin_ids
                or int(sender.id) == connection.owner_user_id
                or int(sender.id) in self.settings.admin_ids):
            return ReplyOutcome.IGNORED

        set_stage("STATUS_LOAD")
        state = await self.db.get_app_state()
        if not state.enabled or not state.session_id or not state.active_status_key:
            return ReplyOutcome.IGNORED

        set_stage("DEDUPE_CLAIM")
        # Deduplicate before claiming; message text is intentionally never retained.
        if not await self.db.claim_inbound_event(connection_id, int(chat.id), int(message.message_id)):
            return ReplyOutcome.DUPLICATE_EVENT
        status = await self.db.get_status(state.active_status_key)
        if status is None or not status.reply_text.strip() or len(status.reply_text) > 4096:
            return ReplyOutcome.IGNORED

        # Policy gates run before any reservation, so refused messages leave no delivery row.
        owner = await self.db.get_owner_settings()
        now = datetime.now(UTC)
        if not status.is_active:
            return ReplyOutcome.IGNORED
        if await self.db.is_reply_exception(int(chat.id)):
            return ReplyOutcome.IGNORED
        if not _hour_allowed(now, owner.timezone, status.active_from_hour, status.active_to_hour):
            return ReplyOutcome.IGNORED
        if status.min_interval_seconds:
            last = await self.db.last_reply_sent_at(connection_id, int(chat.id))
            if last is not None and (now - last).total_seconds() < status.min_interval_seconds:
                return ReplyOutcome.IGNORED

        set_stage("REPLY_CLAIM")
        token = await self.db.claim_reply(connection_id, int(chat.id), state.session_id)
        if token is None:
            return ReplyOutcome.ALREADY_REPLIED

        set_stage("RECHECK")
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

        set_stage("SEND")
        steps = await self.db.list_reply_steps(status.key)
        if not steps:
            steps = [ReplyStep(1, "text", status.reply_text)]
        # Rotation (v1.5): chosen only now, after the claim, so a duplicate update can never
        # advance the sequence or change which variant this contact receives.
        picker = getattr(self.db, "next_reply_variant", None)
        if picker is not None:
            try:
                variant = await picker(status.key)
            except Exception:
                logger.warning("Reply rotation unavailable; using the status reply", exc_info=True)
                variant = None
            if variant:
                steps = [ReplyStep(1, "text", variant)]
        context = _template_context(message, owner, now)
        reply_to = int(message.message_id) if status.reply_mode == "reply" else None
        delivered = 0
        last_message_id: int | None = None
        try:
            for index, step in enumerate(steps):
                if index:
                    await asyncio.sleep(self.step_delay)
                sent = await self._send_step(
                    step, chat_id=int(chat.id), connection_id=connection_id,
                    reply_to=reply_to if index == 0 else None, context=context,
                )
                delivered += 1
                last_message_id = getattr(sent, "message_id", None)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
            if delivered == 0:
                await self._finish(connection_id, int(chat.id), state.session_id, token,
                                   DeliveryOutcome.REJECTED, 0, error=_error_detail(exc))
                logger.warning("Auto-reply rejected by Telegram %s retries=0", _error_detail(exc))
                return ReplyOutcome.FAILED
            await self._finish(connection_id, int(chat.id), state.session_id, token,
                               DeliveryOutcome.PARTIAL, delivered, error=_error_detail(exc))
            logger.warning("Auto-reply partially sent parts=%s then rejected %s retries=0",
                           delivered, _error_detail(exc))
            return ReplyOutcome.PARTIAL
        except asyncio.CancelledError:
            # Once a send call has started, cancellation is ambiguous.
            outcome = DeliveryOutcome.UNCERTAIN if delivered == 0 else DeliveryOutcome.PARTIAL
            await asyncio.shield(self._finish(connection_id, int(chat.id), state.session_id,
                                              token, outcome, delivered,
                                              error="cancelled_during_send"))
            logger.warning("Auto-reply cancelled during send parts=%s retries=0", delivered)
            raise
        except Exception as exc:  # timeout/network/unknown => do not risk duplicate
            outcome = DeliveryOutcome.UNCERTAIN if delivered == 0 else DeliveryOutcome.PARTIAL
            await self._finish(connection_id, int(chat.id), state.session_id, token,
                               outcome, delivered, error=_error_detail(exc))
            logger.warning("Auto-reply outcome %s; not retried %s parts=%s retries=0",
                           outcome.value, _error_detail(exc), delivered)
            return ReplyOutcome.UNCERTAIN if delivered == 0 else ReplyOutcome.PARTIAL

        await self._finish(connection_id, int(chat.id), state.session_id, token,
                           DeliveryOutcome.SENT, delivered, message_id=last_message_id)
        return ReplyOutcome.SENT


def render_template(content: str, context: dict[str, str]) -> str:
    """Fill reply variables via app.templates (single pass, safe fallbacks).

    v1.5: unknown or empty variables use a Persian fallback, and nothing shaped like
    ``{...}`` is ever sent to a customer. v1.4 variables behave exactly as before.
    """
    return templates.render(content, context)


def _template_context(message: Any, owner: OwnerSettings, now_utc: datetime) -> dict[str, str]:
    local = now_utc.astimezone(ZoneInfo(owner.timezone))
    first_name = str(getattr(getattr(message, "from_user", None), "first_name", "") or "").strip()
    # The customer's text is used ONLY to spot an order number they typed; it is never
    # stored or logged (privacy rule of this module).
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    return {
        "name": first_name[:64] or DEFAULT_CONTACT_NAME,
        "customer_name": first_name[:64],
        "time": local.strftime("%H:%M"),
        "date": local.strftime("%Y-%m-%d"),
        "owner": owner.owner_name,
        "store_name": owner.owner_name,
        "support_name": owner.owner_name,
        "order_id": templates.extract_order_id(text if isinstance(text, str) else None),
    }


def _hour_allowed(now_utc: datetime, timezone: str, start: int | None, end: int | None) -> bool:
    """True inside the local [start, end) window; windows may cross midnight."""
    if start is None or end is None:
        return True
    hour = now_utc.astimezone(ZoneInfo(timezone)).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def create_engine(*, db: Any, sender: Any, settings: Any) -> BusinessMessageEngine:
    return BusinessMessageEngine(db=db, sender=sender, settings=settings)
