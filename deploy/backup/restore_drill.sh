#!/usr/bin/env bash
# Restore drill: proves the latest off-server backup really restores. A backup that was never restored is
# not trusted. Weekly cron (user treeway):
#   0 5 * * 0  /home/treeway/app/deploy/backup/restore_drill.sh >> /home/treeway/logs/restore_drill.log 2>&1
#
# Needs, besides backup.env:
#   AGE_IDENTITY=/home/treeway/backups/age-identity.txt   the PRIVATE key (chmod 400). Keep a second copy with
#                                                          Vinay; without it no backup can ever be opened.
#   DRILL_DB=treeway_restore_check                         scratch database the treeway role may create
#                                                          (once, as postgres: ALTER ROLE treeway CREATEDB;)
# Steps: download the newest dump -> verify checksum -> decrypt -> restore into DRILL_DB -> checks -> drop.
set -euo pipefail

APP=/home/treeway/app
DIR=/home/treeway/backups
source "$DIR/backup.env"
DRILL_DB=${DRILL_DB:-treeway_restore_check}
[ "$DRILL_DB" != "treeway" ] || { echo "DRILL_DB must not be the live database"; exit 1; }
DATABASE_URL=$(grep -E '^DATABASE_URL=' "$APP/config/.env" | cut -d= -f2-)
DATABASE_URL=${DATABASE_URL/:6432\//:5432\/}
ADMIN_URL=${DATABASE_URL%/*}/postgres
DRILL_URL=${DATABASE_URL%/*}/$DRILL_DB
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"; psql -q "$ADMIN_URL" -c "DROP DATABASE IF EXISTS $DRILL_DB WITH (FORCE)" >/dev/null 2>&1 || true' EXIT

fail() {
  echo "$(date -u +%FT%TZ) RESTORE DRILL FAILED: $1"
  "$APP/venv/bin/python" "$APP/deploy/monitor/notify.py" "Restore drill FAILED" "$1" || true
  exit 1
}

latest=$(rclone lsf "$RCLONE_REMOTE/daily/" --include "treeway-db-*.dump.age" | sort | tail -1)
[ -n "$latest" ] || fail "no backup found in $RCLONE_REMOTE"
ts=${latest#treeway-db-}; ts=${ts%.dump.age}
rclone copy "$RCLONE_REMOTE/daily/" "$WORK" --include "treeway-*$ts*" || fail "download"
(cd "$WORK" && sha256sum -c "treeway-$ts.sha256") || fail "checksum mismatch for $ts"

psql -q "$ADMIN_URL" -c "DROP DATABASE IF EXISTS $DRILL_DB WITH (FORCE)" -c "CREATE DATABASE $DRILL_DB" || fail "create $DRILL_DB"
age -d -i "$AGE_IDENTITY" "$WORK/$latest" | pg_restore --no-owner --no-privileges -d "$DRILL_URL" || fail "pg_restore"
age -d -i "$AGE_IDENTITY" "$WORK/treeway-uploads-$ts.tar.gz.age" | tar -tzf - >/dev/null || fail "uploads archive unreadable"

# Checks on the restored copy: it has data, it is recent, and the money adds up.
read -r members ledger newest_hours bad_wallets < <(psql -At -F ' ' "$DRILL_URL" -c "
  SELECT (SELECT count(*) FROM users),
         (SELECT count(*) FROM wallet_ledger),
         coalesce(round(extract(epoch FROM now() - (SELECT max(created_at) FROM wallet_ledger)) / 3600), 0),
         (SELECT count(*) FROM wallets w LEFT JOIN (SELECT user_id,
              sum(amount) FILTER (WHERE wallet = 'income') i, sum(amount) FILTER (WHERE wallet = 'fund') f,
              sum(amount) FILTER (WHERE wallet = 'product') p FROM wallet_ledger GROUP BY user_id) l USING (user_id)
          WHERE w.income <> coalesce(l.i, 0) OR w.fund <> coalesce(l.f, 0) OR w.product <> coalesce(l.p, 0))") \
  || fail "checks did not run"
[ "$members" -ge 1 ] || fail "restored database has no users"
[ "$bad_wallets" -eq 0 ] || fail "$bad_wallets wallets do not match their ledger in the backup"
# A backup older than ~2 days means the daily job stopped (only once there is daily activity).
[ "$ledger" -lt 1000 ] || [ "$newest_hours" -le 50 ] || fail "newest money entry in the backup is $newest_hours hours old"

msg="restore drill ok: backup $ts, $members users, $ledger ledger rows, wallets match"
echo "$(date -u +%FT%TZ) $msg"
"$APP/venv/bin/python" "$APP/deploy/monitor/notify.py" "Restore drill OK" "$msg" || true
