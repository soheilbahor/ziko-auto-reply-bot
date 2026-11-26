"""Owner-facing reply diagnosis (app/panel_health.py)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app import panel_health as ph


@dataclass
class Conn:
    connection_id: str = "c"
    is_enabled: bool = True
    can_reply: bool = True
    owner_user_id: int = 1


@dataclass
class State:
    enabled: bool = True
    active_status_key: str = "sleeping"
    session_id: str = "s"
    updated_at: str = "x"


@dataclass
class Status:
    key: str = "sleeping"
    title: str = "در حال خواب"
    emoji: str = "😴"
    reply_text: str = "سلام"
    is_active: bool = True
    reply_mode: str = "reply"
    min_interval_seconds: int = 0
    active_from_hour: int | None = None
    active_to_hour: int | None = None


@dataclass
class Owner:
    timezone: str = "Asia/Tehran"
    language: str = "fa"
    owner_name: str = ""
    updated_at: str = ""


def diagnose(**kw):
    args = dict(connection=Conn(), app_state=State(), status=Status(), owner=Owner())
    args.update(kw)
    return ph.diagnose(**args)


def blocks(d):
    return [f.title for f in d.findings if f.level == ph.BLOCK]


def test_healthy_bot_is_ready():
    d = diagnose()
    assert d.ready and d.badge.startswith("✅")


@pytest.mark.parametrize("kw,needle", [
    ({"connection": None}, "وصل نشده"),
    ({"connection": Conn(is_enabled=False)}, "قطع"),
    ({"connection": Conn(can_reply=False)}, "اجازه"),
    ({"app_state": State(enabled=False)}, "خاموش"),
    ({"status": None}, "پیدا نشد"),
    ({"status": Status(is_active=False)}, "غیرفعال"),
    ({"status": Status(reply_text="  ")}, "خالی"),
    ({"status": Status(active_from_hour=1, active_to_hour=5), "owner": Owner(timezone="Mars/Base")}, "نامعتبر"),
])
def test_each_blocking_gate_is_reported(kw, needle):
    d = diagnose(**kw)
    assert not d.ready
    assert any(needle in t for t in blocks(d))


def test_all_problems_are_listed_together():
    assert len(blocks(diagnose(connection=None, app_state=State(enabled=False)))) == 2


def test_outside_hour_window_shows_local_time():
    d = diagnose(status=Status(active_from_hour=9, active_to_hour=17),
                 now=datetime(2026, 11, 25, 20, 0, tzinfo=UTC))  # 23:30 in Tehran
    assert "23:30" in blocks(d)[0]


def test_hour_window_matches_the_reply_engine_exactly():
    pytest.importorskip("aiogram")
    from app.services.auto_reply import _hour_allowed

    base = datetime(2026, 11, 25, 0, 30, tzinfo=UTC)
    for half_hours in range(48):
        now = base + timedelta(minutes=30 * half_hours)
        for start, end in [(9, 17), (22, 6), (0, 23), (23, 1), (5, 6)]:
            ours = diagnose(status=Status(active_from_hour=start, active_to_hour=end), now=now).ready
            assert ours == _hour_allowed(now, "Asia/Tehran", start, end), (now, start, end)


def test_failure_rate_warning_needs_enough_samples():
    d = diagnose(stats={"sent": 3, "failed": 2, "uncertain": 1})
    assert d.ready and any(f.level == ph.WARN for f in d.findings)
    assert not any(f.level == ph.WARN for f in diagnose(stats={"sent": 0, "failed": 2}).findings)


def test_log_lines_are_grouped_into_explanations():
    lines = (["ERROR Too Many Requests: retry after 5"] * 3
             + ["ERROR TelegramForbiddenError: Forbidden", "WARNING TelegramNetworkError timeout"])
    found = ph.explain_problems(lines)
    assert len(found) == 3 and "(×3)" in found[0].title
    assert ph.explain_problems(["ERROR something odd"]) == []


def test_render_escapes_owner_data_and_fits_telegram():
    text = ph.render(diagnose(status=Status(emoji="<b>", title="<script>x</script>")))
    assert "<script>" not in text and "&lt;script&gt;" in text
    big = ph.render(diagnose(problems=[f"ERROR Too Many Requests {i}" for i in range(5000)]))
    assert len(big) <= 4096


@pytest.mark.parametrize("title,shown", [("Sleeping / در حال خواب", "در حال خواب"), ("24/7 open", "24/7 open")])
def test_title_display(title, shown):
    assert shown in ph.render(diagnose(status=Status(title=title)))


def test_connect_guide_states_telegram_limits_honestly():
    assert "گروه" in ph.CONNECT_GUIDE
    # Since 2026 connecting a bot needs no Premium; the guide must not claim otherwise.
    assert "Premium لازم نیست" in ph.CONNECT_GUIDE and "فقط با <b>Telegram Premium</b>" not in ph.CONNECT_GUIDE
    assert "Premium لازم نیست" in ph.ONBOARDING
