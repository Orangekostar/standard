#!/usr/bin/env bash
# One-click launcher for Technical V2 frontend (Streamlit) + backend worker.
set -euo pipefail

# Resolve symlinks so ./start_technical_v2.sh from repo root still finds scripts/.
SOURCE="${BASH_SOURCE[0]}"
while [[ -L "${SOURCE}" ]]; do
  DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
  LINK="$(readlink "${SOURCE}")"
  if [[ "${LINK}" == /* ]]; then
    SOURCE="${LINK}"
  else
    SOURCE="${DIR}/${LINK}"
  fi
done
SCRIPT_DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PARENT_QUANT_DIR="$(cd "${PROJECT_DIR}/../.." && pwd)"

HOST="${TECHNICAL_V2_HOST:-127.0.0.1}"
PORT="${TECHNICAL_V2_PORT:-8511}"
BROWSER_ADDRESS="${TECHNICAL_V2_BROWSER_ADDRESS:-$(hostname)}"
POLL_SECONDS="${TECHNICAL_V2_POLL_SECONDS:-5}"
# V2 stages share one SQLite store and publication lock.
MAX_CONCURRENCY="${TECHNICAL_V2_MAX_CONCURRENCY:-1}"
OPEN_BROWSER="${TECHNICAL_V2_OPEN_BROWSER:-1}"
WAIT_SECONDS="${TECHNICAL_V2_WAIT_SECONDS:-60}"

RUNTIME_DIR="${PROJECT_DIR}/cache/runtime"
LOG_DIR="${RUNTIME_DIR}/logs"
PID_DIR="${RUNTIME_DIR}/pids"
WORKER_PID_FILE="${PID_DIR}/worker.pid"
UI_PID_FILE="${PID_DIR}/ui.pid"
WORKER_LOG="${LOG_DIR}/worker.log"
UI_LOG="${LOG_DIR}/ui.log"
URL="http://${HOST}:${PORT}"
UNIT_PREFIX="quant-technical-v2-${PORT}"
WORKER_UNIT="${UNIT_PREFIX}-worker.service"
UI_UNIT="${UNIT_PREFIX}-ui.service"

mkdir -p "${LOG_DIR}" "${PID_DIR}" \
  "${PROJECT_DIR}/cache/v2" \
  "${PROJECT_DIR}/artifacts/technical_v2"

resolve_python() {
  local candidates=()
  if [[ -n "${TECHNICAL_V2_PYTHON:-}" ]]; then
    candidates+=("${TECHNICAL_V2_PYTHON}")
  fi
  candidates+=(
    "${PROJECT_DIR}/.venv/bin/python"
    "${PARENT_QUANT_DIR}/.venv/bin/python"
    "python3"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ "${candidate}" == /* ]]; then
      [[ -x "${candidate}" ]] || continue
    else
      command -v "${candidate}" >/dev/null 2>&1 || continue
    fi
    if "${candidate}" -c 'import streamlit' >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  echo "error: no Python with streamlit found (set TECHNICAL_V2_PYTHON)" >&2
  exit 1
}

ensure_env_file() {
  local env_file="${PROJECT_DIR}/.env"
  local example="${PROJECT_DIR}/.env.example"
  local parent_env="${PARENT_QUANT_DIR}/.env"

  if [[ -f "${env_file}" ]]; then
    return 0
  fi

  if [[ -f "${parent_env}" ]]; then
    cp "${parent_env}" "${env_file}"
    echo "created .env from ${parent_env}"
  elif [[ -f "${example}" ]]; then
    cp "${example}" "${env_file}"
    echo "created .env from .env.example (fill TUSHARE_TOKEN / TYPESAFE_API_KEY if needed)"
  else
    echo "error: missing .env and .env.example under ${PROJECT_DIR}" >&2
    exit 1
  fi

  if [[ -f "${example}" ]]; then
    local key value
    while IFS= read -r line || [[ -n "${line}" ]]; do
      [[ "${line}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
      key="${line%%=*}"
      value="${line#*=}"
      if ! grep -qE "^${key}=" "${env_file}"; then
        printf '%s=%s\n' "${key}" "${value}" >>"${env_file}"
      fi
    done <"${example}"
  fi
}

pid_alive() {
  local pid_file="$1"
  [[ -f "${pid_file}" ]] || return 1
  local pid
  pid="$(tr -d '[:space:]' <"${pid_file}")"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null
}

read_pid() {
  tr -d '[:space:]' <"$1"
}

adopt_service_pid() {
  local unit="$1" pid_file="$2" pid
  if systemctl --user is-active --quiet "${unit}" 2>/dev/null; then
    pid="$(systemctl --user show "${unit}" --property=MainPID --value)"
    if [[ "${pid}" =~ ^[1-9][0-9]*$ ]]; then
      printf '%s\n' "${pid}" >"${pid_file}"
    fi
  fi
}

launch_service() {
  local unit="$1" log_file="$2" pid_file="$3"
  shift 3
  if command -v systemd-run >/dev/null 2>&1 \
    && systemctl --user show-environment >/dev/null 2>&1; then
    systemd-run --user --quiet --collect --unit="${unit}" \
      --working-directory="${PROJECT_DIR}" \
      --property=Restart=on-failure --property=RestartSec=5 \
      --property="StandardOutput=append:${log_file}" \
      --property="StandardError=append:${log_file}" \
      --setenv=PYTHONUNBUFFERED=1 --setenv="DATA_MODE=${DATA_MODE:-real}" \
      "$@"
    adopt_service_pid "${unit}" "${pid_file}"
  else
    nohup setsid env PYTHONUNBUFFERED=1 DATA_MODE="${DATA_MODE:-real}" \
      "$@" >>"${log_file}" 2>&1 </dev/null &
    echo $! >"${pid_file}"
  fi
}

wait_for_url() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if curl --noproxy '*' -fsS --max-time 2 "${URL}/_stcore/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

open_browser() {
  [[ "${OPEN_BROWSER}" == "1" ]] || return 0
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${URL}" >/dev/null 2>&1 || true
  elif command -v sensible-browser >/dev/null 2>&1; then
    sensible-browser "${URL}" >/dev/null 2>&1 || true
  fi
}

find_technical_v2_workers() {
  # Require a real Python -m invocation; avoid matching shell wrappers that quote this script.
  ps -eo pid=,args= | awk '/[p]ython/ && /-m core\.background\.precompute_worker/ && /--profile[ =]technical_v2/ {print $1}'
}

start_worker() {
  adopt_service_pid "${WORKER_UNIT}" "${WORKER_PID_FILE}"
  if pid_alive "${WORKER_PID_FILE}"; then
    echo "worker already running (pid $(read_pid "${WORKER_PID_FILE}"))"
    return 0
  fi

  local existing
  existing="$(find_technical_v2_workers | head -n 1 || true)"
  if [[ -n "${existing}" ]]; then
    echo "${existing}" >"${WORKER_PID_FILE}"
    echo "worker already running externally (pid ${existing}); adopted into ${WORKER_PID_FILE}"
    return 0
  fi

  rm -f "${WORKER_PID_FILE}"

  launch_service "${WORKER_UNIT}" "${WORKER_LOG}" "${WORKER_PID_FILE}" \
    "${PYTHON_BIN}" -m core.background.precompute_worker \
    --profile technical_v2 \
    --poll-seconds "${POLL_SECONDS}" \
    --max-concurrency "${MAX_CONCURRENCY}"
  sleep 0.8
  if ! pid_alive "${WORKER_PID_FILE}"; then
    echo "error: worker failed to start; see ${WORKER_LOG}" >&2
    tail -n 40 "${WORKER_LOG}" >&2 || true
    exit 1
  fi
  echo "worker started (pid $(read_pid "${WORKER_PID_FILE}")) → ${WORKER_LOG}"
}

start_ui() {
  adopt_service_pid "${UI_UNIT}" "${UI_PID_FILE}"
  if pid_alive "${UI_PID_FILE}"; then
    echo "ui already running (pid $(read_pid "${UI_PID_FILE}"))"
    return 0
  fi

  if curl --noproxy '*' -fsS --max-time 1 "${URL}/_stcore/health" >/dev/null 2>&1; then
    echo "ui already listening on ${URL} (external process)"
    return 0
  fi

  rm -f "${UI_PID_FILE}"

  launch_service "${UI_UNIT}" "${UI_LOG}" "${UI_PID_FILE}" \
    "${PYTHON_BIN}" -m streamlit run "${PROJECT_DIR}/app_v2.py" \
    --server.address "${HOST}" \
    --server.port "${PORT}" \
    --server.headless true \
    --browser.gatherUsageStats false \
    --browser.serverAddress "${BROWSER_ADDRESS}" \
    --browser.serverPort "${PORT}"

  if ! wait_for_url; then
    echo "error: ui did not become ready on ${URL} within ${WAIT_SECONDS}s; see ${UI_LOG}" >&2
    exit 1
  fi
  echo "ui started (pid $(read_pid "${UI_PID_FILE}")) → ${URL}"
}

kill_tree() {
  local pid="$1"
  local signal="${2:-TERM}"
  local kids
  kids="$(pgrep -P "${pid}" 2>/dev/null || true)"
  local kid
  for kid in ${kids}; do
    kill_tree "${kid}" "${signal}"
  done
  kill "-${signal}" "${pid}" 2>/dev/null || true
}

stop_pid_file() {
  local name="$1"
  local pid_file="$2"
  if ! pid_alive "${pid_file}"; then
    rm -f "${pid_file}"
    echo "${name} not running"
    return 0
  fi
  local pid
  pid="$(read_pid "${pid_file}")"
  # Kill descendants first so orphaned task children cannot keep .command.lock.
  kill_tree "${pid}" TERM
  local i
  for i in {1..40}; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  if kill -0 "${pid}" 2>/dev/null; then
    kill_tree "${pid}" KILL
    sleep 0.2
  fi
  rm -f "${pid_file}"
  echo "${name} stopped (pid ${pid})"
}

cmd_status() {
  adopt_service_pid "${WORKER_UNIT}" "${WORKER_PID_FILE}"
  adopt_service_pid "${UI_UNIT}" "${UI_PID_FILE}"
  echo "project: ${PROJECT_DIR}"
  echo "python:  ${PYTHON_BIN}"
  echo "url:     ${URL}"
  if pid_alive "${WORKER_PID_FILE}"; then
    echo "worker:  running (pid $(read_pid "${WORKER_PID_FILE}"))"
  else
    echo "worker:  stopped"
  fi
  local all_workers
  all_workers="$(find_technical_v2_workers | tr '\n' ' ' || true)"
  if [[ -n "${all_workers// /}" ]]; then
    echo "workers: ${all_workers}"
  fi
  if pid_alive "${UI_PID_FILE}"; then
    echo "ui:      running (pid $(read_pid "${UI_PID_FILE}"))"
  elif curl --noproxy '*' -fsS --max-time 1 "${URL}/_stcore/health" >/dev/null 2>&1; then
    echo "ui:      listening on ${URL} (external)"
  else
    echo "ui:      stopped"
  fi
}

cmd_start() {
  ensure_env_file
  start_worker
  start_ui
  open_browser
  echo
  echo "Technical V2 ready: ${URL}"
  echo "  stop:    $0 stop"
  echo "  status:  $0 status"
  echo "  logs:    ${LOG_DIR}/"
}

cmd_stop() {
  systemctl --user stop "${UI_UNIT}" "${WORKER_UNIT}" 2>/dev/null || true
  stop_pid_file "ui" "${UI_PID_FILE}"
  stop_pid_file "worker" "${WORKER_PID_FILE}"
}

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_open() {
  if ! wait_for_url; then
    echo "error: ui not reachable at ${URL}; run: $0 start" >&2
    exit 1
  fi
  open_browser
  echo "${URL}"
}

usage() {
  cat <<'USAGE'
Usage: start_technical_v2.sh [start|stop|restart|status|open]

  start    Start Technical V2 worker + Streamlit UI (default), then open browser
  stop     Stop processes started by this script
  restart  Stop then start
  status   Show worker/UI status
  open     Open the UI URL if already up

Environment overrides:
  TECHNICAL_V2_PYTHON, TECHNICAL_V2_HOST, TECHNICAL_V2_PORT,
  TECHNICAL_V2_BROWSER_ADDRESS,
  TECHNICAL_V2_POLL_SECONDS, TECHNICAL_V2_MAX_CONCURRENCY,
  TECHNICAL_V2_OPEN_BROWSER (0/1), TECHNICAL_V2_WAIT_SECONDS, DATA_MODE
USAGE
}

PYTHON_BIN="$(resolve_python)"
cd "${PROJECT_DIR}"

ACTION="${1:-start}"
case "${ACTION}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  open) cmd_open ;;
  -h|--help|help) usage ;;
  *)
    echo "unknown action: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
