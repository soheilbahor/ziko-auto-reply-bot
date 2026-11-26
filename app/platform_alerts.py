"""Proactive Telegram alerts for platform admins (pure decision logic + a small async loop).

Off by default: set ZIKO_PLATFORM_ALERTS=1 in ziko.env to enable. /platform works either way.

Design rules, chosen to keep alerts trustworthy rather than noisy:

* Only states an ADMIN can act on alert by default: ``crashed`` (polling stopped or
  silent) and ``token`` (token rejected). States the owner controls -- auto-reply off,
  Business disconnected -- are the owner's business and are shown in /platform only.
  ``ZIKO_ALERT_STATES`` can widen this (e.g. ``crashed,token,warning,blocked``).
* Debounce: a bot must be in an alert state for ``confirm_checks`` consecutive checks
  before anyone is told, so restarts and one-off network blips never page anybody.
* Recovery: when an alerted bot is healthy again for ``confirm_checks`` checks, one
  "recovered" line is sent. No recovery line for something never announced.
* Batching: everything that changed in one check is ONE message.
* Limits: per-bot cooldown before re-alerting the same bot, and a global hourly cap;
  anything suppressed by the cap is counted and reported in the next allowed message.
* Capacity: the polling queue staying non-empty is reported once, then on recovery.
* Startup: existing problems are summarised once (after debounce), not as N alerts.

State is in memory; mute settings persist in ``<data>/run/alerts.json`` (0600).
No token, chat content or contact data is ever placed in an alert.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("ziko.alerts.platform")

DEFAULT_STATES = frozenset({"crashed", "token"})
HEALTHY_STATES = frozenset({"ready", "queued", "disabled"})
ICON = {"crashed": "🟥", "token": "🔑", "blocked": "⛔", "warning": "⚠️"}
LABEL = {"crashed": "پولینگ متوقف یا بی‌پاسخ", "token": "توکن رد شده",
         "blocked": "پاسخ ارسال نمی‌شود", "warning": "هشدار"}
MAX_LISTED = 15  # bots listed per message; the rest are summarised as a count


@dataclass(slots=True)
class AlertConfig:
    interval: float = 60.0
    confirm_checks: int = 2
    cooldown: float = 3600.0
    max_per_hour: int = 6
    states: frozenset[str] = DEFAULT_STATES
    queue_checks: int = 5

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str]) -> "AlertConfig":
        def num(name: str, default: float, lo: float, hi: float) -> float:
            raw = str(env.get(name, "")).strip()
            if not raw:
                return default
            value = float(raw)
            if not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
            return value
        states = DEFAULT_STATES
        raw_states = str(env.get("ZIKO_ALERT_STATES", "")).strip()
        if raw_states:
            states = frozenset(s.strip() for s in raw_states.split(",") if s.strip())
            unknown = states - set(ICON)
            if unknown:
                raise ValueError(f"ZIKO_ALERT_STATES has unknown states: {', '.join(sorted(unknown))}")
        return cls(
            interval=num("ZIKO_ALERT_INTERVAL_SECONDS", 60, 15, 3600),
            confirm_checks=int(num("ZIKO_ALERT_CONFIRM_CHECKS", 2, 1, 20)),
            cooldown=num("ZIKO_ALERT_COOLDOWN_SECONDS", 3600, 60, 86400),
            max_per_hour=int(num("ZIKO_ALERT_MAX_PER_HOUR", 6, 1, 60)),
            states=states,
        )


@dataclass(slots=True)
class _Track:
    state: str
    reason: str
    username: str
    streak: int = 1           # consecutive checks in the current state class
    announced: bool = False   # an alert for this problem was sent
    last_alert_at: float = 0.0
    healthy_streak: int = 0


@dataclass(slots=True)
class AlertEngine:
    config: AlertConfig = field(default_factory=AlertConfig)
    _tracks: dict[int, _Track] = field(default_factory=dict)
    _sent_at: list[float] = field(default_factory=list)
    _suppressed: int = 0
    _queue_streak: int = 0
    _queue_announced: bool = False
    _cycles: int = 0
    muted_until: float = 0.0
    # Undo record of the last message returned by step(); see undo_last().
    _last: dict[str, Any] | None = None

    def _is_alert(self, state: str) -> bool:
        return state in self.config.states

    def step(self, bots: Iterable[Any], *, waiting: int, running: int, limit: int,
             now: float | None = None) -> str | None:
        """Feed one overview snapshot; return the message to send now, or None."""
        now = now or time.time()
        self._cycles += 1
        new_problems: list[tuple[int, _Track]] = []
        recovered: list[tuple[int, _Track]] = []
        seen: set[int] = set()
        for b in bots:
            seen.add(b.bot_id)
            reason = (b.reasons[0] if b.reasons else "")[:160]
            tr = self._tracks.get(b.bot_id)
            bad = self._is_alert(b.state)
            if tr is None:
                tr = self._tracks[b.bot_id] = _Track(b.state, reason, b.username, streak=1 if bad else 0)
                tr.healthy_streak = 0 if bad else 1
            elif bad:
                tr.streak = tr.streak + 1 if self._is_alert(tr.state) else 1
                tr.healthy_streak = 0
                tr.state, tr.reason, tr.username = b.state, reason, b.username
            else:
                tr.healthy_streak += 1
                tr.streak = 0
                tr.state, tr.reason, tr.username = b.state, reason, b.username
            if bad and not tr.announced and tr.streak >= self.config.confirm_checks:
                if now - tr.last_alert_at >= self.config.cooldown or tr.last_alert_at == 0:
                    new_problems.append((b.bot_id, tr))
            elif (not bad and tr.announced and b.state in HEALTHY_STATES
                  and tr.healthy_streak >= self.config.confirm_checks):
                recovered.append((b.bot_id, tr))
        for gone in set(self._tracks) - seen:  # bot deleted from the registry
            del self._tracks[gone]

        capacity_line = None
        self._queue_streak = self._queue_streak + 1 if waiting and running >= limit else 0
        if self._queue_streak >= self.config.queue_checks and not self._queue_announced:
            capacity_line = (f"⏳ ظرفیت پولینگ پر است ({running}/{limit})؛ {waiting} ربات در صف هستند. "
                             "ZIKO_TENANT_MAX_CONCURRENT_POLLERS را افزایش دهید.")
            self._queue_announced = True
        elif self._queue_announced and self._queue_streak == 0:
            capacity_line = "✅ صف پولینگ خالی شد."
            self._queue_announced = False

        # Problems confirmed within the first debounce window existed at start-up.
        startup = self._cycles <= self.config.confirm_checks
        if not (new_problems or recovered or capacity_line):
            return None
        if now < self.muted_until:
            # Muted: remember the decision so nothing is re-announced after unmute,
            # but count it for the summary.
            for _, tr in new_problems:
                tr.announced, tr.last_alert_at = True, now
            for _, tr in recovered:
                tr.announced = False
            self._suppressed += len(new_problems) + len(recovered) + bool(capacity_line)
            return None
        self._sent_at = [t for t in self._sent_at if now - t < 3600]
        if len(self._sent_at) >= self.config.max_per_hour:
            # Over the hourly cap: do NOT mark as announced, so they are retried later.
            self._suppressed += len(new_problems) + len(recovered) + bool(capacity_line)
            if capacity_line:
                self._queue_announced = not self._queue_announced
            return None
        self._last = {
            "problems": [(tr, tr.last_alert_at) for _, tr in new_problems],
            "recovered": [tr for _, tr in recovered],
            "queue_before": (not self._queue_announced) if capacity_line else self._queue_announced,
            "sent_at": now, "suppressed": self._suppressed,
        }
        for _, tr in new_problems:
            tr.announced, tr.last_alert_at = True, now
        for _, tr in recovered:
            tr.announced = False
        self._sent_at.append(now)
        msg = self._render(new_problems, recovered, capacity_line, startup)
        self._suppressed = 0
        return msg

    def undo_last(self) -> bool:
        """The last message reached no admin: forget it was sent, so it is retried.

        Restores announced flags, cooldown stamps, the queue flag, the hourly counter and
        the suppressed count. Before 1.6 the alert was marked as sent before delivery and
        a failed send lost it for good."""
        last, self._last = self._last, None
        if last is None:
            return False
        for tr, previous_stamp in last["problems"]:
            tr.announced, tr.last_alert_at = False, previous_stamp
        for tr in last["recovered"]:
            tr.announced = True
        self._queue_announced = last["queue_before"]
        try:
            self._sent_at.remove(last["sent_at"])
        except ValueError:
            pass
        self._suppressed = last["suppressed"]
        return True

    def _render(self, problems: list[tuple[int, _Track]], recovered: list[tuple[int, _Track]],
                capacity: str | None, startup: bool) -> str:
        e = lambda v: html.escape(str(v), quote=False)  # noqa: E731
        head = "🚨 <b>ZIKO: وضعیت ربات‌ها</b>" if problems else "🔔 <b>ZIKO: به‌روزرسانی وضعیت</b>"
        lines = [head]
        if startup and problems:
            lines.append("<i>خلاصه‌ی مشکلات موجود هنگام راه‌اندازی:</i>")
        if problems:
            lines.append("")
            for bot_id, tr in problems[:MAX_LISTED]:
                lines.append(f"{ICON.get(tr.state, '•')} @{e(tr.username)} ({LABEL.get(tr.state, tr.state)})")
                if tr.reason:
                    lines.append(f"   ↳ {e(tr.reason)}")
            if len(problems) > MAX_LISTED:
                lines.append(f"… و {len(problems) - MAX_LISTED} ربات دیگر")
        if recovered:
            lines += ["", "✅ <b>بهبود یافت:</b> " + "، ".join(f"@{e(t.username)}" for _, t in recovered[:MAX_LISTED])
                      + (f" و {len(recovered) - MAX_LISTED} مورد دیگر" if len(recovered) > MAX_LISTED else "")]
        if capacity:
            lines += ["", capacity]
        if self._suppressed:
            lines += ["", f"<i>{self._suppressed} اعلان دیگر به دلیل سقف ساعتی یا بی‌صدا بودن ارسال نشد.</i>"]
        lines += ["", "جزئیات: /platform   ·   بی‌صدا: /alerts mute 1h"]
        text = "\n".join(lines)
        return text if len(text) <= 4000 else text[:3990] + "…"

    # ------------------------------------------------------------------ mute
    def mute(self, seconds: float, now: float | None = None) -> None:
        self.muted_until = (now or time.time()) + seconds

    def unmute(self) -> None:
        self.muted_until = 0.0

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        return {"muted_for": max(0, int(self.muted_until - now)),
                "tracked": len(self._tracks),
                "alerting": sorted(t.username for t in self._tracks.values() if t.announced),
                "sent_last_hour": len([t for t in self._sent_at if now - t < 3600]),
                "suppressed": self._suppressed}


# ---------------------------------------------------------------------- persistence
def load_mute(path: Path) -> float:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("muted_until", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def save_mute(path: Path, muted_until: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps({"muted_until": muted_until}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def parse_duration(text: str) -> float | None:
    """'30m', '2h', '1d', '45' (minutes). Returns seconds, or None if invalid / > 7 days."""
    t = text.strip().lower()
    if not t:
        return None
    unit = {"m": 60, "h": 3600, "d": 86400}.get(t[-1])
    num = t[:-1] if unit else t
    if not (num.isascii() and num.isdigit()):
        return None
    seconds = int(num) * (unit or 60)
    return float(seconds) if 0 < seconds <= 7 * 86400 else None


# ---------------------------------------------------------------------- loop
async def run_alert_loop(*, engine: AlertEngine, overview: Callable[[], Awaitable[Any]],
                         send: Callable[[int, str], Awaitable[Any]], admin_ids: Iterable[int],
                         mute_path: Path, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
    """Background task. Never raises except on cancellation."""
    engine.muted_until = max(engine.muted_until, load_mute(mute_path))
    admins = sorted(admin_ids)
    while True:
        await sleep(engine.config.interval)
        try:
            ov = await overview()
            msg = engine.step(ov.bots, waiting=ov.waiting, running=ov.running, limit=ov.limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Platform alert check failed error=%s", type(exc).__name__)
            continue
        if msg is None:
            continue
        delivered = 0
        for admin_id in admins:
            try:
                await send(admin_id, msg)
                delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Platform alert not delivered admin_id=%s error=%s",
                               admin_id, type(exc).__name__)
        if admins and not delivered:
            engine.undo_last()
            logger.warning("Platform alert reached no admin; it will be retried next check")
