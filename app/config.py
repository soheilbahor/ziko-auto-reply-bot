"""Application settings loaded from environment variables.

Environment variables:
    BOT_TOKEN      Telegram bot token from BotFather (required).
    ADMIN_IDS      Numeric Telegram user IDs allowed to manage the bot,
                   separated by commas, semicolons or whitespace (required).
    DATABASE_PATH  SQLite file path (default: data/ziko.sqlite3).
    LOG_LEVEL      CRITICAL, ERROR, WARNING, INFO or DEBUG (default: INFO).

Error messages never include the token value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_PATH = Path("data/ziko.sqlite3")
DEFAULT_LOG_LEVEL = "INFO"

_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_TOKEN_PATTERN = re.compile(r"[0-9]+:[A-Za-z0-9_-]{20,}")
_ID_SEPARATORS = re.compile(r"[,;\s]+")


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid. Never contains secrets."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings."""

    # repr=False keeps the token out of accidental log lines and tracebacks.
    bot_token: str = field(repr=False)
    admin_ids: frozenset[int]
    database_path: Path
    log_level: str


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE env file. Returns {} if the file does not exist."""
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"Cannot read env file: {path}") from None

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _parse_admin_ids(raw: str) -> frozenset[int]:
    entries = [entry for entry in _ID_SEPARATORS.split(raw.strip()) if entry]
    if not entries:
        raise ConfigError("ADMIN_IDS must contain at least one numeric Telegram user ID")
    admin_ids: set[int] = set()
    for entry in entries:
        if not (entry.isascii() and entry.isdigit()) or int(entry) <= 0:
            raise ConfigError(f"ADMIN_IDS contains an invalid entry: {entry!r}")
        admin_ids.add(int(entry))
    return frozenset(admin_ids)


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: Path | None = None,
) -> Settings:
    """Build Settings from the process environment.

    With no arguments the real environment is used, falling back to a ``.env``
    file in the working directory for variables that are not set. Real
    environment variables always win over file values. Passing ``environ``
    (used by tests) skips the implicit ``.env`` lookup unless ``env_file`` is
    given explicitly.
    """
    values: dict[str, str] = {}
    if env_file is not None or environ is None:
        values.update(_read_env_file(env_file if env_file is not None else Path(".env")))
    values.update(os.environ if environ is None else environ)

    token = values.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is not set")
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ConfigError("BOT_TOKEN has an invalid format")

    admin_ids = _parse_admin_ids(values.get("ADMIN_IDS", ""))

    database_path = Path(values.get("DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH).expanduser()

    log_level = (values.get("LOG_LEVEL", "").strip() or DEFAULT_LOG_LEVEL).upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of: {', '.join(sorted(_VALID_LOG_LEVELS))}")

    return Settings(
        bot_token=token,
        admin_ids=admin_ids,
        database_path=database_path,
        log_level=log_level,
    )
