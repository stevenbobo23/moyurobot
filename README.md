# 🐟 摸鱼遥控车 (MoYu Robot)

基于 [LeRobot](https://github.com/huggingface/lerobot) 和 MCP (Model Context Protocol) 的智能机器人控制平台，支持 AI 控制、Web 遥控、手势控制和人脸追踪。

## ✨ 功能特性

- 🤖 **MCP AI 控制**: 通过 MCP 协议与 AI 模型（如 Claude、小智 AI）集成，实现自然语言控制机器人
- 🌐 **Web 控制界面**: 响应式 Web 界面，支持桌面和移动设备，内置排队系统
- 🎮 **多种控制模式**:
  - 键盘控制：WASD / QE 方向控制
  - 手势控制：MediaPipe 手势识别
  - 人脸追踪：自动追踪人脸方向
- 🦾 **机械臂控制**: 6 自由度机械臂精确控制
- 📹 **实时视频**: 多摄像头 MJPEG 视频流 + RTMP 推流支持
- 🔊 **语音对讲**: 网页端像云摄像头一样听车端声音，按住说话回传车端喇叭（半双工）
- 🔗 **远程连接**: WebSocket 管道支持远程 AI 控制

## 🔧 硬件要求

本项目基于 LeRobot 的 **LeKiwi** 移动机械臂机器人：

- **LeKiwi 机器人**：三轮全向移动底盘 + 6DOF 机械臂
- **摄像头**：
  - 前置摄像头（推荐 T1 Webcam）
  - 手腕摄像头（推荐 USB Camera）
- **运行环境**：树莓派 / Linux PC

> 详见 LeRobot 硬件文档：https://github.com/huggingface/lerobot

## 📁 项目结构

```
moyurobot/
├── pi_client/              # Pi 端代码（运行在树莓派上）
│   ├── moyurobot/          # Python 包
│   │   ├── core/           # 核心服务
│   │   │   ├── config.py   # 配置管理
│   │   │   ├── actions.py  # 预设动作编排
│   │   │   ├── audio.py    # 音频对讲中枢（采集广播 / 对讲独占）
│   │   │   └── robot_service.py  # 机器人服务封装
│   │   ├── mcp/            # MCP AI 控制
│   │   │   ├── server.py   # MCP 工具服务器
│   │   │   └── pipe.py     # WebSocket 管道
│   │   └── web/            # Web 控制器
│   │       ├── controller.py   # HTTP 路由
│   │       ├── audio_ws.py     # 音频 WebSocket 服务
│   │       ├── site_verify.py  # 域名校验文件（/<名字>.txt）
│   │       ├── session.py      # 会话/排队管理
│   │       ├── streaming.py    # RTMP 推流
│   │       ├── templates/      # HTML 模板
│   │       └── static/         # JS/CSS 资源
│   ├── config/             # 配置文件
│   │   ├── default.json    # 默认配置（含 audio 段）
│   │   └── env.example     # 环境变量模板
│   ├── public/             # 站点根目录下的公开文件（微信域名校验 txt 等）
│   ├── docs/               # 详细文档（语音对讲等）
│   ├── scripts/            # 启动脚本 + 音频自检 / 自签证书
│   ├── setup.py            # Python 包安装
│   └── requirements.txt    # 依赖列表
├── train_server/           # 训练服务器代码（可选）
├── README.md
└── .gitignore
```

## 🚀 快速开始

### 1. 安装 LeRobot（核心依赖）

本项目依赖 [LeRobot](https://github.com/huggingface/lerobot) 机器人控制框架：

```bash
# 安装 lerobot
pip install lerobot

# 验证安装
lerobot-info

# 安装LeKiwi依赖
pip install lerobot[lekiwi]
```

> 📖 LeRobot 详细文档：https://huggingface.co/docs/lerobot

### 2. 安装本项目

```bash
# 克隆项目
git clone https://github.com/your-username/moyurobot.git
cd moyurobot

# 进入 Pi 端代码目录
cd pi_client

# 安装项目（推荐在 lerobot 的虚拟环境中）
pip install -e .

# 安装额外依赖
pip install flask fastmcp websockets python-dotenv opencv-python
```

### 3. 机器人校准（首次使用）

在首次使用前，需要校准机械臂：

```bash
# 使用 lerobot 校准工具
python -m lerobot.scripts.control_robot \
    --robot.type=lekiwi \
    --robot.id=my_awesome_kiwi \
    --control.type=calibrate
```

校准文件保存在 `~/.cache/huggingface/lerobot/calibration/`

### 4. 配置环境变量（可选）

```bash
# 在 pi_client 目录下
# 复制配置模板
cp config/env.example .env

# 编辑配置（API Key、推流地址等）
vim .env
```

### 5. 启动服务

```bash
# 在 pi_client 目录下

# 一键启动所有服务
./scripts/start_all.sh

# 或单独启动 Web 控制器
export PYTHONPATH="$PWD:$PYTHONPATH"
python -m moyurobot.web.controller --robot-id my_awesome_kiwi
```

访问 http://localhost:8080 开始控制！

## 🎮 使用说明

### Web 控制界面

1. 打开浏览器访问 `http://localhost:8080`（或机器人 IP）
2. 输入用户名登录
3. 使用控制面板操作机器人

### 键盘快捷键

| 按键 | 功能 | 按键 | 功能 |
|------|------|------|------|
| W | 前进 | Q | 左转 |
| S | 后退 | E | 右转 |
| A | 左移 | Space | 停止 |
| D | 右移 | H/J | 左/右旋转 |

### 手势控制

开启手势控制后，通过摄像头识别手势：

| 手势 | 功能 |
|------|------|
| ✋ 张开手掌 | 停止 |
| ✊ 握拳 | 关闭夹爪 |
| ☝️ 竖起食指 | 前进 |
| 👍 竖起大拇指 | 打开夹爪 |
| ✌️ 剪刀手 | 左转 |

### 机械臂控制

使用滑块控制 6 个关节：
- **肩部水平** (shoulder_pan): ±60°
- **肩部垂直** (shoulder_lift): ±55°
- **肘关节** (elbow_flex): ±50°
- **腕关节弯曲** (wrist_flex): ±70°
- **腕关节旋转** (wrist_roll): ±70°
- **夹爪** (gripper): 0-60°

### 🔊 语音对讲

控制台右侧「语音对讲」面板，需要车端外接 USB 麦克风 + 音箱（`sudo apt install alsa-utils`）：

| 操作 | 效果 |
| --- | --- |
| 点「监听车端」 | 像云摄像头一样听到车端麦克风的声音，多人可同时听 |
| 按住「按住说话」（或键盘 `T`） | 本地麦克风上传到车端音箱播放，半双工对讲 |

- 没人监听时车端自动释放麦克风；松开说话键立刻释放本地麦克风
- 有人说话时所有客户端本地静音，避免车端喇叭回授啸叫
- 无硬件时可先用模拟后端看效果：`AUDIO_BACKEND=sim python -m moyurobot.web.controller`
- ⚠️ 浏览器只在 HTTPS / localhost 下给麦克风权限，局域网 IP + http 打开时**只能听、不能说**，
  见 `pi_client/scripts/gen_self_signed_cert.sh` 与 [文档](pi_client/docs/语音对讲.md)

自检与测试：

```bash
cd pi_client
./scripts/audio_selftest.sh                                   # 硬件自检（录音+回放）
PYTHONPATH=. python moyurobot/tests/test_audio_pipeline.py     # 端到端协议测试（无需声卡）
```

## 🤖 MCP AI 控制

### 可用的 MCP 工具

本项目提供以下 MCP 工具供 AI 调用：

| 工具名 | 功能 |
|--------|------|
| `move_robot` | 控制机器人移动（forward/backward/left/right/stop） |
| `rotate_robot` | 控制机器人旋转指定角度 |
| `control_gripper` | 控制夹爪开关 |
| `nod_head` | 点头动作 |
| `shake_head` | 摇头动作 |
| `twist_waist` | 扭腰动作 |
| `reset_arm` | 机械臂复位 |
| `stand_at_attention` | 立正姿态 |
| `capture_and_analyze` | 拍照并用视觉模型（默认 DeepSeek）分析 |
| `get_robot_status` | 获取机器人状态 |
| `set_speed_level` | 设置速度等级（slow/medium/fast） |

### 配置 Claude Desktop

编辑 Claude Desktop 配置文件：

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
    "mcpServers": {
        "moyu-robot": {
            "command": "python",
            "args": ["-m", "moyurobot.mcp.server"],
            "cwd": "/path/to/moyurobot/pi_client",
            "env": {
                "ROBOT_ID": "my_awesome_kiwi",
                "DEEPSEEK_API_KEY": "your-api-key",
                "PYTHONPATH": "/path/to/moyurobot/pi_client"
            }
        }
    }
}
```

### 配置 Cursor

编辑 `.cursor/mcp.json`：

```json
{
    "mcpServers": {
        "moyu-robot": {
            "command": "python",
            "args": ["-m", "moyurobot.mcp.server", "--transport", "stdio"],
            "cwd": "/path/to/moyurobot/pi_client",
            "env": {
                "PYTHONPATH": "/path/to/moyurobot/pi_client"
            }
        }
    }
}
```

## ⚙️ 配置说明

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ROBOT_ID` | 机器人 ID（与校准文件匹配） | `my_awesome_kiwi` |
| `FLASK_SECRET_KEY` | Flask 会话密钥 | 随机生成 |
| `DEEPSEEK_API_KEY` | DeepSeek Key（拍照分析默认用它，模型 `deepseek-flash` = V4.1-Flash） | - |
| `VISION_PROVIDER` | `auto` / `deepseek` / `dashscope` | `auto` |
| `QWEN_API_KEY` | 阿里云千问 VL Key（仅作回退，可不配） | - |
| `MCP_ENDPOINT` | 远程 MCP 端点（如小智 AI） | - |
| `RTMP_STREAM_URL` | RTMP 推流地址 | - |
| `STREAMING_ENABLED` | 是否启用推流 | `false` |
| `SESSION_TIMEOUT_SECONDS` | 用户控制超时（秒） | `100` |
| `VIP_SESSION_TIMEOUT_SECONDS` | VIP 用户超时（秒） | `600` |

### 配置文件

`config/default.json`:

```json
{
    "robot": {
        "robot_id": "my_awesome_kiwi",
        "linear_speed": 0.2,
        "angular_speed": 30.0,
        "arm_servo_speed": 0.2,
        "arm_torque_limit": 600
    },
    "cameras": {
        "front": {
            "device_name_or_path": "T1 Webcam",
            "rotate_180": false
        },
        "wrist": {
            "device_name_or_path": "USB Camera",
            "rotate_180": true
        }
    }
}
```

## 🔏 域名校验文件（小程序业务域名 / 支付授权目录）

微信要求把平台下发的校验文件放在**站点根目录**并能直接访问。本项目的做法是：

```bash
# 把平台给的 txt 丢进这个目录即可，不用改代码、不用重启以外的操作
pi_client/public/
└── ChC4AVj1oJ.txt        # 文件名保持平台给的原样
```

服务会自动以 `/<文件名>.txt` 暴露它们：

```bash
curl -i https://lerobot.mycodebro.cn/ChC4AVj1oJ.txt
# HTTP/2 200
# content-type: text/plain; charset=utf-8
# content-length: 32
# 230bf6a07a9c486547c2bbea1dfe58e3
```

几个约束（代码里已经卡死，避免变成任意文件读取）：

- 只服务 `public/` 下真实存在的 `.txt`，不列目录；
- 文件名只允许字母、数字、`-`、`_`，首字符必须是字母或数字，`..` 和斜杠进不来；
- 直接回原始字节，不做编码转换，不会多出换行或 BOM。

> 换域名或平台重新下发文件时，只需替换 `public/` 里的文件；路由是通配的，不绑定具体文件名。

## 🦾 机械臂仿真（/sim）

打开 `http://<机器人IP>:8080/sim`（或控制台「机械臂控制」标题右侧的「仿真」按钮），
可以在浏览器里看到一个 SO-ARM100 的三维模型，和真臂同姿态。

三种驱动方式，优先级从高到低：

| 来源 | 说明 |
| --- | --- |
| **主臂（USB）** | 点「连接主臂」→ 选中主臂串口。主臂扭矩会自动松开，用手带动它，仿真跟手，同时推给从臂 |
| **滑块** | 没连主臂时手动拖；连上主臂后滑块变只读，跟随主臂显示 |
| **真臂回读** | 点「把真臂姿态读回仿真」，把实物的当前姿态同步到画面 |

几个实现要点：

- **浏览器直连舵机**，不需要在那台电脑上装 Python 或 lerobot。用 Web Serial + Feetech 协议
  直接读 `Present_Position`，一次广播包读回 6 个关节（SYNC READ）。
- 角度口径与 lerobot 一致：连上时读一次 `Min_Position_Limit` / `Max_Position_Limit`，
  用 `(raw - mid) * 360 / 4095` 归一化，所以**校准过的主臂也能直接对上**。
- 只支持 **Chrome / Edge**（Web Serial 限制），且必须是 `https://` 或 `localhost`。
- 串口同一时刻只能被一方占用：主臂插在浏览器那台电脑上时，那边就不能同时用 lerobot。
- 推送到从臂仍然走 `/control`，所以要先在控制台启用运动控制，且服务端
  `arm_servo_speed` 建议设为 1.0，否则从臂跟不上手。

仿真资源（three.js、urdf-loader、SO-101 的 URDF 与网格）都放在
`pi_client/moyurobot/web/static/sim/`，离线可用，不依赖 CDN。
其中 three.js / urdf-loader / SO-101 模型来自 [bambot](https://github.com/timqian/bambot)（MIT）。

## 🐛 故障排除

### 机器人连接失败

```bash
# 检查 USB 连接
ls /dev/ttyACM* /dev/ttyUSB*

# 检查摄像头
ls /dev/video*

# 查看设备名称
cat /sys/class/video4linux/video*/name
```

### 摄像头无画面

```bash
# 测试摄像头
ffplay /dev/video0

# 检查权限
sudo usermod -a -G video $USER
```

### MCP 连接问题

```bash
# 测试 MCP 服务器
python -m moyurobot.mcp.server --transport stdio

# 查看日志
tail -f ~/logs/moyurobot_web.log
```

## 📡 API 接口

### REST API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 控制界面 |
| `/status` | GET | 机器人状态 |
| `/control` | POST | 发送控制命令 |
| `/cameras` | GET | 摄像头列表 |
| `/video_feed/<camera>` | GET | 视频流 |
| `/audio/info` | GET | 语音对讲能力探测（后端、设备、ws 地址、权限） |
| `/startmove` | POST | 启用运动控制 |
| `/stopmove` | POST | 禁用运动控制 |
| `/session_info` | GET | 会话信息 |

### 控制命令示例

```bash
# 前进 2 秒
curl -X POST http://localhost:8080/control \
  -H "Content-Type: application/json" \
  -d '{"command": "forward", "duration": 2}'

# 设置机械臂位置
curl -X POST http://localhost:8080/control \
  -H "Content-Type: application/json" \
  -d '{"arm_gripper.pos": 50}'
```

## 🔧 开发

```bash
# 运行测试
pytest tests/

# 代码格式化
black src/
isort src/

# 类型检查
mypy src/
```

## 📝 许可证

MIT License

## 🙏 致谢

- [LeRobot](https://github.com/huggingface/lerobot) - Hugging Face 机器人控制框架
- [FastMCP](https://github.com/jlowin/fastmcp) - MCP 协议 Python 实现
- [MediaPipe](https://mediapipe.dev/) - Google 手势识别库
- [Flask](https://flask.palletsprojects.com/) - Web 框架

## 🔗 相关链接

- LeRobot 文档：https://huggingface.co/docs/lerobot
- LeKiwi 硬件指南：https://github.com/huggingface/lerobot/tree/main/examples/10_use_so100
- MCP 协议规范：https://modelcontextprotocol.io/
