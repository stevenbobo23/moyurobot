#!/usr/bin/env python
"""
音频对讲 WebSocket 服务

给网页端提供两条音频通道（独立端口，默认 8081）：

    车端麦克风 --(二进制 PCM 帧)--> 浏览器    # 监听，像云摄像头开声音
    浏览器麦克风 --(二进制 PCM 帧)--> 车端扬声器  # 按住说话对讲

为什么不用 Flask 路由：
- WSGI 不擅长长连接 + 二进制流，Flask 那套 MJPEG 已经是"够用就行"的做法；
- `websockets` 已经是项目依赖，独立 asyncio 服务跑在守护线程里，不加新依赖、
  不碰现有 HTTP 服务的稳定性，出问题也只影响音频一块。

协议（文本帧 = JSON 控制消息，二进制帧 = S16_LE 裸 PCM）：
    浏览器 -> 服务端:  {"type":"monitor","on":true}
                      {"type":"ptt","on":true}         # 按住说话
                      {"type":"select","capture":"plughw:1,0","playback":"default"}
                      {"type":"ping"}
    服务端 -> 浏览器:  {"type":"hello"|"state"|"error"|"pong", ...}
                      二进制帧 = 车端麦克风 PCM
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote

from moyurobot import audio_token
from moyurobot.core.audio import AudioConfig, IntercomHub, get_hub
from moyurobot.public_url import append_query, resolve_ws_url

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_PATH = "/audio"
STATE_INTERVAL_S = 0.15
CLIENT_QUEUE_SIZE = 32
TALK_TIMEOUT_S = 300.0


def parse_cookies(cookie_header: Optional[str]) -> Dict[str, str]:
    """极简 Cookie 解析（WebSocket 握手会带上同源 Cookie，包括跨端口）"""
    out: Dict[str, str] = {}
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key.strip()] = unquote(value.strip())
    return out


def _header(ws, name: str, default: Optional[str] = None) -> Optional[str]:
    """兼容 websockets 新旧实现的取头方式"""
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        headers = getattr(ws, "request_headers", None)
    if headers is None:
        return default
    try:
        return headers.get(name, default)
    except Exception:
        return default


def _request_path(ws) -> str:
    """握手请求的路径（**含查询串**，令牌就在里面），兼容 websockets 新旧实现"""
    candidates: List[str] = []
    request = getattr(ws, "request", None)
    for holder in (request, ws):
        for attr in ("path", "raw_path"):
            value = getattr(holder, attr, None)
            if isinstance(value, str) and value:
                candidates.append(value)
    for value in candidates:      # 优先取带查询串的那个，令牌只可能在那儿
        if "?" in value:
            return value
    return candidates[0] if candidates else ""


@dataclass
class _Client:
    id: str
    ws: object
    username: Optional[str] = None
    user_id: Optional[str] = None
    can_monitor: bool = False
    can_talk: bool = False
    monitor: bool = False
    ptt: bool = False
    ptt_started_at: float = 0.0
    listener_token: Optional[int] = None
    queue: "asyncio.Queue" = None  # type: ignore[assignment]
    remote_address: str = ""
    connected_at: float = field(default_factory=time.time)
    closed: bool = False


class AudioWebSocketServer:
    """音频 WebSocket 服务（跑在守护线程的独立事件循环里）"""

    def __init__(
        self,
        hub: Optional[IntercomHub] = None,
        config: Optional[AudioConfig] = None,
        auth_fn: Optional[Callable[[Optional[str], Optional[str]], Dict]] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        path: str = DEFAULT_PATH,
    ):
        self.hub = hub or get_hub(config)
        self.config = self.hub.config
        self.auth_fn = auth_fn
        self.host = host or self.config.ws_host
        self.port = int(port or self.config.ws_port)
        self.path = path

        self._clients: Dict[str, _Client] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._started = threading.Event()
        self._start_error: Optional[str] = None
        self._serving = False

    # ---------------- 外部接口 ----------------

    @property
    def running(self) -> bool:
        return self._serving

    @property
    def start_error(self) -> Optional[str]:
        return self._start_error

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self, wait: float = 3.0) -> bool:
        """启动服务；wait 秒内未就绪则返回 False"""
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._run, name="moyu-audio-ws", daemon=True
        )
        self._thread.start()
        ok = self._started.wait(timeout=wait)
        if not ok:
            self._start_error = self._start_error or "音频服务启动超时"
            return False
        return self._serving

    def stop(self):
        loop = self._loop
        if loop and not loop.is_closed() and self._stop_event:
            try:
                loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._serving = False

    def describe_state(self) -> Dict:
        return {
            "running": self._serving,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "public_url": (self.config.ws_public_url or "").strip(),
            "token_secret": audio_token.secret_source(),
            "token_ttl": audio_token.token_ttl(),
            "clients": self.client_count,
            "listeners": self.hub.listener_count,
            "error": self._start_error,
        }

    def build_client_config(
        self,
        request=None,
        ws_public_url: Optional[str] = None,
        username: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict:
        """生成注入到网页的配置（含根据当前访问方式推导出的 ws 地址）

        地址优先用 ``AUDIO_WS_PUBLIC_URL`` / ``config/default.json`` 里配的值
        （反代、多域名场景必须配，见 moyurobot/public_url.py），没配才按当前请求推导。

        ``username`` / ``user_id`` 是**网页域名**上的登录身份：给了就把身份签成令牌拼进
        ``ws_url``（见 moyurobot/audio_token.py）。网页和音频不同域名时，Cookie 不会跟着
        WebSocket 握手跑过去，令牌是唯一可靠的身份来源；同域直连时带着也无害。
        """
        hub_state = self.hub.state()

        configured = (ws_public_url or self.config.ws_public_url or "").strip()
        base_url, url_source = resolve_ws_url(
            configured, request=request, port=self.port, path=self.path
        )

        token = audio_token.mint(username, user_id) if username else ""
        sim = self.hub.backend == "sim"
        return {
            "available": bool(self.config.enabled),
            "running": self._serving,
            "ws_url": append_query(base_url, audio_token.TOKEN_PARAM, token),
            "ws_url_base": base_url,
            "ws_url_source": url_source,
            "ws_public_url": configured,
            "logged_in": bool(username),
            "token_param": audio_token.TOKEN_PARAM,
            "token_ttl": audio_token.token_ttl(),
            "path": self.path,
            "sample_rate": self.config.sample_rate,
            "channels": self.config.channels,
            "chunk_ms": self.config.chunk_ms,
            "backend": self.hub.backend,
            "simulated": sim,
            "max_clients": self.config.max_clients,
            "state": hub_state,
            "error": self._start_error,
        }

    # ---------------- 线程 + 事件循环 ----------------

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:  # 端口占用等
            self._start_error = f"音频服务启动失败: {e}"
            logger.error(self._start_error)
            self._started.set()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    async def _serve(self):
        try:
            import websockets
        except ImportError:
            self._start_error = "缺少 websockets 依赖：pip install websockets"
            logger.error(self._start_error)
            self._started.set()
            return

        self._stop_event = asyncio.Event()
        try:
            async with websockets.serve(
                self._handle,
                self.host,
                self.port,
                max_size=2 ** 20,
                compression=None,
                ping_interval=20,
                ping_timeout=20,
            ):
                self._serving = True
                self._started.set()
                logger.info(
                    "音频对讲 WebSocket 已启动: ws://%s:%d%s（后端=%s）",
                    self.host, self.port, self.path, self.hub.backend,
                )
                state_task = asyncio.create_task(self._state_broadcaster())
                try:
                    await self._stop_event.wait()
                finally:
                    state_task.cancel()
        except OSError as e:
            self._start_error = f"音频端口 {self.port} 不可用: {e}"
            logger.error(self._start_error)
            self._started.set()
        finally:
            for client in list(self._clients.values()):
                await self._cleanup_client(client)
            self._serving = False

    # ---------------- 连接处理 ----------------

    async def _handle(self, websocket, path=None):
        req_path = path or getattr(getattr(websocket, "request", None), "path", self.path) or self.path
        if not str(req_path).startswith(self.path):
            logger.warning("音频连接路径不匹配: %s", req_path)
            await self._safe_close(websocket)
            return

        if len(self._clients) >= self.config.max_clients:
            await self._send_json(websocket, {
                "type": "error",
                "message": f"监听人数已满（上限 {self.config.max_clients}）",
            })
            await self._safe_close(websocket)
            return

        auth = self._authorize(websocket, raw_path=req_path)
        if not auth.get("ok"):
            await self._send_json(websocket, {
                "type": "error",
                "message": auth.get("reason", "未授权"),
                "auth_source": auth.get("auth_source", "none"),
            })
            await self._safe_close(websocket)
            return

        peer = ""
        try:
            addr = getattr(websocket, "remote_address", None)
            if addr:
                peer = f"{addr[0]}:{addr[1]}"
        except Exception:
            pass

        client = _Client(
            id=uuid.uuid4().hex[:8],
            ws=websocket,
            username=auth.get("username"),
            user_id=auth.get("user_id"),
            can_monitor=bool(auth.get("can_monitor")),
            can_talk=bool(auth.get("can_talk")),
            queue=asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE),
            remote_address=peer,
        )
        self._clients[client.id] = client
        logger.info(
            "音频客户端接入 %s (%s, 用户=%s, 身份来源=%s)",
            client.id, peer, client.username, auth.get("auth_source", "none"),
        )

        send_task = asyncio.create_task(self._sender(client))
        try:
            await self._send_json(websocket, self._hello(client))
            await self._recv_loop(client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("音频客户端 %s 异常: %s", client.id, e)
        finally:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._cleanup_client(client)
            logger.info("音频客户端断开 %s (用户=%s)", client.id, client.username)

    def _authorize(self, websocket, raw_path: Optional[str] = None) -> Dict:
        """鉴权顺序：地址里的令牌 -> 握手 Cookie -> 未登录

        令牌优先是因为它跨域名有效：网页和音频不同域名时，浏览器不会把网页的 Cookie
        带到音频域名的握手上，只有令牌能证明身份。Cookie 分支保留给同域/局域网直连
        以及老页面（缓存里的旧 JS 不会拼令牌）。
        """
        user_id = None
        username = None
        source = "none"

        token = audio_token.token_from_path(
            raw_path if raw_path is not None else _request_path(websocket)
        )
        if token:
            claims = audio_token.verify(token)
            if claims:
                user_id = claims.get("user_id")
                username = claims.get("username")
                source = "token"
            else:
                logger.info("音频握手令牌无效或已过期，回退检查 Cookie")

        if not username:
            cookies = parse_cookies(_header(websocket, "Cookie"))
            user_id = cookies.get("moyu_user_id")
            username = cookies.get("moyu_username")
            if username:
                source = "cookie"

        if self.auth_fn is not None:
            try:
                result = self.auth_fn(user_id, username)
                result.setdefault("user_id", user_id)
                result.setdefault("username", username)
                result["auth_source"] = source
                return result
            except Exception as e:
                return {"ok": False, "reason": f"鉴权失败: {e}", "auth_source": source}
        return {
            "ok": True,
            "user_id": user_id,
            "username": username,
            "can_monitor": True,
            "can_talk": True,
            "reason": "",
            "auth_source": source,
        }

    def _hello(self, client: _Client) -> Dict:
        caps = self.hub.capabilities()
        return {
            "type": "hello",
            "version": PROTOCOL_VERSION,
            "client_id": client.id,
            "username": client.username,
            "sample_rate": self.config.sample_rate,
            "channels": self.config.channels,
            "chunk_ms": self.config.chunk_ms,
            "chunk_bytes": self.config.chunk_bytes,
            "backend": self.hub.backend,
            "simulated": self.hub.backend == "sim",
            "can_monitor": client.can_monitor,
            "can_talk": client.can_talk,
            "max_clients": self.config.max_clients,
            "devices": self.hub.device_selection(),
            "options": {
                "capture_devices": caps.get("capture_devices", []),
                "playback_devices": caps.get("playback_devices", []),
            },
            "hints": caps.get("hints", []),
            "state": self.hub.state(),
        }

    async def _recv_loop(self, client: _Client):
        async for message in client.ws:
            if isinstance(message, (bytes, bytearray, memoryview)):
                if client.ptt:
                    self.hub.write_talk(client.id, bytes(message))
                continue
            try:
                payload = json.loads(message)
            except (TypeError, ValueError):
                logger.debug("音频客户端 %s 发送了非法 JSON", client.id)
                continue
            if isinstance(payload, dict):
                await self._dispatch(client, payload)

    async def _dispatch(self, client: _Client, payload: Dict):
        msg_type = str(payload.get("type") or "").lower()

        if msg_type == "monitor":
            await self._set_monitor(client, bool(payload.get("on")))
        elif msg_type == "ptt":
            await self._set_ptt(client, bool(payload.get("on")))
        elif msg_type == "select":
            if not client.can_talk and not client.can_monitor:
                return
            result = self.hub.set_devices(
                capture=payload.get("capture"), playback=payload.get("playback")
            )
            await self._send_json(client.ws, {"type": "devices", **result})
            await self._broadcast_state()
        elif msg_type == "ping":
            await self._send_json(client.ws, {"type": "pong", "t": time.time()})
        elif msg_type == "hello":
            await self._send_json(client.ws, self._hello(client))
        else:
            await self._send_json(client.ws, {
                "type": "error", "message": f"未知消息类型: {msg_type}"
            })

    async def _set_monitor(self, client: _Client, on: bool):
        if not client.can_monitor:
            await self._send_json(client.ws, {"type": "error", "message": "没有监听权限"})
            return
        if on and client.listener_token is None:
            if not self.config.enabled:
                await self._send_json(client.ws, {"type": "error", "message": "音频功能已在服务端关闭"})
                return
            client.listener_token = self.hub.add_listener(
                lambda data, c=client: self._push_remote(c, data)
            )
            client.monitor = True
        elif not on and client.listener_token is not None:
            self.hub.remove_listener(client.listener_token)
            client.listener_token = None
            client.monitor = False
            self._offer(client, {"type": "monitor_stopped"})
        await self._send_json(client.ws, {
            "type": "monitor",
            "on": client.monitor,
            "listeners": self.hub.listener_count,
        })
        await self._broadcast_state()

    async def _set_ptt(self, client: _Client, on: bool):
        if on:
            # 控制权可能已经易主，说话前重新鉴权一次
            if self.auth_fn is not None:
                try:
                    refreshed = self.auth_fn(client.user_id, client.username)
                    client.can_talk = bool(refreshed.get("can_talk"))
                    client.can_monitor = bool(refreshed.get("can_monitor", client.can_monitor))
                except Exception as e:
                    logger.debug("对讲前重新鉴权失败: %s", e)
            if not client.can_talk:
                await self._send_json(client.ws, {
                    "type": "error",
                    "message": "当前控制权不在你手上，无法对讲（或会话已过期，刷新页面重试）",
                })
                await self._broadcast_state()
                return
            if client.ptt:
                return
            result = self.hub.begin_talk(client.id)
            if not result.get("ok"):
                await self._send_json(client.ws, {"type": "error", "message": result.get("reason")})
                await self._broadcast_state()
                return
            client.ptt = True
            client.ptt_started_at = time.time()
            await self._send_json(client.ws, {"type": "ptt", "on": True})
        else:
            if not client.ptt:
                return
            self.hub.end_talk(client.id)
            client.ptt = False
            await self._send_json(client.ws, {"type": "ptt", "on": False})
        await self._broadcast_state()

    # ---------------- 数据下发 ----------------

    def _push_remote(self, client: _Client, data: bytes):
        """在采集线程被调用：把车端 PCM 送到该客户端的事件循环队列"""
        if client.closed or not client.monitor:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._offer, client, data)
        except RuntimeError:
            pass

    def _offer(self, client: _Client, item):
        """在事件循环线程被调用：队列满则丢最旧的一帧（宁可丢帧也不要涨延迟）"""
        if client.closed or client.queue is None:
            return
        try:
            client.queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            client.queue.get_nowait()
        except Exception:
            pass
        try:
            client.queue.put_nowait(item)
        except Exception:
            pass

    async def _sender(self, client: _Client):
        while not client.closed:
            try:
                item = await client.queue.get()
            except asyncio.CancelledError:
                raise
            if item is None:
                return
            try:
                if isinstance(item, (bytes, bytearray)):
                    # 半双工时不再下发，避免把对讲者的声音回灌
                    if client.monitor and not self.hub.ptt_active:
                        await client.ws.send(item)
                else:
                    await client.ws.send(json.dumps(item, ensure_ascii=False))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("音频下发失败 %s: %s", client.id, e)
                return

    async def _send_json(self, ws, payload: Dict):
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug("发送 JSON 失败: %s", e)

    async def _safe_close(self, ws):
        try:
            await ws.close()
        except Exception:
            pass

    # ---------------- 状态广播 ----------------

    async def _state_broadcaster(self):
        while True:
            await asyncio.sleep(STATE_INTERVAL_S)
            # 对讲超时保护：客户端崩溃/网络中断时不会永久占麦
            now = time.time()
            for client in list(self._clients.values()):
                if client.ptt and now - client.ptt_started_at > TALK_TIMEOUT_S:
                    logger.warning("客户端 %s 对讲超时，强制结束", client.id)
                    self.hub.end_talk(client.id)
                    client.ptt = False
                    await self._send_json(client.ws, {
                        "type": "error", "message": "本次对讲超过时长上限，已自动结束",
                    })
            await self._broadcast_state()

    async def _broadcast_state(self):
        if not self._clients:
            return
        base = self.hub.state()
        for client in list(self._clients.values()):
            payload = dict(base)
            payload.update({
                "type": "state",
                "monitor": client.monitor,
                "ptt": client.ptt,
                "can_talk": client.can_talk,
                "can_monitor": client.can_monitor,
                "clients": len(self._clients),
            })
            self._offer(client, payload)

    async def _cleanup_client(self, client: _Client):
        if client.closed:
            return
        client.closed = True
        if client.listener_token is not None:
            self.hub.remove_listener(client.listener_token)
            client.listener_token = None
        client.monitor = False
        self.hub.release_client(client.id)
        client.ptt = False
        self._clients.pop(client.id, None)
        try:
            self._offer(client, None)
        except Exception:
            pass
        await self._broadcast_state()


# --------------------------------------------------------------------------- #
# 模块级单例，方便 controller 调用
# --------------------------------------------------------------------------- #

_server: Optional[AudioWebSocketServer] = None
_server_lock = threading.Lock()
_last_config: Optional[AudioConfig] = None


def get_server() -> Optional[AudioWebSocketServer]:
    return _server


def get_config() -> AudioConfig:
    global _last_config
    if _last_config is None:
        _last_config = AudioConfig.load()
    return _last_config


def start_audio_server(
    config: Optional[AudioConfig] = None,
    auth_fn: Optional[Callable[[Optional[str], Optional[str]], Dict]] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Optional[AudioWebSocketServer]:
    """启动全局音频服务（幂等）"""
    global _server, _last_config
    with _server_lock:
        cfg = config or get_config()
        _last_config = cfg
        if not cfg.enabled:
            logger.info("音频功能已禁用（AUDIO_ENABLED=0）")
            return None
        if _server is not None and _server.running:
            return _server
        hub = get_hub(cfg)
        hub.start()
        server = AudioWebSocketServer(hub=hub, config=cfg, auth_fn=auth_fn, host=host, port=port)
        if server.start():
            _server = server
            return server
        logger.error("音频服务未能启动: %s", server.start_error)
        server.stop()
        _server = server  # 保留引用以便 /audio/info 报告错误
        return None


def stop_audio_server():
    global _server
    with _server_lock:
        if _server is not None:
            _server.stop()
            _server = None
        try:
            from moyurobot.core.audio import get_hub as _gh
            _gh().shutdown()
        except Exception:
            pass


def build_client_config(
    request=None,
    username: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict:
    cfg = get_config()
    if _server is not None:
        return _server.build_client_config(
            request=request, username=username, user_id=user_id
        )

    # 服务没起来时也给出同样的字段（地址 + 令牌）：调用方不用区分两种情况，
    # 自检面板也能照常显示「地址对不对、令牌有没有」，排起错来少一个分支
    base_url, url_source = resolve_ws_url(
        (cfg.ws_public_url or "").strip(),
        request=request,
        port=int(cfg.ws_port or 8081),
        path=DEFAULT_PATH,
    )
    token = audio_token.mint(username, user_id) if username else ""
    return {
        "available": bool(cfg.enabled),
        "running": False,
        "ws_url": append_query(base_url, audio_token.TOKEN_PARAM, token),
        "ws_url_base": base_url,
        "ws_url_source": url_source,
        "ws_public_url": (cfg.ws_public_url or "").strip(),
        "logged_in": bool(username),
        "token_param": audio_token.TOKEN_PARAM,
        "token_ttl": audio_token.token_ttl(),
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "chunk_ms": cfg.chunk_ms,
        "backend": None,
        "simulated": False,
        "state": {},
        "error": "音频服务未启动",
    }


__all__: List[str] = [
    "AudioWebSocketServer",
    "start_audio_server",
    "stop_audio_server",
    "get_server",
    "get_config",
    "build_client_config",
    "parse_cookies",
]
