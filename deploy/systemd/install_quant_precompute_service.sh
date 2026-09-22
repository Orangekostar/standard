#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SERVICE_DST="/etc/systemd/system/quant-precompute.service"
USER_SERVICE_DST="${HOME}/.config/systemd/user/quant-precompute.service"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
POLL_SECONDS="${POLL_SECONDS:-5}"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-3}"
LOG_FILE="${LOG_FILE:-${PROJECT_DIR}/cache/precompute_worker.log}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "python not executable: ${PYTHON_BIN}" >&2
  exit 1
fi

mkdir -p "${PROJECT_DIR}/cache"
SERVICE_TMP="$(mktemp)"
USER_SERVICE_TMP="$(mktemp)"
trap 'rm -f "${SERVICE_TMP}" "${USER_SERVICE_TMP}"' EXIT

cat >"${SERVICE_TMP}" <<EOF
[Unit]
Description=Quant background precompute worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-${PROJECT_DIR}/.env
ExecStart=${PYTHON_BIN} -m core.background.precompute_worker --poll-seconds ${POLL_SECONDS} --max-concurrency ${MAX_CONCURRENCY}
Restart=always
RestartSec=5
TimeoutStopSec=20
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF

cat >"${USER_SERVICE_TMP}" <<EOF
[Unit]
Description=Quant background precompute worker (user)
After=default.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-${PROJECT_DIR}/.env
ExecStart=${PYTHON_BIN} -m core.background.precompute_worker --poll-seconds ${POLL_SECONDS} --max-concurrency ${MAX_CONCURRENCY}
Restart=always
RestartSec=5
TimeoutStopSec=20
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=default.target
EOF

if sudo -n true >/dev/null 2>&1; then
  sudo cp "${SERVICE_TMP}" "${SERVICE_DST}"
  sudo systemctl daemon-reload
  sudo systemctl enable --now quant-precompute.service
  sudo systemctl status --no-pager quant-precompute.service
  exit 0
fi

mkdir -p "${HOME}/.config/systemd/user"
cp "${USER_SERVICE_TMP}" "${USER_SERVICE_DST}"
systemctl --user daemon-reload
systemctl --user disable --now quant-precompute.service >/dev/null 2>&1 || true
systemctl --user enable --now quant-precompute.service
systemctl --user status --no-pager quant-precompute.service
