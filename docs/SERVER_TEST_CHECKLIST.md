# ZIKO: server test checklist (all changes since v1.4.3)

Use a **throwaway test server** (Ubuntu 24.04, snapshot taken first), a **test master
bot** and **2–3 test child bots**. Never run this on production. Expect 1.5–2 hours.

For every step, record the **output and the exit code** (`echo $?`). A step that fails
is useful: stop, send me the output, and continue with the next phase if it is independent.

Paths used below are the installer defaults:

| What | Path |
|---|---|
| Code | `/opt/ziko-assistant` |
| Config | `/etc/ziko-assistant/ziko.env` |
| Data | `/var/lib/ziko-assistant` |
| Backups | `/var/backups/ziko-assistant` |
| Service | `ziko.service` |

---

## Phase 0: Automated tests (before installing anything)

```bash
unzip ziko-assistant-v1.4.3-latest.zip && cd ziko-assistant-v1.4.3
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q -ra 2>&1 | tee pytest.txt
```

- [ ] **0.1** Pytest finishes. Expected: **0 skipped** (aiogram and aiosqlite are now installed).
  The 3 tests skipped in my sandbox must now **run and pass**:
  - `test_real_engine_keeps_at_most_once_under_concurrency` (the most important one)
  - `test_registry_loads_external_key_and_never_creates_one`
  - `test_hour_window_matches_the_reply_engine_exactly`
- [ ] **0.2** Note any failures in the **old** tests. Likely candidates, because I changed
  button layouts and texts: `test_admin_security.py`, `test_phase4_replies.py`, `test_cli_*.py`.
- [ ] **0.3** Optional lint, same as CI:
  ```bash
  .venv/bin/pip install "ruff==0.8.6" && .venv/bin/ruff check app scripts tests
  shellcheck --severity=error install.sh scripts/*.sh
  ```

**Send me:** `pytest.txt` (and the ruff/shellcheck output if you ran them).

---

## Phase 1: Install and start

- [ ] **1.1** Fresh install (asks for the test BOT_TOKEN, ADMIN_IDS, BUSINESS_OWNER_ID):
  ```bash
  sudo bash install.sh
  ```
  Expected: `ZIKO installed successfully`, plus an **IMPORTANT** line saying the token key
  is at `/etc/ziko-assistant/tenant-token.key` and is not in backups.
- [ ] **1.2** The key was created outside the data folder:
  ```bash
  sudo ls -l /etc/ziko-assistant/tenant-token.key        # expect: -rw-r----- root ziko
  sudo grep ZIKO_TENANT_KEY_FILE /etc/ziko-assistant/ziko.env
  sudo ls /var/lib/ziko-assistant/tenant-token.key        # expect: No such file
  ```
- [ ] **1.3** The service starts and stays up (this proves the aiogram telemetry hook works):
  ```bash
  sudo systemctl status ziko --no-pager
  sudo journalctl -u ziko -n 50 --no-pager
  ```
  Expected: `active (running)`, with no traceback mentioning `TelemetryRequestMiddleware`,
  `overview_cache` or `platform_alerts`. **This is the highest-risk step;** if it fails, send the journal.
- [ ] **1.4** Create 2–3 test child bots with `/create_bot` in the master bot.
- [ ] **1.5** After about 40 seconds, both status files exist and are private:
  ```bash
  sudo ls -l /var/lib/ziko-assistant/run/
  # expect telegram-status.json and overview.json, both -rw------- ziko
  ```

---

## Phase 2: CLI monitoring (read-only, safe)

