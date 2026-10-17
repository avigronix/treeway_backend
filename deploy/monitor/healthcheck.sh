#!/usr/bin/env bash
# Every 5 minutes (cron, user treeway):  */5 * * * *  /home/treeway/app/deploy/monitor/healthcheck.sh
# Emails ALERT_EMAIL when something is wrong (once an hour per problem, so it does not spam) and when it is fixed.
# Outside check: an UptimeRobot / Better Stack monitor on https://apitreeway.avigronix.com/api/v1/health every 1 min,
# alerting by email + SMS (that one also notices if this whole server is down).
set -uo pipefail

APP=/home/treeway/app
STATE=/home/treeway/logs/health_state
mkdir -p "$STATE"
problems=()

# 1. The API answers (straight to gunicorn, so nginx / Cloudflare problems do not hide app problems).
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8100/api/v1/health)
[ "$code" = "200" ] || problems+=("api: /health answered $code")

# 2. Both pm2 processes online.
for p in treeway-api treeway-scheduler; do
  s=$(pm2 jlist 2>/dev/null | python3 -c "import sys,json; print(next((a['pm2_env']['status'] for a in json.load(sys.stdin) if a['name']=='$p'),'missing'))")
  [ "$s" = "online" ] || problems+=("pm2: $p is $s")
done

# 3. Disk: at least 15% free on / ; RAM: at least 10% available.
disk_free=$(df --output=pcent / | tail -1 | tr -dc 0-9); disk_free=$((100 - disk_free))
[ "$disk_free" -ge 15 ] || problems+=("disk: only $disk_free% free")
mem_avail=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print int(a*100/t)}' /proc/meminfo)
[ "$mem_avail" -ge 10 ] || problems+=("ram: only $mem_avail% available")

# 4. Last good backup is less than 26 hours old.
if [ -f /home/treeway/backups/last_ok ]; then
  age_h=$(( ($(date +%s) - $(date -d "$(cat /home/treeway/backups/last_ok)" +%s)) / 3600 ))
  [ "$age_h" -lt 26 ] || problems+=("backup: last good backup $age_h hours ago")
else
  problems+=("backup: no backup has finished yet")
fi

notify() { "$APP/venv/bin/python" "$APP/deploy/monitor/notify.py" "$1" "$2" >/dev/null 2>&1 || true; }

for prob in "${problems[@]}"; do
  key=$(echo "$prob" | cut -d: -f1)
  f="$STATE/$key"
  if [ ! -f "$f" ] || [ $(( $(date +%s) - $(stat -c %Y "$f") )) -ge 3600 ]; then
    notify "ALERT $key" "$prob ($(hostname), $(date))"
    touch "$f"
  fi
done
# Problems that went away: say so once.
for f in "$STATE"/*; do
  [ -e "$f" ] || continue
  key=$(basename "$f")
  if ! printf '%s\n' "${problems[@]}" | grep -q "^$key:"; then
    notify "OK $key" "$key is fine again ($(hostname), $(date))"
    rm -f "$f"
  fi
done
