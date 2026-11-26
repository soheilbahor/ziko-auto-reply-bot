"""Ready-made reply library and safe templates (app/reply_library.py, app/templates.py)."""
from __future__ import annotations

import json
import re

import pytest

from app import reply_library as rl
from app import templates

PERSIAN = re.compile(r"[\u0600-\u06FF]")


# --------------------------------------------------------------------------- library data
def test_shipped_library_is_valid_and_rich():
    lib = rl.load()
    groups = {g.id for g in lib.groups}
    assert {"business", "vpn", "live_support", "personal"} <= groups
    assert lib.count >= 60
    for g in lib.groups:
        for c in g.categories:
            assert len(c.messages) >= 2, c.id                       # real variations per category
            assert 1 <= len(c.title) <= 40                          # becomes a status title
            for m in c.messages:
                assert PERSIAN.search(m.text), m.id                 # customer text is Persian
                assert len(m.text) <= 4096
                templates.validate(m.text)                          # only known variables


def test_required_business_examples_are_present():
    texts = [m.text for g in rl.load().groups for c in g.categories for m in c.messages]
    for needle in ["به فروشگاه ما خوش آمدید", "ادمین‌های ما آفلاین هستند", "پشتیبانی فروشگاه خوش آمدید",
                   "شماره سفارش", "رسید پرداخت شما دریافت شد", "نوع سرویس و توضیح کوتاهی"]:
        assert any(needle in t for t in texts), needle


def test_message_ids_are_unique_and_stable():
    lib = rl.load()
    ids = [m.id for g in lib.groups for c in g.categories for m in c.messages]
    assert len(ids) == len(set(ids))
    for g in lib.groups:
        for c in g.categories:
            for m in c.messages:
                assert lib.message(m.id) == m and m.id.startswith(c.id + ".")
    assert lib.message("nope.1") is None and lib.message("no_dot") is None


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["groups"][0]["categories"][0]["messages"][0].update(text="سلام {phone}"), "unknown variable"),
    (lambda d: d["groups"][0]["categories"][0]["messages"][0].update(tone="angry"), "unknown tone"),
    (lambda d: d["groups"][0]["categories"][0].update(messages=[]), "no messages"),
    (lambda d: d["groups"][0]["categories"][1].update(id=d["groups"][0]["categories"][0]["id"]), "duplicated"),
    (lambda d: d["groups"][0]["categories"][0].update(title="x" * 41), "1-40"),
    (lambda d: d["groups"][0]["categories"][0]["messages"][0].update(id="other.1"), "must start with"),
])
def test_invalid_library_is_rejected(mutate, needle):
    data = json.loads(rl.LIBRARY_PATH.read_text(encoding="utf-8"))
    mutate(data)
    with pytest.raises(rl.LibraryError, match=needle):
        rl.parse(data)


def test_status_from_message_uses_existing_status_model():
    lib = rl.load()
    args = rl.status_from_message(lib, "store_welcome.1")
    assert args["title"] == lib.category("store_welcome").title
    assert args["steps"] == [("text", lib.message("store_welcome.1").text)]
    with pytest.raises(KeyError):
        rl.status_from_message(lib, "missing.9")


# --------------------------------------------------------------------------- templates
def test_known_variables_render_and_v14_variables_still_work():
    ctx = {"name": "Ali", "time": "10:30", "date": "2026-11-25", "owner": "Reza", "store_name": "شیک‌شاپ",
           "order_id": "10482", "support_name": "مریم"}
    out = templates.render("سلام {name}، {store_name} · سفارش {order_id} · {support_name} · {time} {date} {owner}", ctx)
    assert out == "سلام Ali، شیک‌شاپ · سفارش 10482 · مریم · 10:30 2026-11-25 Reza"
    assert templates.render("{customer_name}", {"name": "Ali"}) == "Ali"   # alias


@pytest.mark.parametrize("template", [
    "سلام {name}", "سفارش {order_id}", "{store_name} خوش آمدید", "{support_name}", "{customer_name} عزیز",
])
def test_missing_values_use_persian_fallbacks_never_braces(template):
    out = templates.render(template, {})
    assert "{" not in out and "}" not in out and out.strip()


def test_inline_fallback_and_no_leak_of_unknown_or_injected_braces():
    assert templates.render("سلام {name|مشتری عزیز}", {}) == "سلام مشتری عزیز"
    assert "{" not in templates.render("{unknown} {foo|bar}", {})
    out = templates.render("سلام {name}", {"name": "{order_id}{store_name}"})   # value is never re-expanded
    assert "{" not in out and "فروشگاه ما" not in out


