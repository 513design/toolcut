#!/usr/bin/env bash
# One-shot provisioning for Ubuntu 24.04 LTS (Lightsail / EC2), multi-tenant build.
# Run as root:  sudo bash setup.sh
set -euo pipefail

# 1. System libs OpenCV-headless needs + Python tooling + Caddy
apt-get update
apt-get install -y python3-venv python3-pip libglib2.0-0 libgl1 libgomp1 \
                   debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy

# 2. Dedicated service user + app dir
id toolcut &>/dev/null || useradd --system --create-home --home-dir /opt/toolcut toolcut
install -d -o toolcut -g toolcut /opt/toolcut

# 3. App code (expects these files next to this script)
cp toolcut.py server.py requirements.txt /opt/toolcut/
[ -f /opt/toolcut/.env ] || cp env.example /opt/toolcut/.env
chown -R toolcut:toolcut /opt/toolcut
chmod 600 /opt/toolcut/.env

# 4. Virtualenv + deps
sudo -u toolcut python3 -m venv /opt/toolcut/venv
sudo -u toolcut /opt/toolcut/venv/bin/pip install --upgrade pip -q
sudo -u toolcut /opt/toolcut/venv/bin/pip install -r /opt/toolcut/requirements.txt -q

# 5. systemd service (reads /opt/toolcut/.env)
cp toolcut.service /etc/systemd/system/toolcut.service
systemctl daemon-reload
systemctl enable toolcut

# 6. Caddy reverse proxy (edit the domain first for real HTTPS)
cp Caddyfile /etc/caddy/Caddyfile

echo "------------------------------------------------------------"
echo "Installed. BEFORE starting, do two things:"
echo "  1. Edit  /opt/toolcut/.env       (Supabase + S3 credentials)"
echo "  2. Edit  /etc/caddy/Caddyfile    (your domain)"
echo "  3. Run the SQL in schema.sql in the Supabase SQL editor."
echo "Then:  systemctl start toolcut && systemctl reload caddy"
echo "------------------------------------------------------------"
