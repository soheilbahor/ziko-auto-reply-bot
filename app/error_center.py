"""`ziko logs errors`: group recent WARNING/ERROR lines by cause, with an explanation and a fix.

Reads master.log and every child-bot log (logs/tenants/bot-<id>.log), only the last
``MAX_BYTES`` of each file. Logs are already token-redacted; samples are redacted again
and truncated. English only (CLI rule); owners get Persian explanations via /diagnose.

Exit codes: 0 nothing found, 1 warnings only, 2 errors or critical.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

MAX_BYTES = 4 * 1024 * 1024
LINE = re.compile(r"^(?P<ts>\S+) (?P<level>WARNING|ERROR|CRITICAL)\s+(?P<logger>\S+) cid=\S+ "
                  r"bot=(?P<bot>\S+) stage=(?P<stage>\S+) (?P<msg>.*)$")
_TOKEN = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")
_LEVEL_RANK = {"WARNING": 1, "ERROR": 2, "CRITICAL": 2}

# (code, pattern, title, fix). First match wins; order from most to least specific.
RULES: tuple[tuple[str, re.Pattern[str], str, str], ...] = tuple(
    (code, re.compile(pattern, re.I), title, fix) for code, pattern, title, fix in (
        ("token_rejected", r"Unauthorized|token was rejected|token_revoked|TokenMismatch",
         "Bot token rejected by Telegram",
         "The owner changed or revoked the token in @BotFather. They reconnect via /connect_bot "
         "in the master bot; `ziko bots inspect BOT_ID` shows the bot."),
        ("polling_conflict", r"TelegramConflictError|Conflict|\b409\b|terminated by other getUpdates",
         "Two processes poll the same bot",
         "Another server or an old process uses this token. Stop it, or revoke the token in "
         "@BotFather. ZIKO backs off 30 s and retries."),
        ("flood_wait", r"Too Many Requests|RetryAfter|retry after|Flood",
         "Telegram rate limit (flood wait)",
         "Usually harmless; replies wait. If frequent, raise the status's minimum interval."),
        ("business_mode_off", r"business_mode_off|can_connect_to_business|Secretary Mode",
         "Business Mode is off for a child bot",
         "Owner enables it: @BotFather > bot > Bot Settings > Business Mode. No API can do it."),
        ("peer_not_allowed", r"BUSINESS_PEER_USAGE_MISSING|BUSINESS_PEER_INVALID|peer.*usage",
         "Telegram refused a reply in one chat",
         "The chat is outside the bot's allowed chats in Telegram Business settings."),
        ("forbidden", r"TelegramForbiddenError|Forbidden|bot was blocked",
         "Message refused (Forbidden)",
         "Recipient blocked the bot or restricted messages. Nothing to fix on the server."),
        ("bad_request", r"TelegramBadRequest|Bad Request",
         "Telegram rejected a request (Bad Request)",
         "Often an invalid sticker/file id or an edited-away message. `ziko logs --bot BOT_ID "
         "--level WARNING` shows the context."),
        ("network", r"TelegramNetworkError|ClientConnector|Cannot connect|Timeout|TimeoutError|"
                    r"ServerDisconnected|Bad Gateway|ServerError|Network",
         "Network problem reaching Telegram",
         "Usually temporary. If it persists: `ziko monitor telegram`, check DNS/firewall to "
         "api.telegram.org:443. Uncertain deliveries are never resent (no duplicates)."),
        ("database_locked", r"database is locked|SQLITE_BUSY",
         "SQLite database busy",
         "Another process holds the database (a manual sqlite3 shell, a stuck backup). Close it; "
         "`ziko monitor database` shows locks."),
        ("disk_full", r"No space left|disk I/O error|SQLITE_FULL|database or disk is full",
         "Disk full or I/O error",
         "Free space now: `ziko monitor disk`; old backups are in /var/backups/ziko-assistant."),
        ("permission", r"Permission denied|PermissionError|readonly database|EACCES",
         "File permission problem",
         "Run `sudo ziko security audit`; it prints the exact chmod/chown to apply."),
        ("schedule", r"Reply schedule unavailable|Schedule unavailable",
         "Reply schedule could not be read",
         "Replies continue as 'always'. Check the bot's database: `ziko monitor database`."),
        ("daily_report", r"Daily report",
         "Daily owner report not delivered",
         "Retried automatically within minutes; 'blocked the bot' means the owner blocked it."),
        ("alert_delivery", r"Platform alert",
         "Admin alert not delivered",
         "Retried at the next check. Make sure each admin has started the master bot."),
        ("update", r"update (?:failed|refused|aborted)|rollback|attestation|checksum mismatch",
         "Update or rollback problem",
         "`ziko update logs` shows the journal; `ziko update verify` checks the installed files."),
        ("restart", r"restart|crashed|supervisor|watchdog|degraded",
         "Child bot restarted",
         "Occasional restarts are normal after network drops. Many: `ziko bots inspect BOT_ID --probe`."),
    )
)


@dataclass
class Group:
    code: str
    title: str
    fix: str
    level: str = "WARNING"
    count: int = 0
    bots: set[str] = field(default_factory=set)
    first: datetime | None = None
    last: datetime | None = None
    sample: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "title": self.title, "fix": self.fix, "level": self.level,
                "count": self.count, "bots": sorted(self.bots),
                "first": self.first.isoformat() if self.first else None,
                "last": self.last.isoformat() if self.last else None, "sample": self.sample}


def parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d{1,4})\s*([mhd])\s*", text or "")
    if not m:
        raise ValueError("use a duration such as 30m, 24h or 7d")
    value = int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]]
    if not 0 < value <= 90 * 86400:
        raise ValueError("duration must be between 1 minute and 90 days")
    return timedelta(seconds=value)


def classify(message: str) -> tuple[str, str, str]:
    for code, pattern, title, fix in RULES:
        if pattern.search(message):
            return code, title, fix
    kind = re.search(r"\b([A-Z][A-Za-z]+(?:Error|Exception))\b", message)
    name = kind.group(1) if kind else "other"
    return (f"other:{name}", f"Unclassified: {name}" if kind else "Unclassified warning",
            "Not a known pattern. Read the lines around it: `ziko logs --master --level WARNING`.")


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - MAX_BYTES))
        data = fh.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > MAX_BYTES else lines      # first line may be cut


def log_files(log_dir: Path) -> list[Path]:
    files = [log_dir / "master.log", *sorted((log_dir / "tenants").glob("bot-*.log"))]
    rotated = sorted(log_dir.glob("master.log.*")) + sorted((log_dir / "tenants").glob("bot-*.log.*"))
    return [p for p in files + rotated if p.is_file() and not p.is_symlink() and not p.name.endswith(".gz")]


def collect(lines: Iterable[str], *, since: datetime, bot: str | None = None) -> list[Group]:
    groups: dict[str, Group] = {}
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m["ts"])
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < since or (bot is not None and m["bot"] != bot):
            continue
        message = _TOKEN.sub("<redacted>", m["msg"])
        code, title, fix = classify(message)
        g = groups.setdefault(code, Group(code, title, fix))
        g.count += 1
        if _LEVEL_RANK[m["level"]] > _LEVEL_RANK[g.level]:
            g.level = m["level"]
        if m["bot"] not in ("-", ""):
            g.bots.add(m["bot"])
        g.first = ts if g.first is None or ts < g.first else g.first
        if g.last is None or ts >= g.last:
            g.last, g.sample = ts, message[:200]
    return sorted(groups.values(), key=lambda g: (-_LEVEL_RANK[g.level], -g.count))


def exit_code(groups: list[Group]) -> int:
    if not groups:
        return 0
    return 2 if any(_LEVEL_RANK[g.level] >= 2 for g in groups) else 1


def render(groups: list[Group], *, window: str, files: int, now: datetime) -> str:
    if not groups:
        return f"No warnings or errors in the last {window} ({files} log file(s) checked)."
    total = sum(g.count for g in groups)
    out = [f"Errors and warnings, last {window}: {total} line(s) in {len(groups)} group(s), "
           f"{files} log file(s) checked", ""]
    for i, g in enumerate(groups, 1):
        ago = _ago(now - g.last) if g.last else "?"
        bots = ", ".join(sorted(g.bots)[:5]) + (" ..." if len(g.bots) > 5 else "") if g.bots else "master"
        out += [f"{i}. [{g.level}] {g.title}  x{g.count}  (code {g.code})",
                f"   last: {ago} ago   bots: {bots}",
                f"   sample: {g.sample}",
                f"   fix: {g.fix}", ""]
    return "\n".join(out).rstrip()


def _ago(delta: timedelta) -> str:
    s = max(0, int(delta.total_seconds()))
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m" if s >= 60 else f"{s}s"


def cmd_errors(*, log_dir: Path, since: str = "24h", bot: int | None = None, as_json: bool = False) -> int:
    try:
        window = parse_since(since)
    except ValueError as exc:
        print(f"Invalid --since: {exc}")
        return 2
    now = datetime.now(timezone.utc)
    files = log_files(log_dir)
    if not files:
        print(f"No log files found in {log_dir}.")
        return 0
    lines: list[str] = []
    readable = 0
    for path in files:
        try:
            lines.extend(_tail_lines(path))
            readable += 1
        except PermissionError:
            print(f"Cannot read {path}: run with sudo.")
            return 2
        except OSError:
            continue
    groups = collect(lines, since=now - window, bot=str(bot) if bot is not None else None)
    if as_json:
        print(json.dumps({"window": since, "files": readable, "groups": [g.as_dict() for g in groups]},
                         ensure_ascii=False, indent=2))
    else:
        print(render(groups, window=since, files=readable, now=now))
    return exit_code(groups)
