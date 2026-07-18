#!/usr/bin/env bash
set -Eeuo pipefail

# Run this on the target server.
#
# Common flow:
#   PROXY_URL=http://127.0.0.1:17892 ./scripts/server_prepare_qwen_omni.sh deps
#   PROXY_URL=http://127.0.0.1:17892 ./scripts/server_prepare_qwen_omni.sh download
#   ./scripts/server_prepare_qwen_omni.sh system-cuda
#   PROXY_URL=http://127.0.0.1:17892 ./scripts/server_prepare_qwen_omni.sh gptq
#   ./scripts/server_prepare_qwen_omni.sh env check

SERVICE_DIR="${SERVICE_DIR:-/home/aidev/dso_multimodal_model_service}"
VENV_DIR="${VENV_DIR:-${SERVICE_DIR}/.venv}"
PYTHON="${PYTHON:-${VENV_DIR}/bin/python}"
HF_HOME="${HF_HOME:-/home/aidev/models/huggingface}"
MODEL_ID="${MODEL_ID:-Qwen/Qwen2.5-Omni-7B-GPTQ-Int4}"
MODEL_DIR="${MODEL_DIR:-/home/aidev/models/Qwen2.5-Omni-7B-GPTQ-Int4}"
PROXY_URL="${PROXY_URL:-}"
GPTQMODEL_VERSION="${GPTQMODEL_VERSION:-2.0.0}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
FFMPEG_BIN_DIR="${FFMPEG_BIN_DIR:-/home/aidev/.local/opt/ffmpeg-official-8.1.2/bin}"
MAX_WORKERS="${MAX_WORKERS:-4}"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"

usage() {
  cat <<'EOF'
Usage:
  server_prepare_qwen_omni.sh [deps] [download] [system-cuda] [gptq] [env] [check]

Actions:
  deps         Install Python-side Omni runtime dependencies into the service venv.
  download     Download Qwen/Qwen2.5-Omni-7B-GPTQ-Int4 to MODEL_DIR.
  system-cuda  Install CUDA Toolkit 12.8 with sudo via NVIDIA's Ubuntu apt repo.
  gptq         Build/install gptqmodel after nvcc is available.
  env          Write qwen-omni.env for the service.
  check        Print dependency/model readiness.

Important env vars:
  SERVICE_DIR=/home/aidev/dso_multimodal_model_service
  PROXY_URL=http://127.0.0.1:17892
  MODEL_DIR=/home/aidev/models/Qwen2.5-Omni-7B-GPTQ-Int4
  FFMPEG_BIN_DIR=/home/aidev/.local/opt/ffmpeg-official-8.1.2/bin
  GPTQMODEL_VERSION=2.0.0
  HF_HUB_DISABLE_XET=1
EOF
}

export_proxy() {
  if [[ -n "${PROXY_URL}" ]]; then
    export HTTP_PROXY="${PROXY_URL}"
    export HTTPS_PROXY="${PROXY_URL}"
    export http_proxy="${PROXY_URL}"
    export https_proxy="${PROXY_URL}"
  fi
  export HF_HUB_DISABLE_XET
}

require_python() {
  if [[ ! -x "${PYTHON}" ]]; then
    echo "Missing Python venv at ${PYTHON}" >&2
    exit 1
  fi
}

install_deps() {
  require_python
  export_proxy
  cd "${SERVICE_DIR}"
  "${PYTHON}" -m pip install -U \
    "qwen-omni-utils==0.0.9" \
    soundfile \
    decord \
    librosa \
    ninja
}

download_model() {
  require_python
  export_proxy
  mkdir -p "${HF_HOME}" "${MODEL_DIR}"
  HF_HOME="${HF_HOME}" HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET}" "${PYTHON}" - <<PY
from huggingface_hub import snapshot_download

path = snapshot_download(
    repo_id="${MODEL_ID}",
    local_dir="${MODEL_DIR}",
    max_workers=${MAX_WORKERS},
)
print(path)
PY
}

install_system_cuda() {
  export_proxy

  if command -v nvcc >/dev/null 2>&1; then
    nvcc --version
    return 0
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to install CUDA Toolkit." >&2
    exit 1
  fi

  . /etc/os-release
  if [[ "${ID}" != "ubuntu" ]]; then
    echo "This helper currently supports Ubuntu only. Detected: ${ID}" >&2
    exit 1
  fi

  local repo_tag
  repo_tag="ubuntu${VERSION_ID//./}"
  local keyring
  keyring="$(mktemp /tmp/cuda-keyring.XXXXXX.deb)"
  local url="https://developer.download.nvidia.com/compute/cuda/repos/${repo_tag}/x86_64/cuda-keyring_1.1-1_all.deb"
  local sudo_cmd=(sudo)
  if [[ -n "${PROXY_URL}" ]]; then
    sudo_cmd=(sudo --preserve-env=HTTP_PROXY,HTTPS_PROXY,http_proxy,https_proxy)
  fi

  echo "Installing NVIDIA CUDA Toolkit 12.8. This requires your sudo password."
  wget -O "${keyring}" "${url}"
  "${sudo_cmd[@]}" dpkg -i "${keyring}"
  "${sudo_cmd[@]}" apt-get update
  "${sudo_cmd[@]}" apt-get install -y cuda-toolkit-12-8
}

