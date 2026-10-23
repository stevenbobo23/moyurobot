#!/usr/bin/env bash
# ============================================================================
# HTTPS 启动（默认 8443，自签证书自动生成）
#
#   ./scripts/start_web_https.sh                       # https://<本机IP>:8443
#   sudo -E WEB_PORT=443 ./scripts/start_web_https.sh  # 用 443，不用带端口号
#   WEB_REGENERATE_CERT=1 ./scripts/start_web_https.sh # 换网段后重签证书
#   ./scripts/start_web_https.sh --enable-stream       # 其余参数原样透传
#
# 为什么默认 HTTPS：Web Serial（主臂联动）、麦克风、crypto.subtle 都是
# secure context 专属 API，局域网 http://<IP> 下 Chrome 会直接删掉
# navigator.serial，表现为"此浏览器不支持 Web Serial"。
#
# 证书来源：certs/robot.crt / certs/robot.key，不存在时自动调用
# gen_self_signed_cert.sh 按当前网卡 IP 签发（SAN 含本机 IP + 127.0.0.1）。
# 首次访问点"高级 → 继续前往"信任一次即可。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export WEB_HTTPS=1
export WEB_PORT="${WEB_PORT:-8443}"

exec "${SCRIPT_DIR}/start_web.sh" "$@"