| # | Command | Expected |
|---|---|---|
| 2.1 | `sudo ziko monitor cpu` | Per-core bars, load and top processes. No made-up values: anything unsupported is labelled "not measured". |
| 2.2 | `sudo ziko monitor memory` · `disk` · `network` · `processes` | Real numbers. `processes` shows the `app.main` process. |
| 2.3 | `sudo ziko monitor database` | main, registry and every tenant DB: ✔, `integrity ok`. |
| 2.4 | `sudo ziko monitor cpu --json \| python3 -m json.tool` | Valid JSON. |
| 2.5 | `sudo ziko monitor gpu; echo $?` | Usage error, exit **64**. |
| 2.6 | `sudo ziko dashboard` (Ctrl+C to quit) | Live screen: CPU/RAM, a **bots** section and health lines. |
| 2.7 | `sudo ziko health; echo $?` | One line per check, including `bots.snapshot`. Exit 0, 1 or 2 matching the worst line. |
| 2.8 | `sudo ziko health --json` | Valid JSON. |
| 2.9 | `sudo ziko monitor database` with a real backlog | Optional: `stuck` column counts pending deliveries older than 10 minutes. |

**Send me:** the output of 2.1, 2.3, 2.6 (a screenshot) and 2.7.

---

## Phase 3: Telegram diagnostics

- [ ] **3.1** `sudo ziko monitor telegram; echo $?` shows:
  - DNS, TCP and TLS checks (expect **TLSv1.3**, certificate verified), and the HTTPS round trip;
  - master bot auth and Business mode;
  - delivery mode `long polling`;
  - live telemetry with one line per bot, including p50/p95 latency;
  - Business connections per tenant.
- [ ] **3.2** `sudo ziko bots inspect <BOT_ID>`: registry, database, Business, runtime and log errors.
  Confirm **no token** appears anywhere in the output.
- [ ] **3.3** `sudo ziko bots inspect <BOT_ID> --probe`: adds `token: valid · @username`. Still no token shown.
- [ ] **3.4** `sudo ziko bots inspect 999; echo $?`: "not registered", exit **3**.
- [ ] **3.5** Stop the service, wait 70 s, then run `sudo ziko monitor telegram`.
  Expected: live telemetry is reported as **STALE**, not as current numbers. Start the service again.

---

## Phase 4: Child-bot owner panel

Connect one test child bot to a Telegram **Premium** test account
(Settings → Telegram Business → Chatbots).

| # | Action | Expected |
|---|---|---|
| 4.1 | In the child bot, `/start` | The main card has a new **آمادگی** line, e.g. ⛔ "ربات هنوز به حساب تلگرام شما وصل نشده". |
| 4.2 | Tap **🩺 چرا پاسخ ارسال نمیشود؟** | A list of checks with fixes, plus 📘 and 🔄 buttons. |
| 4.3 | Tap **📘 راهنمای اتصال**, then follow it | The steps match Telegram's real menus. Tell me if any menu name differs. |
| 4.4 | Connect **without** "Reply to messages", then `/diagnose` | ⛔ "ربات وصل است ولی اجازهٔ پاسخدادن ندارد". |
| 4.5 | Grant reply permission and choose a status, then `/diagnose` | ✅ "همهچیز آماده است". |
| 4.6 | Set an active-hours window that excludes now, then `/diagnose` | ⛔ outside the window, showing the **local time**. |
| 4.7 | Message the business account from another account | Exactly **one** auto-reply. A second message in the same session gets no reply. |
| 4.8 | 📊 آمار | Success rate, and an explanation of "نامشخص". |
| 4.9 | The child bot's `/` menu | Includes `/diagnose`, `/help` and `/logs`. |

---

## Phase 5: Master admin dashboard

| # | Action | Expected |
|---|---|---|
| 5.1 | As admin, `/platform` | Summary, polling bar and one line per bot, worst first. |
| 5.2 | Tap a bot | Detail screen: polling, Telegram latency, last success, readiness checks. |
| 5.3 | Tap **⏸ غیرفعالسازی** → confirm | Bot shows ⏸. `sudo ziko bots status` agrees within 30 s. |
| 5.4 | Tap the old confirm button again | "وضعیت از قبل همین بود", and no restart in the journal. |
| 5.5 | Enable it again | Polling resumes. |
| 5.6 | Tap 🔄 five times quickly | Each tap responds. The journal shows no error flood. |
| 5.7 | A **non-admin** account sends `/platform` | **No response at all.** |
| 5.8 | The admin `/` menu | Includes `/platform`, `/alerts`, `/tenants` and `/logs`. A non-admin's menu does **not**. |

