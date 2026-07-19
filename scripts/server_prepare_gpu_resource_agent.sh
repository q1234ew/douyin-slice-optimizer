#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_DIR="${SERVICE_DIR:-/home/aidev/dso_gpu_resource_agent}"
SOURCE_FILE="${SOURCE_FILE:-$(pwd)/scripts/gpu_resource_agent.py}"
PYTHON="${PYTHON:-/home/aidev/dso_multimodal_model_service/.venv/bin/python}"
PORT="${PORT:-8010}"
MODEL_SERVICE_DIR="${MODEL_SERVICE_DIR:-/home/aidev/dso_multimodal_model_service}"
EMBEDDING_MODEL_ID="${EMBEDDING_MODEL_ID:-Qwen/Qwen3-VL-Embedding-2B}"
HF_HOME="${HF_HOME:-/home/aidev/models/huggingface}"
EMBEDDING_MODEL_DIR="${EMBEDDING_MODEL_DIR:-${HF_HOME}/models--Qwen--Qwen3-VL-Embedding-2B}"

if [[ ! -f "${SOURCE_FILE}" ]]; then
  echo "Missing gpu_resource_agent.py at ${SOURCE_FILE}" >&2
  exit 1
fi

mkdir -p "${SERVICE_DIR}"
install -m 0644 "${SOURCE_FILE}" "${SERVICE_DIR}/gpu_resource_agent.py"

cat > "${SERVICE_DIR}/qwen-embedding.env" <<EOF
DSO_MODEL_BACKEND=sentence_transformers
DSO_MODEL_ID=${EMBEDDING_MODEL_ID}
DSO_MODEL_LOCAL_PATH=${EMBEDDING_MODEL_DIR}
DSO_MODEL_LOCKED=1
HF_HOME=${HF_HOME}
HF_HUB_OFFLINE=1
EOF

mkdir -p "${HOME}/.config/systemd/user" /home/aidev/bin
cat > "${HOME}/.config/systemd/user/dso-qwen-embedding.service" <<EOF
[Unit]
Description=DSO Qwen3 VL Embedding Service
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${MODEL_SERVICE_DIR}
EnvironmentFile=${SERVICE_DIR}/qwen-embedding.env
ExecStart=${MODEL_SERVICE_DIR}/.venv/bin/uvicorn app:app --app-dir ${MODEL_SERVICE_DIR} --host 0.0.0.0 --port 8001
Restart=on-failure
RestartSec=3
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF

cat > /home/aidev/bin/dso-agent-asr-on <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop dso-qwen-embedding.service >/dev/null 2>&1 || true
exec /home/aidev/bin/dso-asr-on
EOF

cat > /home/aidev/bin/dso-agent-omni-on <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop dso-qwen-embedding.service >/dev/null 2>&1 || true
exec /home/aidev/bin/dso-omni-on
EOF

cat > /home/aidev/bin/dso-agent-embedding-on <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop dso-multimodal-model.service dso-qwen3-asr.service >/dev/null 2>&1 || true
systemctl --user restart dso-qwen-embedding.service
python3 - <<'PY'
import json
import time
from urllib.request import ProxyHandler, Request, build_opener

opener = build_opener(ProxyHandler({}))
for _ in range(90):
    try:
        with opener.open("http://127.0.0.1:8001/health", timeout=2) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("Qwen embedding service did not start")
request = Request(
    "http://127.0.0.1:8001/load",
    data=b'{"model_id":"Qwen/Qwen3-VL-Embedding-2B","backend":"sentence_transformers"}',
    method="POST",
    headers={"Content-Type": "application/json"},
)
with opener.open(request, timeout=1800) as response:
    print(json.loads(response.read().decode()))
PY
EOF
chmod 0755 /home/aidev/bin/dso-agent-asr-on /home/aidev/bin/dso-agent-omni-on /home/aidev/bin/dso-agent-embedding-on

if [[ ! -f "${SERVICE_DIR}/resource-agent.env" ]]; then
  token="$(${PYTHON} -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > "${SERVICE_DIR}/resource-agent.env" <<EOF
DSO_GPU_RESOURCE_AGENT_TOKEN=${token}
DSO_GPU_AGENT_ASR_COMMAND=/home/aidev/bin/dso-agent-asr-on
DSO_GPU_AGENT_OMNI_COMMAND=/home/aidev/bin/dso-agent-omni-on
DSO_GPU_AGENT_EMBEDDING_COMMAND=/home/aidev/bin/dso-agent-embedding-on
EOF
  chmod 0600 "${SERVICE_DIR}/resource-agent.env"
  echo "Created ${SERVICE_DIR}/resource-agent.env; copy its token into the application host secret store."
fi

cat > "${HOME}/.config/systemd/user/dso-gpu-resource-agent.service" <<EOF
[Unit]
Description=DSO GPU Resource Agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${SERVICE_DIR}
EnvironmentFile=${SERVICE_DIR}/resource-agent.env
ExecStart=${PYTHON} -m uvicorn gpu_resource_agent:app --app-dir ${SERVICE_DIR} --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now dso-gpu-resource-agent.service
echo "gpu-resource-agent installed on port ${PORT}"
