# ZIKO release and versioning guide

## Single source of truth

The root `VERSION` file is the only manually maintained application version. Do not edit a version literal in Python, TOML, shell scripts, or workflow files. `pyproject.toml` reads its package version from `VERSION`; `app.__version__` reads the same file in a source checkout and installed package metadata after installation. CI and the release workflow use `scripts/check_version.py`.

## Publishing a release

1. Update only the root `VERSION` file and add the matching `CHANGELOG.md` entry.
2. Push the changes to `main` and wait for CI to pass.
3. Create and push the matching tag, for example `git tag -a vX.Y.Z -m "ZIKO vX.Y.Z" && git push origin vX.Y.Z`, where X.Y.Z equals the root `VERSION` file.
4. The release workflow verifies that the tag matches `VERSION`, preserves the same marker in the archive, and publishes the assets.
5. Never reuse or move a published tag. If a release tag is wrong, publish a new patch version with a new tag.

## Before updating an existing server

The first upgrade from the older 1.0.6 updater should have a full-platform backup before applying the release. After the updated `main` branch is available, run the following on Ubuntu 24.04:

```bash
sudo curl -fsSL \
  https://raw.githubusercontent.com/ziko-freedom/ziko-auto-reply-bot/main/scripts/backup.sh \
  -o /tmp/ziko-platform-backup.sh
sudo bash /tmp/ziko-platform-backup.sh
```

The backup creates an encrypted main-database `.sqlite3.age` backup and a full-platform `.tar.gz.age` archive encrypted with age and a `.sha256` sidecar. Keep both files private; the platform archive contains the encrypted tenant registry and the key required to decrypt child-bot tokens. Set `ZIKO_AGE_RECIPIENT` before backup/update and keep the matching age identity separately.

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
