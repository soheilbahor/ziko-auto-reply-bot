"""Store / support names (v1.6): storage, reply variables and the owner settings screen."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import auto_reply


def owner(name: str = "") -> Any:
    return SimpleNamespace(timezone="Asia/Tehran", language="fa", owner_name=name, updated_at="")


MSG = SimpleNamespace(from_user=SimpleNamespace(first_name="Sara"), text="hi")
NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)


def test_brand_names_fill_the_variables():
    ctx = auto_reply._template_context(MSG, owner("Ali"), NOW, {"store_name": "Alfa Shop", "support_name": "Maryam"})
    assert (ctx["store_name"], ctx["support_name"], ctx["owner"]) == ("Alfa Shop", "Maryam", "Ali")


def test_empty_brand_falls_back_to_owner_name_like_1_5():
    for brand in (None, {"store_name": "", "support_name": ""}):
        ctx = auto_reply._template_context(MSG, owner("Ali"), NOW, brand)
        assert ctx["store_name"] == ctx["support_name"] == "Ali"


def test_nothing_set_uses_persian_default_and_never_leaks_braces():
    ctx = auto_reply._template_context(MSG, owner(""), NOW, None)
    text = auto_reply.render_template("سلام، از {store_name} پیام می‌دهم. {support_name}", ctx)
    assert "{" not in text and "فروشگاه ما" in text


@pytest.mark.asyncio
async def test_storage_round_trip_clear_and_validation(db: Any) -> None:
    from app.storage import InvalidReplyTextError
    assert await db.get_brand_names() == {"store_name": "", "support_name": ""}
    assert await db.set_brand_name("store_name", "  فروشگاه   آلفا ") == "فروشگاه آلفا"
    await db.set_brand_name("support_name", "مریم")
    assert await db.get_brand_names() == {"store_name": "فروشگاه آلفا", "support_name": "مریم"}
    await db.set_brand_name("store_name", "-")
    assert (await db.get_brand_names())["store_name"] == ""
    for field, value in (("store_name", "x" * 65), ("store_name", "{name}"), ("owner", "a"),
                         ("support_name", "<b>x</b>")):
        with pytest.raises(InvalidReplyTextError):
            await db.set_brand_name(field, value)


@pytest.mark.asyncio
async def test_engine_sends_the_store_name(armed: Any) -> None:
    from conftest import make_message
    state = await armed.db.get_app_state()
    await armed.db.save_status_text(state.active_status_key, "سلام، اینجا {store_name} است.")
    await armed.db.set_brand_name("store_name", "فروشگاه آلفا")
    assert await armed.deliver(make_message(message_id=601)) is auto_reply.ReplyOutcome.SENT
    assert any("فروشگاه آلفا" in str(m.get("text")) for m in armed.sent)


@pytest.mark.asyncio
async def test_owner_screen_sets_names_and_refuses_strangers(admin: Any) -> None:
    pytest.importorskip("aiogram")
    from conftest import ADMIN_ID, STRANGER_ID
    from app.keyboards.admin import Action, AdminCB

    def d(action):
        return AdminCB(action=action, key="").pack()
    mark = admin.mark()
    await admin.press(ADMIN_ID, d(Action.OWNER))
    found = admin.callback_data_in(admin.calls_since(mark))
    assert d(Action.STORE_NAME) in found and d(Action.SUPPORT_NAME) in found
    await admin.press(ADMIN_ID, d(Action.STORE_NAME))
    await admin.send_text(ADMIN_ID, "فروشگاه آلفا")
    await admin.press(ADMIN_ID, d(Action.SUPPORT_NAME))
    await admin.send_text(ADMIN_ID, "{bad}")                          # refused, still waiting
    await admin.send_text(ADMIN_ID, "مریم")
    assert await admin.db.get_brand_names() == {"store_name": "فروشگاه آلفا", "support_name": "مریم"}
    await admin.press(STRANGER_ID, d(Action.STORE_NAME))
    await admin.send_text(STRANGER_ID, "hacked")
    assert (await admin.db.get_brand_names())["store_name"] == "فروشگاه آلفا"
