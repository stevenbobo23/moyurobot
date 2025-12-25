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
│   ├── core/           # 核心服务（机器人控制）
│   ├── mcp/            # MCP AI 控制
│   └── web/            # Web 控制器
├── config/             # 配置文件
├── scripts/            # 启动脚本
├── setup.py            # 安装脚本
└── requirements.txt    # 依赖列表
```

## 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Web 控制器 | 8080 | HTTP 控制界面 |
| MCP HTTP | 8000 | AI 控制接口 |

## 环境变量

参见 `config/env.example` 文件。

## 详细文档

请参阅项目根目录的 [README.md](../README.md)。