---

## Phase 6: CLI child-bot snapshot

- [ ] **6.1** `sudo ziko bots status; echo $?`: a table matching `/platform`, with exit 0, 1 or 2.
- [ ] **6.2** `sudo ziko bots status --attention` and `--json`.
- [ ] **6.3** Stop the service, wait **3.5 minutes**, then run `sudo ziko bots status; echo $?`.
  Expected: **STALE**, exit 1. Start the service again.

---

## Phase 7: Alerts (off by default)

- [ ] **7.1** `/alerts` before enabling: "اعلانهای پلتفرم خاموش است (پیشفرض)" with instructions.
- [ ] **7.2** Turn them on with a short interval and restart:
  ```bash
  echo -e 'ZIKO_PLATFORM_ALERTS=1\nZIKO_ALERT_INTERVAL_SECONDS=15' | sudo tee -a /etc/ziko-assistant/ziko.env
  sudo systemctl restart ziko
  ```
  `/alerts` now shows the settings.
- [ ] **7.3** Capacity alert (the most reliable test): set `ZIKO_TENANT_MAX_CONCURRENT_POLLERS=1`
  with at least 2 child bots enabled, then restart. Within about 90 s: **one** "⏳ ظرفیت پولینگ پر است"
  message. Remove the setting and restart: one "✅ صف پولینگ خالی شد".
- [ ] **7.4** `/alerts mute 30m`, then `/alerts`: muted. `/alerts unmute`.
- [ ] **7.5** Remove the two lines from 7.2 and restart (back to the default).

---

## Phase 8: Security audit and beta gate

- [ ] **8.1** `sudo ziko security audit; echo $?`: expect **ok** for permissions, service user,
  hardening and the key location. Note every WARN or FAIL. A missing `ZIKO_AGE_RECIPIENT` is a known WARN.
- [ ] **8.2** Break something on purpose, then run the audit again:
  ```bash
  sudo chmod 644 /var/lib/ziko-assistant/ziko.sqlite3 && sudo ziko security audit; echo $?
  sudo chmod 600 /var/lib/ziko-assistant/ziko.sqlite3
  ```
  Expected: FAIL for `ziko.sqlite3`, exit 2.
- [ ] **8.3** The beta gate refuses unverified code:
  ```bash
  sudo bash /opt/ziko-assistant/scripts/update.sh --repo ziko-freedom/ziko-auto-reply-bot --channel beta; echo $?
  ```
  Expected: `beta is UNVERIFIED code ... --allow-unverified`, non-zero exit, **nothing changed**.
- [ ] **8.4** `sudo ziko update --channel beta`, then type anything except `UNVERIFIED`: "Cancelled".

---

## Phase 9: Keys, backup and restore (most important for data safety)

Prepare a backup key once:
```bash
sudo apt-get install -y age
sudo age-keygen -o /etc/ziko-assistant/backup-age-identity && sudo chmod 400 /etc/ziko-assistant/backup-age-identity
RCPT="$(sudo age-keygen -y /etc/ziko-assistant/backup-age-identity)"
```

- [ ] **9.1** Back up:
  ```bash
  sudo env ZIKO_AGE_RECIPIENT="$RCPT" bash /opt/ziko-assistant/scripts/backup.sh
  ```
  Expected: `Full platform backup created and verified: ...tar.gz.age`.
- [ ] **9.2** The backup must **not** contain the key:
  ```bash
  A="$(ls -t /var/backups/ziko-assistant/ziko-platform-*.tar.gz.age | head -1)"
  sudo age -d -i /etc/ziko-assistant/backup-age-identity "$A" | tar -tz
  ```
  Expected: `main.sqlite3`, `registry.sqlite3`, `tenants/...` and `manifest.json`, but **no `tenant-token.key`**.
  The manifest has a `tenant_key` fingerprint.
