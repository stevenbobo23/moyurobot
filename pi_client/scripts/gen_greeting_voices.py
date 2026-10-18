#!/usr/bin/env python
"""生成打招呼的本地音频池（assets/greetings/*.wav）

为什么要预先生成：营地信号不稳定，云端 TTS 掉了会很尴尬。生成一次之后
断网也能打招呼，运行时零延迟。

用法::

    # 用配置文件里 speech.texts 那几句
    QWEN_API_KEY=sk-xxx python scripts/gen_greeting_voices.py

    # 指定音色 / 自定义文案
    python scripts/gen_greeting_voices.py --voice longwan --text "你好呀～" --text "需要帮忙吗？"

    # 强制重生成（默认已存在的同文案会跳过）
    python scripts/gen_greeting_voices.py --force

生成的 wav 会附带一个 ``manifest.json``（文件名 -> 原文），界面上就能直接显示
「你好呀～」而不是 ``greet_01``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 让脚本能直接跑：把 pi_client 加到 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from moyurobot import dashscope_tts  # noqa: E402
from moyurobot.greeting.config import GreetingConfig  # noqa: E402


def _load_manifest(folder: Path) -> dict:
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成打招呼音频池")
    parser.add_argument("--voice", default=None, help="音色（默认取配置 tts_voice）")
    parser.add_argument("--model", default=None, help="模型（默认取配置 tts_model）")
    parser.add_argument("--out", default=None, help="输出目录（默认 assets/greetings）")
    parser.add_argument("--text", action="append", default=None,
                        help="自定义文案，可重复；不传则用配置文件里的 speech.texts")
    parser.add_argument("--from-file", default=None, help="从文本文件读文案，一行一句")
    parser.add_argument("--force", action="store_true", help="已存在的同文案也重新生成")
    parser.add_argument("--timeout", type=float, default=20.0, help="单句合成超时（秒）")
    args = parser.parse_args()

    cfg = GreetingConfig.load()
    voice = args.voice or cfg.speech.tts_voice
    model = args.model or cfg.speech.tts_model

    texts = []
    if args.text:
        texts.extend(args.text)
    if args.from_file:
        content = Path(args.from_file).expanduser().read_text(encoding="utf-8")
        texts.extend(line.strip() for line in content.splitlines() if line.strip())
    if not texts:
        texts = list(cfg.speech.texts)

    folder = Path(args.out) if args.out else PROJECT_ROOT / cfg.speech.audio_dir
    if not folder.is_absolute():
        folder = PROJECT_ROOT / folder
    folder.mkdir(parents=True, exist_ok=True)

    if not dashscope_tts.available():
        print("✗ 没有可用的 API Key：请设置 QWEN_API_KEY（或 DASHSCOPE_API_KEY）", file=sys.stderr)
        print("  真的不想用云端？那就自己录几段 wav 丢进 %s 也行。" % folder, file=sys.stderr)
        return 2

    manifest = _load_manifest(folder)
    existing_texts = {text: stem for stem, text in manifest.items()}

    print("输出目录: %s" % folder)
    print("音色: %s / 模型: %s" % (voice, model))
    print("待生成: %d 句\n" % len(texts))

    succeeded, skipped, failed = 0, 0, 0
    next_index = len([p for p in folder.glob("*.wav")]) + 1

    for index, text in enumerate(texts, start=1):
        if not args.force and text in existing_texts:
            stem = existing_texts[text]
            if (folder / (stem + ".wav")).is_file():
                print("[%d/%d] 跳过（已存在 %s.wav）: %s" % (index, len(texts), stem, text))
                skipped += 1
                continue

        print("[%d/%d] 合成: %s" % (index, len(texts), text))
        try:
            wav = dashscope_tts.synthesize(text, voice=voice, model=model, timeout=args.timeout)
        except Exception as e:
            print("        ✗ 失败: %s" % e)
            failed += 1
            continue

        stem = "greet_%02d" % next_index
        next_index += 1
        target = folder / (stem + ".wav")
        target.write_bytes(wav)
        manifest[stem] = text
        succeeded += 1
        print("        ✓ %s（%.1f KB）" % (target.name, len(wav) / 1024.0))

    manifest_path = folder / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n完成：成功 %d / 跳过 %d / 失败 %d" % (succeeded, skipped, failed))
    print("清单: %s" % manifest_path)
    if succeeded:
        print("重启服务（或等下一次刷新）后，语音来源会自动切到本地音频池。")
    return 0 if (succeeded or skipped) and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
