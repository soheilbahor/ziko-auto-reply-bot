"""Ready-made reply library (data in reply_library.json) and per-tenant favourites.

Design: a library message is *installed* as a normal ZIKO status (title, emoji, one
text step). The proven reply engine, its at-most-once delivery, hours, intervals and
exceptions therefore apply unchanged; the library adds no second sending path.

Favourites are stored per tenant in that tenant's own database (owner_favourites), so
they follow the tenant's isolation and backups. The library file itself is read-only,
shipped with the release, validated at load time.
"""
from __future__ import annotations

import re

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app import templates

LIBRARY_PATH = Path(__file__).with_name("reply_library.json")
TONES = {"formal": "رسمی", "friendly": "دوستانه", "short": "کوتاه", "warm": "گرم", "playful": "بازیگوش"}


class LibraryError(ValueError):
    """The shipped library file is invalid (English, for logs and CI)."""


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    category_id: str
    tone: str
    text: str


@dataclass(frozen=True, slots=True)
class Category:
    id: str
    group_id: str
    title: str
    emoji: str
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class Group:
    id: str
    title: str
    emoji: str
    categories: tuple[Category, ...]


@dataclass(frozen=True, slots=True)
class Library:
    version: int
    groups: tuple[Group, ...]

    def group(self, group_id: str) -> Group | None:
        return next((g for g in self.groups if g.id == group_id), None)

    def category(self, category_id: str) -> Category | None:
        return next((c for g in self.groups for c in g.categories if c.id == category_id), None)

    def message(self, message_id: str) -> Message | None:
        cat = self.category(message_id.rsplit(".", 1)[0]) if "." in message_id else None
        return next((m for m in cat.messages if m.id == message_id), None) if cat else None

    @property
    def count(self) -> int:
        return sum(len(c.messages) for g in self.groups for c in g.categories)


def _require(value: Any, kind: type, where: str) -> Any:
    if not isinstance(value, kind):
        raise LibraryError(f"{where}: expected {kind.__name__}")
    return value


_ID = re.compile(r"[a-z0-9_.]{1,32}")


def _check_id(value: str, kind: str) -> str:
    """Ids travel in callback data: lowercase ASCII, no ':' (aiogram's separator), <=32."""
    if not _ID.fullmatch(value):
        raise LibraryError(f"{kind} id {value!r} must match [a-z0-9_.]{{1,32}}")
    return value


def parse(data: dict[str, Any]) -> Library:
    """Validate the whole library. Any problem is a release bug, caught by tests and CI."""
    seen: set[str] = set()
    groups = []
    for g in _require(data.get("groups"), list, "groups"):
        gid = _check_id(_require(g.get("id"), str, "group.id"), "group")
        cats = []
        for c in _require(g.get("categories"), list, f"{gid}.categories"):
            cid = _check_id(_require(c.get("id"), str, f"{gid}: category.id"), "category")
            if not cid.replace("_", "").isalnum() or not cid.isascii() or cid in seen:
                raise LibraryError(f"category id invalid or duplicated: {cid!r}")
            seen.add(cid)
            msgs = []
            for i, m in enumerate(_require(c.get("messages"), list, f"{cid}.messages"), 1):
                mid = _check_id(_require(m.get("id"), str, f"{cid}[{i}].id"), "message")
                if mid != f"{cid}.{i}" and mid in seen:
                    raise LibraryError(f"duplicated message id {mid!r}")
                if not mid.startswith(cid + "."):
                    raise LibraryError(f"message id {mid!r} must start with {cid!r}")
                seen.add(mid)
                tone = _require(m.get("tone"), str, f"{mid}.tone")
                if tone not in TONES:
                    raise LibraryError(f"{mid}: unknown tone {tone!r}")
                try:
                    text = templates.validate(_require(m.get("text"), str, f"{mid}.text"))
                except templates.TemplateError as exc:
                    raise LibraryError(f"{mid}: {exc}") from None
                msgs.append(Message(mid, cid, tone, text))
            if not msgs:
                raise LibraryError(f"category {cid!r} has no messages")
            title = _require(c.get("title"), str, f"{cid}.title")
            if not 1 <= len(title) <= 40:
                raise LibraryError(f"{cid}: title must be 1-40 characters (it becomes a status title)")
            cats.append(Category(cid, gid, title, _require(c.get("emoji"), str, f"{cid}.emoji"), tuple(msgs)))
        groups.append(Group(gid, _require(g.get("title"), str, f"{gid}.title"),
                            _require(g.get("emoji"), str, f"{gid}.emoji"), tuple(cats)))
    return Library(int(data.get("version", 1)), tuple(groups))


@lru_cache(maxsize=1)
def load(path: Path = LIBRARY_PATH) -> Library:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LibraryError(f"cannot read reply library {path.name}: {type(exc).__name__}") from None
    return parse(data)


def status_from_message(lib: Library, message_id: str) -> dict[str, Any]:
    """Arguments for Database.create_status() that install one library message."""
    msg = lib.message(message_id)
    if msg is None:
        raise KeyError(message_id)
    cat = lib.category(msg.category_id)
    assert cat is not None
    return {"title": cat.title, "emoji": cat.emoji[:16] or "💬", "steps": [("text", msg.text)]}


# Favourites are stored per tenant by Database.list_favourites / toggle_favourite.
MAX_FAVOURITES = 50
