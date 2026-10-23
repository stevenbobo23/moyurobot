# 公网访问：frp + nginx + HTTPS

面向「树莓派在局域网、公网服务器跑 frps + nginx」的部署方式。改完配置后，公网域名和局域网直连两条路都能用。

## 0. 端口全景

| 组件 | 端口 | 说明 |
|---|---|---|
| Web 服务（Flask） | Pi 本地 **8443**（HTTPS）或 **8080**（HTTP） | `/`、`/sim`、`/video_feed/<camera>`、`/audio/info` |
| 语音对讲（aiohttp WS） | Pi 本地 **8081**（明文 ws） | 路径必须 `/audio` 开头，`require_auth=true` |
| frpc → 公网 | 8003（对应网页）、8004（对应音频） | TCP 隧道，TLS 内容透传不解析 |
| nginx | 443 | `lerobot.mycodebro.cn`（网页）、`lerobotwws.mycodebro.cn`（音频） |

关键点：**frp 是 TCP 隧道，它不关心上面跑的是 http 还是 https**，所以本地服务换成 HTTPS 后，只要把 `local_port` 改对、nginx 上游协议改对即可，隧道本身不用动。

## 1. 选一个方案

两套方案都能让公网域名正常访问，区别在于 **TLS 在哪一层终止**：

| | 方案 B（推荐）| 方案 A（最省事）|
|---|---|---|
| Pi 服务 | HTTPS 8443 | HTTP 8080 |
| nginx 上游 | `https://127.0.0.1:8003`（再加密一次） | `http://127.0.0.1:8003` |
| 局域网直连 | `https://192.168.0.184:8443` ✅ | `http://192.168.0.184:8080` ❌ 无 Web Serial |
| Cookie 配置 | `SESSION_COOKIE_SECURE=1` 全局安全 | 置 1 会让局域网 http 登录失效 |
| Pi CPU | 多一次 TLS 加解密（视频流有开销） | 只有明文转发 |

**选 B**：局域网也能用 Web Serial，`SESSION_COOKIE_SECURE=1` 一套配置两条路都成立。
**选 A**：只在公网用、局域网不碰 `/sim` 的话更省心，Pi 少一层加密，MJPEG 视频更流畅。

---

## 2. 方案 B：Pi 起 HTTPS，nginx 反代 https

### 2.1 Pi 上启动服务

```bash
cd ~/moyurobot && git pull
cd pi_client
./scripts/start_web_https.sh    # 默认 https://192.168.0.184:8443，证书自动生成
```

### 2.2 frpc.ini（改动只有一处：local_port 8080 → 8443）

```ini
[common]
server_addr = <公网服务器 IP>
server_port = 7000
token = <你的 token>

[lerobot_uiweb]
type = tcp
local_ip = 127.0.0.1
local_port = 8443
remote_port = 8003

[lerobot_websocket]
type = tcp
local_ip = 127.0.0.1
local_port = 8081
remote_port = 8004
```

重启：`sudo systemctl restart frpc`（或 `nohup ./frpc -c frpc.ini &`）。

> frps.ini 的 `allow_ports` 必须覆盖 `8003,8004`，否则隧道起不来。

### 2.3 nginx：网页

```nginx
# http {} 块里加一次即可
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# keepalive 到 Pi：双 TLS 时省掉每次请求的握手开销
upstream lerobot_web {
    server 127.0.0.1:8003;
    keepalive 16;
}

server {
    listen 80;
    server_name lerobot.mycodebro.cn;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;                 # nginx < 1.25.1 写成: listen 443 ssl http2;
    server_name lerobot.mycodebro.cn;

    ssl_certificate     /etc/letsencrypt/live/lerobot.mycodebro.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/lerobot.mycodebro.cn/privkey.pem;

    client_max_body_size 50m;

    location / {
        proxy_pass https://lerobot_web;

        # Pi 上是自签证书（CN=moyurobot.local），不校验，否则 502
        proxy_ssl_verify       off;
        proxy_ssl_server_name  on;

        proxy_http_version 1.1;
        proxy_set_header Connection        "";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # MJPEG 视频流（/video_feed/<camera>）：必须关缓冲，否则一直转圈
        proxy_buffering         off;
        proxy_request_buffering off;
        proxy_read_timeout      3600s;
        proxy_send_timeout      3600s;
    }
}
```

### 2.4 nginx：音频（WebSocket）

音频是**明文 ws**，同一端口不能混跑，所以单独一个域名 + `Upgrade` 头：