- [ ] **9.3** Rotate the key:
  ```bash
  sudo ziko security rotate-key; echo $?                  # service running: refused, exit 3
  sudo systemctl stop ziko && sudo ziko security rotate-key   # type ROTATE
  sudo ls -l /etc/ziko-assistant/tenant-token.key         # still -rw-r----- root ziko
  sudo systemctl start ziko && sudo ziko bots inspect <BOT_ID> --probe   # token: valid
  ```
- [ ] **9.4** Restore the backup from 9.1. It was taken with the **old** key, so restore must refuse:
  ```bash
  sudo bash /opt/ziko-assistant/scripts/restore-platform.sh "$A"
  ```
  Expected: it stops with "cannot be decrypted" before changing anything. Child bots still run.
- [ ] **9.5** Take a new backup, then restore that one (type `RESTORE PLATFORM`):
  ```bash
  sudo env ZIKO_AGE_RECIPIENT="$RCPT" bash /opt/ziko-assistant/scripts/backup.sh
  B="$(ls -t /var/backups/ziko-assistant/ziko-platform-*.tar.gz.age | head -1)"
  sudo bash /opt/ziko-assistant/scripts/restore-platform.sh "$B"
  ```
  Expected: success, the service restarts, `bots inspect --probe` is valid, and the key file is unchanged.
- [ ] **9.6** Move the key back and forth:
  ```bash
  sudo systemctl stop ziko
  sudo ziko security move-key --to-data      # type MOVE; key now in /var/lib/ziko-assistant, 0600 ziko
  sudo ziko security move-key                # type MOVE; key back in /etc, 0640 root:ziko
  sudo systemctl start ziko && sudo ziko bots inspect <BOT_ID> --probe
  ```
- [ ] **9.7** Upgrade an **old** install (snapshot the VM first, or use a second VM): install the
  original v1.4.3, create a child bot, then install this build over it. Expected: the key stays in
  `/var/lib/ziko-assistant` (no tenant data is lost), and the audit recommends `move-key`.
  Run `move-key`, and the child bot still works.
- [ ] **9.8** (Last step, VM only) `sudo ziko uninstall`, **keeping** the database. Expected: the key
  is copied to `/var/lib/ziko-assistant/tenant-token.key` before `/etc/ziko-assistant` is removed.

---

## Phase 10: Benchmark baseline

Run while the service is idle; it is CPU heavy for a few minutes.
```bash
cd /opt/ziko-assistant
sudo -u ziko .venv/bin/python scripts/benchmark.py --out /tmp/bench-base.json --latency-ms 80; echo $?
```
- [ ] **10.1** Exit 0. The `engine` and `tenants` suites ran (not "not run"), and no result is marked **INVALID**.
- [ ] **10.2** `sudo -u ziko .venv/bin/python scripts/benchmark.py --compare /tmp/bench-base.json --suite overview`
  runs and reports `same` or `improved`.

**Send me:** `/tmp/bench-base.json`. I'll use it to set real performance baselines and to
decide whether anything else needs optimising.

---

## Known limitations to watch for

1. **Alerts can be lost if sending fails.** A problem is marked as announced before the
   message is sent. If Telegram can't be reached at that moment, the admin is never told.
   Phase 7 uses the capacity alert for this reason. I'll fix this next unless you prefer otherwise.
2. **The 🔑 "token rejected" state rarely appears.** When Telegram rejects a child bot's
   token, the existing code immediately **disables** the bot and notifies admins itself. So
   `/platform` shows ⏸, and no 🔑 alert fires. The admin is still told, by the existing message.
3. **`ziko backup` from the menu** needs `ZIKO_AGE_RECIPIENT` in the environment. This is
   unchanged from v1.4.3; Phase 9 calls `backup.sh` directly for that reason.

## Results to send back

| Phase | Pass / fail | Notes |
|---|---|---|
| 0 Tests | | |
| 1 Install | | |
| 2 CLI monitoring | | |
| 3 Telegram diagnostics | | |
| 4 Owner panel | | |
| 5 Admin dashboard | | |
| 6 CLI snapshot | | |
| 7 Alerts | | |
| 8 Security / beta gate | | |
| 9 Keys / backup / restore | | |
| 10 Benchmark | | |
