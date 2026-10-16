# 🐟 摸鱼遥控车 - Pi 端

运行在树莓派上的机器人控制服务。

## 快速开始

```bash
# 安装依赖
pip install -e .

# 配置环境变量
cp config/env.example .env
vim .env

# 启动服务
./scripts/start_all.sh
```

## 目录结构

```
pi_client/
├── moyurobot/          # Python 包
│   ├── core/           # 核心服务（机器人控制 + 音频对讲中枢）
│   ├── mcp/            # MCP AI 控制
│   ├── web/            # Web 控制器（含音频 WebSocket 服务）
│   └── tests/          # 测试（含音频端到端测试，无需硬件）
├── config/             # 配置文件
├── scripts/            # 启动脚本 + 日志轮转 + 音频自检 / 自签证书
├── docs/               # 详细文档
├── setup.py            # 安装脚本
└── requirements.txt    # 依赖列表
```

## 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Web 控制器 | 8080 | HTTP 控制界面 |
| MCP HTTP | 8000 | AI 控制接口 |
| 语音对讲 | 8081 | 音频 WebSocket（监听车端声音 / 按住说话） |

## 语音对讲

网页端可像云摄像头一样监听车端声音，也能按住说话对讲。需要外接 USB 麦克风 + 音箱，
并安装 `alsa-utils`：

```bash
sudo apt install -y alsa-utils
./scripts/audio_selftest.sh                  # 硬件自检（录音+回放）
./scripts/audio_selftest.sh plughw:1,0       # 指定设备

# 无硬件时先用模拟后端看效果（监听会听到 440Hz 测试音）
AUDIO_BACKEND=sim python -m moyurobot.web.controller --port 8080
```

> ⚠️ 浏览器只在 HTTPS 或 localhost 下给麦克风权限。用局域网 IP + http 打开时**监听可用、
> 说话不可用**。需要自签证书：`./scripts/gen_self_signed_cert.sh <机器IP>`，然后
> `--ssl-cert certs/robot.crt --ssl-key certs/robot.key`。

端到端测试（无需声卡，26 项断言）：

```bash
PYTHONPATH=. python moyurobot/tests/test_audio_pipeline.py
```

### 用域名访问（反向代理 / 内网穿透）

网页和音频服务的端口不是一个（8080 / 8081），一旦前面挂了域名，前端**没法自己猜出**
该连哪儿，得把浏览器实际用的地址告诉它。写 `config/default.json` 的
`audio.ws_public_url`，或 `pi_client/.env` 里的 `AUDIO_WS_PUBLIC_URL`（环境变量优先）：

```bash
# 网页与音频用不同域名的情况（外网是 wss，内网仍是 ws://<树莓派>:8081）
AUDIO_WS_PUBLIC_URL=https://lerobotwws.mycodebro.cn/
# 解析结果：wss://lerobotwws.mycodebro.cn/audio
```

写法很宽松：`http`/`https` 会自动换成 `ws`/`wss`，只写域名会自动补 `/audio`
且**不会**自作多情加上 `:8081`；`{host}` / `{port}` / `{path}` 可做占位符。
留空 = 用 `config/default.json` 的值，写 `auto` / `none` / `-` = 强制按当前访问地址推导。

改完重启服务，然后确认生效（返回的 `ws_url` 就是前端真正去连的地址）：

```bash
curl -s https://lerobot.mycodebro.cn/audio/info | python -m json.tool | grep -E 'ws_url|ws_public_url'
```

详见 [docs/语音对讲.md](docs/语音对讲.md)：硬件准备、HTTPS 三个解法、面板说明、
参数表、协议格式、排错表。

## 日志治理（防刷屏 / 防撑爆磁盘）

LeRobot 的摄像头在设备掉线或被占用时，会在采集后台线程里**按帧率（30fps）反复打印同一句警告**：

```
WARNING lerobot.cameras.opencv.camera_opencv: Error reading frame in background thread
for OpenCVCamera(/dev/video0): OpenCVCamera(/dev/video0) read failed (status=False).
```

一路摄像头一天就是 260 万行（几百 MB），两路能轻松写爆 SD 卡。系统做了三层处理：

1. **同一条消息去重 + 指数退避**：先打印一次，之后按 `1s → 2s → 4s …` 最长 300s 重报，
   被压掉的条数会挂在下一条消息后面。持续故障的日志量从 O(时间) 降到 O(log 时间)：
   实测 30fps × 2 路跑 6 秒 = 338 条输入 → 只输出 6 行。
2. **单 logger 限速**：每条都不同的日志洪流，按 `LOG_RATE_LIMIT_PER_S` 兜住。
3. **滚动落盘**：`LOG_TO_FILE=1` 时由 `RotatingFileHandler` 限制单文件大小与备份数，
   磁盘占用有硬上限。
4. **设备缺失直接跳过**：启动前校验摄像头设备文件，不存在就不注册该摄像头，
   从根上避免无效重试（`CAMERA_SKIP_MISSING=0` 可恢复旧行为）。

常用配置（完整列表见 `config/env.example`）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_LEVEL` | `INFO` | 根日志级别 |
| `LOG_NOISY_LEVEL` | `WARNING` | 噪声 logger（lerobot 摄像头、PIL 等）级别；设 `ERROR` 可**彻底静音**摄像头刷屏 |
| `LOG_DEDUP_WINDOW_S` | `1.0` | 同一消息初始抑制窗口，`0` = 关闭去重 |
| `LOG_DEDUP_MAX_WINDOW_S` | `300` | 退避上限 |
| `LOG_RATE_LIMIT_PER_S` | `20` | 单 logger 每秒最多放行条数，`0` = 关闭限速 |
| `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | `5MB` / `3` | 单文件上限与备份数量（最多 20MB） |
| `CAMERA_SKIP_MISSING` | `1` | 设备不存在的摄像头直接跳过 |

服务是用 `>>` 重定向写 `~/logs/*.log` 的，Python 侧的 `RotatingFileHandler` 管不到这个 fd，
所以脚本会额外做**原地截断**轮转（保持 inode 不变，运行中的服务不会丢日志、无需重启）：

```bash
./scripts/rotate_logs.sh                                   # 预览（默认 dry-run，不改文件）
./scripts/rotate_logs.sh --max-mb 20 --keep-lines 5000 --apply
# 挂 cron：每天凌晨 4 点
# 0 4 * * * /path/to/pi_client/scripts/rotate_logs.sh --apply >/dev/null 2>&1
```

`./scripts/start_all.sh` 每次启动前也会按 `LOG_MAX_MB`（默认 8MB）自动轮转一次。

命令行同样可以覆盖：`python -m moyurobot.web.controller --log-level WARNING --log-file`。

## 环境变量

参见 `config/env.example` 文件。`cp config/env.example .env` 后，**网页服务、MCP 服务、
命令行入口都会自动加载 `pi_client/.env`**（此前只有 MCP 管道加载过它，`.env` 里的
`AUDIO_*`、API Key 对网页服务其实一直无效）。

优先级：shell / systemd 里已 export 的变量 > `.env` > `config/default.json` > 代码默认值。
也就是说 `.env` **不会**覆盖已经存在的环境变量，只有缺失的才由它补齐。

## 详细文档

- [语音对讲（监听 / 对讲）](docs/语音对讲.md)
- 项目根目录的 [README.md](../README.md)

