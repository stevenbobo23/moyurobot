#!/usr/bin/env python
"""DashScope 语音合成（阿里云百炼 / 通义）

只用标准库（urllib），这样打招呼的语音环节不引入额外依赖，也能被
``scripts/gen_greeting_voices.py`` 单独调用。

支持两个模型，按配置优先，一个不行自动试另一个：

- ``cosyvoice-v1``：音色如 ``longxiaochun``（默认）
- ``qwen-tts``：音色如 ``Cherry`` / ``Serena``

返回的是 **wav 字节流**：合成结果通常给一个 OSS 链接，这里顺手下回来，
免得每次播放都要多一次网络往返。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_BASE = "https://dashscope.aliyuncs.com/api/v1/services/aigc"
COSYVOICE_URL = _BASE + "/text2speech/speech-synthesis"
QWEN_TTS_URL = _BASE + "/multimodal-generation/generation"

# qwen-tts 不认 cosyvoice 的中文音色名，回退时换一个
QWEN_VOICE_FALLBACK = "Cherry"


class TTSError(RuntimeError):
    """合成失败（网络、鉴权、格式异常都归到这里）"""


def api_key() -> str:
    return (os.environ.get("QWEN_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or "").strip()


def available() -> bool:
    return bool(api_key())


def synthesize(text: str, voice: str = "longxiaochun", model: str = "cosyvoice-v1",
               timeout: float = 8.0) -> bytes:
    """把文本合成为 wav 字节流"""
    text = (text or "").strip()
    if not text:
        raise TTSError("文本为空")
    key = api_key()
    if not key:
        raise TTSError("未配置 QWEN_API_KEY / DASHSCOPE_API_KEY")

    order = _model_order(model)
    errors = []
    for name, url, voice_name in order:
        try:
            return _synthesize_once(name, url, voice_name or voice, text, key, timeout)
        except TTSError as e:
            errors.append(f"{name}: {e}")
            logger.info("TTS %s 失败，尝试下一个: %s", name, e)
    raise TTSError("；".join(errors) or "合成失败")


def _model_order(model: str) -> list:
    model = (model or "").strip().lower()
    cosy = ("cosyvoice-v1", COSYVOICE_URL, None)
    qwen = ("qwen-tts", QWEN_TTS_URL, None)
    if model.startswith("cosyvoice") or not model:
        return [cosy, qwen]
    if model.startswith("qwen"):
        return [qwen, cosy]
    return [cosy, qwen]


def _post_json(url: str, payload: Dict[str, Any], key: str, timeout: float) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise TTSError(f"HTTP {e.code} {detail}".strip())
    except urllib.error.URLError as e:
        raise TTSError(f"网络不可达: {e.reason}")
    except Exception as e:
        raise TTSError(str(e))

    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise TTSError(f"响应不是 JSON: {raw[:200]!r}")


def _synthesize_once(name: str, url: str, voice: str, text: str,
                     key: str, timeout: float) -> bytes:
    if name == "qwen-tts" and voice.startswith("long"):
        voice = QWEN_VOICE_FALLBACK
    payload = {
        "model": name,
        "input": {"text": text, "voice": voice},
    }
    data = _post_json(url, payload, key, timeout)

    if data.get("code") and not data.get("output"):
        raise TTSError(f"{data.get('code')} {data.get('message', '')}".strip())

    kind, value = _extract_audio(data)
    if kind is None:
        raise TTSError(f"响应里没有音频: {json.dumps(data, ensure_ascii=False)[:200]}")

    if kind == "b64":
        try:
            return base64.b64decode(value)
        except Exception as e:
            raise TTSError(f"base64 解码失败: {e}")

    return _download(value, timeout)


def _extract_audio(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    output = data.get("output") or {}
    if not isinstance(output, dict):
        return None, None

    audio = output.get("audio")
    if isinstance(audio, dict):
        if audio.get("url"):
            return "url", str(audio["url"])
        if audio.get("data"):
            return "b64", str(audio["data"])
    if isinstance(audio, str) and audio.startswith("http"):
        return "url", audio

    if output.get("audio_url"):
        return "url", str(output["audio_url"])

    results = output.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            for key in ("url", "audio_url"):
                if first.get(key):
                    return "url", str(first[key])

    return None, None


def _download(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise TTSError(f"下载音频失败 HTTP {e.code}")
    except Exception as e:
        raise TTSError(f"下载音频失败: {e}")
