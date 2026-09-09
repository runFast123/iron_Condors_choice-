#!/usr/bin/env bash
#
# Provision a fresh Ubuntu box to run the engine.
#
# Written to be read before it is run. It installs Python, clones the repo,
# creates a service user, sets up systemd and puts Caddy in front for TLS. It
# does not touch Choice: declaring this machine's IP against your API key is a
# manual step in the FinX portal, and the script prints the IP you need.
#
#   curl -fsSL https://raw.githubusercontent.com/runFast123/iron_Condors_choice-/main/scripts/provision-vps.sh -o provision.sh
#   less provision.sh          # read it first
#   sudo bash provision.sh engine.yourdomain.com
#
# Idempotent: safe to re-run after fixing something.

set -euo pipefail

DOMAIN="${1:-}"
REPO="${REPO:-https://github.com/runFast123/iron_Condors_choice-.git}"
APP_DIR=/opt/iron-condor
SERVICE_USER=condor

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Checking this machine's public IP"
PUBLIC_IP="$(curl -fsS --max-time 15 https://api.ipify.org || echo unknown)"
echo "    $PUBLIC_IP"
echo "    This is the address Choice will see. It must be declared against every"
echo "    user's API key, or Choice rejects the request (Integration Guide 8)."

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates

say "Creating the service user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/$SERVICE_USER --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "Fetching the code into $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin
  git -C "$APP_DIR" reset --hard --quiet origin/main
else
  git clone --quiet "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR/state/logs"

say "Building the virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "Generating the engine shared secret"
ENV_FILE="$APP_DIR/.env.engine.local"
if [[ -f "$ENV_FILE" ]] && grep -q ENGINE_SHARED_SECRET "$ENV_FILE"; then
  echo "    already present; leaving it alone"
else
  SECRET="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=')"
  echo "ENGINE_SHARED_SECRET=$SECRET" > "$ENV_FILE"
  echo "    written to $ENV_FILE"
  echo "    Set the SAME value as ENGINE_SHARED_SECRET in Vercel, then redeploy."
fi
chmod 600 "$ENV_FILE"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

say "Installing the systemd unit"
cp "$APP_DIR/scripts/iron-condor-engine.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now iron-condor-engine

sleep 3
if curl -fsS --max-time 5 http://127.0.0.1:8020/health >/dev/null; then
  echo "    engine is answering on 127.0.0.1:8020"
else
  echo "    engine did not answer; check: journalctl -u iron-condor-engine -n 50" >&2
fi

if [[ -n "$DOMAIN" ]]; then
  say "Putting Caddy in front of it for TLS on $DOMAIN"
  # Caddy rather than nginx purely because it gets and renews the certificate
  # on its own; one less thing to expire silently in six months.
  if ! command -v caddy >/dev/null; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy
  fi
  cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8020
}
EOF
  systemctl reload caddy || systemctl restart caddy
  echo "    point $DOMAIN at $PUBLIC_IP in DNS, then Caddy issues the certificate"
else
  say "No domain given, so no TLS was set up"
  echo "    Re-run with a hostname, or expose the engine some other way."
  echo "    Do not open port 8020 to the internet unencrypted: the engine"
  echo "    carries broker session tokens."
fi

say "Done"
cat <<EOF

  Engine IP        $PUBLIC_IP
  Service          systemctl status iron-condor-engine
  Logs             journalctl -u iron-condor-engine -f
  Update           cd $APP_DIR && sudo git pull && sudo systemctl restart iron-condor-engine

  Still to do, by hand:
    1. Declare $PUBLIC_IP against each user's Choice API key in the FinX portal.
    2. Set ENGINE_URL in Vercel to https://${DOMAIN:-<your-host>} and redeploy.
    3. Set ENGINE_SHARED_SECRET in Vercel to the value in $ENV_FILE.

EOF
