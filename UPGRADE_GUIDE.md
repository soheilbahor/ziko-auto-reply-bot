# ZIKO 1.0.8 upgrade guide

## Before publishing

1. Upload the complete contents of this source archive to the canonical GitHub repository on `main`.
2. Wait for the GitHub Actions CI workflow to finish successfully.
3. Create and push the annotated tag `v1.0.8`. The Release workflow verifies that `app.__version__`, `pyproject.toml`, and `VERSION` agree, then publishes the ZIP, tar.gz, and SHA256SUMS assets.
4. Do not deploy from an unverified branch archive. Use the published release assets and checksum.

## Before updating an existing server

The first upgrade from the older 1.0.6 updater should have a full-platform backup before applying the release. After the updated `main` branch is available, run the following on Ubuntu 24.04:

```bash
sudo curl -fsSL \
  https://raw.githubusercontent.com/ziko-freedom/ziko-auto-reply-bot/main/scripts/backup.sh \
  -o /tmp/ziko-platform-backup.sh
sudo bash /tmp/ziko-platform-backup.sh
```

The backup creates a regular main-database backup and a full-platform `.tar.gz` archive with a `.sha256` sidecar. Keep both files private; the platform archive contains the encrypted tenant registry and the key required to decrypt child-bot tokens.

Then update using the stable channel:

```bash
sudo ziko update --channel stable
sudo ziko doctor
sudo ziko bots list
```

Do not run an update if the pre-update backup did not complete successfully. Do not delete `/var/lib/ziko-assistant`, `/var/backups/ziko-assistant`, or `/etc/ziko-assistant` during the upgrade.

## Multi-bot setup

1. Create each additional Telegram bot with `@BotFather`.
2. In the master ZIKO bot, send `/create_bot` and submit the new bot's token in the private chat.
3. The platform administrator can create multiple child bots. Regular users remain limited to one.
4. Use `/tenants` to list bots. Use `/tenant_disable BOT_ID` and `/tenant_enable BOT_ID` to stop or start one bot without deleting its data.
5. Each child bot owner connects that bot under Telegram Settings → Telegram Business → Chatbots and grants reply permission.

## Full-platform restore

For a full restore, use the platform archive and its checksum sidecar together:

```bash
sudo ziko restore /var/backups/ziko-assistant/ziko-platform-YYYYMMDDTHHMMSS-PID.tar.gz
```

The restore validates the checksum, SQLite integrity, and that the archived Fernet key can decrypt the tenant registry. It creates a pre-restore snapshot and asks for an explicit confirmation before replacing platform data. Use the exact archive filename created by your backup command.

## Important boundaries

- This release does not deploy itself. Review the GitHub Actions result and release assets before updating production.
- No production server or production database was accessed while preparing this source package.
- The multi-bot runtime remains a single-server, single-process long-polling design; it is not horizontally scalable yet.
- Bot deletion is intentionally not exposed. Disabling a bot preserves its registry entry and database.


## Managed Bots onboarding
The master bot must have Bot Management Mode enabled. Confirm with `getMe` that `can_manage_bots` is `true`. Customers use `/create_bot`, open the official Telegram managed-bot creation link, and confirm creation in Telegram. ZIKO receives the `managed_bot` update and retrieves the token through `getManagedBotToken`; the customer does not send the token. Existing bots can still be connected with `/connect_bot`.
