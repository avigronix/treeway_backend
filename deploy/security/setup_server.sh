#!/usr/bin/env bash
# One-time server hardening for Treeway. Run as root:  bash deploy/security/setup_server.sh
# Safe to run again. It STOPS (changes nothing) if the treeway user has no SSH key yet, so you never lock
# yourself out when password login is switched off.
#
# Before running: put your public key in /home/treeway/.ssh/authorized_keys (the script creates the user and
# prints what to do if the key is missing), and keep this root SSH session open until you have logged in
# again as treeway in a second window.
set -euo pipefail

APP_USER=treeway
[ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }

# ---- 1. Treeway's own Linux user (no sudo; owns only its app folder)
if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "Treeway app" "$APP_USER"
fi
install -d -m 700 -o "$APP_USER" -g "$APP_USER" /home/$APP_USER/.ssh /home/$APP_USER/backups /home/$APP_USER/logs
touch /home/$APP_USER/.ssh/authorized_keys
chown "$APP_USER:$APP_USER" /home/$APP_USER/.ssh/authorized_keys
chmod 600 /home/$APP_USER/.ssh/authorized_keys
if ! grep -qE '^(ssh-(ed25519|rsa)|ecdsa-)' /home/$APP_USER/.ssh/authorized_keys; then
  echo "STOP: add your public key to /home/$APP_USER/.ssh/authorized_keys first, then run this again."
  exit 1
fi

# ---- 2. Packages
apt-get update -qq
apt-get install -y -qq ufw fail2ban unattended-upgrades age rclone postgresql-client-16 >/dev/null || \
apt-get install -y -qq ufw fail2ban unattended-upgrades age rclone >/dev/null

# ---- 3. Firewall: only SSH, HTTP, HTTPS from outside (Postgres, Redis, the app stay on localhost)
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ---- 4. SSH: keys only, no root login, no passwords
cat > /etc/ssh/sshd_config.d/90-treeway.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 20
X11Forwarding no
EOF
# Test the new SSH config first; a broken config is never loaded (that could lock everyone out).
sshd -t || { echo "SSH config test failed; nothing reloaded"; rm -f /etc/ssh/sshd_config.d/90-treeway.conf; exit 1; }
systemctl reload ssh 2>/dev/null || systemctl reload sshd

# ---- 5. fail2ban: SSH only (web abuse: app limits, nginx limit_req, Cloudflare rule)
cp "$(dirname "$0")/fail2ban-treeway.local" /etc/fail2ban/jail.d/treeway.local
systemctl enable --now fail2ban
systemctl restart fail2ban

# ---- 6. Security updates by themselves
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "Done. In a NEW window check: ssh $APP_USER@<server>  (keep this one open until that works)."
