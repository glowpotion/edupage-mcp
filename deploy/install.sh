#!/usr/bin/env bash
# Install or update the server as a systemd service. Run on the Pi, as the
# user the service should run as (not root); it uses sudo where it must.
#
#   bash deploy/install.sh [--lan] [--env-from FILE]
#
# First run: creates /etc/edupage-mcp.env from FILE (an .env like
# .env.example, deleted afterwards) or by prompting, and generates a bearer
# token. Later runs keep that file and just update and restart the service.
#
# The service listens on 127.0.0.1 only, for an agent on the same machine.
# --lan makes a first install listen on all interfaces instead, for clients
# elsewhere on the network; to switch later, edit MCP_HOST in the env file.
set -euo pipefail

SERVICE=edupage-mcp
ENV_FILE=/etc/edupage-mcp.env
UNIT_FILE=/etc/systemd/system/$SERVICE.service
STATE_FILE=/var/lib/edupage-mcp/sessions.json
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
ENV_FROM=""
BIND_HOST=127.0.0.1
DEFAULT_PORT=8766

while [ $# -gt 0 ]; do
  case "$1" in
    --lan) BIND_HOST=0.0.0.0; shift ;;
    --env-from) ENV_FROM="${2:?--env-from needs a file}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$RUN_USER" != root ] || die "Run this as your normal user, not root; it calls sudo itself."
command -v systemctl >/dev/null || die "systemd is required."

case "$(uname -m)" in
  aarch64|x86_64) ;;
  *) echo "WARNING: $(uname -m) detected. 64-bit Raspberry Pi OS is recommended;" \
          "on 32-bit some dependencies (pydantic-core) may have to compile." >&2 ;;
esac

UV="$(command -v uv || true)"
[ -n "$UV" ] || { [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"; }
[ -n "$UV" ] || die "uv is not installed. Install it with:
    curl -LsSf https://astral.sh/uv/install.sh | sh
then re-run this script."

say "Installing dependencies into $APP_DIR/.venv"
cd "$APP_DIR"
# The unit forbids writes under /home, so compile bytecode now.
"$UV" sync --frozen --no-dev --compile-bytecode

if sudo test -f "$ENV_FILE"; then
  say "Keeping existing $ENV_FILE"
  [ -z "$ENV_FROM" ] || { rm -f "$ENV_FROM"; echo "(ignored and removed $ENV_FROM)"; }
else
  say "Creating $ENV_FILE"
  if [ -n "$ENV_FROM" ]; then
    get() { sed -n "s/^$1=//p" "$ENV_FROM" | tail -n1; }
    USERNAME="$(get EDUPAGE_USERNAME)"
    PASSWORD="$(get EDUPAGE_PASSWORD)"
    SUBDOMAINS="$(get EDUPAGE_SUBDOMAINS)"
    TIMEZONE="$(get EDUPAGE_TIMEZONE)"
    READ_ONLY="$(get EDUPAGE_READ_ONLY)"
  else
    read -rp "EduPage username (usually your email): " USERNAME
    read -rsp "EduPage password: " PASSWORD; echo
    read -rp "School subdomains, comma-separated (Enter to discover them): " SUBDOMAINS
    read -rp "Read-only, so the agent cannot send messages or order meals? [y/N] " RO
    case "$RO" in [yY]*) READ_ONLY=true ;; *) READ_ONLY="" ;; esac
    TIMEZONE=""
  fi
  [ -n "$USERNAME" ] && [ -n "$PASSWORD" ] || die "EDUPAGE_USERNAME and EDUPAGE_PASSWORD are required."
  if [ -z "$TIMEZONE" ]; then
    TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Bratislava)"
  fi
  TOKEN="$(openssl rand -hex 32 2>/dev/null || "$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"

  TMP="$(mktemp)"
  chmod 600 "$TMP"
  {
    echo "EDUPAGE_USERNAME=$USERNAME"
    echo "EDUPAGE_PASSWORD=$PASSWORD"
    [ -z "$SUBDOMAINS" ] || echo "EDUPAGE_SUBDOMAINS=$SUBDOMAINS"
    echo "EDUPAGE_TIMEZONE=$TIMEZONE"
    [ -z "$READ_ONLY" ] || echo "EDUPAGE_READ_ONLY=$READ_ONLY"
    echo "EDUPAGE_SESSION_FILE=$STATE_FILE"
    echo "MCP_HOST=$BIND_HOST"
    echo "MCP_PORT=$DEFAULT_PORT"
    echo "MCP_AUTH_TOKEN=$TOKEN"
  } > "$TMP"
  sudo install -m 600 -o root -g root "$TMP" "$ENV_FILE"
  rm -f "$TMP"
  [ -z "$ENV_FROM" ] || rm -f "$ENV_FROM"
fi

say "Checking the EduPage login"
# The check runs without the service's state directory, so it must not try to
# write the session file there.
sudo bash -c "set -a; . '$ENV_FILE'; set +a; unset EDUPAGE_SESSION_FILE; exec runuser -u '$RUN_USER' -- '$APP_DIR/.venv/bin/edupage-mcp' --check" \
  || die "Could not log in to EduPage with the credentials in $ENV_FILE (edit it with: sudo nano $ENV_FILE)."

say "Installing $UNIT_FILE"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" \
  "$APP_DIR/deploy/$SERVICE.service" | sudo tee "$UNIT_FILE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"

PORT="$(sudo sed -n 's/^MCP_PORT=//p' "$ENV_FILE")"; PORT="${PORT:-$DEFAULT_PORT}"
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 \
  || die "The service did not come up. See: sudo journalctl -u $SERVICE -n 50"

TOKEN="$(sudo sed -n 's/^MCP_AUTH_TOKEN=//p' "$ENV_FILE")"
LISTEN="$(sudo sed -n 's/^MCP_HOST=//p' "$ENV_FILE")"

if [ "$LISTEN" = 127.0.0.1 ] || [ "$LISTEN" = localhost ]; then
  URL="http://127.0.0.1:$PORT/mcp"
  say "Running on this machine only: $URL"
  WHERE="an agent on this machine"
else
  URL="http://$(hostname).local:$PORT/mcp"
  say "Running: $URL  (or http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/mcp)"
  WHERE="a client on another machine"
fi
cat <<EOT

Point $WHERE at the streamable-HTTP endpoint:

  URL:     $URL
  Header:  Authorization: Bearer $TOKEN

For Claude Code, that is:

  claude mcp add --transport http edupage $URL \\
    --header "Authorization: Bearer $TOKEN"

Logs:    sudo journalctl -u $SERVICE -f
Config:  sudo nano $ENV_FILE && sudo systemctl restart $SERVICE
EOT
