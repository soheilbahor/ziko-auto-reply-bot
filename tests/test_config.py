"""Unit tests for app.config (uses explicit mappings; never touches the real environment)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import ConfigError, Settings, load_settings

TOKEN = "123456789:" + "A" * 35


def env(**overrides: str) -> dict[str, str]:
    base = {"BOT_TOKEN": TOKEN, "ADMIN_IDS": "1001"}
    base.update(overrides)
    return base


class LoadSettingsTests(unittest.TestCase):
    def test_minimal_valid_environment_uses_defaults(self) -> None:
        settings = load_settings(env())
        self.assertIsInstance(settings, Settings)
        self.assertEqual(settings.bot_token, TOKEN)
        self.assertEqual(settings.admin_ids, frozenset({1001}))
        self.assertEqual(settings.database_path, Path("data/ziko.sqlite3"))
        self.assertEqual(settings.log_level, "INFO")

    def test_admin_ids_accept_mixed_separators_and_deduplicate(self) -> None:
        settings = load_settings(env(ADMIN_IDS="1001, 2002;3003  1001"))
        self.assertEqual(settings.admin_ids, frozenset({1001, 2002, 3003}))
        self.assertIsInstance(settings.admin_ids, frozenset)

    def test_custom_path_and_log_level(self) -> None:
        settings = load_settings(env(DATABASE_PATH="/opt/ziko-assistant/data/ziko.db", LOG_LEVEL="debug"))
        self.assertEqual(settings.database_path, Path("/opt/ziko-assistant/data/ziko.db"))
        self.assertEqual(settings.log_level, "DEBUG")

    def test_missing_or_malformed_token(self) -> None:
        for token in (None, "", "   ", "not-a-token", "123:short"):
            with self.subTest(token=token):
                values = env()
                if token is None:
                    del values["BOT_TOKEN"]
                else:
                    values["BOT_TOKEN"] = token
                with self.assertRaises(ConfigError) as ctx:
                    load_settings(values)
                self.assertNotIn(token or "<none>", str(ctx.exception).replace("BOT_TOKEN", ""))

    def test_invalid_admin_ids(self) -> None:
        for raw in ("", "   ", "abc", "-5", "0", "12.5", "1001,abc"):
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    load_settings(env(ADMIN_IDS=raw))

    def test_missing_admin_ids(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings({"BOT_TOKEN": TOKEN})

    def test_invalid_log_level(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(env(LOG_LEVEL="LOUD"))

    def test_token_is_not_in_repr(self) -> None:
        settings = load_settings(env())
        self.assertNotIn(TOKEN, repr(settings))
        self.assertNotIn("A" * 35, str(settings))

    def test_env_file_is_fallback_and_real_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# comment\n"
                f"BOT_TOKEN='{TOKEN}'\n"
                'ADMIN_IDS="1001"\n'
                "export LOG_LEVEL=WARNING\n",
                encoding="utf-8",
            )
            settings = load_settings({"LOG_LEVEL": "ERROR"}, env_file=env_file)
            self.assertEqual(settings.bot_token, TOKEN)
            self.assertEqual(settings.admin_ids, frozenset({1001}))
            self.assertEqual(settings.log_level, "ERROR")

    def test_missing_env_file_is_ignored(self) -> None:
        settings = load_settings(env(), env_file=Path("/nonexistent/.env"))
        self.assertEqual(settings.admin_ids, frozenset({1001}))


if __name__ == "__main__":
    unittest.main()
