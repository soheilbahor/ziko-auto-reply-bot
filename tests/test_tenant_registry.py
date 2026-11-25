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


@pytest.mark.asyncio
async def test_platform_owner_can_register_multiple_bots(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.sqlite3")
    await registry.initialize()
    token_one = "123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
    token_two = "223456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
    try:
        await registry.add_tenant(user_id=1001, bot_id=2001, username="first_bot", token=token_one)
        with pytest.raises(UserAlreadyHasBot):
            await registry.add_tenant(user_id=1001, bot_id=2002, username="second_bot", token=token_two)
        second = await registry.add_tenant(
            user_id=1001, bot_id=2002, username="second_bot", token=token_two, allow_multiple=True
        )
        assert second.bot_id == 2002
        owned = await registry.list_tenants(user_id=1001)
        assert {tenant.bot_id for tenant in owned} == {2001, 2002}
        assert await registry.set_tenant_enabled(2002, False)
        disabled = await registry.get_tenant_by_bot_id(2002)
        assert disabled is not None and not disabled.enabled
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_registry_migrates_original_user_primary_key_schema(tmp_path):
    import sqlite3
    path = tmp_path / "tenants.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE tenants (
        user_id INTEGER PRIMARY KEY CHECK(user_id > 0), bot_id INTEGER NOT NULL UNIQUE CHECK(bot_id > 0),
        username TEXT NOT NULL, encrypted_token TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), created_at TEXT NOT NULL)""")
    connection.execute(
        "INSERT INTO tenants(user_id,bot_id,username,encrypted_token,enabled,created_at) "
        "VALUES(1001,2001,'legacy_bot','encrypted-placeholder',1,'2026-01-01T00:00:00Z')"
    )
    connection.commit()
    connection.close()
    registry = TenantRegistry(path)
    await registry.initialize()
    try:
        legacy = await registry.get_tenant_by_bot_id(2001)
        assert legacy is not None and legacy.user_id == 1001
        await registry.add_tenant(
            user_id=1001, bot_id=2002, username="new_bot",
            token="223456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN", allow_multiple=True,
        )
        assert len(await registry.list_tenants(user_id=1001)) == 2
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_managed_bot_token_update_is_encrypted_and_owner_bound(tmp_path):

    registry = TenantRegistry(tmp_path / "tenants.sqlite3")
    await registry.initialize()
    old_token = "123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
    new_token = "123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMO"
    try:
        await registry.add_tenant(
            user_id=1001, bot_id=2001, username="shop_bot", token=old_token
        )
        updated = await registry.update_tenant_credentials(
            user_id=1001, bot_id=2001, username="shop_bot_new", token=new_token
        )
        assert updated is not None
        assert updated.username == "shop_bot_new"
        assert updated.bot_token == new_token
        assert new_token not in updated.encrypted_token
        assert old_token not in updated.encrypted_token

        # A managed-bot update must not silently transfer ownership.
        assert await registry.update_tenant_credentials(
            user_id=9999, bot_id=2001, username="stolen", token=old_token
        ) is None
        still_owned = await registry.get_tenant(1001, reveal_token=True)
        assert still_owned is not None
        assert still_owned.user_id == 1001
        assert still_owned.bot_token == new_token
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_global_tenant_capacity_is_enforced(tmp_path):
    from app.tenant_registry import TenantLimitReached

    registry = TenantRegistry(tmp_path / "tenants.sqlite3", max_total=1)
    await registry.initialize()
    try:
        await registry.add_tenant(
            user_id=1001, bot_id=2001, username="first_bot",
            token="123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
        )
        with pytest.raises(TenantLimitReached):
            await registry.add_tenant(
                user_id=1002, bot_id=2002, username="second_bot",
                token="223456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
            )
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_per_user_hourly_registration_limit_is_enforced(tmp_path):
    from app.tenant_registry import TenantLimitReached

    registry = TenantRegistry(
        tmp_path / "tenants.sqlite3", max_total=10, per_user_per_hour=1
    )
    await registry.initialize()
    try:
        await registry.add_tenant(
            user_id=1001, bot_id=2001, username="first_bot",
            token="123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
            allow_multiple=True,
        )
        with pytest.raises(TenantLimitReached):
            await registry.add_tenant(
                user_id=1001, bot_id=2002, username="second_bot",
                token="223456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
                allow_multiple=True,
            )
    finally:
        await registry.close()