def test_values_are_single_line_and_bounded():
    out = templates.render("{name}!", {"name": "A\nB\tC " + "x" * 500})
    assert "\n" not in out and len(out) <= templates.MAX_VALUE_LENGTH + 1


def test_output_never_exceeds_telegram_limit():
    assert len(templates.render("{name}" * 2000, {"name": "x" * 64})) <= 4096


@pytest.mark.parametrize("bad,needle", [
    ("", "empty"), ("   ", "empty"), ("سلام {phone}", "unknown variable"),
    ("سلام {name", "braces"), ("سلام name}", "braces"), ("x" * 5000, "too long"),
])
def test_validate_rejects_mistakes_with_a_persian_message(bad, needle):
    with pytest.raises(templates.TemplateError, match=needle) as info:
        templates.validate(bad)
    assert PERSIAN.search(info.value.persian)


@pytest.mark.parametrize("text,order", [
    ("سفارش 10482 رسید؟", "10482"), ("order #77881", "77881"), ("کد 12", ""), ("", ""), (None, ""),
    ("09121234567", ""),                    # 11 digits is a phone number, not an order id
    ("ord-12345-x", ""),                    # part of another token
])
def test_order_id_extraction_never_invents(text, order):
    assert templates.extract_order_id(text) == order


def test_preview_is_realistic_and_complete():
    p = templates.preview("سلام {name}، سفارش {order_id} در {store_name}")
    assert "{" not in p and "سارا" in p and "10482" in p


# --------------------------------------------------------------------------- panel views
def test_panel_views_fit_telegram_and_escape():
    pytest.importorskip("aiogram")
    from app.handlers import library as panel

    lib = rl.load()
    text, kb = panel.groups_view(lib)
    assert str(lib.count) in text and len(kb.inline_keyboard) == len(lib.groups) + 2
    for g in lib.groups:
        assert panel.group_view(lib, g.id) is not None
        for c in g.categories:
            pages = -(-len(c.messages) // panel.PER_PAGE)
            for page in range(pages):
                text, kb = panel.category_view(lib, c.id, set(), page=page)
                assert len(text) <= 4096
                previews = re.findall(r"<blockquote>(.*?)</blockquote>", text, re.S)
                assert previews and not any("{" in q for q in previews)   # previews fully rendered
            for m in c.messages:
                text, kb = panel.message_view(lib, m.id, False)
                assert len(text) <= 4096
                for row in kb.inline_keyboard:
                    for button in row:
                        assert len(button.callback_data.encode()) <= 64, button.callback_data
    assert panel.group_view(lib, "nope") is None and panel.message_view(lib, "nope.1", False) is None
    assert panel.category_view(lib, "store_welcome", set(), page=999)[0]          # page clamps
    assert "هنوز پیامی" in panel.favourites_view(lib, [])[0]
    assert panel.favourites_view(lib, ["removed.1", "store_welcome.1"])[1].inline_keyboard[0]


# --------------------------------------------------------------------------- real database
async def test_install_and_favourites_on_a_real_tenant_database(tmp_path):
    pytest.importorskip("aiosqlite")
    from app.storage import Database

    db = Database(tmp_path / "bot-1.sqlite3")
    await db.initialize()
    try:
        lib = rl.load()
        status = await db.create_status(**rl.status_from_message(lib, "admin_offline.1"))
        await db.activate_status(status.key)
        state = await db.get_app_state()
        assert state.enabled and state.active_status_key == status.key
        assert (await db.get_status(status.key)).reply_text == lib.message("admin_offline.1").text
        assert await db.toggle_favourite("vpn_welcome.1") is True
        assert await db.toggle_favourite("store_welcome.2") is True
        assert await db.list_favourites() == ["vpn_welcome.1", "store_welcome.2"]
        assert await db.toggle_favourite("vpn_welcome.1") is False
        assert await db.list_favourites() == ["store_welcome.2"]
        with pytest.raises(OverflowError):
            for i in range(60):
                await db.toggle_favourite(f"x{i:02d}.1", limit=50)
    finally:
        await db.close()


async def test_favourites_are_isolated_per_tenant(tmp_path):
    pytest.importorskip("aiosqlite")
    from app.storage import Database

    a, b_ = Database(tmp_path / "bot-1.sqlite3"), Database(tmp_path / "bot-2.sqlite3")
    await a.initialize()
    await b_.initialize()
    try:
        await a.toggle_favourite("store_welcome.1")
        assert await b_.list_favourites() == []
    finally:
        await a.close()
        await b_.close()
