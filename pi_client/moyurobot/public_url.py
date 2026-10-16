#!/usr/bin/env python
"""对外访问地址解析

服务只知道自己在 ``0.0.0.0:8081`` 上监听，但浏览器要连的是「域名 + 端口」——
中间隔了反向代理 / 内网穿透时，这两者并不等价（比如网页在
``https://lerobot.mycodebro.cn/``，音频却在 ``wss://lerobotwws.mycodebro.cn/``）。
本模块负责把配置里的公开地址换算成浏览器真正能连的 ws 地址。

只依赖标准库（放在包顶层，任何环境都能导入、能单独测试）。

``AUDIO_WS_PUBLIC_URL`` 支持的写法：

    https://lerobotwws.mycodebro.cn/        -> wss://lerobotwws.mycodebro.cn/audio
    wss://lerobotwws.mycodebro.cn/audio     -> 原样使用（已带路径）
    wss://lerobotwws.mycodebro.cn           -> 自动补上 /audio
    wss://lerobotwws.mycodebro.cn:9443/audio-> 显式端口照原样保留
    wss://{host}/audio                      -> {host}/{port}/{path} 占位符按当前请求替换
    auto / none / - / 留空                   -> 不手工配置，按当前请求自动推导

两个容易踩的点：
1. 只写域名时**不会**自作主张补 ``:8081`` —— 前面挂了反代就该是 443/80；
2. 协议写 http/https 也能用，会自动换成 ws/wss（手上只有浏览器地址时直接抄）。
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# 这些取值表示「不手工指定」，交回按请求自动推导
_AUTO_TOKENS = frozenset({"", "auto", "none", "-", "default"})

# http/https 也接受：很多人手上只有浏览器里的地址
_SCHEME_MAP = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}


# --------------------------------------------------------------------------- #
# 请求侧小工具（不 import flask，测试里给个带 host/is_secure 的对象即可）
# --------------------------------------------------------------------------- #


def request_host_header(request) -> str:
    """取请求的 Host 头（形如 ``example.com:8080``），取不到返回空串"""
    if request is None:
        return ""
    host = ""
    try:
        host = request.host or ""
    except Exception:
        host = ""
    if not host:
        try:
            headers = getattr(request, "headers", None)
            host = (headers.get("Host") if headers is not None else "") or ""
        except Exception:
            host = ""
    return str(host).strip()


def split_host_port(raw: str) -> Tuple[str, Optional[int]]:
    """拆分 ``host[:port]``，兼容 ``[::1]:8080`` 这种 IPv6 写法"""
    raw = (raw or "").strip()
    if not raw:
        return "", None
    if raw.startswith("["):                      # [::1]:8080
        end = raw.find("]")
        if end == -1:
            return raw, None
        host, rest = raw[: end + 1], raw[end + 1:]
        return host, _to_int(rest[1:]) if rest.startswith(":") else None
    if raw.count(":") == 1:                      # example.com:8080
        host, _, port = raw.partition(":")
        return host, _to_int(port)
    return raw, None


def _to_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def request_is_secure(request) -> bool:
    """当前访问是不是 HTTPS（AUDIO_WS_TLS=1 可手动兜住反代不转发协议的情况）"""
    secure = False
    if request is not None:
        try:
            secure = bool(request.is_secure)
        except Exception:
            secure = False
    return bool(secure) or os.environ.get("AUDIO_WS_TLS") == "1"


# --------------------------------------------------------------------------- #
# 主逻辑
# --------------------------------------------------------------------------- #


def normalize_public_ws_url(
    raw,
    *,
    request=None,
    port: Optional[int] = None,
    path: str = "/audio",
) -> Optional[str]:
    """把手工配置的公开地址规范化成可用的 ws 地址

    返回 ``None`` 表示「没配置 / 配置不可用」，调用方应退回自动推导。
    """
    value = str(raw or "").strip()
    if value.lower() in _AUTO_TOKENS:
        return None

    path = path or "/audio"
    hostname, _ = split_host_port(request_host_header(request))

    value = (
        value.replace("{host}", hostname)
        .replace("{hostname}", hostname)
        .replace("{port}", "" if port is None else str(port))
        .replace("{path}", path)
    )
    if "://" not in value:                       # 允许只写域名或 域名:端口
        value = "//" + value.lstrip("/")

    parts = urlsplit(value)
    if parts.scheme:
        scheme = _SCHEME_MAP.get(parts.scheme.lower())
        if scheme is None:
            logger.warning(
                "无法识别的对外地址（协议只支持 ws/wss/http/https）：%r", raw
            )
            return None
    else:
        scheme = "wss" if request_is_secure(request) else "ws"

    netloc = parts.netloc or hostname
    if not netloc:
        logger.warning("对外地址缺少域名，且请求头里也没有 Host，无法使用：%r", raw)
        return None

    req_path = parts.path or ""
    if req_path in ("", "/"):                    # 只给了域名 -> 补上音频路径
        req_path = path
    elif len(req_path) > 1:
        req_path = req_path.rstrip("/")

    return urlunsplit((scheme, netloc, req_path, parts.query, ""))


def resolve_ws_url(
    raw,
    *,
    request=None,
    port: int = 8081,
    path: str = "/audio",
) -> Tuple[str, str]:
    """返回 ``(浏览器可用的 ws 地址, 来源)``，来源为 ``config`` 或 ``auto``"""
    configured = normalize_public_ws_url(raw, request=request, port=port, path=path)
    if configured:
        return configured, "config"

    host_header = request_host_header(request)
    hostname, explicit_port = split_host_port(host_header)
    if not hostname:
        hostname = "127.0.0.1"

    secure = request_is_secure(request)
    scheme = "wss" if secure else "ws"

    # HTTPS 且访问地址没带端口，说明前面是标准端口的 TLS 反代，
    # 这时再拼 :8081 是错的（也不是反代的入口），按「同域同路径」推导，
    # 由反代把 /audio 转发到音频服务；显式写了端口（如 https://host:8443）则照旧拼监听端口。
    if secure and explicit_port is None:
        return f"{scheme}://{hostname}{path}", "auto"
    return f"{scheme}://{hostname}:{port}{path}", "auto"


__all__ = [
    "resolve_ws_url",
    "normalize_public_ws_url",
    "request_host_header",
    "request_is_secure",
    "split_host_port",
]
