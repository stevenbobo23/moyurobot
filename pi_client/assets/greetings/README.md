# 打招呼音频池

放 `*.wav` 到这里，运行时就会优先用它（`speech.mode: auto`），**不依赖网络**。
营地里信号不稳时，这是最可靠的做法。

## 生成

```bash
cd pi_client
QWEN_API_KEY=sk-xxx python scripts/gen_greeting_voices.py
```

会生成 `greet_01.wav`、`greet_02.wav`…，并写一份 `manifest.json`：

```json
{ "greet_01": "你好呀～", "greet_02": "欢迎来营地，随便看看～" }
```

清单的作用只是让网页/日志显示原文而不是文件名。删掉也能用，那就显示 `greet_01`。

## 自己录

- 格式：任意采样率/声道的 `wav` 都行（运行时会转成车端的 16k 单声道）
- 时长：1~3 秒最自然，别超过 5 秒
- 命名：`任意名字.wav`；想让界面显示中文就在 `manifest.json` 里加一条 `{"任意名字": "你好呀～"}`
- 音量：录得偏低没关系，`GREETING_GAIN` 可以放大（默认 0.8，相对对讲音量）

## 注意

- 每次服务启动时扫一次；改了文件要重启（或等 worker 重建 `SpeechLibrary`）
- 位深只支持 8/16 bit。24 bit 的录音会被跳过并在日志里提示
