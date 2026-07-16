#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/opt/openclaw/data/workspace/daily-signal-collector"
WORKSPACE_DIR="/opt/openclaw/data/workspace"
OPENCLAW_DIR="/opt/openclaw/source"
EXCHANGE_DIR="/var/lib/daily-signal-exchange"
VAULT_DIR="${WORKSPACE_DIR}/daily-signal-vault"
STATE_DIR="/var/lib/daily-signal-collector"
SERVICE_USER="ubuntu"

[[ "$EUID" -eq 0 ]] || { echo "Run this installer as root (sudo)." >&2; exit 1; }

for command in git docker flock runuser; do
  command -v "$command" >/dev/null || { echo "Required command not found: $command" >&2; exit 1; }
done
id "$SERVICE_USER" >/dev/null 2>&1 || { echo "Service user not found: $SERVICE_USER" >&2; exit 1; }
[[ -d "$REPO_DIR/.git" ]] || { echo "Collector Git repository not found: $REPO_DIR" >&2; exit 1; }
[[ -d "$OPENCLAW_DIR" ]] || { echo "OpenClaw source directory not found: $OPENCLAW_DIR" >&2; exit 1; }
[[ -x "$REPO_DIR/.venv/bin/python" && -x "$REPO_DIR/.venv/bin/pip" ]] || {
  echo "Create ${REPO_DIR}/.venv as ${SERVICE_USER} before installation." >&2
  exit 1
}
[[ "$(git -C "$REPO_DIR" branch --show-current)" == "main" ]] || {
  echo "Collector repository must be on main." >&2
  exit 1
}
[[ -z "$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" ]] || {
  echo "Collector repository has tracked local changes." >&2
  exit 1
}
git -C "$REPO_DIR" remote get-url origin >/dev/null
docker compose -f "$OPENCLAW_DIR/docker-compose.yml" version >/dev/null
id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx docker || {
  echo "Service user must belong to the docker group." >&2
  exit 1
}

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
  "$EXCHANGE_DIR" "$VAULT_DIR" "$STATE_DIR" \
  "${EXCHANGE_DIR}/candidates" "${EXCHANGE_DIR}/feedback" "${EXCHANGE_DIR}/archive"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "${WORKSPACE_DIR}/skills"
ln -sfn "${REPO_DIR}/openclaw/skills/daily-signal-learning" \
  "${WORKSPACE_DIR}/skills/daily-signal-learning"

runuser -u "$SERVICE_USER" -- "${REPO_DIR}/.venv/bin/pip" install -r "${REPO_DIR}/requirements.txt"
install -m 0644 "${REPO_DIR}/ops/daily-signal-collector@.service" \
  "/etc/systemd/system/daily-signal-collector@.service"
systemctl daemon-reload

echo "Daily Signal Collector installed."
echo "Vault: ${VAULT_DIR}"
echo "Exchange: ${EXCHANGE_DIR}"
echo "Next: systemctl start daily-signal-collector@digest.service"
