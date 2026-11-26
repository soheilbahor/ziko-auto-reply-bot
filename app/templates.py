"""Safe reply templates: variables, fallbacks and validation.

Rules (enforced here, tested in tests/test_reply_library.py):

* Variables are written ``{name}``. Supported: see ``VARIABLES``.
* A variable with no value is replaced by its Persian fallback (``FALLBACKS``), never
  left as ``{...}`` in a customer message.
* ``{name|fallback text}`` sets a per-template fallback.
* ``validate()`` rejects unknown variables and unbalanced braces when a template is
  SAVED, so an owner learns about a typo in the panel, not from a customer.
* ``render()`` is single-pass: a value containing braces is never expanded again,
  and any brace pattern that still survives is removed as a last line of defence.
* Values are plain text (Telegram messages are sent without parse_mode), are trimmed,
  and length-limited. The result never exceeds Telegram's 4096-character limit.

Backward compatible with v1.4 replies: ``{name}``, ``{time}``, ``{date}``, ``{owner}``.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

MAX_MESSAGE_LENGTH = 4096
MAX_VALUE_LENGTH = 64

# variable -> (English description for docs/CLI, Persian label for the Telegram panel)
VARIABLES: dict[str, tuple[str, str]] = {
    "name": ("contact's first name", "نام مخاطب"),
    "customer_name": ("contact's first name (alias of name)", "نام مشتری"),
    "time": ("current local time HH:MM", "ساعت فعلی"),
    "date": ("current local date", "تاریخ امروز"),
    "owner": ("owner name from owner settings", "نام شما"),
    "store_name": ("store name from owner settings", "نام فروشگاه"),
    "support_name": ("support / admin display name", "نام پشتیبان"),
    "order_id": ("order number written by the customer, if any", "شماره سفارش"),
}

# Persian fallbacks: what the customer sees when a value is unknown.
FALLBACKS: dict[str, str] = {
    "name": "دوست عزیز",
    "customer_name": "مشتری گرامی",
    "time": "",
    "date": "",
    "owner": "",
    "store_name": "فروشگاه ما",
    "support_name": "تیم پشتیبانی",
    "order_id": "سفارش شما",
}

_TOKEN = re.compile(r"\{([a-z_]+)(?:\|([^{}]{0,60}))?\}")
_ANY_BRACE_TOKEN = re.compile(r"\{[^{}\n]{0,80}\}")
# 4-10 digits: order numbers. 11+ digits (Iranian mobile numbers are 11) are not orders.
_ORDER_ID = re.compile(r"(?<![\w-])#?(\d{4,10})(?![\w-])")


_EMPTY = "\x00"   # marks an empty value during render; never left in the output

# Owner-facing preview: each variable is shown as a visible label, not as an invented
# person. ‹ › never appear in real messages.
PREVIEW_LABELS: dict[str, str] = {
    "name": "‹نام مخاطب›", "customer_name": "‹نام مخاطب›", "time": "‹ساعت›", "date": "‹تاریخ›",
    "owner": "‹نام شما›", "store_name": "‹نام فروشگاه›", "support_name": "‹نام پشتیبان›",
    "order_id": "‹شماره سفارش›",
}


class TemplateError(ValueError):
    """Template rejected at save time. Message is English (logs); panel shows ``persian``."""

    def __init__(self, message: str, persian: str) -> None:
        super().__init__(message)
        self.persian = persian


def variables_in(template: str) -> list[str]:
    return [m.group(1) for m in _TOKEN.finditer(template)]


def validate(template: str) -> str:
    """Return the cleaned template or raise TemplateError. Called when an owner saves."""
    text = (template or "").strip()
    if not text:
        raise TemplateError("template is empty", "متن پیام نمیتواند خالی باشد.")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise TemplateError("template too long", f"متن بیش از {MAX_MESSAGE_LENGTH} نویسه است.")
    unknown = sorted({v for v in variables_in(text) if v not in VARIABLES})
    if unknown:
        names = "، ".join("{" + v + "}" for v in unknown)
        allowed = "، ".join("{" + v + "}" for v in VARIABLES)
        raise TemplateError(f"unknown variable(s): {', '.join(unknown)}",
                            f"متغیر ناشناخته: {names}\nمتغیرهای مجاز: {allowed}")
    leftover = _TOKEN.sub("", text)
    if "{" in leftover or "}" in leftover:
        raise TemplateError("unbalanced or malformed braces",
                            "آکولاد { } ناقص یا اشتباه است. متغیرها را مثل {name} بنویسید.")
    return text


def clean_value(value: object) -> str:
    """Single-line, trimmed, length-limited plain text."""
    text = " ".join(str(value or "").split())
    return text[:MAX_VALUE_LENGTH]


def extract_order_id(message_text: str | None) -> str:
    """An order number the customer typed (4-10 digits, optional #). Never invents one."""
    if not message_text:
        return ""
    found = _ORDER_ID.search(message_text)
    return found.group(1) if found else ""


def render(template: str, context: Mapping[str, object]) -> str:
    """Fill variables. Unknown or empty values use fallbacks; no ``{...}`` ever survives."""
    values = {k: clean_value(v) for k, v in context.items()}
    if values.get("name") and not values.get("customer_name"):
        values["customer_name"] = values["name"]

    def fill(match: re.Match[str]) -> str:
        key, inline_fallback = match.group(1), match.group(2)
        value = values.get(key, "")
        if value:
            return value
        if inline_fallback is not None:
            return clean_value(inline_fallback) or _EMPTY
        return FALLBACKS.get(key, "") or _EMPTY

    text = _TOKEN.sub(fill, template)
    # An empty value leaves no stray space before punctuation: "سلام {name|}!" -> "سلام!".
    text = re.sub(r"[ \t]*" + _EMPTY + r"(?=[!،.؟?:؛,])", "", text).replace(_EMPTY, "")
    # Defence in depth: anything still shaped like a placeholder never reaches a customer.
    text = _ANY_BRACE_TOKEN.sub("", text)
    # Tidy spacing left by empty values, without touching line structure.
    text = "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_MESSAGE_LENGTH]


def preview(template: str, sample: Mapping[str, object] | None = None) -> str:
    """Owner-facing preview. Variables appear as labels such as ‹نام مخاطب›, so nobody
    mistakes an example name for the text that is sent; ``sample`` overrides labels."""
    demo: dict[str, object] = dict(PREVIEW_LABELS)
    demo.update(sample or {})
    return render(template, demo)
