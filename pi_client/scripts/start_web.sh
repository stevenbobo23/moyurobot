#!/usr/bin/env bash
# ============================================================================
# Web 控制器启动脚本 —— 底层实现，日常请直接用下面两个入口：
#
#   ./scripts/start_web_https.sh   # https://<本机IP>:8443（自签证书，默认）
#   ./scripts/start_web_http.sh    # http://<本机IP>:8080（旧端口，给反代终止 TLS）
#
# 直接调本脚本也等价于 https 入口，下面的环境变量可用于临时覆盖：
#
#   sudo -E WEB_PORT=443 ./scripts/start_web_https.sh      # 想用 443（无需端口号）
#   WEB_HTTPS=0 ./scripts/start_web.sh                     # 临时退回纯 HTTP
#   ./scripts/start_web_https.sh --enable-stream           # 其余参数原样透传
#
# 可用环境变量：
#   WEB_HOST            监听地址，默认 0.0.0.0
#   WEB_PORT            端口，默认 8443（HTTPS）/ 8080（HTTP）
#   WEB_HTTPS           auto（默认，有证书就 HTTPS）| 1 强制 | 0 关闭
#   WEB_SSL_CERT/KEY    证书与私钥，默认 certs/robot.crt / certs/robot.key
#   WEB_REGENERATE_CERT 1 = 证书 SAN 不含当前 IP 时自动重签
#   PYTHON              解释器，默认按 conda 环境 / python3 自动挑选
#   ROBOT_ID / WEB_PASSWORD  同原脚本
#
# 为什么默认开 HTTPS：Web Serial、麦克风、crypto.subtle 都是 secure context
# 专属 API，局域网 http://<IP> 下 Chrome 会直接删掉 navigator.serial，
# 表现为"此浏览器不支持 Web Serial"。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"
export WEB_PASSWORD="${WEB_PASSWORD:-moyu123}"
export ROBOT_ID="${ROBOT_ID:-my_awesome_kiwi}"

# 解释器：显式 PYTHON 优先；否则按 当前 conda 环境 > lerobot 环境 > python3 挑，
# 免得忘了 conda activate 就跑，报一堆 ModuleNotFoundError
pick_python() {
    if [ -n "${PYTHON:-}" ]; then echo "${PYTHON}"; return; fi
    if [ -n "${CONDA_PREFIX:-}" ] && [ -x "${CONDA_PREFIX}/bin/python" ]; then
        echo "${CONDA_PREFIX}/bin/python"; return
    fi
    for c in "$HOME/miniconda3/envs/lerobot/bin/python" \
             "$HOME/anaconda3/envs/lerobot/bin/python"; do
        [ -x "$c" ] && { echo "$c"; return; }
    done
    command -v python3 >/dev/null 2>&1 && { echo python3; return; }
    echo python
}
PYTHON="$(pick_python)"
WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_HTTPS="${WEB_HTTPS:-auto}"
CERT="${WEB_SSL_CERT:-certs/robot.crt}"
KEY="${WEB_SSL_KEY:-certs/robot.key}"

# 未显式指定 WEB_PORT 时：HTTPS 用 8443，HTTP 用 8080
if [ -z "${WEB_PORT:-}" ]; then
    if [ "${WEB_HTTPS}" = "0" ]; then WEB_PORT=8080; else WEB_PORT=8443; fi
fi

# ---------------------------------------------------------------- 网卡 IP
detect_lan_ip() {
    local ip=""
    if command -v ip >/dev/null 2>&1; then
        # 取默认路由对应的源地址，比 hostname -I 更准（不会挑到 docker0）
        ip=$(ip -4 route get 1.1.1.1 2>/dev/null \
             | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i+1); exit}}')
    fi
    [ -z "$ip" ] && ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$ip" ] && ip=$(ipconfig getifaddr en0 2>/dev/null || true)
    echo "$ip"
}
LAN_IP="$(detect_lan_ip)"
[ -z "${LAN_IP}" ] && LAN_IP="127.0.0.1"

# ---------------------------------------------------------------- 证书处理
cert_has_ip() {
    openssl x509 -in "$1" -noout -text 2>/dev/null \
        | awk '/Subject Alternative Name/{getline; print}' \
        | grep -q "IP Address:$2"
}

gen_cert() {
    echo "🔐 生成自签证书（SAN: ${LAN_IP}）..."
    # gen 脚本固定输出 robot.crt / robot.key，所以只取目录
    if DAYS=825 OUT_DIR="$(dirname "${CERT}")" "$SCRIPT_DIR/gen_self_signed_cert.sh" "${LAN_IP}" >/dev/null; then
        echo "   证书: ${CERT}"
    else
        echo "   ❌ 生成失败，请手动跑：./scripts/gen_self_signed_cert.sh ${LAN_IP}"
    fi
}

