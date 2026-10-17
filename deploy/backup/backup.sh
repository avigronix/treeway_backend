#!/usr/bin/env bash
# Daily encrypted backup of the Treeway database and uploads, sent off the server, 30 days kept.
# cron (user treeway):  30 1 * * *  /home/treeway/app/deploy/backup/backup.sh >> /home/treeway/logs/backup.log 2>&1
#
# Needs /home/treeway/backups/backup.env (chmod 600):
#   BACKUP_AGE_RECIPIENT=age1....        public key (the private key stays OFF this server, with Vinay)
#   RCLONE_REMOTE=r2:treeway-backups     an rclone remote (Cloudflare R2 or Backblaze B2), set up with `rclone config`
#   KEEP_DAYS=30
# The database URL is read from the app's config/.env.
set -euo pipefail

APP=/home/treeway/app
DIR=/home/treeway/backups
source "$DIR/backup.env"
DATABASE_URL=$(grep -E '^DATABASE_URL=' "$APP/config/.env" | cut -d= -f2-)
# Back up straight from Postgres (port 5432), not through PgBouncer.
DATABASE_URL=${DATABASE_URL/:6432\//:5432\/}
TS=$(date -u +%Y%m%dT%H%MZ)
WORK=$(mktemp -d "$DIR/.work.XXXX")
trap 'rm -rf "$WORK"' EXIT

fail() {
  echo "$(date -u +%FT%TZ) BACKUP FAILED: $1"
  "$APP/venv/bin/python" "$APP/deploy/monitor/notify.py" "Backup FAILED" "$1" || true
  exit 1
}

# 1. Database: custom format (compressed, restorable table by table), then encrypted.
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  | age -r "$BACKUP_AGE_RECIPIENT" -o "$WORK/treeway-db-$TS.dump.age" || fail "pg_dump"
# 2. Uploads (receipts, health files, product photos ...).
tar -C "$APP" -czf - uploads | age -r "$BACKUP_AGE_RECIPIENT" -o "$WORK/treeway-uploads-$TS.tar.gz.age" || fail "uploads tar"
# 3. Checksums, so the restore can tell a broken download from a good one.
(cd "$WORK" && sha256sum treeway-*-"$TS".* > "treeway-$TS.sha256")

# Refuse to send a suspiciously small database dump (an empty dump would quietly replace good ones).
size=$(stat -c %s "$WORK/treeway-db-$TS.dump.age")
[ "$size" -gt 100000 ] || fail "database dump is only $size bytes"

# 4. Off the server, then drop copies older than KEEP_DAYS there.
rclone copy "$WORK" "$RCLONE_REMOTE/daily/" --include "treeway-*" --retries 5 || fail "upload to $RCLONE_REMOTE"
rclone delete "$RCLONE_REMOTE/daily/" --min-age "${KEEP_DAYS:-30}d" || fail "old backup cleanup"

# 5. Keep the last 3 locally for a quick restore.
mv "$WORK"/treeway-* "$DIR/"
ls -1t "$DIR"/treeway-db-*.dump.age | tail -n +4 | while read -r f; do
  base=${f##*/treeway-db-}; ts=${base%.dump.age}
  rm -f "$DIR"/treeway-*-"$ts".* "$DIR/treeway-$ts.sha256"
done

date -u +%FT%TZ > "$DIR/last_ok"
echo "$(date -u +%FT%TZ) backup ok: $TS ($(numfmt --to=iec "$size") database)"
