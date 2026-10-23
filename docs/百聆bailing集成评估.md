# 百聆（bailing）能不能集成？

> 评估对象：https://github.com/wwbin2017/bailing （MIT，Python 3.12+）
> 结论：**架构高度适配，但不要当依赖引进来——把它当"编排层骨架"移植。**
> 日期：2026-10-22

---

## 0. 一句话结论

它的**模块划分和打断状态机正好是我们缺的那块**（省 300~500 行），但它的**依赖清单在树莓派上装不起来**，默认音频回路也是"浏览器播放"而不是"车端喇叭"。

所以：**抄骨架，不装全家桶。** 移植成本约 2~3 天，能同时解决"网页对话"和"车旁对话"。

---

## 1. 它是什么（代码级事实，不是 README 宣传）

| 项 | 实际情况 |
|---|---|
| 定位 | 类 GPT-4o 语音对话助手，宣称端到端 800ms、无需 GPU |
| ASR | FunASR + `FunAudioLLM/SenseVoiceSmall` |
| VAD | `silero_vad`，`threshold 0.5`、**`min_silence_duration_ms: 200`** |
| LLM | `deepseek-chat`（走 `OpenAILLM`，也可配 Ollama 本地） |
| TTS | 可配 5 种：`EdgeTTS(zh-CN-XiaoxiaoNeural)` / Kokoro / ChatTTS / CosyVoice / MacTTS / gTTS |
| 通信 | **FastAPI + WebSocket `/ws?user_id=...`**（不是 WebRTC） |
| 音频上下行 | 浏览器推 PCM 二进制帧 → `recorder.put_audio()`；TTS 音频 **回推浏览器播放**，前端再回报 `{"type":"playback_status"}` |
| 前端 | `static/` 自带一个对话页，`python server.py` 起在 `:8000` |
| 唤醒词 | **未实现**（Roadmap 未勾） |
| 工具 | `plugins/` + `TaskManager`，用 `plugins/function_calls_config.json` 注册函数 |
| 许可 | MIT ✅ |
| 免责声明 | "仅个人学习研究，不适用生产" |

---

## 2. 三个高价值适配点（这是它值得抄的原因）

### ① 全模块策略化，换实现 = 改一行配置

`config/config.yaml`：

```yaml
selected_module:
  Recorder: WebSocketRecorder   # ← 换音频来源
  ASR: FunASR                   # ← 换识别引擎
  VAD: SileroVAD
  LLM: OpenAILLM                # ← 换模型
  TTS: KOKOROTTS                # ← 换音色引擎
  Player: WebSocketPlayer       # ← 换播放去向
```

每个模块都有 `create_instance(类名)` 按名字实例化。`Player` 已经实现了 **6 种**：
`CmdPlayer` / `PyaudioPlayer` / `PygamePlayer` / `PygameSoundPlayer` / `SoundDevicePlayer` / `WebSocketPlayer`。

> 意味着：**加一个 `HubPlayer` 走我们的 `IntercomHub`，大约 40 行**——车端喇叭的问题就解决了。

### ② 打断状态机是现成的、而且想得很清楚

`bailing/robot.py` 里的 `_duplex()`：

- 收 VAD `"start"` 事件
- 若「正在播放」或「chat_lock 为真」且 `INTERRUPT: true` → 重置对话锁、`interrupt_playback()`（`player.stop()`）、开始采集
- 否则不打断

配上一张明确的状态表（README）：

| 播放器状态 | 用户是否说话 | 处理 |
|---|---|---|
| 播放中 | 未说话 | 正常播放 |
| 播放中 | 说话 | **打断** |
| 未播放 | 未说话 | 待命 |
| 未播放 | 说话 | VAD 判定 → ASR |

这正是"连续对话"要的那套，比我们自己从零设计省事得多。

### ③ 工具层和 OpenManus 解耦了

README 把 OpenClaw/OpenManus 说得很重，但 **`robot.py` 全文没有 import `third_party/OpenManus` 或 `openclaw`**——工具能力实际由本地 `plugins.task_manager.TaskManager` + `plugins.registry.Action` 提供。

> 意味着：**可以不装 OpenManus 那套重依赖**，直接在 `plugins/` 里加 `move_robot` / `look_and_analyze` 打 `/control`。

---

## 3. 三个真实的不适配点

### ① `asr.py` 顶部硬 import funasr（连带 torch）

```python
# bailing/asr.py 第 8 行
from funasr import AutoModel
```

**模块级导入**，意味着：即使你选了别的 ASR，只要 import 这个文件，就会拉 `funasr → torch → torchaudio`。
要避 torch 必须动这一行（把 import 挪进 `FunASR.__init__`），或整个换掉 `asr.py`。

### ② 依赖清单在树莓派上装不动

