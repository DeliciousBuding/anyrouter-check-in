#!/usr/bin/env bash
set -euo pipefail

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PID_FILE="${PROXY_DIR}/mihomo.pid"

if [[ -f "${PID_FILE}" ]]; then
	echo "[INFO] Stopping mihomo proxy (pid $(cat "${PID_FILE}") )"
	kill "$(cat "${PID_FILE}")" 2>/dev/null || true
	rm -f "${PID_FILE}"
fi

# config.yaml 里是明文订阅 URL，subscription.yaml 是完整节点清单，mihomo.log 可能带
# 两者。runner 是 ephemeral 的，但落盘文件不该留到 job 结束之外。
rm -f "${PROXY_DIR}/config.yaml" "${PROXY_DIR}/subscription.yaml" "${PROXY_DIR}/mihomo.log"
