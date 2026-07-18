#!/usr/bin/env bash
set -euo pipefail

SERVICE_DIR="${SERVICE_DIR:-/home/aidev/dso_qwen3_asr_service}"
MODEL_ROOT="${MODEL_ROOT:-/home/aidev/models/Qwen3-ASR}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
ACTION="${1:-all}"

install_env() {
  mkdir -p "${SERVICE_DIR}" "${MODEL_ROOT}"
  if [[ ! -x "${SERVICE_DIR}/.venv/bin/python" ]]; then
    "${PYTHON_BIN}" -m venv "${SERVICE_DIR}/.venv"
  fi
  "${SERVICE_DIR}/.venv/bin/python" -m pip install -U pip 'setuptools<82' wheel
  local omni_site="/home/aidev/dso_multimodal_model_service/.venv/lib/python3.12/site-packages"
  local asr_site="${SERVICE_DIR}/.venv/lib/python3.12/site-packages"
  if [[ -d "${omni_site}/torch" && -d "${omni_site}/nvidia" ]]; then
    local pattern path name
    for pattern in \
      cuda cuda_bindings-*.dist-info cuda_pathfinder-*.dist-info cuda_toolkit-*.dist-info \
      functorch nvidia nvidia_*.dist-info torch torch-*.dist-info torchgen triton triton-*.dist-info; do
      for path in "${omni_site}"/${pattern}; do
        [[ -e "${path}" ]] || continue
        name="${path##*/}"
        [[ -e "${asr_site}/${name}" ]] || cp -al "${path}" "${asr_site}/${name}"
      done
    done
    "${SERVICE_DIR}/.venv/bin/python" -m pip install \
      -i https://mirrors.aliyun.com/pypi/simple/ \
      typing-extensions filelock sympy networkx jinja2 fsspec mpmath MarkupSafe
  else
    "${SERVICE_DIR}/.venv/bin/python" -m pip install \
      --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0
  fi
  "${SERVICE_DIR}/.venv/bin/python" -m pip install \
    -i https://mirrors.aliyun.com/pypi/simple/ --no-deps qwen-asr==0.0.6
  "${SERVICE_DIR}/.venv/bin/python" -m pip install \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    fastapi uvicorn python-multipart transformers==4.57.6 accelerate==1.12.0 \
    qwen-omni-utils==0.0.9 librosa soundfile sox nagisa==0.2.11 soynlp==0.0.493 pytz modelscope
}

download_models() {
  export HF_HOME="${HF_HOME:-/home/aidev/models/huggingface}"
  MODELSCOPE_DOWNLOAD_PARALLEL_WORKERS="${MODELSCOPE_DOWNLOAD_PARALLEL_WORKERS:-16}" \
  MODELSCOPE_DOWNLOAD_PARALLEL_THRESHOLD_MB="${MODELSCOPE_DOWNLOAD_PARALLEL_THRESHOLD_MB:-50}" \
  MODELSCOPE_DOWNLOAD_TIMEOUT="${MODELSCOPE_DOWNLOAD_TIMEOUT:-300}" \
  "${SERVICE_DIR}/.venv/bin/modelscope" download --model Qwen/Qwen3-ASR-1.7B \
    --local_dir "${MODEL_ROOT}/Qwen3-ASR-1.7B"
  MODELSCOPE_DOWNLOAD_PARALLEL_WORKERS="${MODELSCOPE_DOWNLOAD_PARALLEL_WORKERS:-16}" \
  MODELSCOPE_DOWNLOAD_PARALLEL_THRESHOLD_MB="${MODELSCOPE_DOWNLOAD_PARALLEL_THRESHOLD_MB:-50}" \
  MODELSCOPE_DOWNLOAD_TIMEOUT="${MODELSCOPE_DOWNLOAD_TIMEOUT:-300}" \
  "${SERVICE_DIR}/.venv/bin/modelscope" download --model Qwen/Qwen3-ForcedAligner-0.6B \
    --local_dir "${MODEL_ROOT}/Qwen3-ForcedAligner-0.6B"
}

