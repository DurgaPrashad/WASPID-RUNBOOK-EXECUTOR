#!/usr/bin/env bash
# Bring up the live WASPID stack: demo infra on real Docker + operator dashboard.
#
#   ./scripts/live.sh            start everything (dashboard on http://localhost:8787)
#   ./scripts/live.sh --public   same, plus a public HTTPS URL via ngrok
#
# Secrets (DB password, operator token) are generated once into .env (gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\033[1;33m▸\033[0m %s\n' "$*"; }

# 1. Docker runtime
if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null; then
    say "Starting Colima (Docker runtime)…"
    colima start --cpu 2 --memory 4
  else
    echo "Docker is not running. Start Docker Desktop / Colima / OrbStack and retry." >&2
    exit 1
  fi
fi

# 2. Secrets
if [ ! -f .env ]; then
  say "Generating .env (DB password + operator token)…"
  umask 077
  {
    echo "WASPID_DB_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
    echo "WASPID_OPERATOR_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
  } > .env
fi
set -a; . ./.env; set +a

# 3. Python environment
if [ ! -x .venv/bin/python ]; then
  say "Creating .venv and installing requirements…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

# 4. Demo infrastructure + sandbox image
say "Starting demo infrastructure (waspid-api, waspid-worker, waspid-db)…"
docker compose -f demo_infra/docker-compose.yml up -d --build
docker image inspect python:3.12-slim >/dev/null 2>&1 || docker pull -q python:3.12-slim

# 5. Optional public tunnel
if [ "${1:-}" = "--public" ]; then
  command -v ngrok >/dev/null || { echo "ngrok is not installed (brew install ngrok)." >&2; exit 1; }
  say "Opening public tunnel (ngrok)…"
  ngrok http "${WASPID_DASHBOARD_PORT:-8787}" --log stdout > .ngrok.log 2>&1 &
  trap 'kill $! 2>/dev/null' EXIT
  for _ in $(seq 1 20); do
    url=$(curl -s localhost:4040/api/tunnels | python3 -c 'import json,sys; t=json.load(sys.stdin)["tunnels"]; print(t[0]["public_url"] if t else "")' 2>/dev/null || true)
    [ -n "${url:-}" ] && break; sleep 0.5
  done
  if [ -n "${url:-}" ]; then
    say "Public URL (read-only for visitors): $url"
    say "Operator link:                       $url/#operator=$WASPID_OPERATOR_TOKEN"
  else
    echo "ngrok did not come up — see .ngrok.log (is your authtoken configured?)" >&2
  fi
fi

say "Operator link: http://localhost:${WASPID_DASHBOARD_PORT:-8787}/#operator=$WASPID_OPERATOR_TOKEN"
exec .venv/bin/python dashboard/server.py
