#!/usr/bin/env bash
# Rebuilds the two nginx include files from Cloudflare's published IP lists, then reloads nginx.
# Run once at setup and monthly from root's cron:
#   0 4 1 * * /home/treeway/app/deploy/scripts/update_cloudflare_ips.sh >> /var/log/treeway-cf.log 2>&1
set -euo pipefail

OUT=/etc/nginx/treeway
mkdir -p "$OUT"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

curl -fsS --max-time 20 https://www.cloudflare.com/ips-v4 -o "$tmp/v4"
curl -fsS --max-time 20 https://www.cloudflare.com/ips-v6 -o "$tmp/v6"
# Refuse an empty or odd answer: a broken list would lock every visitor out (or let everyone in).
[ "$(grep -c / "$tmp/v4")" -ge 10 ] || { echo "Cloudflare v4 list looks wrong"; exit 1; }
[ "$(grep -c / "$tmp/v6")" -ge 5 ] || { echo "Cloudflare v6 list looks wrong"; exit 1; }

{
  echo "# Generated $(date -u +%F) by update_cloudflare_ips.sh"
  cat "$tmp/v4" "$tmp/v6" | sed 's/^/set_real_ip_from /; s/$/;/'
  echo "real_ip_header CF-Connecting-IP;"
} > "$tmp/realip.conf"

{
  echo "# Generated $(date -u +%F) by update_cloudflare_ips.sh"
  echo "geo \$realip_remote_addr \$from_cloudflare {"
  echo "    default 0;"
  echo "    127.0.0.1 1;   # local health checks"
  echo "    ::1 1;"
  cat "$tmp/v4" "$tmp/v6" | sed 's/^/    /; s/$/ 1;/'
  echo "}"
} > "$tmp/allow.conf"

cp "$tmp/realip.conf" "$OUT/cloudflare-realip.conf"
cp "$tmp/allow.conf" "$OUT/cloudflare-allow.conf"
nginx -t && systemctl reload nginx
echo "Cloudflare IP lists updated"