write_config() {
  cat > "${SERVICE_DIR}/qwen3-asr.env" <<EOF
QWEN3_ASR_MODEL=${MODEL_ROOT}/Qwen3-ASR-1.7B
QWEN3_ASR_ALIGNER=${MODEL_ROOT}/Qwen3-ForcedAligner-0.6B
QWEN3_ASR_DEVICE=cuda:0
QWEN3_ASR_ALIGNER_DEVICE=cuda:0
QWEN3_ASR_DTYPE=bfloat16
QWEN3_ASR_ATTN=sdpa
QWEN3_ASR_MAX_BATCH=1
QWEN3_ASR_MAX_NEW_TOKENS=1024
QWEN3_ASR_MAX_AUDIO_SECONDS=300
QWEN3_ASR_MAX_UPLOAD_MB=32
QWEN3_ASR_MIN_FREE_GPU_GB=10
QWEN3_ASR_TIMESTAMPS=1
QWEN3_ASR_AUTOLOAD=0
HF_HOME=/home/aidev/models/huggingface
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
EOF

  mkdir -p /home/aidev/.config/systemd/user
  cat > /home/aidev/.config/systemd/user/dso-qwen3-asr.service <<EOF
[Unit]
Description=DSO Qwen3 ASR Service
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${SERVICE_DIR}
EnvironmentFile=${SERVICE_DIR}/qwen3-asr.env
ExecStart=${SERVICE_DIR}/.venv/bin/uvicorn qwen3_asr_service:app --app-dir ${SERVICE_DIR} --host 0.0.0.0 --port 8002
Restart=on-failure
RestartSec=3
TimeoutStopSec=20

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable dso-qwen3-asr.service

  mkdir -p /home/aidev/bin
  cat > /home/aidev/bin/dso-asr-on <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
trap '/home/aidev/bin/dso-omni-on >/dev/null 2>&1 || true' ERR
systemctl --user stop dso-multimodal-model.service
systemctl --user start dso-qwen3-asr.service
python3 - <<'PY'
import json
import time
from urllib.request import ProxyHandler, Request, build_opener

opener = build_opener(ProxyHandler({}))
for _ in range(60):
    try:
        with opener.open("http://127.0.0.1:8002/health", timeout=2) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("Qwen3-ASR service did not start")
request = Request(
    "http://127.0.0.1:8002/load",
    data=b'{"force":false}',
    method="POST",
    headers={"Content-Type": "application/json"},
)
with opener.open(request, timeout=1800) as response:
    print(json.loads(response.read().decode()))
PY
EOF
  chmod +x /home/aidev/bin/dso-asr-on

  cat > /home/aidev/bin/dso-omni-on <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
from urllib.request import ProxyHandler, Request, build_opener

try:
    request = Request("http://127.0.0.1:8002/unload", data=b"{}", method="POST")
    build_opener(ProxyHandler({})).open(request, timeout=120).read()
except Exception:
    pass
PY
systemctl --user start dso-qwen3-asr.service
systemctl --user restart dso-multimodal-model.service
python3 - <<'PY'
import json
import time
from urllib.request import ProxyHandler, Request, build_opener

opener = build_opener(ProxyHandler({}))
for _ in range(60):
    try:
        with opener.open("http://127.0.0.1:8001/health", timeout=2) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("Omni service did not start")
request = Request(
    "http://127.0.0.1:8001/load",
    data=(
        b'{"model_id":"Qwen/Qwen2.5-Omni-7B-GPTQ-Int4",'
        b'"backend":"qwen_omni","low_vram":true}'
    ),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with opener.open(request, timeout=1800) as response:
    print(json.loads(response.read().decode()))
PY
EOF
  chmod +x /home/aidev/bin/dso-omni-on
}

case "${ACTION}" in
  env) install_env ;;
  download) download_models ;;
  config) write_config ;;
  all)
    install_env
    download_models
    write_config
    ;;
  *)
    echo "Usage: $0 [env|download|config|all]" >&2
    exit 2
    ;;
esac
