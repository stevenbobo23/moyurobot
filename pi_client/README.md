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
├── scripts/            # 启动脚本 + 音频自检 / 自签证书
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

详见 [docs/语音对讲.md](docs/语音对讲.md)：硬件准备、HTTPS 三个解法、面板说明、
参数表、协议格式、排错表。

## 环境变量

参见 `config/env.example` 文件。

## 详细文档

- [语音对讲（监听 / 对讲）](docs/语音对讲.md)
- 项目根目录的 [README.md](../README.md)

