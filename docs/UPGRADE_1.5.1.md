# Upgrading to 1.5.1

## Why this release needs one manual step

`ziko update` always runs the updater that is **already installed**. The updater in 1.5.0
and older has a permissions bug: under `umask 077` it unpacks the release readable by root
only, so the service user cannot run the staged `backup.sh` and, after deploy, the service
cannot read its own code. The update then rolls back automatically and nothing is lost,
but it never completes.

1.5.1 fixes the updater. For this one update, run the **1.5.1 updater** (downloaded and
verified first) against your installation. From 1.5.1 on, `ziko update` works normally.

## Steps (as root, on the server)

```bash
cd /
export ZIKO_AGE_RECIPIENT='age1...your public backup key...'
# (if unsure: age-keygen -y /etc/ziko-assistant/backup-age-identity)

rm -rf /root/ziko-1.5.1 && mkdir -p /root/ziko-1.5.1 && cd /root/ziko-1.5.1
gh release download v1.5.1 --repo ziko-freedom/ziko-auto-reply-bot \
  --pattern 'ziko-assistant-v1.5.1.tar.gz' --pattern SHA256SUMS
sha256sum -c SHA256SUMS                                   # must print: OK
gh attestation verify ziko-assistant-v1.5.1.tar.gz --repo ziko-freedom/ziko-auto-reply-bot
tar -xzf ziko-assistant-v1.5.1.tar.gz ziko-assistant-v1.5.1/scripts/update.sh

cd /
ZIKO_ROOT=/opt/ziko-assistant bash /root/ziko-1.5.1/ziko-assistant-v1.5.1/scripts/update.sh \
  --version v1.5.1 2>&1 | tee /root/ziko-update-1.5.1.log
```

The updater downloads the release again and verifies the checksum and the GitHub
attestation itself. It makes an encrypted platform backup and a code snapshot, deploys,
watches the service and runs the health check. If any stage fails it restores the
previous version. The last line must contain `UPDATE_COMPLETE`.

## Check

```bash
cat /opt/ziko-assistant/VERSION                      # 1.5.1
sudo -u ziko cat /opt/ziko-assistant/VERSION         # 1.5.1 (the service user can read it)
systemctl is-active ziko; systemctl show -p NRestarts --value ziko
sudo ziko update verify --record
sudo ziko                                            # the new home screen
rm -rf /root/ziko-1.5.1
```

Then send `/start` to a child bot. It must answer exactly once.

## Rollback

```bash
sudo ziko update history
sudo ziko update rollback          # code only; databases are kept
```
