#!/usr/bin/env python
"""自动打招呼配置

优先级：环境变量 > ``config/default.json`` 的 ``greeting`` 段 > 本文件的默认值。

刻意只用标准库：这份配置和状态机要能在没有 numpy / opencv / lerobot 的机器上
单独跑单测（``tests/test_greeting.py``），不要在这里引入重依赖。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


# ---------------------------------------------------------------- 解析小工具


def as_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def as_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return value


def as_int(raw: Any, default: int) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def parse_active_hours(spec: Optional[str]) -> Optional[Tuple[int, int]]:
    """把 ``"08:00-22:00"`` 解析成 ``(480, 1320)``（分钟）

    返回 None 表示「全天不限」。支持跨零点（``"22:00-06:00"``）。
    格式不认识时也返回 None —— 配置写错时宁可全天打招呼，也别静默变成永不触发。
    """
    if spec is None:
        return None
    text = str(spec).strip().replace("–", "-").replace("—", "-").replace("~", "-")
    if not text or text.lower() in ("all", "always", "any", "0-24", "00:00-24:00"):
        return None
    if "-" not in text:
        logger.warning("active_hours 格式不认识: %r（应为 HH:MM-HH:MM），按全天处理", spec)
        return None

    start_text, _, end_text = text.partition("-")

    def _minutes(chunk: str) -> Optional[int]:
        chunk = chunk.strip()
        if not chunk:
            return None
        parts = chunk.split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 24) or not (0 <= minute < 60):
            return None
        return hour * 60 + minute

    start, end = _minutes(start_text), _minutes(end_text)
    if start is None or end is None:
        logger.warning("active_hours 解析失败: %r，按全天处理", spec)
        return None
    if start == end:
        return None
    return start, end


def in_active_hours(spec: Optional[str], now: Optional[float] = None) -> bool:
    """当前是否在允许打招呼的时段内（本地时间）"""
    window = parse_active_hours(spec)
    if window is None:
        return True
    start, end = window
    lt = time.localtime(time.time() if now is None else now)
    current = lt.tm_hour * 60 + lt.tm_min
    if start < end:
        return start <= current < end
    # 跨零点，例如 22:00-06:00
    return current >= start or current < end


def _default_config_path() -> Path:
    # moyurobot/greeting/config.py -> pi_client/config/default.json
    return Path(__file__).resolve().parents[2] / "config" / "default.json"


# ---------------------------------------------------------------- 配置结构


@dataclass
class SpeechConfig:
    """打招呼的语音来源"""

    # auto = 先找本地音频池，没有就用云端 TTS（并缓存）
    # pool = 只用本地音频；tts = 只用云端 TTS；off = 不发声
    mode: str = "auto"
    texts: List[str] = field(default_factory=lambda: [
        "你好呀～",
        "欢迎来营地，随便看看～",
        "嗨，需要帮忙吗？",
        "我是小猫，今天在这儿值班～",
    ])
    audio_dir: str = "assets/greetings"
    tts_voice: str = "longxiaochun"
    tts_model: str = "cosyvoice-v1"
    timeout_s: float = 8.0
    gain: float = 0.8
    cache_dir: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SpeechConfig":
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        cfg.mode = str(data.get("mode", cfg.mode)).strip().lower() or "auto"
        texts = data.get("texts")
        if isinstance(texts, list):
            cleaned = [str(t).strip() for t in texts if str(t).strip()]
            if cleaned:
                cfg.texts = cleaned
        cfg.audio_dir = str(data.get("audio_dir", cfg.audio_dir)).strip()
        cfg.tts_voice = str(data.get("tts_voice", cfg.tts_voice)).strip()
        cfg.tts_model = str(data.get("tts_model", cfg.tts_model)).strip()
        cfg.timeout_s = as_float(data.get("timeout_s"), cfg.timeout_s)
        cfg.gain = as_float(data.get("gain"), cfg.gain)
        cfg.cache_dir = str(data.get("cache_dir", cfg.cache_dir)).strip()
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "texts": list(self.texts),
            "audio_dir": self.audio_dir,
            "tts_voice": self.tts_voice,
            "tts_model": self.tts_model,
            "timeout_s": self.timeout_s,
            "gain": self.gain,
            "cache_dir": self.cache_dir,
        }


@dataclass
class GreetingConfig:
    """自动打招呼总配置"""

    enabled: bool = True
    camera: str = "front"

    # 检测
    detector: str = "auto"          # auto | yolo | hog | none
    model_path: str = ""            # 空 = 自动找 models/yolo11n.onnx 等
    detect_fps: float = 4.0
    input_size: int = 320
    min_person_ratio: float = 0.30  # bbox 高 / 画面高，≈ 1.62 / 距离(m)
    center_zone: float = 0.70       # bbox 中心需落在画面中央多少比例内；1.0=不限制
    yolo_conf: float = 0.35
    hog_scale: float = 1.05

    # 触发判定
    trigger_frames: int = 3         # 累积分（命中 +1，未命中 -0.5）
    trigger_seconds: float = 0.5    # 至少持续这么久
    leave_seconds: float = 8.0      # 连续无人多久算离开
    cooldown_seconds: float = 180.0
    max_per_hour: int = 6
    active_hours: str = "08:00-22:00"

    # 表演
    gesture: str = "nod"            # nod | shake_head | none
    gesture_delay_ms: int = 600
    respect_intercom: bool = True   # 对讲中不插话
    intercom_wait_s: float = 3.0
    respect_arm_busy: bool = True   # 有人在操作机械臂时只出声不动手

    snapshot_dir: str = ""          # 空 = 不落盘

    speech: SpeechConfig = field(default_factory=SpeechConfig)

    # ---------------------------------------------------------- 载入

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GreetingConfig":
        cfg = cls()
        if not isinstance(data, dict):
            return cfg

        cfg.enabled = as_bool(data.get("enabled"), cfg.enabled)
        cfg.camera = str(data.get("camera", cfg.camera)).strip() or "front"

        cfg.detector = str(data.get("detector", cfg.detector)).strip().lower() or "auto"
        cfg.model_path = str(data.get("model_path", cfg.model_path)).strip()
        cfg.detect_fps = max(0.2, as_float(data.get("detect_fps"), cfg.detect_fps))
        cfg.input_size = max(160, as_int(data.get("input_size"), cfg.input_size))
        cfg.min_person_ratio = min(1.0, max(0.01, as_float(
            data.get("min_person_ratio"), cfg.min_person_ratio)))
        cfg.center_zone = min(1.0, max(0.1, as_float(data.get("center_zone"), cfg.center_zone)))
        cfg.yolo_conf = min(0.99, max(0.05, as_float(data.get("yolo_conf"), cfg.yolo_conf)))
        cfg.hog_scale = min(2.0, max(1.01, as_float(data.get("hog_scale"), cfg.hog_scale)))

        cfg.trigger_frames = max(1, as_int(data.get("trigger_frames"), cfg.trigger_frames))
        cfg.trigger_seconds = max(0.0, as_float(data.get("trigger_seconds"), cfg.trigger_seconds))
        cfg.leave_seconds = max(0.5, as_float(data.get("leave_seconds"), cfg.leave_seconds))
        cfg.cooldown_seconds = max(0.0, as_float(data.get("cooldown_seconds"), cfg.cooldown_seconds))
        cfg.max_per_hour = max(1, as_int(data.get("max_per_hour"), cfg.max_per_hour))
        cfg.active_hours = str(data.get("active_hours", cfg.active_hours)).strip()

        gesture = str(data.get("gesture", cfg.gesture)).strip().lower() or "nod"
        cfg.gesture = gesture if gesture in ("nod", "shake_head", "none") else "nod"
        cfg.gesture_delay_ms = max(0, as_int(data.get("gesture_delay_ms"), cfg.gesture_delay_ms))
        cfg.respect_intercom = as_bool(data.get("respect_intercom"), cfg.respect_intercom)
        cfg.intercom_wait_s = max(0.0, as_float(data.get("intercom_wait_s"), cfg.intercom_wait_s))
        cfg.respect_arm_busy = as_bool(data.get("respect_arm_busy"), cfg.respect_arm_busy)

        cfg.snapshot_dir = str(data.get("snapshot_dir", cfg.snapshot_dir)).strip()
        cfg.speech = SpeechConfig.from_dict(data.get("speech"))

        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        env = os.environ

        def _bool(key, attr):
            if key in env and env[key] != "":
                setattr(self, attr, as_bool(env[key], getattr(self, attr)))

        def _float(key, attr, minimum=None, maximum=None):
            if env.get(key):
                value = as_float(env[key], getattr(self, attr))
                if minimum is not None:
                    value = max(minimum, value)
                if maximum is not None:
                    value = min(maximum, value)
                setattr(self, attr, value)

        def _int(key, attr, minimum=None):
            if env.get(key):
                value = as_int(env[key], getattr(self, attr))
                if minimum is not None:
                    value = max(minimum, value)
                setattr(self, attr, value)

        def _str(key, attr):
            if env.get(key):
                setattr(self, attr, env[key].strip())

        _bool("GREETING_ENABLED", "enabled")
        _str("GREETING_CAMERA", "camera")
        _str("GREETING_DETECTOR", "detector")
        _str("GREETING_MODEL_PATH", "model_path")
        _float("GREETING_DETECT_FPS", "detect_fps", 0.2)
        _int("GREETING_INPUT_SIZE", "input_size", 160)
        # 上限必须和 from_dict 保持一致：env 里手滑写成 2.0 会让检测永不触发，
        # 而且不会有任何报错，属于最难查的一类配置事故
        _float("GREETING_MIN_PERSON_RATIO", "min_person_ratio", 0.01, 1.0)
        _float("GREETING_CENTER_ZONE", "center_zone", 0.1, 1.0)
        _float("GREETING_YOLO_CONF", "yolo_conf", 0.05, 0.99)
        _float("GREETING_HOG_SCALE", "hog_scale", 1.01, 2.0)
        _int("GREETING_TRIGGER_FRAMES", "trigger_frames", 1)
        _float("GREETING_TRIGGER_SECONDS", "trigger_seconds", 0.0)
        _float("GREETING_LEAVE_SECONDS", "leave_seconds", 0.5)
        _float("GREETING_COOLDOWN_SECONDS", "cooldown_seconds", 0.0)
        _int("GREETING_MAX_PER_HOUR", "max_per_hour", 1)
        _str("GREETING_ACTIVE_HOURS", "active_hours")
        _str("GREETING_GESTURE", "gesture")
        _int("GREETING_GESTURE_DELAY_MS", "gesture_delay_ms", 0)
        _bool("GREETING_RESPECT_INTERCOM", "respect_intercom")
        _bool("GREETING_RESPECT_ARM_BUSY", "respect_arm_busy")
        _float("GREETING_INTERCOM_WAIT_S", "intercom_wait_s", 0.0)
        _str("GREETING_SNAPSHOT_DIR", "snapshot_dir")

        if self.gesture not in ("nod", "shake_head", "none"):
            self.gesture = "nod"

        # 语音相关（嵌套在 speech 段里，单独处理）
        if env.get("GREETING_SPEECH_MODE"):
            self.speech.mode = env["GREETING_SPEECH_MODE"].strip().lower() or "auto"
        if env.get("GREETING_AUDIO_DIR"):
            self.speech.audio_dir = env["GREETING_AUDIO_DIR"].strip()
        if env.get("GREETING_TTS_VOICE"):
            self.speech.tts_voice = env["GREETING_TTS_VOICE"].strip()
        if env.get("GREETING_TTS_MODEL"):
            self.speech.tts_model = env["GREETING_TTS_MODEL"].strip()
        if env.get("GREETING_TIMEOUT_S"):
            self.speech.timeout_s = max(1.0, as_float(env["GREETING_TIMEOUT_S"], self.speech.timeout_s))
        if env.get("GREETING_GAIN"):
            self.speech.gain = min(3.0, max(0.0, as_float(env["GREETING_GAIN"], self.speech.gain)))
        if env.get("GREETING_TEXTS"):
            parts = [p.strip() for p in env["GREETING_TEXTS"].replace("|", ",").split(",")]
            cleaned = [p for p in parts if p]
            if cleaned:
                self.speech.texts = cleaned

    @classmethod
    def load(cls, config_path: Optional[str] = None,
             env: Optional[Dict[str, str]] = None) -> "GreetingConfig":
        """从 default.json 读 greeting 段，再用环境变量覆盖

        ``env`` 参数只为单测方便（临时替换 os.environ），正常调用不用传。
        """
        path = Path(config_path) if config_path else _default_config_path()
        data: Dict[str, Any] = {}
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    section = raw.get("greeting")
                    data = section if isinstance(section, dict) else {}
        except Exception as e:  # 配置坏了也别让服务起不来
            logger.warning("读取打招呼配置失败（%s）: %s，使用默认值", path, e)

        if env is not None:
            original = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update({k: str(v) for k, v in env.items()})
                return cls.from_dict(data)
            finally:
                os.environ.clear()
                os.environ.update(original)
        return cls.from_dict(data)

    # ---------------------------------------------------------- 导出

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "camera": self.camera,
            "detector": self.detector,
            "model_path": self.model_path,
            "detect_fps": self.detect_fps,
            "input_size": self.input_size,
            "min_person_ratio": self.min_person_ratio,
            "center_zone": self.center_zone,
            "trigger_frames": self.trigger_frames,
            "trigger_seconds": self.trigger_seconds,
            "leave_seconds": self.leave_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "max_per_hour": self.max_per_hour,
            "active_hours": self.active_hours,
            "gesture": self.gesture,
            "gesture_delay_ms": self.gesture_delay_ms,
            "respect_intercom": self.respect_intercom,
            "intercom_wait_s": self.intercom_wait_s,
            "respect_arm_busy": self.respect_arm_busy,
            "snapshot_dir": self.snapshot_dir,
            "speech": self.speech.to_dict(),
        }

    def detect_interval(self) -> float:
        return 1.0 / max(0.2, self.detect_fps)
