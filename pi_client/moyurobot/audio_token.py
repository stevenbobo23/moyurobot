#!/usr/bin/env python
"""语音对讲的连接令牌（WebSocket 身份票据）

**为什么需要它**

网页和音频服务是两个域名（网页 ``lerobot.mycodebro.cn``、音频
``lerobotwws.mycodebro.cn``）。会话 Cookie 是 host-only 的，属于网页域名，浏览器
**不会**把它带到音频域名的 WebSocket 握手上 —— 服务端读不到用户名，只能回一句
「未登录：请先进入控制台页面再使用语音对讲」。

让 Cookie 跨子域（``SESSION_COOKIE_DOMAIN=.mycodebro.cn``）是一种办法，但它要求改配置、
重新登录，还会被浏览器各自的 Cookie 策略波及。把身份直接签进 WebSocket 地址更稳：

    wss://lerobotwws.mycodebro.cn/audio?token=<签名>

登录后的网页服务签发令牌（用户身份来自**网页域名**上的 Cookie，那里一定有），
音频服务独立验签，两边不共享存储、不共享 Cookie。

**格式**

    v1.<base64url(JSON 负载)>.<base64url(HMAC-SHA256 签名)>
    负载 = {"u": 用户名, "i": 会话 ID, "e": 过期时间戳}

改一个字节验签就不过；过期即失效。令牌里没有任何密钥信息，泄漏一个令牌也只影响
这一条连接的身份（等同于一串临时的用户名）。

**密钥**

取环境变量 ``AUDIO_TOKEN_SECRET``；没配就用进程内随机值 —— 签发（Flask 线程）和
校验（音频服务线程）在**同一个进程**里，所以开箱即用、无需配置。只有把网页服务和
音频服务拆成两个进程部署时，才必须显式配置同一个密钥。

只用标准库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Dict, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

#: WebSocket 地址里携带令牌的参数名，前端拼地址时会用到
TOKEN_PARAM = "token"

_VERSION = "v1"

#: 默认有效期（秒）。页面常驻时会靠 /audio/info 重新拉取刷新，所以不必设得很长
DEFAULT_TTL_SECONDS = 6 * 3600

# 进程内随机密钥：签发与校验同进程，无需任何配置
_EPHEMERAL_SECRET = secrets.token_bytes(32)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s 不是整数（%r），按默认值 %d 处理", name, raw, default)
        return default
    return value if value > 0 else default


def token_ttl() -> int:
    """令牌有效期（秒），AUDIO_TOKEN_TTL 可覆盖"""
    return _env_int("AUDIO_TOKEN_TTL", DEFAULT_TTL_SECONDS)


def secret() -> bytes:
    """签名密钥。配了 AUDIO_TOKEN_SECRET 就用它，否则用进程内随机值"""
    raw = (os.environ.get("AUDIO_TOKEN_SECRET") or "").strip()
    if raw:
        # 任意长度都能当密钥：先哈希成 32 字节，省得纠结密钥长度
        return hashlib.sha256(raw.encode("utf-8")).digest()
    return _EPHEMERAL_SECRET


def secret_source() -> str:
    """密钥来源，给自检面板用：env = 显式配置，ephemeral = 进程内随机"""
    return "env" if (os.environ.get("AUDIO_TOKEN_SECRET") or "").strip() else "ephemeral"


# --------------------------------------------------------------------------- #
# base64url（去 padding，便于直接放进查询串）
# --------------------------------------------------------------------------- #


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> Optional[bytes]:
    try:
        padded = text + "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 签发 / 校验
# --------------------------------------------------------------------------- #


def mint(
    username: Optional[str],
    user_id: Optional[str] = None,
    ttl: Optional[int] = None,
    now: Optional[float] = None,
) -> str:
    """为用户签发令牌；username 为空（未登录）时返回空串"""
    if not username:
        return ""

    issued = float(now if now is not None else time.time())
    payload = {
        "u": username,
        "i": user_id or "",
        "e": int(issued + (ttl if ttl is not None else token_ttl())),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _b64e(hmac.new(secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{_VERSION}.{body}.{signature}"


def verify(token: Optional[str], now: Optional[float] = None) -> Optional[Dict]:
    """校验令牌，通过返回 ``{"username", "user_id", "exp"}``，否则返回 None"""
    raw = (token or "").strip()
    if not raw:
        return None

    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != _VERSION:
        return None
    _, body, signature = parts

    expected = _b64e(hmac.new(secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        return None

    decoded = _b64d(body)
    if decoded is None:
        return None
    try:
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    expires = payload.get("e")
    if not isinstance(expires, (int, float)):
        return None
    if float(now if now is not None else time.time()) >= float(expires):
        return None

    username = (payload.get("u") or "").strip()
    if not username:
        return None

    return {
        "username": username,
        "user_id": payload.get("i") or None,
        "exp": int(expires),
    }


# --------------------------------------------------------------------------- #
# 握手侧小工具
# --------------------------------------------------------------------------- #


def token_from_path(path: Optional[str]) -> str:
    """从 WebSocket 的请求路径里取令牌（``/audio?token=xxx``）

    握手路径形如 ``/audio?token=v1.xxx.yyy``；取不到或格式不对都返回空串。
    路径里的其它参数一律忽略。
    """
    raw = str(path or "")
    if "?" not in raw:
        return ""
    query = raw.split("?", 1)[1]
    try:
        values = parse_qs(query, keep_blank_values=False).get(TOKEN_PARAM) or []
    except Exception:
        return ""
    return values[0].strip() if values else ""


__all__ = [
    "TOKEN_PARAM",
    "DEFAULT_TTL_SECONDS",
    "mint",
    "verify",
    "token_ttl",
    "secret",
    "secret_source",
    "token_from_path",
]
