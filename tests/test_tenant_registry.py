from __future__ import annotations

import pytest

from app.tenant_registry import (
    BotAlreadyRegistered,
    TenantRegistry,
    UserAlreadyHasBot,
)


@pytest.mark.asyncio
async def test_tokens_are_encrypted_and_decrypted_only_on_request(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.sqlite3")
    await registry.initialize()
    token = "123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
    try:
        created = await registry.add_tenant(
            user_id=1001, bot_id=2001, username="shop_bot", token=token
        )
        assert created.bot_token == token
        assert token not in created.encrypted_token

        metadata = await registry.get_tenant(1001)
        assert metadata is not None
        assert metadata.bot_token == ""
        assert token not in metadata.encrypted_token

        with_token = await registry.get_tenant(1001, reveal_token=True)
        assert with_token is not None
        assert with_token.bot_token == token
        assert (tmp_path / "tenant-token.key").stat().st_mode & 0o777 == 0o600
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_one_bot_per_user_and_unique_bot_identity(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.sqlite3")
    await registry.initialize()
    try:
        await registry.add_tenant(
            user_id=1001, bot_id=2001, username="first_bot",
            token="123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
        )
        with pytest.raises(UserAlreadyHasBot):
            await registry.add_tenant(
                user_id=1001, bot_id=2002, username="second_bot",
                token="123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLO",
            )
        with pytest.raises(BotAlreadyRegistered):
            await registry.add_tenant(
                user_id=1002, bot_id=2001, username="duplicate_bot",
                token="123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMO",
            )
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_master_can_enable_and_disable_tenant(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.sqlite3")
    await registry.initialize()
    try:
        await registry.add_tenant(
            user_id=1001, bot_id=2001, username="shop_bot",
            token="123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
        )
        assert await registry.set_enabled(1001, False)
        tenant = await registry.get_tenant(1001)
        assert tenant is not None and not tenant.enabled
        assert await registry.set_enabled(1001, True)
        tenant = await registry.get_tenant(1001)
        assert tenant is not None and tenant.enabled
        assert not await registry.set_enabled(9999, False)
    finally:
        await registry.close()
