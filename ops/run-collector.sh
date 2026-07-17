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
SCOUT_PREVIOUS="${WORK_DIR}/scout-invalid.json"
SCOUT_VALIDATION="${WORK_DIR}/scout-validation.json"
PROMPT="${CONTAINER_REPO}/ops/scout-prompt.md"
LOCK_FILE="${STATE_DIR}/collector.lock"
SCOUT_ENABLED="${DAILY_SIGNAL_SCOUT_ENABLED:-1}"
SCOUT_ATTEMPTS="${DAILY_SIGNAL_SCOUT_ATTEMPTS:-2}"
SCOUT_MAX_AGE_HOURS=6
SCOUT_TIMEOUT="${DAILY_SIGNAL_SCOUT_TIMEOUT:-1800}"
SCOUT_REPAIR_TIMEOUT="${DAILY_SIGNAL_SCOUT_REPAIR_TIMEOUT:-900}"
SCOUT_WALL_CLOCK_GRACE=60

case "$EDITION" in
  digest|deep-dive) CONFIG="config/sources.yaml"; DEFAULT_SCOUT_MAX_ITEMS=80 ;;
  market) CONFIG="config/market_sources.yaml"; DEFAULT_SCOUT_MAX_ITEMS=60 ;;
  *) echo "Unsupported edition: $EDITION" >&2; exit 2 ;;
esac
SCOUT_MAX_ITEMS="$DEFAULT_SCOUT_MAX_ITEMS"

case "$SCOUT_ATTEMPTS" in
  1|2|3) ;;
  *) echo "DAILY_SIGNAL_SCOUT_ATTEMPTS must be 1, 2, or 3." >&2; exit 2 ;;
esac
[[ "$SCOUT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || {
  echo "DAILY_SIGNAL_SCOUT_TIMEOUT must be a positive integer." >&2
  exit 2
}
[[ "$SCOUT_REPAIR_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || {
  echo "DAILY_SIGNAL_SCOUT_REPAIR_TIMEOUT must be a positive integer." >&2
  exit 2
}

OUTPUT="${EXCHANGE_DIR}/candidates/${EDITION}.json"
FEEDBACK="${EXCHANGE_DIR}/feedback"
EXPLICIT_FEEDBACK="${WORK_DIR}/feedback-inbox"
ARCHIVE="${EXCHANGE_DIR}/archive"

mkdir -p "$WORK_DIR" "$EXPLICIT_FEEDBACK" "$VAULT_DIR" "$STATE_DIR" "$(dirname "$OUTPUT")" "$FEEDBACK" "$ARCHIVE"
active_scout_container=""
cleanup_scout_container() {
  if [[ -n "$active_scout_container" ]]; then
    docker rm -f "$active_scout_container" >/dev/null 2>&1 || true
  fi
}
trap cleanup_scout_container EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec 9>"$LOCK_FILE"
flock 9

cd "$REPO_DIR"
[[ -x "$PYTHON" ]] || { echo "Python environment not found: $PYTHON" >&2; exit 1; }
[[ "$(git branch --show-current)" == "main" ]] || { echo "Collector repository must be on main." >&2; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
  echo "Refusing to collect with tracked local changes." >&2
  exit 1
}
if ! git pull --ff-only origin main; then
  echo "warning: repository update failed; continuing with installed clean revision $(git rev-parse --short HEAD)" >&2
fi

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

rm -f "$SCOUT" "$SCOUT_PREVIOUS" "$SCOUT_VALIDATION" "$WORK_DIR"/scout-agent-result-*.json
SCOUT_ARGS=(--no-scout)
if [[ "$SCOUT_ENABLED" == "1" ]]; then
  scout_valid=0
  run_token="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  for ((attempt=1; attempt<=SCOUT_ATTEMPTS; attempt++)); do
    rm -f "$SCOUT"
    attempt_timeout="$SCOUT_TIMEOUT"
    [[ "$attempt" -gt 1 ]] && attempt_timeout="$SCOUT_REPAIR_TIMEOUT"
    attempt_wall_timeout=$((attempt_timeout + SCOUT_WALL_CLOCK_GRACE))
    active_scout_container="daily-signal-scout-${EDITION}-${run_token}-a${attempt}"
    if ! timeout --foreground --kill-after=30s "${attempt_wall_timeout}s" \
      docker compose -f "$OPENCLAW_DIR/docker-compose.yml" run -T --rm \
      --name "$active_scout_container" openclaw-cli agent \
      --local \
      --session-id "daily-signal-collector-${EDITION}-${run_token}-a${attempt}" \
      --model "$MODEL" \
      --thinking "$THINKING" \
      --message-file "$PROMPT" \
      --json \
      --timeout "$attempt_timeout" \
      >"$WORK_DIR/scout-agent-result-${attempt}.json"; then
      cleanup_scout_container
      active_scout_container=""
      echo "warning: web scout attempt ${attempt} failed" >&2
      continue
    fi
    active_scout_container=""
    validation_tmp="${SCOUT_VALIDATION}.tmp"
    if "$PYTHON" -m scripts.web_scout validate "$SCOUT" \
      --research-plan "$PLAN" \
      --max-age-hours "$SCOUT_MAX_AGE_HOURS" \
      --max-items "$SCOUT_MAX_ITEMS" \
      >"$validation_tmp"; then
      mv -f "$validation_tmp" "$SCOUT_VALIDATION"
      scout_valid=1
      SCOUT_ARGS=(--scout "$SCOUT")
      echo "Web scout strict validation passed on attempt ${attempt}:"
      cat "$SCOUT_VALIDATION"
      break
    fi
    mv -f "$validation_tmp" "$SCOUT_VALIDATION"
    [[ -f "$SCOUT" ]] && mv -f "$SCOUT" "$SCOUT_PREVIOUS"
    echo "warning: web scout attempt ${attempt} failed strict validation" >&2
    cat "$SCOUT_VALIDATION" >&2
  done
  if [[ "$scout_valid" != "1" ]]; then
    rm -f "$SCOUT"
    echo "warning: no valid web scout handoff; continuing with RSS/Atom" >&2
  fi
fi

"$PYTHON" -m scripts.collector_pipeline \
  --config "$CONFIG" \
  --vault "$VAULT_DIR" \
  --learning-db "$LEARNING_DB" \
  "${SCOUT_ARGS[@]}" \
  --edition "$EDITION" \
  --output "$OUTPUT"

batch_id="$($PYTHON -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["batch_id"])' "$OUTPUT")"
install -m 0640 "$OUTPUT" "$ARCHIVE/${batch_id}.json"
echo "Collector handoff: ${OUTPUT} (${batch_id})"
