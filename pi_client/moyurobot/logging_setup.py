#!/usr/bin/env python
"""
统一日志配置 —— 防刷屏 / 防日志撑爆磁盘

背景
----
LeRobot 的摄像头（``lerobot.cameras.opencv.camera_opencv``）在设备掉线或读取失败时，
会在采集后台线程里按帧率（30fps）反复打印同一句 WARNING：

    Error reading frame in background thread for OpenCVCamera(/dev/video0): ... read failed

一晚上就能写满几个 GB，把树莓派的 SD 卡写爆。

三层手段
--------
1. **去重 + 指数退避**（:class:`DedupBackoffFilter`）
   同一条消息先正常打印，之后按 1s → 2s → 4s → … 最长 300s 的间隔重报，
   期间被压掉多少条会挂在下一条消息后面。持续故障的日志量从 O(时间) 变成 O(log 时间)。
2. **限速**（:class:`RateLimitFilter`）
   单个 logger 每秒最多放行 N 条，防止"每条内容都不一样"的日志洪流。
3. **滚动落盘**（:class:`logging.handlers.RotatingFileHandler`，可选）
   限制单文件大小 + 备份数量，磁盘占用有硬上限。

环境变量（全部可选，见 ``config/env.example``）
------------------------------------------------
=======================  ======================  ==========================================
变量                      默认值                   说明
=======================  ======================  ==========================================
LOG_LEVEL                INFO                    根日志级别
LOG_NOISY_LOGGERS        见 DEFAULT_NOISY_...    需要单独设级别的 logger 前缀，逗号分隔
LOG_NOISY_LEVEL          WARNING                 上述 logger 的级别（设 ERROR 可彻底静音摄像头刷屏）
LOG_DEDUP_WINDOW_S       1.0                     同一消息的初始抑制窗口（秒），0 = 关闭去重
LOG_DEDUP_MAX_WINDOW_S   300                     指数退避上限（秒）
LOG_RATE_LIMIT_PER_S     20                      单 logger 每秒最大条数，0 = 关闭限速
LOG_TO_FILE              0（或 LOG_DIR 存在时为 1） 是否额外写日志文件
LOG_DIR                  ~/logs                  日志目录
LOG_FILE                 moyurobot.log           日志文件名
LOG_MAX_BYTES            5242880                 单文件上限（默认 5MB）
LOG_BACKUP_COUNT         3                       滚动备份数量
=======================  ======================  ==========================================

用法::

    from moyurobot.logging_setup import setup_logging
    setup_logging()            # 幂等，重复调用无副作用

放在包顶层（而不是 ``moyurobot.core``）是刻意的：``core/__init__`` 会连带导入
numpy / lerobot，而这个模块只依赖标准库，应该能在任何环境下最先配置好日志。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "DedupBackoffFilter",
    "RateLimitFilter",
    "DEFAULT_NOISY_LOGGERS",
    "setup_logging",
    "get_log_file_path",
]

logger = logging.getLogger(__name__)

# 默认需要单独设级别的"噪声源"。注意：这里只调级别，不删消息，
# 想彻底静音摄像头刷屏可以设 LOG_NOISY_LEVEL=ERROR。
DEFAULT_NOISY_LOGGERS: Tuple[str, ...] = (
    "lerobot.cameras",
    "lerobot.cameras.opencv",
    "lerobot.cameras.opencv.camera_opencv",
    "PIL",
    "matplotlib",
    "urllib3",
    "httpx",
    "httpcore",
    "werkzeug",
    "asyncio",
)

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
MAX_ANNOTATED_MESSAGE = 500  # 附带抑制计数的消息长度上限，避免把超长日志改得更长


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("环境变量 %s=%r 不是合法数字，使用默认值 %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def parse_level(value) -> int:
    """把 ``INFO`` / ``20`` / ``logging.INFO`` 之类的写法统一转成 level 数字。"""
    if isinstance(value, int):
        return value
    if value is None:
        return logging.INFO
    text = str(value).strip().upper()
    if not text:
        return logging.INFO
    if text.isdigit():
        return int(text)
    return logging.getLevelName(text) if isinstance(logging.getLevelName(text), int) else logging.INFO


class _OncePerRecordFilter(logging.Filter):
    """保证同一个 LogRecord 只被判定一次

    一条记录会依次经过所有附加了该过滤器的 handler（stderr、文件）。
    如果每次都真的判定，计数会被重复累加、去重器还会把第二条 handler 当成"重复"而丢弃，
    结果就是第二个 handler 什么都收不到。这里用记录上的标记缓存判定结果。
    """

    _MARKS_ATTR = "_moyurobot_filter_marks"

    def filter(self, record: logging.LogRecord) -> bool:
        marks = getattr(record, self._MARKS_ATTR, None)
        if marks is None:
            marks = {}
            try:
                setattr(record, self._MARKS_ATTR, marks)
            except Exception:
                return self._decide(record)
        if self.name in marks:
            return marks[self.name]
        verdict = self._decide(record)
        marks[self.name] = verdict
        return verdict

    def _decide(self, record: logging.LogRecord) -> bool:  # pragma: no cover - 由子类实现
        raise NotImplementedError


class _MsgState:
    """单个消息键的抑制状态"""

    __slots__ = ("first_ts", "last_ts", "next_allowed", "interval", "suppressed")

    def __init__(self, now: float, interval: float):
        self.first_ts = now
        self.last_ts = now
        self.next_allowed = now + interval
        self.interval = interval
        self.suppressed = 0


class DedupBackoffFilter(_OncePerRecordFilter):
    """同一消息去重 + 指数退避重报

    判定键为 ``(level, logger 名, 格式化后的消息)``，所以摄像头对不同设备
    （``/dev/video0``、``/dev/video2``…）打印的警告会各自独立计数。

    行为：第 1 条立即放行；窗口内（默认 1s）后续全部丢弃并计数；
    窗口到期时放行一条并在末尾标注被压掉多少条、下次最早何时重报；
    之后每次重报间隔翻倍，直到 ``max_window_s`` 封顶。
    """

    def __init__(self, window_s: float = 1.0, max_window_s: float = 300.0,
                 max_keys: int = 5000):
        super().__init__("moyurobot-dedup")
        self.window_s = max(0.0, float(window_s))
        self.max_window_s = max(self.window_s, float(max_window_s))
        self.max_keys = max(64, int(max_keys))
        self.enabled = self.window_s > 0
        self.total_suppressed = 0
        self._states: Dict[Tuple[int, str, str], _MsgState] = {}
        self._lock = threading.Lock()

    # -- 内部辅助 ---------------------------------------------------------
    def _annotate(self, record: logging.LogRecord, suppressed: int, interval: float) -> None:
        """在被放行的日志后面追加"已抑制 N 条"的说明"""
        try:
            text = record.getMessage()
        except Exception:  # 参数不匹配时保持原样，不要因为日志本身报错
            return
        if len(text) > MAX_ANNOTATED_MESSAGE:
            text = text[:MAX_ANNOTATED_MESSAGE] + "…"
        record.msg = f"{text}（同一消息已抑制 {suppressed} 条，下次最早 {interval:.0f}s 后重报）"
        record.args = ()
        record.dedup_suppressed = suppressed  # type: ignore[attr-defined]

    def _prune(self, now: float) -> None:
        if len(self._states) < self.max_keys:
            return
        cutoff = now - max(self.max_window_s * 2, 60.0)
        for key, state in list(self._states.items()):
            if state.last_ts < cutoff:
                self._states.pop(key, None)
        if len(self._states) >= self.max_keys:
            # 仍然超限：按最后出现时间丢掉最冷的一半
            stale = sorted(self._states.items(), key=lambda kv: kv[1].last_ts)
            for key, _ in stale[: self.max_keys // 2]:
                self._states.pop(key, None)

    # -- Filter 接口 ------------------------------------------------------
    def _decide(self, record: logging.LogRecord) -> bool:
        if not self.enabled:
            return True

        try:
            key = (record.levelno, record.name, record.getMessage())
        except Exception:
            return True

        now = time.monotonic()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                interval = self.window_s
                self._states[key] = _MsgState(now, interval)
                self._prune(now)
                return True

            # 长时间没再出现：视为新一轮故障，退避间隔重置
            if now - state.last_ts > self.max_window_s * 2:
                state.interval = self.window_s
                state.next_allowed = now

            state.last_ts = now

            if now >= state.next_allowed:
                suppressed = state.suppressed
                state.suppressed = 0
                state.interval = min(max(state.interval * 2, self.window_s), self.max_window_s)
                state.next_allowed = now + state.interval
                if suppressed:
                    self.total_suppressed += suppressed
                    self._annotate(record, suppressed, state.interval)
                return True

            state.suppressed += 1
            return False

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "keys": len(self._states),
                "suppressed": self.total_suppressed,
                "pending": sum(s.suppressed for s in self._states.values()),
            }

    def reset(self) -> None:
        with self._lock:
            self._states.clear()
            self.total_suppressed = 0


class RateLimitFilter(_OncePerRecordFilter):
    """单 logger 每秒最多放行 ``limit_per_s`` 条

    用来兜住"每条消息都不一样"的日志洪流（例如把帧内容拼进日志里）。
    被限速丢弃的条数会挂在下一条放行的消息后面。
    """

    def __init__(self, limit_per_s: float = 20.0, max_buckets: int = 512):
        super().__init__("moyurobot-ratelimit")
        self.limit_per_s = float(limit_per_s)
        self.max_buckets = max(16, int(max_buckets))
        self.enabled = self.limit_per_s > 0
        self.total_dropped = 0
        # logger 名 -> [窗口起点, 本窗口已放行, 本窗口已丢弃]
        self._buckets: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _decide(self, record: logging.LogRecord) -> bool:
        if not self.enabled:
            return True

        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(record.name)
            if bucket is None:
                if len(self._buckets) >= self.max_buckets:
                    # 桶太多说明 logger 名在爆炸，直接清空重来，避免内存无上限
                    self._buckets.clear()
                self._buckets[record.name] = [now, 1.0, 0.0]
                return True

            if now - bucket[0] >= 1.0:
                dropped = int(bucket[2])
                self._buckets[record.name] = [now, 1.0, 0.0]
                if dropped:
                    self.total_dropped += dropped
                    try:
                        record.msg = f"{record.getMessage()}（该 logger 上一秒另有 {dropped} 条被限速丢弃）"
                        record.args = ()
                    except Exception:
                        pass
                return True

            if bucket[1] < self.limit_per_s:
                bucket[1] += 1
                return True

            bucket[2] += 1
            return False

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"loggers": len(self._buckets), "dropped": self.total_dropped}


def _noisy_loggers() -> List[str]:
    raw = os.environ.get("LOG_NOISY_LOGGERS")
    if raw is None:
        return list(DEFAULT_NOISY_LOGGERS)
    names = [item.strip() for item in raw.split(",") if item.strip()]
    return names


def get_log_file_path(log_dir: Optional[str] = None, log_file: Optional[str] = None) -> Optional[str]:
    """根据环境变量推断日志文件路径，未启用文件日志时返回 None"""
    explicit_dir = log_dir or os.environ.get("LOG_DIR")
    to_file = _env_bool("LOG_TO_FILE", bool(explicit_dir))
    if not to_file:
        return None

    directory = os.path.expanduser(explicit_dir or os.path.join("~", "logs"))
    filename = log_file or _env_str("LOG_FILE", "moyurobot.log")
    return os.path.join(directory, filename)


_configure_lock = threading.Lock()
_configured = False
_dedup_filter: Optional[DedupBackoffFilter] = None
_rate_filter: Optional[RateLimitFilter] = None


def _remove_foreign_handlers(root: logging.Logger) -> None:
    """移除根 logger 上已有的 handler，避免 basicConfig / 重复调用造成双份输出"""
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def setup_logging(level=None, force: bool = False, log_to_file: Optional[bool] = None) -> logging.Logger:
    """初始化全局日志配置（幂等）

    Args:
        level: 根日志级别，可以是 ``"INFO"`` / ``logging.INFO``；默认取 LOG_LEVEL
        force: 为 True 时即使已配置过也重新配置
        log_to_file: 是否写日志文件；None 表示按 LOG_TO_FILE / LOG_DIR 自动判断

    Returns:
        根 logger
    """
    global _configured, _dedup_filter, _rate_filter

    with _configure_lock:
        root = logging.getLogger()
        if _configured and not force:
            return root

        root.setLevel(parse_level(level if level is not None else _env_str("LOG_LEVEL", "INFO")))

        # stdio 模式下 stdout 是 MCP 协议通道，日志必须走 stderr
        _remove_foreign_handlers(root)

        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

        _dedup_filter = DedupBackoffFilter(
            window_s=_env_float("LOG_DEDUP_WINDOW_S", 1.0),
            max_window_s=_env_float("LOG_DEDUP_MAX_WINDOW_S", 300.0),
        )
        _rate_filter = RateLimitFilter(limit_per_s=_env_float("LOG_RATE_LIMIT_PER_S", 20.0))

        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(_dedup_filter)
        stream_handler.addFilter(_rate_filter)
        root.addHandler(stream_handler)

        file_path = None if log_to_file is False else get_log_file_path()

        if file_path:
            try:
                directory = os.path.dirname(file_path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                max_bytes = max(64 * 1024, _env_int("LOG_MAX_BYTES", 5 * 1024 * 1024))
                backup_count = max(0, _env_int("LOG_BACKUP_COUNT", 3))
                file_handler = RotatingFileHandler(
                    file_path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                file_handler.addFilter(_dedup_filter)
                file_handler.addFilter(_rate_filter)
                root.addHandler(file_handler)
            except OSError as exc:
                root.warning("无法创建日志文件 %s: %s（仅输出到 stderr）", file_path, exc)

        # 噪声 logger 单独设级别（默认不动过滤，只调级别）
        noisy_level = parse_level(_env_str("LOG_NOISY_LEVEL", "WARNING"))
        for name in _noisy_loggers():
            logging.getLogger(name).setLevel(noisy_level)

        _configured = True
        root.debug(
            "日志已配置: level=%s dedup_window=%.1fs rate_limit=%.0f/s file=%s",
            logging.getLevelName(root.level), _dedup_filter.window_s,
            _rate_filter.limit_per_s, file_path or "-",
        )
        return root


def get_filters() -> Tuple[Optional[DedupBackoffFilter], Optional[RateLimitFilter]]:
    """返回当前的去重/限速过滤器（未初始化时为 None），便于状态展示或测试"""
    return _dedup_filter, _rate_filter
