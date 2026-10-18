#!/usr/bin/env python3
"""视觉理解（拍照问答）统一入口

原来拍照分析是写死千问 ``qwen-vl-plus`` 的，这里抽成一层，按可用性选 provider：

- ``deepseek``（默认）：DeepSeek 多模态，走 OpenAI 兼容 ``/chat/completions``
- ``dashscope``：阿里云千问 VL（旧行为，保留作回退）

只依赖 ``requests`` + 标准库，不引入 openai SDK。

配置（环境变量，``.env`` 也会被 ``env_file.load_env`` 加载）：

===========================  ============================================
``VISION_PROVIDER``          ``auto`` / ``deepseek`` / ``dashscope``；默认 auto
``DEEPSEEK_API_KEY``         DeepSeek Key（也可写 ``DEEPSEEK_KEY``）
``DEEPSEEK_BASE_URL``        默认 ``https://api.deepseek.com``
``DEEPSEEK_VISION_MODEL``    默认 ``deepseek-flash``（即 V4.1-Flash，原生多模态）
``QWEN_API_KEY``             千问 Key（也可写 ``DASHSCOPE_API_KEY``）
``DASHSCOPE_VISION_MODEL``   默认 ``qwen-vl-plus``
===========================  ============================================

``auto`` 的选择顺序是：配了 DeepSeek Key 就用 DeepSeek，否则用千问。
也就是说**只填 DEEPSEEK_API_KEY 就会自动切过去**，不用改别的配置。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DASHSCOPE_DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DASHSCOPE_DEFAULT_MODEL = "qwen-vl-plus"

SUPPORTED_PROVIDERS = ("deepseek", "dashscope")


class VisionError(RuntimeError):
    """视觉请求失败（鉴权、网络、响应格式异常都归到这里）"""


def _env(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def deepseek_key() -> str:
    return _env("DEEPSEEK_API_KEY", "DEEPSEEK_KEY")


def dashscope_key() -> str:
    return _env("QWEN_API_KEY", "DASHSCOPE_API_KEY")


def deepseek_url() -> str:
    base = _env("DEEPSEEK_BASE_URL") or DEEPSEEK_DEFAULT_BASE
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def deepseek_model() -> str:
    return _env("DEEPSEEK_VISION_MODEL") or DEEPSEEK_DEFAULT_MODEL


def dashscope_model() -> str:
    return _env("DASHSCOPE_VISION_MODEL") or DASHSCOPE_DEFAULT_MODEL


def provider_order(preferred: Optional[str] = None) -> List[str]:
    """按优先级返回真正可用的 provider（看 Key 是否配置）"""
    wanted = (preferred or _env("VISION_PROVIDER") or "auto").strip().lower()
    if wanted in ("", "auto", "-", "none"):
        available = []
        if deepseek_key():
            available.append("deepseek")
        if dashscope_key():
            available.append("dashscope")
        return available
    if wanted not in SUPPORTED_PROVIDERS:
        logger.warning("VISION_PROVIDER=%s 不认识，按 auto 处理", wanted)
        return provider_order("auto")
    # 明确指定就只用它（Key 缺失交给调用时报错，不要静默换厂商）
    return [wanted]


def available() -> bool:
    return bool(provider_order())


def status() -> Dict[str, Any]:
    """给 MCP 状态工具 / 网页展示用"""
    return {
        "providers": provider_order(),
        "provider": provider_order()[0] if provider_order() else None,
        "deepseek_key": bool(deepseek_key()),
        "deepseek_model": deepseek_model(),
        "deepseek_url": deepseek_url(),
        "dashscope_key": bool(dashscope_key()),
        "dashscope_model": dashscope_model(),
        "configured": _env("VISION_PROVIDER") or "auto",
    }


def analyze_image(image_bytes: bytes, prompt: str, mime: str = "image/jpeg",
                  timeout: float = 30.0,
                  provider: Optional[str] = None) -> Dict[str, Any]:
    """把图片 + 问题丢给视觉模型，返回 ``{answer, provider, model, elapsed_ms, usage}``"""
    if not image_bytes:
        raise VisionError("图片数据为空")
    prompt = (prompt or "").strip() or "用中文描述这张图片的内容。"

    order = provider_order(provider)
    if not order:
        raise VisionError(
            "没有可用的视觉模型密钥：请配置 DEEPSEEK_API_KEY（推荐）或 QWEN_API_KEY"
        )

    b64 = base64.b64encode(image_bytes).decode("ascii")
    errors = []
    for name in order:
        started = time.time()
        try:
            data = _call(name, b64, mime, prompt, timeout)
        except VisionError as exc:
            errors.append(f"{name}: {exc}")
            logger.info("视觉模型 %s 失败，尝试下一个: %s", name, exc)
            continue
        answer = _extract_answer(data)
        if not answer:
            errors.append(f"{name}: 响应里没有文本内容")
            continue
        logger.info("视觉分析完成（%s / %s）", name, _model_of(name))
        return {
            "answer": answer,
            "provider": name,
            "model": _model_of(name),
            "elapsed_ms": int((time.time() - started) * 1000),
            "usage": data.get("usage") or {},
        }

    raise VisionError("；".join(errors) or "视觉分析失败")


def _model_of(provider: str) -> str:
    return deepseek_model() if provider == "deepseek" else dashscope_model()


def _call(provider: str, b64: str, mime: str, prompt: str, timeout: float) -> Dict[str, Any]:
    if provider == "deepseek":
        url = deepseek_url()
        key = deepseek_key()
        model = deepseek_model()
        if not key:
            raise VisionError("未配置 DEEPSEEK_API_KEY")
    else:
        url = DASHSCOPE_DEFAULT_URL
        key = dashscope_key()
        model = dashscope_model()
        if not key:
            raise VisionError("未配置 QWEN_API_KEY / DASHSCOPE_API_KEY")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        raise VisionError(f"请求超时（>{timeout:g}s）")
    except requests.exceptions.RequestException as exc:
        raise VisionError(f"网络请求失败: {exc}")

    if response.status_code == 401:
        raise VisionError("鉴权失败（401），Key 无效或已过期")
    if response.status_code == 429:
        raise VisionError("请求过于频繁（429）")
    if response.status_code >= 400:
        detail = _short_body(response)
        raise VisionError(f"HTTP {response.status_code} {detail}".strip())

    try:
        data = response.json()
    except ValueError:
        raise VisionError(f"响应不是 JSON: {_short_body(response)}")

    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise VisionError(f"API 返回错误: {message}")
    return data


def _short_body(response: Any, limit: int = 200) -> str:
    try:
        return str(response.text)[:limit]
    except Exception:
        return ""


def _extract_answer(data: Dict[str, Any]) -> str:
    """``content`` 可能是字符串，也可能是 [{'type':'text','text':...}] 这种块数组"""
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    return ""


def describe_response(answer: str) -> str:
    """给命令行/脚本打日志用（截断）"""
    answer = (answer or "").replace("\n", " ").strip()
    return answer if len(answer) <= 60 else answer[:60] + "…"
