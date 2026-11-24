"""Admin access control, FSM validation and static security checks (Module 4).

Part 1 drives the real admin router through aiogram with a recording session
(no network). Callback data is *discovered* from the keyboards the panel
renders, so these tests do not hardcode production callback formats.

Part 2 are static / environment checks over the project tree.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    ADMIN_ID, BOT_TOKEN, OWNER_ID, PROJECT_ROOT, STRANGER_ID, import_area, mismatch,
)

LONG_TEXT = "ا" * 10_000          # far beyond Telegram's 4096-char message limit
EDIT_HINTS = ("edit", "text", "reply", "ویرایش")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
async def open_panel(admin: Any, user_id: int) -> list[str]:
    for command in ("/start", "/admin", "/panel", "/menu"):
        mark = admin.mark()
        await admin.send_text(user_id, command)
        found = admin.callback_data_in(admin.calls_since(mark))
        if found:
            return found
    return []


async def explore_as_admin(admin: Any, max_presses: int = 60) -> tuple[set[str], bool]:
    """Press every reachable button as the admin. Returns (callbacks, activated?)."""
    queue = await open_panel(admin, ADMIN_ID)
    seen: set[str] = set()
    activated = False
    presses = 0
    while queue and presses < max_presses:
        data = queue.pop(0)
        if data in seen:
            continue
        seen.add(data)
        presses += 1
        mark = admin.mark()
        await admin.press(ADMIN_ID, data)
        queue.extend(admin.callback_data_in(admin.calls_since(mark)))
        state = await admin.db.get_app_state()
        activated = activated or (state.enabled and state.active_status_key is not None)
    return seen, activated


async def enter_edit_flow(admin: Any, user_id: int, discovered: set[str]) -> bool:
    """Press edit-like buttons until the bot asks for text; True if one worked."""
    for data in sorted(discovered):
        if not any(hint in data.lower() for hint in EDIT_HINTS):
            continue
        mark = admin.mark()
        await admin.press(user_id, data)
        if admin.calls_since(mark):
            return True
    return False


# ----------------------------------------------------------------------------
# Part 1 — admin panel
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_authorized_admin_can_open_panel_and_activate_status(admin: Any) -> None:
    callbacks, activated = await explore_as_admin(admin)
    assert callbacks, "admin /start should render an inline control panel"
    assert activated, "admin should be able to activate a status through the panel"


@pytest.mark.asyncio
@pytest.mark.parametrize("intruder", [STRANGER_ID, OWNER_ID])
async def test_unauthorized_user_gets_no_control_panel(admin: Any, intruder: int) -> None:
    for command in ("/start", "/admin", "/panel", "/menu", "/status"):
        mark = admin.mark()
        await admin.send_text(intruder, command)
        assert admin.callback_data_in(admin.calls_since(mark)) == [], (
            f"{command} exposed inline controls to non-admin {intruder}")


@pytest.mark.asyncio
async def test_unauthorized_callbacks_are_rejected_and_change_nothing(admin: Any) -> None:
    discovered, _ = await explore_as_admin(admin)
    assert discovered
    # Reset to a known baseline, then let an intruder press everything.
    await admin.db.disable_auto_reply()
    before = await admin.snapshot()
    for data in sorted(discovered):
        await admin.press(STRANGER_ID, data)
    assert await admin.snapshot() == before


@pytest.mark.asyncio
async def test_unauthorized_callback_cannot_activate_status(admin: Any) -> None:
    statuses = await admin.db.list_statuses()
    discovered, _ = await explore_as_admin(admin)
    await admin.db.disable_auto_reply()
    for data in sorted(discovered):
        await admin.press(STRANGER_ID, data)
    state = await admin.db.get_app_state()
    assert state.enabled is False
    # Also try raw, plausible forged payloads (formats are unknown, so be broad).
    for status in statuses:
        for template in ("status:{k}", "activate:{k}", "set_status:{k}", "st:{k}", "{k}"):
            await admin.press(STRANGER_ID, template.format(k=status.key))
    state = await admin.db.get_app_state()
    assert state.enabled is False


@pytest.mark.asyncio
async def test_unauthorized_text_never_edits_reply_texts(admin: Any) -> None:
    before = await admin.snapshot()
    for text in ("/set_text hacked", "hacked reply text", "/edit hacked"):
        await admin.send_text(STRANGER_ID, text)
    assert (await admin.snapshot())["texts"] == before["texts"]


@pytest.mark.asyncio
async def test_authorized_admin_can_edit_text_with_valid_input(admin: Any) -> None:
    discovered, _ = await explore_as_admin(admin)
    if not await enter_edit_flow(admin, ADMIN_ID, discovered):
        mismatch("could not reach a text-edit flow by discovering callbacks "
                 f"containing {EDIT_HINTS}; provide ZIKO_ADMIN_FACTORY/panel details")
    before = await admin.snapshot()
    await admin.send_text(ADMIN_ID, "متن جدید و معتبر برای تست")
    after = await admin.snapshot()
    assert after["texts"] != before["texts"], "valid text should have been saved"
    assert "متن جدید و معتبر برای تست" in after["texts"].values()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_text", ["", "   ", "\n\t \n", LONG_TEXT])
async def test_empty_or_excessively_long_text_is_rejected(admin: Any, bad_text: str) -> None:
    discovered, _ = await explore_as_admin(admin)
    if not await enter_edit_flow(admin, ADMIN_ID, discovered):
        mismatch("could not reach a text-edit flow (see test above)")
    before = await admin.snapshot()
    await admin.send_text(ADMIN_ID, bad_text)
    assert (await admin.snapshot())["texts"] == before["texts"]


@pytest.mark.asyncio
async def test_non_admin_cannot_complete_edit_flow_via_forged_fsm_state(admin: Any) -> None:
    """Identity must be re-validated on every FSM step, not only on entry."""
    from aiogram.fsm.storage.base import StorageKey

    discovered, _ = await explore_as_admin(admin)
    if not await enter_edit_flow(admin, ADMIN_ID, discovered):
        mismatch("could not reach a text-edit flow (see test above)")
    storage = admin.dispatcher.fsm.storage
    admin_key = StorageKey(bot_id=admin.bot.id, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    state_name = await storage.get_state(admin_key)
    if state_name is None:
        mismatch("edit flow does not use FSM state; cannot forge a mid-flow state")
    data = await storage.get_data(admin_key)

    intruder_key = StorageKey(bot_id=admin.bot.id, chat_id=STRANGER_ID, user_id=STRANGER_ID)
    await storage.set_state(intruder_key, state_name)
    await storage.set_data(intruder_key, data)

    before = await admin.snapshot()
    await admin.send_text(STRANGER_ID, "متن مهاجم")
    assert (await admin.snapshot())["texts"] == before["texts"]


@pytest.mark.asyncio
async def test_non_admin_pressing_edit_button_does_not_enter_fsm(admin: Any) -> None:
    from aiogram.fsm.storage.base import StorageKey

    discovered, _ = await explore_as_admin(admin)
    storage = admin.dispatcher.fsm.storage
    key = StorageKey(bot_id=admin.bot.id, chat_id=STRANGER_ID, user_id=STRANGER_ID)
    for data in sorted(discovered):
        await admin.press(STRANGER_ID, data)
    assert await storage.get_state(key) is None


# ----------------------------------------------------------------------------
# Part 2 — static / environment security checks
# ----------------------------------------------------------------------------
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "tests",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"}


def production_python_files() -> list[Path]:
    files = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        files.append(path)
    return files


def require_production_sources() -> list[Path]:
    files = production_python_files()
    if not files:
        mismatch(f"no production Python files found under {PROJECT_ROOT}")
    return files


def test_env_file_is_excluded_from_git() -> None:
    gitignore = PROJECT_ROOT / ".gitignore"
    if not gitignore.exists():
        pytest.fail(".gitignore is missing; .env would be committable")
    if (PROJECT_ROOT / ".git").exists() and shutil.which("git"):
        for candidate in (".env", ".env.production", ".env.local"):
            result = subprocess.run(["git", "check-ignore", "-q", candidate],
                                    cwd=PROJECT_ROOT, capture_output=True)
            assert result.returncode == 0, f"git does not ignore {candidate}"
    else:
        lines = {ln.strip() for ln in gitignore.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")}
        assert lines & {".env", ".env*", "*.env", ".env.*"}, ".gitignore must ignore .env"


def test_sqlite_files_are_excluded_from_git() -> None:
    gitignore = PROJECT_ROOT / ".gitignore"
    if not gitignore.exists():
        pytest.fail(".gitignore is missing")
    text = gitignore.read_text()
    assert re.search(r"(?m)^\s*\*\.(db|sqlite3?)\b|^\s*data/", text), (
        ".gitignore should exclude SQLite database files (*.db / *.sqlite* / data/)")


def test_no_real_secret_committed_in_env_example() -> None:
    example = PROJECT_ROOT / ".env.example"
    if not example.exists():
        mismatch(".env.example not found")
    content = example.read_text()
    assert not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b", content), (
        ".env.example appears to contain a real-looking bot token")


FORBIDDEN_MODULES = {"telethon", "pyrogram", "hydrogram", "kurigram", "pyrogrammer",
                     "tdlib", "python_telegram_bot_userbot"}
FORBIDDEN_TOKENS = ("api_hash", "stringsession", "send_code_request", "sign_in(",
                    "phone_code_hash", "session_string")


def test_no_mtproto_or_userbot_imports() -> None:
    offenders = []
    for path in require_production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0].lower() in FORBIDDEN_MODULES:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}: import {name}")
    assert not offenders, offenders


def test_no_personal_account_login_material_in_source() -> None:
    offenders = []
    for path in require_production_sources():
        lowered = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            if token in lowered:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {token}")
    assert not offenders, offenders


def test_dependency_files_do_not_list_mtproto_clients() -> None:
    checked = 0
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml"):
        path = PROJECT_ROOT / name
        if path.exists():
            checked += 1
            text = path.read_text().lower()
            for module in FORBIDDEN_MODULES | {"tgcrypto"}:
                assert not re.search(rf"(?m)^\s*[\"']?{module}\b", text), f"{name} lists {module}"
    if not checked:
        mismatch("no dependency files found")


INBOUND_PORT_PATTERNS = (
    r"\bstart_webhook\b", r"\bset_webhook\b", r"\bSimpleRequestHandler\b",
    r"\bweb\.run_app\b", r"\bTCPSite\b", r"\bweb\.Application\b",
    r"\buvicorn\b", r"\bfastapi\b", r"\bflask\b", r"\bhttp\.server\b",
    r"from\s+aiohttp\s+import\s+web\b", r"\.listen\(", r"\.bind\(\s*\(",
    r"\bstart_server\(",
)


def test_no_inbound_web_server_or_webhook() -> None:
    offenders = []
    for path in require_production_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in INBOUND_PORT_PATTERNS:
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: /{pattern}/")
    assert not offenders, offenders


def test_polling_is_the_update_mechanism() -> None:
    sources = "\n".join(p.read_text(encoding="utf-8") for p in require_production_sources())
    assert "start_polling" in sources or "getUpdates" in sources, (
        "expected long polling (start_polling/getUpdates)")


def test_business_updates_are_requested_from_telegram() -> None:
    """allowed_updates must include business updates or Telegram will not send them."""
    sources = "\n".join(p.read_text(encoding="utf-8") for p in require_production_sources())
    for update_type in ("business_connection", "business_message"):
        assert update_type in sources, f"'{update_type}' is never referenced"


LEGACY_DELIVERY_PATTERNS = (
    r"\bsent\s*=\s*(True|False)\b",           # complete_reply(..., sent=True/False)
    r"complete_reply\([^)]*,\s*(True|False)\s*[,)]",  # positional legacy bool
)


def test_production_code_does_not_use_legacy_sent_flag_api() -> None:
    offenders = []
    for path in require_production_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in LEGACY_DELIVERY_PATTERNS:
            for match in re.finditer(pattern, text, flags=re.S):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {match.group(0)[:60]!r}")
    assert not offenders, offenders


def test_every_complete_reply_call_in_production_passes_claim_token_and_outcome() -> None:
    offenders = []
    for path in require_production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"complete_reply", "release_reply"}):
                names = {kw.arg for kw in node.keywords}
                positional = len(node.args)
                has_token = "claim_token" in names or positional >= 4
                has_outcome = node.func.attr == "release_reply" or "outcome" in names or positional >= 5
                if not (has_token and has_outcome):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} "
                                     f"{node.func.attr}")
    assert not offenders, offenders


def test_settings_repr_does_not_leak_bot_token() -> None:
    module = import_area("config")
    if module is None or not hasattr(module, "load_settings"):
        mismatch("config module / load_settings not found")
    settings = _load_settings_from_env(module)
    assert BOT_TOKEN not in repr(settings)
    assert BOT_TOKEN not in str(settings)


def test_settings_require_token_and_parse_admin_ids() -> None:
    module = import_area("config")
    if module is None or not hasattr(module, "load_settings"):
        mismatch("config module / load_settings not found")
    settings = _load_settings_from_env(module)
    assert isinstance(settings.admin_ids, frozenset)
    assert settings.admin_ids == frozenset({ADMIN_ID, 2002})
    assert settings.bot_token == BOT_TOKEN


def test_missing_token_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = import_area("config")
    if module is None or not hasattr(module, "load_settings"):
        mismatch("config module / load_settings not found")
    monkeypatch.chdir(tmp_path)  # ensure no stray .env is picked up
    for var in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN_ID))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    with pytest.raises(Exception):
        module.load_settings()


def _load_settings_from_env(module: Any) -> Any:
    mp = pytest.MonkeyPatch()
    try:
        import tempfile

        workdir = tempfile.mkdtemp()
        mp.chdir(workdir)  # avoid reading a developer's real .env
        mp.setenv("BOT_TOKEN", BOT_TOKEN)
        mp.setenv("ADMIN_IDS", f"{ADMIN_ID},2002")
        mp.setenv("DATABASE_PATH", str(Path(workdir) / "ziko.db"))
        mp.setenv("LOG_LEVEL", "INFO")
        return module.load_settings()
    finally:
        mp.undo()