```text
torch, torchaudio, funasr==1.1.6, transformers==4.41.1,
chattts==0.1.1, kokoro>=0.8.1, misaki[zh], PyAudio, sounddevice, pygame
```

`torch + transformers + chattts` 三个加起来就上 GB。Pi 4/5 上要么装不了、要么装完没内存跑 lerobot。

**对策**：只保留 `numpy + edge-tts + fastapi/uvicorn + pyaudio(或直接不用)`，ASR 换 `sherpa-onnx`（上一轮已验证：`VoiceActivityDetector` + `OfflineRecognizer` 都在，无 torch）。

### ③ 自带的网页是"对话页"，不是"小车控制台"

`static/` 里是一个独立的聊天界面（开始按钮 + 对话气泡）。要做成"控制页面里的一个面板"，UI 得自己写——这部分反正我们本来就要做。

### ④ 另外两点
- **没有唤醒词**：车旁"喊它就应"还得自己加 KWS（sherpa-onnx 也能做）
- 作者明确写了"不适用生产"

---

## 4. 移植清单（如果走这条路）

| 动作 | 文件 | 工作量 |
|---|---|---|
| 拿骨架 | `bailing/robot.py`(18KB) `vad.py` `player.py` `recorder.py` `llm.py` `tts.py` | 复制 |
| 断 torch | `asr.py` 把 `from funasr import AutoModel` 挪进类内；新增 `SherpaAsr`（用 `OfflineRecognizer`） | ~80 行 |
| 接车端喇叭 | 新增 `HubPlayer(AbstractPlayer)`：`to_wav()` → 16k PCM → `hub.begin_talk/write_talk/end_talk` | ~40 行 |
| 接车端麦克风 | 新增 `HubRecorder`：把 `hub.add_listener()` 的 PCM 塞进 `put_audio()` | ~30 行 |
| 接工具 | `plugins/function_calls_config.json` 加 `move_robot` / `rotate_robot` / `stop_robot` / `look_and_analyze`，执行函数打 `127.0.0.1:8080` | ~100 行 |
| 接控制台 | 把 `dialog` 消息接进现有 `audio_ws.py`，UI 加到 `index.html` | ~200 行 |
| TTS 配置 | `selected_module.TTS: EdgeTTS` | 1 行 |

合计约 **450 行新增 + 2 个文件改造**，比自写省一半。

---

## 5. 和另外两个候选横向比

| | **百聆 bailing** | **xiaozhi-esp32-server** | **自写三段式** |
|---|---|---|---|
| 定位 | 单机语音助手 + 网页 | 多设备协议服务端 | 完全自控 |
| 音频回路 | 前端/本地声卡，**策略可换** | ESP32 设备协议 | 自己定 |
| 打断 | ✅ 现成状态机 | ✅ 现成 | 自己写 |
| 工具 | `plugins` JSON 注册 | MCP 接入点 | 自己写 |
| Pi 可跑 | ❌ 依赖重（可裁剪） | ⚠️ 要独立机器 | ✅ 按需 |
| 网页控制台 | 自带对话页（要重写 UI） | 管理后台（非控制） | 完全自制 |
| 移植成本 | **中（抄骨架）** | 高（设备协议模拟） | 高（从零） |

> 对我们的场景（**一台车 + 一个控制页 + 车旁对话**），百聆的骨架最贴合。

---

## 6. 建议路线

### 第一步：旁挂体验（0.5 天，不动车）

```bash
git clone https://github.com/wwbin2017/bailing && cd bailing
pip install -r requirements.txt          # 在 Mac 上装全量，别在 Pi 上
# 配 config/config.yaml：LLM.api_key 填 DeepSeek；TTS 选 EdgeTTS（免 key）
python server.py                          # 打开 https://本机IP:8000
```
目的：**先确认"连续对话 + 打断"的手感你能不能接受**（这是唯一无法靠读代码判断的东西）。

### 第二步：移植骨架（2~3 天）

按第 4 节清单做，重点是 `HubPlayer` + `SherpaAsr` 两处，其余是接线。

### 第三步：补唤醒词（1 天，可选）

sherpa-onnx KWS + 关键字模型，做到"喊它就应"。

---

## 7. 结论

| 问题 | 回答 |
|---|---|
| 适不适合集成？ | **架构适合，整体依赖不适合** |
| 能不能直接跑在车上？ | ❌ 不能，torch 那套装不动 |
| 值不值得抄？ | ✅ **非常值得**——打断状态机 + 策略化模块 = 我们最缺的 300 行 |
| 建议姿势 | **旁挂验证体验 → 移植骨架（删 torch、换 ASR、加 HubPlayer）** |
| 预期收益 | 比我上一轮"自写三段式"省一半工作量，且打断逻辑更成熟 |

> 一句话：**把它当"参考实现 + 骨架来源"，不要当"依赖包"。**
