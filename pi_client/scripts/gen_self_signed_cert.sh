#!/usr/bin/env bash
# ============================================================================
# 生成自签 HTTPS 证书（浏览器申请麦克风权限需要安全上下文）
#
#     ./scripts/gen_self_signed_cert.sh 192.168.1.50
#
# 生成的证书含 SAN（IP + localhost），然后用以下方式启动服务：
#     python -m moyurobot.web.controller \
#         --ssl-cert certs/robot.crt --ssl-key certs/robot.key
#
# 首次访问浏览器会报"证书不受信任"，点"高级 → 继续前往"即可；
# 之后该 origin 就属于安全上下文，麦克风（按住说话）可用。
# ============================================================================
set -eu

HOST_IP="${1:-}"
DAYS="${DAYS:-825}"
OUT_DIR="${OUT_DIR:-certs}"

if ! command -v openssl >/dev/null 2>&1; then
    echo "❌ 需要 openssl：sudo apt install -y openssl"
    exit 1
fi

if [ -z "$HOST_IP" ]; then
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
if [ -z "$HOST_IP" ]; then
    echo "用法: $0 <机器IP>   （例: $0 192.168.1.50）"
    exit 1
fi

mkdir -p "$OUT_DIR"
KEY="$OUT_DIR/robot.key"
CRT="$OUT_DIR/robot.crt"
CONF="$OUT_DIR/openssl.cnf"

cat >"$CONF" <<EOF
[req]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
x509_extensions    = v3_req

[dn]
CN = moyurobot.local

[v3_req]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
IP.1 = $HOST_IP
IP.2 = 127.0.0.1
DNS.1 = localhost
DNS.2 = moyurobot.local
EOF

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$KEY" -out "$CRT" -days "$DAYS" -config "$CONF" >/dev/null 2>&1

chmod 600 "$KEY"
echo "✅ 已生成自签证书（有效期 $DAYS 天，SAN 含 $HOST_IP / 127.0.0.1 / localhost）"
echo "   证书: $CRT"
echo "   私钥: $KEY"
echo
echo "启动："
echo "   python -m moyurobot.web.controller --port 8443 --ssl-cert $CRT --ssl-key $KEY"
echo "访问："
echo "   https://$HOST_IP:8443/   （首次需手动信任证书）"
