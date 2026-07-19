#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-}"
ROOT="${DSO_LAN_ROOT:-/Users/fuqiang/Dev/douyin-slice-optimizer}"
KEYCHAIN_SERVICE="${DSO_LAN_KEYCHAIN_SERVICE:-dso-gpu-resource-agent}"
KEYCHAIN_ACCOUNT="${DSO_LAN_KEYCHAIN_ACCOUNT:-$(id -un)}"
PYTHON="${DSO_LAN_PYTHON:-${ROOT}/.venv/bin/python}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "DSO LAN Python is not executable: ${PYTHON}" >&2
  exit 1
fi

TOKEN="$(/usr/bin/security find-generic-password -a "${KEYCHAIN_ACCOUNT}" -s "${KEYCHAIN_SERVICE}" -w)"
if [[ -z "${TOKEN}" ]]; then
  echo "DSO GPU Resource Agent token is missing from Keychain" >&2
  exit 1
fi

export PYTHONPATH="${ROOT}/src"
export DSO_ROOT="${ROOT}"
export DSO_MODEL_SCHEDULER_DB_PATH="${ROOT}/data/db/model_scheduler.sqlite3"
export DSO_MODEL_SCHEDULER_ENABLED=1
export DSO_MODEL_RESOURCE_ID="${DSO_MODEL_RESOURCE_ID:-gpu:0}"
export DSO_MODEL_PREP_WORKERS="${DSO_MODEL_PREP_WORKERS:-2}"
export DSO_MODEL_MAX_PARENT_BURST="${DSO_MODEL_MAX_PARENT_BURST:-4}"
export DSO_MODEL_MAX_CONSECUTIVE_ITEMS="${DSO_MODEL_MAX_CONSECUTIVE_ITEMS:-4}"
export DSO_MODEL_PROFILE_READY_TIMEOUT_SECONDS="${DSO_MODEL_PROFILE_READY_TIMEOUT_SECONDS:-1800}"
export DSO_GPU_RESOURCE_AGENT_URL="${DSO_GPU_RESOURCE_AGENT_URL:-http://192.168.31.143:8010}"
export DSO_GPU_RESOURCE_AGENT_TOKEN="${TOKEN}"
export DSO_GPU_RESOURCE_AGENT_HEALTH_TIMEOUT_SECONDS="${DSO_GPU_RESOURCE_AGENT_HEALTH_TIMEOUT_SECONDS:-5}"
export DSO_GPU_RESOURCE_AGENT_ACTIVATION_TIMEOUT_SECONDS="${DSO_GPU_RESOURCE_AGENT_ACTIVATION_TIMEOUT_SECONDS:-1800}"

case "${MODE}" in
  worker)
    exec "${PYTHON}" -m dso.cli model-worker --resource "${DSO_MODEL_RESOURCE_ID}" --poll-seconds 0.5
    ;;
  web)
    exec "${PYTHON}" -m dso.cli web --host "${DSO_LAN_WEB_HOST:-127.0.0.1}" --port "${DSO_LAN_WEB_PORT:-8127}"
    ;;
  *)
    echo "Usage: $0 worker|web" >&2
    exit 2
    ;;
esac
