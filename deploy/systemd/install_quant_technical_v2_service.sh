#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${TECHNICAL_V2_PROJECT_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
TEMPLATE="${SCRIPT_DIR}/quant-technical-v2.service"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
DESTINATION="${USER_UNIT_DIR}/quant-technical-v2.service"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "python not executable: ${PYTHON_BIN}" >&2
  exit 1
fi

if systemctl --user is-active --quiet quant-precompute.service 2>/dev/null; then
  echo "refusing install: quant-precompute.service is active and may share V2 output" >&2
  exit 2
fi

if systemctl is-active --quiet quant-precompute.service 2>/dev/null; then
  echo "refusing install: system quant-precompute.service is active and may share V2 output" >&2
  exit 2
fi

mkdir -p "${USER_UNIT_DIR}" "${PROJECT_DIR}/cache/v2" "${PROJECT_DIR}/artifacts/technical_v2"
escaped_project="${PROJECT_DIR//|/\\|}"
sed "s|%h/standard|${escaped_project}|g" "${TEMPLATE}" >"${DESTINATION}"
systemd-analyze --user verify "${DESTINATION}"
systemctl --user daemon-reload

echo "installed ${DESTINATION}"
echo "service was not started; inspect it, then run: systemctl --user enable --now quant-technical-v2.service"
