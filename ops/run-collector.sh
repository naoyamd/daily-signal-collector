#!/usr/bin/env bash
set -Eeuo pipefail

EDITION="${1:-digest}"
REPO_DIR="/opt/openclaw/data/workspace/daily-signal-collector"
OPENCLAW_DIR="${OPENCLAW_DIR:-/opt/openclaw/source}"
PYTHON="${DAILY_SIGNAL_COLLECTOR_PYTHON:-${REPO_DIR}/.venv/bin/python}"
CONTAINER_REPO="/home/node/.openclaw/workspace/daily-signal-collector"
MODEL="${DAILY_SIGNAL_MODEL:-openai/gpt-5.6-luna}"
THINKING="${DAILY_SIGNAL_THINKING:-xhigh}"
EXCHANGE_DIR="/var/lib/daily-signal-exchange"
VAULT_DIR="/opt/openclaw/data/workspace/daily-signal-vault"
STATE_DIR="/var/lib/daily-signal-collector"
LEARNING_DB="${STATE_DIR}/learning.sqlite3"
WORK_DIR="${REPO_DIR}/.collector"
PLAN="${WORK_DIR}/research-plan.json"
SCOUT="${WORK_DIR}/scout.json"
PROMPT="${CONTAINER_REPO}/ops/scout-prompt.md"
LOCK_FILE="${STATE_DIR}/collector.lock"
SCOUT_ENABLED="${DAILY_SIGNAL_SCOUT_ENABLED:-1}"

case "$EDITION" in
  digest|deep-dive) CONFIG="config/sources.yaml" ;;
  market) CONFIG="config/market_sources.yaml" ;;
  *) echo "Unsupported edition: $EDITION" >&2; exit 2 ;;
esac

OUTPUT="${EXCHANGE_DIR}/candidates/${EDITION}.json"
FEEDBACK="${EXCHANGE_DIR}/feedback"
EXPLICIT_FEEDBACK="${WORK_DIR}/feedback-inbox"
ARCHIVE="${EXCHANGE_DIR}/archive"

mkdir -p "$WORK_DIR" "$EXPLICIT_FEEDBACK" "$VAULT_DIR" "$STATE_DIR" "$(dirname "$OUTPUT")" "$FEEDBACK" "$ARCHIVE"
exec 9>"$LOCK_FILE"
flock 9

cd "$REPO_DIR"
[[ -x "$PYTHON" ]] || { echo "Python environment not found: $PYTHON" >&2; exit 1; }
[[ "$(git branch --show-current)" == "main" ]] || { echo "Collector repository must be on main." >&2; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
  echo "Refusing to collect with tracked local changes." >&2
  exit 1
}
git pull --ff-only origin main

if ! "$PYTHON" -m scripts.adaptive_learning --db "$LEARNING_DB" ingest \
  --config "$CONFIG" --inbox "$FEEDBACK"; then
  echo "warning: some publisher or explicit feedback could not be learned" >&2
fi
if ! "$PYTHON" -m scripts.adaptive_learning --db "$LEARNING_DB" ingest \
  --config "$CONFIG" --inbox "$EXPLICIT_FEEDBACK"; then
  echo "warning: some explicit OpenClaw feedback could not be learned" >&2
fi
if ! "$PYTHON" -m scripts.feedback_bridge \
  --vault "$VAULT_DIR" --feedback "$FEEDBACK" --candidates "$EXCHANGE_DIR"; then
  echo "warning: some publisher outcomes could not be reflected in the Vault" >&2
fi

"$PYTHON" -m scripts.adaptive_learning --db "$LEARNING_DB" plan \
  --config "$CONFIG" --output "$PLAN"

rm -f "$SCOUT" "$WORK_DIR/scout-agent-result.json"
if [[ "$SCOUT_ENABLED" == "1" ]]; then
  if ! docker compose -f "$OPENCLAW_DIR/docker-compose.yml" run -T --rm openclaw-cli agent \
    --session-id "daily-signal-collector-${EDITION}-$(TZ=Asia/Tokyo date +%F)" \
    --model "$MODEL" \
    --thinking "$THINKING" \
    --message-file "$PROMPT" \
    --json \
    --timeout 1800 \
    >"$WORK_DIR/scout-agent-result.json"; then
    echo "warning: web scout failed; continuing with RSS/Atom" >&2
  fi
fi

"$PYTHON" -m scripts.collector_pipeline \
  --config "$CONFIG" \
  --vault "$VAULT_DIR" \
  --learning-db "$LEARNING_DB" \
  --scout "$SCOUT" \
  --edition "$EDITION" \
  --output "$OUTPUT"

batch_id="$($PYTHON -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["batch_id"])' "$OUTPUT")"
install -m 0640 "$OUTPUT" "$ARCHIVE/${batch_id}.json"
echo "Collector handoff: ${OUTPUT} (${batch_id})"
