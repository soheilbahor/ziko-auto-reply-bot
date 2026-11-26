"""Reply rotation and online/offline availability: pure selection logic.

Storage (app/storage.py) keeps the state; this module only decides, so it is fully
testable without a database or Telegram.

Rotation
  A status may hold 2..MAX_VARIANTS text variants. When a reply is sent, ONE variant is
  chosen after the delivery has been claimed, so at-most-once delivery is unchanged.
  * random   - uniform choice (secrets), independent per reply
  * sequence - round robin; the cursor advances atomically in the same transaction

Availability
  The owner links an "online" and an "offline" status. Switching activates the other
  status, which starts a new session: every contact receives the new message once.
"""
from __future__ import annotations

import secrets
from collections.abc import Callable

MODES = ("random", "sequence")
MAX_VARIANTS = 30
STATES = ("online", "offline")
MODE_LABELS = {"random": "تصادفی", "sequence": "به ترتیب"}


def choose(count: int, mode: str, cursor: int,
           randbelow: Callable[[int], int] = secrets.randbelow) -> tuple[int, int]:
    """Return (index of the variant to send, new cursor). ``count`` must be >= 1."""
    if count < 1:
        raise ValueError("no variants")
    if mode == "sequence":
        index = cursor % count
        return index, (cursor + 1) % (count * 1_000_000)  # bounded, keeps the order
    if mode == "random":
        return randbelow(count), cursor
    raise ValueError(f"unknown rotation mode: {mode!r}")


def other(state: str) -> str:
    return "offline" if state == "online" else "online"
