#!/usr/bin/env bash
# ============================================================================
# 纯 HTTP 启动（旧端口 8080，和加 HTTPS 之前完全一样）
#
#   ./scripts/start_web_http.sh              # http://<本机IP>:8080
#   WEB_PORT=9000 ./scripts/start_web_http.sh # 换端口
#   ./scripts/start_web_http.sh --enable-stream
#
# 什么时候用它：TLS 在 nginx / frp 那侧终止，Pi 只需吐明文。
#   nginx: proxy_pass http://127.0.0.1:8003;
#   frpc : [lerobot_uiweb] local_port = 8080
#
# 代价：局域网 http 属非安全上下文，浏览器会隐藏 navigator.serial，
# /sim 页的主臂联动和麦克风都用不了（公网经 nginx 的 https 访问不受影响）。
# 需要局域网直连也能用 Web Serial，请改用 start_web_https.sh。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export WEB_HTTPS=0
export WEB_PORT="${WEB_PORT:-8080}"

exec "${SCRIPT_DIR}/start_web.sh" "$@"