install_gptq() {
  require_python
  export_proxy

  if ! command -v nvcc >/dev/null 2>&1 && [[ ! -x "${CUDA_HOME}/bin/nvcc" ]]; then
    cat >&2 <<EOF
nvcc was not found. Install CUDA Toolkit first, then rerun:
  ./scripts/server_prepare_qwen_omni.sh system-cuda
  PROXY_URL=${PROXY_URL:-http://127.0.0.1:17892} ./scripts/server_prepare_qwen_omni.sh gptq
EOF
    exit 1
  fi

  if [[ -x "${CUDA_HOME}/bin/nvcc" ]]; then
    export PATH="${CUDA_HOME}/bin:${PATH}"
  else
    CUDA_HOME="$(dirname "$(dirname "$(command -v nvcc)")")"
  fi
  export CUDA_HOME

  cd "${SERVICE_DIR}"
  "${PYTHON}" -m pip install --no-build-isolation "gptqmodel==${GPTQMODEL_VERSION}"
}

write_env() {
  mkdir -p "${SERVICE_DIR}"
  cat > "${SERVICE_DIR}/qwen-omni.env" <<EOF
DSO_MODEL_BACKEND=qwen_omni
DSO_MODEL_ID=${MODEL_ID}
DSO_MODEL_LOCAL_PATH=${MODEL_DIR}
DSO_MODEL_LOCKED=1
DSO_OMNI_LOW_VRAM=1
DSO_OMNI_ATTN=sdpa
DSO_OMNI_TEXT_ONLY=1
DSO_OMNI_USE_CACHE=1
DSO_OMNI_VIDEO_FPS=0.35
DSO_OMNI_VIDEO_MIN_PIXELS=12544
DSO_OMNI_VIDEO_MAX_PIXELS=50176
DSO_OMNI_MEDIA_MAX_NEW_TOKENS=128
DSO_OMNI_INFERENCE_TIMEOUT_SECONDS=150
DSO_OMNI_RESTART_ON_TIMEOUT=1
DSO_OMNI_RESTART_GRACE_SECONDS=3
HF_HOME=${HF_HOME}
HF_HUB_OFFLINE=1
PATH=${FFMPEG_BIN_DIR}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin
EOF
  echo "Wrote ${SERVICE_DIR}/qwen-omni.env"
}

check_ready() {
  require_python
  cd "${SERVICE_DIR}"
  "${PYTHON}" - <<'PY'
import importlib.util
from pathlib import Path

mods = [
    "torch",
    "transformers",
    "accelerate",
    "qwen_omni_utils",
    "decord",
    "soundfile",
    "librosa",
    "gptqmodel",
]
for name in mods:
    print(f"{name}: {bool(importlib.util.find_spec(name))}")
PY
  echo "MODEL_DIR=${MODEL_DIR}"
  if [[ -d "${MODEL_DIR}" ]]; then
    du -sh "${MODEL_DIR}" || true
    find "${MODEL_DIR}" -maxdepth 1 -type f | sed -n '1,40p'
  else
    echo "model dir missing"
  fi
  if command -v nvcc >/dev/null 2>&1; then
    nvcc --version | sed -n '1,4p'
  else
    echo "nvcc: false"
  fi
  if [[ -x "${FFMPEG_BIN_DIR}/ffmpeg" ]]; then
    "${FFMPEG_BIN_DIR}/ffmpeg" -version | sed -n '1,2p'
  elif command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -version | sed -n '1,2p'
  else
    echo "ffmpeg: false"
  fi
}

if [[ "$#" -eq 0 ]]; then
  usage
  exit 0
fi

for action in "$@"; do
  case "${action}" in
    deps) install_deps ;;
    download) download_model ;;
    system-cuda) install_system_cuda ;;
    gptq) install_gptq ;;
    env) write_env ;;
    check) check_ready ;;
    -h|--help|help) usage ;;
    *)
      echo "Unknown action: ${action}" >&2
      usage >&2
      exit 2
      ;;
  esac
done
