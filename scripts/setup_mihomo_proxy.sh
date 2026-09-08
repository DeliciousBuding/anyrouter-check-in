#!/usr/bin/env bash
# 通过 mihomo 拉取订阅、启动本地代理并探测可用节点。
# 环境变量:
#   PROXY_SUBSCRIPTION_URL  订阅链接（必填才启用）
#   PROXY_TEST_URL          探测目标，默认 https://www.google.com/generate_204
#   PROXY_NODE_FILTER       节点名正则，把出口收窄到指定地区（不设则全订阅节点参与选路）
#   PROXY_PROBE_URL         业务侧探测目标，用于判断该出口是否被目标站点下发人机验证
#   PROXY_REQUIRED          true 时探测失败则退出 1
#   PROXY_PORT              本地 mixed-port，默认 7890
#
# 日志纪律（本仓是公开仓，Actions 日志任何人可读）:
#   订阅 URL、节点名、节点服务器地址一律不得进日志。mihomo 的 warning 会带这些内容，
#   因此失败时只输出错误分类，不 tail 原始日志；GitHub 的 secret mask 只匹配原值，
#   URL 经重定向/编码/截断变形后会漏，脱敏必须在脚本内自己做。

set -euo pipefail

if [[ -z "${PROXY_SUBSCRIPTION_URL:-}" ]]; then
	echo "[INFO] PROXY_SUBSCRIPTION_URL not set, skip proxy setup"
	exit 0
fi

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_TEST_URL="${PROXY_TEST_URL:-https://www.google.com/generate_204}"
PROXY_NODE_FILTER="${PROXY_NODE_FILTER:-}"
PROXY_PROBE_URL="${PROXY_PROBE_URL:-}"
MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.0}"
PROXY_REQUIRED="${PROXY_REQUIRED:-false}"
# 本地控制面只用于自检选路结果；口令每次随机生成，不来自 secret、也不落日志
PROXY_API_PORT="${PROXY_API_PORT:-9097}"
PROXY_API_SECRET="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

mkdir -p "${PROXY_DIR}"
cd "${PROXY_DIR}"

echo "[INFO] Downloading mihomo ${MIHOMO_VERSION}..."
ARCHIVE="mihomo-linux-amd64-${MIHOMO_VERSION}.gz"
if ! curl --retry 3 --retry-delay 5 --retry-all-errors -fsSL -o "${ARCHIVE}" \
	"https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/${ARCHIVE}"; then
	echo "[WARN] Failed to download mihomo ${MIHOMO_VERSION}, skip proxy setup"
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi
gunzip -f "${ARCHIVE}"
chmod +x "mihomo-linux-amd64-${MIHOMO_VERSION}"
MIHOMO_BIN="${PROXY_DIR}/mihomo-linux-amd64-${MIHOMO_VERSION}"

# filter 为空时不写该行，避免 mihomo 拿到空正则
GROUP_FILTER_YAML=""
if [[ -n "${PROXY_NODE_FILTER}" ]]; then
	GROUP_FILTER_YAML="
    filter: '${PROXY_NODE_FILTER}'"
	echo "[INFO] Node filter applied (value withheld: it reveals the egress region)"
else
	echo "[WARN] PROXY_NODE_FILTER not set; egress node is whatever url-test picks fastest"
fi

cat > config.yaml <<EOF
mixed-port: ${PROXY_PORT}
allow-lan: false
ipv6: false
mode: rule
log-level: warning
unified-delay: true
external-controller: 127.0.0.1:${PROXY_API_PORT}
secret: "${PROXY_API_SECRET}"

proxy-providers:
  subscription:
    type: http
    url: "${PROXY_SUBSCRIPTION_URL}"
    interval: 3600
    path: ./subscription.yaml
    health-check:
      enable: true
      interval: 60
      url: https://www.gstatic.com/generate_204

proxy-groups:
  - name: CHECKIN
    type: url-test
    url: "${PROXY_TEST_URL}"
    interval: 60
    tolerance: 150
    lazy: false${GROUP_FILTER_YAML}
    use:
      - subscription

rules:
  - MATCH,CHECKIN
EOF

echo "[INFO] Starting mihomo on 127.0.0.1:${PROXY_PORT}..."
nohup "${MIHOMO_BIN}" -d "${PROXY_DIR}" -f config.yaml > mihomo.log 2>&1 &
echo $! > mihomo.pid

PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
READY=false
for attempt in $(seq 1 45); do
	if curl -fsS -x "${PROXY_URL}" --max-time 20 "${PROXY_TEST_URL}" -o /dev/null 2>/dev/null; then
		READY=true
		break
	fi
	echo "[INFO] Waiting for proxy health check (${attempt}/45)..."
	sleep 2
done