```nginx
server {
    listen 80;
    server_name lerobotwws.mycodebro.cn;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;                 # nginx < 1.25.1 写成: listen 443 ssl http2;
    server_name lerobotwws.mycodebro.cn;

    ssl_certificate     /etc/letsencrypt/live/lerobotwws.mycodebro.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/lerobotwws.mycodebro.cn/privkey.pem;

    location /audio {
        proxy_pass http://127.0.0.1:8004;   # 不带 URI，保留原始 /audio 路径

        proxy_http_version 1.1;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        $connection_upgrade;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering    off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

> `proxy_pass` 后面**不要**加 `/`，也**不要**写 `/audio`。写了会把路径改写掉，而音频服务校验「路径必须以 `/audio` 开头」，会直接拒绝握手。
>
> 这里不能复用 `upstream ... keepalive`：WebSocket 长连接本来就不复用，且 `Connection ""` 会把 `Upgrade` 抹掉。

### 2.5 Pi 上的 `pi_client/.env`

```bash
# 浏览器走的是 https，Cookie 要带 Secure
SESSION_COOKIE_SECURE=1
# 用 IP / localhost 直连时必须留空，否则浏览器直接丢弃 Cookie
SESSION_COOKIE_DOMAIN=
# 音频公开地址（config/default.json 里已有同样默认值，可只在需要覆盖时写）
AUDIO_WS_PUBLIC_URL=wss://lerobotwws.mycodebro.cn/audio
```

### 2.6 生效与自检

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart frpc
```

```bash
# Pi 本地
curl -sk https://127.0.0.1:8443/ -o /dev/null -w 'local  %{http_code}\n'
# 经隧道 + nginx
curl -s  https://lerobot.mycodebro.cn/ -o /dev/null -w 'public %{http_code}\n'
# 证书链是否为 Let's Encrypt（不是的话浏览器报错）
echo | openssl s_client -connect lerobot.mycodebro.cn:443 -servername lerobot.mycodebro.cn 2>/dev/null \
  | openssl x509 -noout -issuer -dates
# 音频真实下发的 ws 地址
curl -s https://lerobot.mycodebro.cn/audio/info | python3 -m json.tool | grep -E 'ws_url|ws_public_url'
```

`ws_url` 应以 `wss://lerobotwws.mycodebro.cn/audio?token=` 开头 —— 不是的话说明 `AUDIO_WS_PUBLIC_URL` 没被读到。

---

## 3. 方案 A：nginx 终止 TLS，Pi 保持 HTTP

Pi 上：

```bash
./scripts/start_web_http.sh
```

frpc.ini **完全不用改**（还是你原来那份 8080 → 8003、8081 → 8004）。

nginx 只改一行：

```nginx
location / {
    proxy_pass http://127.0.0.1:8003;   # 原来是 https://
    # 删掉 proxy_ssl_verify / proxy_ssl_server_name
    # 其余 set_header、proxy_buffering、timeout 全部照抄方案 B
}
```

⚠️ **Cookie 的坑**：`SESSION_COOKIE_SECURE=1` 时浏览器只在 https 下保存 Cookie，所以

- 只用公网域名 → 置 1，没问题
- 还要用 `http://192.168.0.184:8080` 直连 → **必须留空**，否则登录后立刻掉回 `/login`，无限循环

代价：局域网 http 下 `navigator.serial` 不存在，`/sim` 的主臂联动用不了（就是最开始那个报错）。

---

## 4. 两个方案都会踩的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 网页 502 | nginx 上游协议和 Pi 实际协议不一致 | B 用 `https://`，A 用 `http://`；B 还要 `proxy_ssl_verify off` |
| 域名能开、登录后跳回 `/login` | Cookie 带 `Secure` 但走的是 http | 见 2.5 / 3 的 Cookie 说明 |
| `/video_feed` 一直转圈、画面不出来 | nginx 默认缓冲整个 MJPEG 流 | `proxy_buffering off` |
| 语音面板「未连接」 | `/audio` 路径被改写，或 `Upgrade` 头丢了 | 见 2.4 的两条注意 |
| 音频提示「未登录」 | 跨域 Cookie 带不过去 | 项目已改用 `?token=` 连接令牌，确认前端不是缓存的旧 JS |
| 换网段后浏览器报证书错误 | 自签证书 SAN 里没有新 IP | `WEB_REGENERATE_CERT=1 ./scripts/start_web_https.sh` |
| frpc 连上但公网端口不通 | frps `allow_ports` 没放开 | 加 `8003,8004` 并重启 frps |

## 5. 局域网 https 直连时的一个已知限制

`https://192.168.0.184:8443` 页面是安全的（Web Serial 可用），但**语音对讲仍走公网域名**：

- `config/default.json` 里 `ws_public_url` 是写死的 `wss://lerobotwws.mycodebro.cn/audio`
- 音频服务 8081 是**明文 ws**，https 页面不允许连 `ws://`（混合内容拦截）
- 想局域网内直连音频，得给 8081 也套 TLS（`stunnel` / socat + `wss://`），属于另一件事

影响面：家里断网时，控制、仿真、视频可用，语音不可用。
