#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${DSO_LAN_ROOT:-/Users/fuqiang/Dev/douyin-slice-optimizer}"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
LOG_DIR="${ROOT}/logs"
UID_VALUE="$(id -u)"
DOMAIN="gui/${UID_VALUE}"
RUNNER="${ROOT}/scripts/run_lan_service.sh"
KEYCHAIN_SERVICE="${DSO_LAN_KEYCHAIN_SERVICE:-dso-gpu-resource-agent}"
KEYCHAIN_ACCOUNT="${DSO_LAN_KEYCHAIN_ACCOUNT:-$(id -un)}"

if [[ ! -x "${RUNNER}" ]]; then
  echo "LAN service runner is not executable: ${RUNNER}" >&2
  exit 1
fi
if ! /usr/bin/security find-generic-password -a "${KEYCHAIN_ACCOUNT}" -s "${KEYCHAIN_SERVICE}" -w >/dev/null; then
  echo "Store the GPU Resource Agent token in macOS Keychain before installing launchd services." >&2
  exit 1
fi

mkdir -p "${LAUNCH_AGENTS}" "${LOG_DIR}"

install_plist() {
  local label="$1"
  local mode="$2"
  local plist="${LAUNCH_AGENTS}/${label}.plist"
  cat > "${plist}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${RUNNER}</string>
    <string>${mode}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/${label}.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/${label}.err.log</string>
</dict>
</plist>
EOF
  /usr/bin/plutil -lint "${plist}" >/dev/null
  /bin/launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  /bin/launchctl bootstrap "${DOMAIN}" "${plist}"
}

install_plist "com.dso.lan-model-worker" "worker"
install_plist "com.dso.lan-web" "web"

/bin/launchctl kickstart -k "${DOMAIN}/com.dso.lan-model-worker"
/bin/launchctl kickstart -k "${DOMAIN}/com.dso.lan-web"
echo "DSO LAN scheduler services installed: com.dso.lan-model-worker, com.dso.lan-web"