if [[ "${READY}" != "true" ]]; then
	echo "[FAILED] Proxy health check failed for ${PROXY_TEST_URL}"
	# 只给错误分类：原始日志含订阅 URL 与节点清单，公开仓不外发
	ERROR_CLASS="unknown"
	if grep -qiE 'connection refused|dial tcp.*refused' mihomo.log; then
		ERROR_CLASS="connection-refused"
	elif grep -qiE 'no such host|server misbehavior|dns' mihomo.log; then
		ERROR_CLASS="dns"
	elif grep -qiE '401|403|unauthorized|forbidden' mihomo.log; then
		ERROR_CLASS="subscription-auth"
	elif grep -qiE 'timeout|deadline exceeded|EOF' mihomo.log; then
		ERROR_CLASS="timeout-or-reset"
	elif grep -qiE 'no proxy|empty|parse' mihomo.log; then
		ERROR_CLASS="subscription-empty-or-unparsable"
	fi
	echo "[INFO] mihomo error class: ${ERROR_CLASS} (raw log withheld by design)"
	if [[ -f mihomo.pid ]]; then
		kill "$(cat mihomo.pid)" 2>/dev/null || true
	fi
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi

echo "[SUCCESS] Proxy is ready: ${PROXY_URL}"
echo "[INFO] Proxy is scoped to CHECKIN_PROXY_URL (browser/python only, not global HTTP_PROXY)"

# 出口自检：只打印国家/POP，不打印出口 IP——公开日志里钉一个 IP 等于给目标站
# 一份「这个 IP 每 6 小时来登录 4 个号」的清单。
TRACE="$(curl -fsS -x "${PROXY_URL}" --max-time 20 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null || true)"
if [[ -n "${TRACE}" ]]; then
	EXIT_LOC="$(printf '%s\n' "${TRACE}" | sed -n 's/^loc=//p' | head -n1)"
	EXIT_COLO="$(printf '%s\n' "${TRACE}" | sed -n 's/^colo=//p' | head -n1)"
	echo "[INFO] Egress check: loc=${EXIT_LOC:-unknown} colo=${EXIT_COLO:-unknown} (ip withheld)"
else
	echo "[WARN] Egress check unavailable (cloudflare trace failed through proxy)"
fi

# 选路自检：只报数量与哈希，不报节点名——节点名会同时暴露机场和出口地区。
# candidates 与 filter_matched 用来判断 filter 是否真的收窄了候选集：
# 两者相等且等于全订阅节点数即说明 filter 没生效。
GROUP_JSON="$(curl -fsS --max-time 15 -H "Authorization: Bearer ${PROXY_API_SECRET}" \
	"http://127.0.0.1:${PROXY_API_PORT}/proxies/CHECKIN" 2>/dev/null || true)"
if [[ -n "${GROUP_JSON}" ]]; then
	printf '%s' "${GROUP_JSON}" | python3 -c '
import hashlib, json, re, sys

pattern = sys.argv[1]
data = json.load(sys.stdin)
selected = data.get("now") or ""
candidates = data.get("all") or []
matched = sum(1 for name in candidates if re.search(pattern, name)) if pattern else len(candidates)
# 用集合成员判断选中项是否合规：对 now 再跑一次正则会因为 mihomo 内部命名与
# all 列表的表示形式不一致而误报 False。
selected_in_candidates = selected in candidates if selected else None
digest = hashlib.sha256(selected.encode()).hexdigest()[:8]
print(f"[INFO] Proxy group: candidates={len(candidates)} filter_matched={matched} "
      f"selected_in_candidates={selected_in_candidates} selected_sha={digest}")
' "${PROXY_NODE_FILTER}" || echo "[WARN] Proxy group self-check could not be parsed"
else
	echo "[WARN] Proxy group self-check unavailable (mihomo API not reachable)"
fi

# 业务侧探测：用普通 HTTP 客户端看这个出口对目标站是否被下发人机验证页。
# 判据偏保守：探测用的是 curl，没有浏览器指纹。2026-09-08 实测 probe 报挑战页的同时，
# CloakBrowser 走同一出口登录成功——所以 WARN 只代表「裸 HTTP 客户端过不去」，
# 不代表浏览器路由会失败，别据此判定 provider 不可用。
if [[ -n "${PROXY_PROBE_URL}" ]]; then
	PROBE_BODY="$(curl -sS -x "${PROXY_URL}" --compressed --max-time 30 \
		-A 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36' \
		"${PROXY_PROBE_URL}" 2>/dev/null || true)"
	PROBE_BYTES="${#PROBE_BODY}"
	if printf '%s' "${PROBE_BODY}" | grep -qiE 'Access Verification|slide to complete|please slide|请进行验证|为了更好的访问体验'; then
		echo "[WARN] Probe: plain HTTP client got a verification challenge (${PROBE_BYTES} bytes); browser route may still pass"
	elif [[ "${PROBE_BYTES}" -gt 0 ]]; then
		echo "[SUCCESS] Probe: target responded through the egress, no challenge page (${PROBE_BYTES} bytes)"
	else
		echo "[WARN] Probe: empty response from target through this egress"
	fi
fi

if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "CHECKIN_PROXY_URL=${PROXY_URL}" >> "${GITHUB_ENV}"
fi