# auto 的语义是"能开就开"：证书没有就现生成，只有显式 WEB_HTTPS=0 才关
USE_HTTPS=1
case "$WEB_HTTPS" in
    0|no|false|off) USE_HTTPS=0 ;;
esac

if [ "$USE_HTTPS" = "1" ]; then
    if ! command -v openssl >/dev/null 2>&1; then
        echo "⚠️  没有 openssl，无法生成/校验证书，本次降级为 HTTP"
        echo "   安装：sudo apt install -y openssl"
        USE_HTTPS=0
    elif [ ! -f "${CERT}" ] || [ ! -f "${KEY}" ]; then
        gen_cert
    elif ! cert_has_ip "${CERT}" "${LAN_IP}"; then
        echo "⚠️  证书 SAN 里没有当前 IP ${LAN_IP}（换过 IP / 换过网段），浏览器会报证书错误"
        if [ "${WEB_REGENERATE_CERT:-0}" = "1" ]; then
            gen_cert
        else
            echo "   重签：WEB_REGENERATE_CERT=1 ./scripts/start_web_https.sh"
        fi
    fi

    # 生成失败就别硬撑，降级 HTTP 也比起不来强
    if [ "$USE_HTTPS" = "1" ] && { [ ! -f "${CERT}" ] || [ ! -f "${KEY}" ]; }; then
        echo "⚠️  证书缺失（${CERT}），本次降级为 HTTP"
        USE_HTTPS=0
    fi
    if [ "$USE_HTTPS" = "1" ] && [ ! -r "${KEY}" ]; then
        echo "❌ 私钥不可读: ${KEY}（检查属主，或 chmod 600）"
        exit 1
    fi
fi

# ---------------------------------------------------------------- 端口检查
if command -v ss >/dev/null 2>&1; then
    if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${WEB_PORT}$"; then
        echo "⚠️  端口 ${WEB_PORT} 已被占用，占用者："
        ss -tlnp 2>/dev/null | grep -E "[:.]${WEB_PORT}\s" || true
        echo "   先停掉旧进程，或换 WEB_PORT=xxxx 启动"
        echo
    fi
fi
if [ "${WEB_PORT}" -lt 1024 ] && [ "$(id -u)" -ne 0 ]; then
    echo "❌ 端口 ${WEB_PORT} < 1024 需要 root 权限：sudo -E ./scripts/start_web_https.sh"
    exit 1
fi

# ---------------------------------------------------------------- 启动
SCHEME="http"; [ "$USE_HTTPS" = "1" ] && SCHEME="https"

# 依赖自检：选错解释器是这个项目最常见的启动失败原因
if ! "${PYTHON}" -c "import flask" >/dev/null 2>&1; then
    echo "⚠️  ${PYTHON} 里 import flask 失败，多半是选错环境"
    echo "   手动指定：PYTHON=/home/bobo/miniconda3/envs/lerobot/bin/python $0"
    echo
fi

echo "🌐 启动 Web 控制器"
echo "   访问地址: $SCHEME://${LAN_IP}:${WEB_PORT}/"
echo "   本机访问: $SCHEME://localhost:${WEB_PORT}/"
echo "   默认密码: $WEB_PASSWORD"
echo "   机器人ID: ${ROBOT_ID}"
echo "   HTTPS:    $([ "$USE_HTTPS" = "1" ] && echo "启用（${CERT}）" || echo "未启用 —— Web Serial / 麦克风不可用")"
echo "   项目路径: $PROJECT_ROOT"

CALIB_FILE="$HOME/.cache/huggingface/lerobot/calibration/robots/lekiwi/${ROBOT_ID}.json"
if [ -f "$CALIB_FILE" ]; then
    echo "   ✓ 校准文件: $CALIB_FILE"
else
    echo "   ⚠️  未找到校准文件: $CALIB_FILE"
    CALIB_DIR="$(dirname "$CALIB_FILE")"
    if [ -d "$CALIB_DIR" ]; then
        echo "      已有:"
        ls -1 "$CALIB_DIR"/*.json 2>/dev/null | sed 's/^/        /' || echo "        (无)"
    fi
fi
echo

ARGS=(--host "${WEB_HOST}" --port "${WEB_PORT}" --robot-id "${ROBOT_ID}")
if [ "$USE_HTTPS" = "1" ]; then
    ARGS+=(--ssl-cert "${CERT}" --ssl-key "${KEY}")
fi
ARGS+=("$@")

echo "▶ ${PYTHON} -m moyurobot.web.controller ${ARGS[*]}"
echo
exec "${PYTHON}" -m moyurobot.web.controller "${ARGS[@]}"
