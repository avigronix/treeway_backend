# Restoring Treeway from a backup

Backups: `<RCLONE_REMOTE>/daily/` (and the last 3 in `/home/treeway/backups/`), one set per night:
`treeway-db-<TS>.dump.age`, `treeway-uploads-<TS>.tar.gz.age`, `treeway-<TS>.sha256`.
They are encrypted with `age`; the private key (`age-identity.txt`) is kept by Vinay (and on the server
for the weekly drill). **Without that key no backup can be opened**: keep two copies in safe places.

The weekly `restore_drill.sh` does steps 1–3 below into a scratch database and checks the money, so these
steps are tested every week.

## 1. Stop writes

```bash
sudo -iu treeway
pm2 stop treeway-api treeway-scheduler          # members see the Cloudflare error page meanwhile
```

## 2. Get and check the backup

```bash
cd /home/treeway/backups
source backup.env
TS=20261020T0130Z                                # the backup to restore (rclone lsf $RCLONE_REMOTE/daily/)
rclone copy "$RCLONE_REMOTE/daily/" . --include "treeway-*$TS*"
sha256sum -c "treeway-$TS.sha256"                # must say OK for every file
```

## 3. Restore the database

Keep the broken database for later checks; restore into a fresh one, then swap names.

```bash
# as postgres
psql -c "ALTER DATABASE treeway RENAME TO treeway_broken_$(date +%Y%m%d)"
psql -c "CREATE DATABASE treeway OWNER treeway"
# as treeway
age -d -i age-identity.txt "treeway-db-$TS.dump.age" | pg_restore --no-owner --no-privileges -d treeway
```

## 4. Restore uploads (only if they were lost)

```bash
mv ~/app/uploads ~/app/uploads.broken
age -d -i age-identity.txt "treeway-uploads-$TS.tar.gz.age" | tar -C ~/app -xzf -
```

## 5. Start and check

```bash
cd ~/app && venv/bin/python seed.py              # applies any migration newer than the backup
pm2 start treeway-api treeway-scheduler
venv/bin/python deploy/preflight.py
```

Then in the admin panel: dashboard numbers look right, open a few members' wallets, check the newest
payment requests / withdrawals. Anything that happened **after** `$TS` (payments approved, withdrawals paid)
is not in the backup: compare with the bank statement and the admin audit log of the broken copy
(`treeway_broken_*`) and redo those by hand.
