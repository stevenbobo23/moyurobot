#!/usr/bin/env python
"""
音频采集 / 播放 / 对讲中枢

把树莓派侧的「麦克风采集」和「扬声器播放」抽象成可换后端的小模块，供网页端
像"云摄像头"那样监听车端声音、按住说话对讲。

后端（AUDIO_BACKEND）：
- auto  : 自动探测 alsa(arecord/aplay) -> pulse(parec/pacat) -> ffmpeg
- alsa  : 强制 ALSA（树莓派默认，需要 alsa-utils）
- pulse : 强制 PulseAudio / PipeWire-pulse
- ffmpeg: ffmpeg 软桥接（alsa 设备）
- sim   : 模拟后端，不需要任何声卡，用于开发/演示/自动化测试

三条设计约束：
1. 采集只有一路 —— 麦克风是独占资源，所有网页客户端共享同一条采集流；
2. 播放是独占的 —— 对讲（PTT）同一时刻只允许一个客户端说话，避免多人抢麦；
3. 对讲是半双工 —— 有人说话时暂停下发车端声音，从协议层避免回授啸叫。
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_BACKENDS = ("auto", "alsa", "pulse", "ffmpeg", "sim")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@dataclass
class AudioConfig:
    """音频对讲配置（环境变量优先，其次 config/default.json 的 audio 段）"""

    enabled: bool = True
    backend: str = "auto"            # auto | alsa | pulse | ffmpeg | sim
    capture_device: str = ""         # 空字符串 = 系统默认麦克风
    playback_device: str = ""        # 空字符串 = 系统默认扬声器
    sample_rate: int = 16000         # 语音够用，带宽 32 KB/s
    channels: int = 1
    chunk_ms: int = 100              # 每包时长，100ms = 局域网端到端约 200ms
    monitor_gain: float = 1.0        # 车端麦克风 -> 浏览器 的软件增益
    talk_gain: float = 1.0           # 浏览器 -> 车端扬声器 的软件增益
    ws_host: str = "0.0.0.0"
    ws_port: int = 8081
    ws_public_url: str = ""          # 反向代理 / 内网穿透场景下手动指定 ws 地址
    max_clients: int = 8
    capture_idle_close_s: float = 2.0  # 没人监听多久后释放麦克风
    sim_realtime: bool = True        # sim 后端是否按真实时长节奏产出数据
    require_auth: bool = True        # 是否要求已登录（cookie）才能听/说

    # ---------- 派生属性 ----------

    @property
    def chunk_samples(self) -> int:
        return max(1, int(self.sample_rate * self.chunk_ms / 1000))

    @property
    def chunk_bytes(self) -> int:
        return self.chunk_samples * 2 * self.channels  # S16_LE

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * 2 * self.channels

    # ---------- 构造 ----------

    _KNOWN_KEYS = (
        "enabled", "backend", "capture_device", "playback_device", "sample_rate",
        "channels", "chunk_ms", "monitor_gain", "talk_gain", "ws_host", "ws_port",
        "ws_public_url", "max_clients", "capture_idle_close_s", "sim_realtime",
        "require_auth",
    )

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AudioConfig":
        if not isinstance(data, dict):
            return cls()
        clean = {k: v for k, v in data.items() if k in cls._KNOWN_KEYS and v is not None}
        cfg = cls(**clean)
        cfg._normalize()
        return cfg

    @classmethod
    def from_env(cls, data: Optional[dict] = None) -> "AudioConfig":
        cfg = cls.from_dict(data)
        env = os.environ

        if "AUDIO_ENABLED" in env:
            cfg.enabled = _env_bool(env, "AUDIO_ENABLED", cfg.enabled)
        cfg.backend = env.get("AUDIO_BACKEND", cfg.backend).strip().lower() or "auto"
        cfg.capture_device = env.get("AUDIO_CAPTURE_DEVICE", cfg.capture_device).strip()
        cfg.playback_device = env.get("AUDIO_PLAYBACK_DEVICE", cfg.playback_device).strip()
        # 空值 = 沿用 config/default.json 里的配置（env.example 里这行默认就是空的，
        # 直接赋值会把配置文件里的地址抹掉）；要强制回到自动推导就写 auto / none / -
        raw_public = env.get("AUDIO_WS_PUBLIC_URL")
        if raw_public is not None:
            raw_public = str(raw_public).strip()
            if raw_public.lower() in ("auto", "none", "-"):
                cfg.ws_public_url = ""
            elif raw_public:
                cfg.ws_public_url = raw_public
        cfg.ws_host = env.get("AUDIO_WS_HOST", cfg.ws_host).strip() or "0.0.0.0"
        cfg.sample_rate = _env_int(env, "AUDIO_SAMPLE_RATE", cfg.sample_rate)
        cfg.channels = _env_int(env, "AUDIO_CHANNELS", cfg.channels)
        cfg.chunk_ms = _env_int(env, "AUDIO_CHUNK_MS", cfg.chunk_ms)
        cfg.ws_port = _env_int(env, "AUDIO_WS_PORT", cfg.ws_port)
        cfg.max_clients = _env_int(env, "AUDIO_MAX_CLIENTS", cfg.max_clients)
        cfg.monitor_gain = _env_float(env, "AUDIO_MONITOR_GAIN", cfg.monitor_gain)
        cfg.talk_gain = _env_float(env, "AUDIO_TALK_GAIN", cfg.talk_gain)
        cfg.capture_idle_close_s = _env_float(
            env, "AUDIO_CAPTURE_IDLE_CLOSE_S", cfg.capture_idle_close_s
        )
        cfg.sim_realtime = _env_bool(env, "AUDIO_SIM_REALTIME", cfg.sim_realtime)
        cfg.require_auth = _env_bool(env, "AUDIO_REQUIRE_AUTH", cfg.require_auth)
        cfg._normalize()
        return cfg

    @staticmethod
    def default_config_path() -> str:
        """pi_client/config/default.json"""
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(os.path.dirname(os.path.dirname(here)), "config", "default.json")

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "AudioConfig":
        """优先级：代码默认值 < config/default.json 的 audio 段 < 环境变量"""
        data = None
        path = config_path or cls.default_config_path()
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data = raw.get("audio")
            except Exception as e:
                logger.warning("读取音频配置失败 %s: %s", path, e)
        return cls.from_env(data)

    def _normalize(self):
        if self.backend not in _BACKENDS:
            logger.warning("未知音频后端 %r，回退 auto", self.backend)
            self.backend = "auto"
        # 采样率取整到常见档位，避免某些声卡不支持
        sr = int(self.sample_rate) if self.sample_rate else 16000
        self.sample_rate = min(48000, max(8000, sr))
        self.channels = 1 if int(self.channels) <= 1 else 2
        self.chunk_ms = min(500, max(20, int(self.chunk_ms)))
        self.ws_host = (self.ws_host or "").strip() or "0.0.0.0"
        self.ws_public_url = (self.ws_public_url or "").strip()
        self.ws_port = int(self.ws_port) if self.ws_port else 8081
        self.max_clients = max(1, int(self.max_clients))


def _env_bool(env, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(env, key: str, default: int) -> int:
    try:
        return int(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(env, key: str, default: float) -> float:
    try:
        return float(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# PCM 小工具
# --------------------------------------------------------------------------- #


def apply_gain(pcm: bytes, gain: float) -> bytes:
    """对 S16_LE PCM 施加软件增益（带削顶保护）"""
    if not pcm or gain == 1.0:
        return pcm
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = array.array("h")
    samples.frombytes(pcm)
    out = array.array("h", (
        -32768 if v * gain < -32768 else (32767 if v * gain > 32767 else int(v * gain))
        for v in samples
    ))
    return out.tobytes()


def rms_level(pcm: bytes) -> float:
    """归一化 RMS 电平，0.0 ~ 1.0，用于网页端电平条"""
    if len(pcm) < 2:
        return 0.0
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0
    total = 0
    for v in samples:
        total += v * v
    return min(1.0, math.sqrt(total / len(samples)) / 32768.0)


class AudioUnavailable(RuntimeError):
    """音频后端不可用（缺少命令 / 没有声卡 / 设备被占用）"""


# --------------------------------------------------------------------------- #
# 采集端
# --------------------------------------------------------------------------- #


class PcmReader:
    """S16_LE 裸流读取器基类"""

    def read(self, nbytes: int) -> bytes:  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self):
        pass


class _ProcReader(PcmReader):
    """基于子进程 stdout 的读取器（arecord / parec / ffmpeg）"""

    def __init__(self, cmd: List[str], name: str):
        self.name = name
        self.cmd = cmd
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError as e:
            raise AudioUnavailable(f"找不到命令 {cmd[0]}: {e}") from e
        except Exception as e:
            raise AudioUnavailable(f"启动 {cmd[0]} 失败: {e}") from e
        logger.info("音频采集已启动: %s", " ".join(cmd))

    def read(self, nbytes: int) -> bytes:
        if self.proc.poll() is not None:
            raise AudioUnavailable(
                f"采集进程 {self.name} 已退出（code={self.proc.returncode}），"
                "请检查设备号是否正确、是否被其他程序占用"
            )
        data = self.proc.stdout.read(nbytes)
        return data or b""

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        finally:
            try:
                if self.proc.stdout:
                    self.proc.stdout.close()
            except Exception:
                pass


class _SimReader(PcmReader):
    """模拟麦克风：产出 440Hz 正弦波，便于无硬件时跑通全链路"""

    def __init__(self, config: AudioConfig):
        self.config = config
        self._phase = 0.0
        self._closed = False
        logger.info("音频采集使用模拟后端（440Hz 测试音）")

    def read(self, nbytes: int) -> bytes:
        if self._closed:
            return b""
        n = max(1, nbytes // 2)
        sr = self.config.sample_rate
        amp = 0.25 * 32767
        step = 2.0 * math.pi * 440.0 / sr
        buf = array.array("h")
        for _ in range(n):
            buf.append(int(amp * math.sin(self._phase)))
            self._phase += step
            if self._phase > 2.0 * math.pi:
                self._phase -= 2.0 * math.pi
        if self.config.sim_realtime:
            time.sleep(n / sr)
        return buf.tobytes()

    def close(self):
        self._closed = True


# --------------------------------------------------------------------------- #
# 播放端
# --------------------------------------------------------------------------- #


class PcmWriter:
    """S16_LE 裸流写入器基类"""

    def write(self, data: bytes):  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self):
        pass


class _ProcWriter(PcmWriter):
    """基于子进程 stdin 的播放器（aplay / pacat / ffmpeg）"""

    def __init__(self, cmd: List[str], name: str):
        self.name = name
        self.cmd = cmd
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError as e:
            raise AudioUnavailable(f"找不到命令 {cmd[0]}: {e}") from e
        except Exception as e:
            raise AudioUnavailable(f"启动 {cmd[0]} 失败: {e}") from e
        logger.info("音频播放已启动: %s", " ".join(cmd))

    def write(self, data: bytes):
        if self.proc.poll() is not None:
            raise AudioUnavailable(f"播放进程 {self.name} 已退出（code={self.proc.returncode}）")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def close(self, drain: bool = True):
        """drain=True 时先关 stdin 让缓冲区播完（最多等 1.5s）"""
        try:
            if self.proc.poll() is None and self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                if drain:
                    try:
                        self.proc.wait(timeout=1.5)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


class _SimWriter(PcmWriter):
    """模拟扬声器：只统计丢弃的字节数"""

    def __init__(self, config: AudioConfig):
        self.config = config
        self.bytes_written = 0
        logger.info("音频播放使用模拟后端（数据丢弃）")

    def write(self, data: bytes):
        self.bytes_written += len(data)
        if self.config.sim_realtime:
            time.sleep(len(data) / max(1, self.config.bytes_per_second))

    def close(self, drain: bool = True):
        pass


# --------------------------------------------------------------------------- #
# 后端探测与设备枚举
# --------------------------------------------------------------------------- #


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def detect_backend(preferred: str = "auto") -> str:
    """返回实际可用的后端名；完全不可用时返回 sim（并在 capabilities 里标注）"""
    preferred = (preferred or "auto").strip().lower()
    has = {
        "alsa": bool(_which("arecord") and _which("aplay")),
        "pulse": bool(_which("parec") and _which("pacat")),
        "ffmpeg": bool(_which("ffmpeg")),
    }
    if preferred == "sim":
        return "sim"
    if preferred in has:
        if has[preferred]:
            return preferred
        logger.warning("指定的音频后端 %s 缺少命令，尝试自动探测", preferred)
    for name in ("alsa", "pulse", "ffmpeg"):
        if has[name]:
            return name
    logger.warning("未检测到任何音频命令（arecord/aplay/parec/pacat/ffmpeg），使用模拟后端")
    return "sim"


_CARD_RE = re.compile(
    r"^card\s+(\d+):\s+(\S+)\s+\[([^\]]*)\],\s*device\s+(\d+):\s+(\S+)\s+\[([^\]]*)\]"
)


def _parse_card_listing(text: str, kind: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        m = _CARD_RE.match(line.strip())
        if not m:
            continue
        card, card_short, card_long, dev, dev_short, dev_long = m.groups()
        devices.append(
            {
                "id": f"plughw:{card},{dev}",
                "name": f"{card_long or card_short} · {dev_long or dev_short} (card {card} dev {dev})",
                "kind": kind,
            }
        )
    return devices


def list_capture_devices() -> List[Dict[str, str]]:
    devices = [{"id": "default", "name": "系统默认麦克风", "kind": "capture"}]
    if _which("arecord"):
        try:
            out = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True, timeout=5
            ).stdout
            devices.extend(_parse_card_listing(out, "capture"))
        except Exception as e:
            logger.debug("arecord -l 失败: %s", e)
    return devices


def list_playback_devices() -> List[Dict[str, str]]:
    devices = [{"id": "default", "name": "系统默认扬声器", "kind": "playback"}]
    if _which("aplay"):
        try:
            out = subprocess.run(
                ["aplay", "-l"], capture_output=True, text=True, timeout=5
            ).stdout
            devices.extend(_parse_card_listing(out, "playback"))
        except Exception as e:
            logger.debug("aplay -l 失败: %s", e)
    return devices


def _capture_cmd(config: AudioConfig, backend: str) -> List[str]:
    rate, ch = str(config.sample_rate), str(config.channels)
    dev = config.capture_device.strip()
    if backend == "alsa":
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", rate, "-c", ch,
               "--buffer-time=200000"]
        if dev:
            cmd += ["-D", dev]
        return cmd + ["-"]
    if backend == "pulse":
        cmd = ["parec", "--format=s16le", f"--rate={rate}", f"--channels={ch}", "--raw"]
        if dev:
            cmd += [f"--device={dev}"]
        return cmd
    # ffmpeg
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "alsa", "-i", dev or "default",
        "-ar", rate, "-ac", ch, "-f", "s16le", "-",
    ]


def _playback_cmd(config: AudioConfig, backend: str) -> List[str]:
    rate, ch = str(config.sample_rate), str(config.channels)
    dev = config.playback_device.strip()
    if backend == "alsa":
        cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", rate, "-c", ch,
               "--buffer-time=200000", "--period-time=50000"]
        if dev:
            cmd += ["-D", dev]
        return cmd + ["-"]
    if backend == "pulse":
        cmd = ["pacat", "--playback", "--format=s16le", f"--rate={rate}",
               f"--channels={ch}", "--raw"]
        if dev:
            cmd += [f"--device={dev}"]
        return cmd
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", rate, "-ac", ch, "-i", "-",
        "-f", "alsa", dev or "default",
    ]


def create_reader(config: AudioConfig, backend: str) -> PcmReader:
    if backend == "sim":
        return _SimReader(config)
    return _ProcReader(_capture_cmd(config, backend), backend)


def create_writer(config: AudioConfig, backend: str) -> PcmWriter:
    if backend == "sim":
        return _SimWriter(config)
    return _ProcWriter(_playback_cmd(config, backend), backend)


# --------------------------------------------------------------------------- #
# 对讲中枢
# --------------------------------------------------------------------------- #


class IntercomHub:
    """全局唯一实例：单路采集 + 多路下发 + 独占对讲上行"""

    def __init__(self, config: Optional[AudioConfig] = None):
        self.config = config or AudioConfig.load()
        self.backend = detect_backend(self.config.backend)
        self._lock = threading.RLock()
        self._listeners: Dict[int, Callable[[bytes], None]] = {}
        self._token_seq = 0
        self._reader: Optional[PcmReader] = None
        self._writer: Optional[PcmWriter] = None
        self._ptt_owner: Optional[str] = None
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._level = 0.0
        self._peak = 0.0
        self._monitor_frames = 0
        self._monitor_bytes = 0
        self._talk_bytes = 0
        self._started_at = time.time()
        self._last_error: Optional[str] = None
        self._last_error_at = 0.0

    # ---------------- 生命周期 ----------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._capture_loop, name="moyu-audio-capture",
                                        daemon=True)
        self._thread.start()
        logger.info("音频对讲中枢已启动（后端=%s, %dHz, %dms/包）",
                    self.backend, self.config.sample_rate, self.config.chunk_ms)

    def shutdown(self):
        self._stop_evt.set()
        with self._lock:
            self._close_reader_locked()
            writer, self._writer = self._writer, None
            self._ptt_owner = None
        if writer:
            try:
                writer.close(drain=False)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        logger.info("音频对讲中枢已停止")

    @property
    def ptt_active(self) -> bool:
        with self._lock:
            return self._ptt_owner is not None

    # ---------------- 监听（车端 -> 浏览器） ----------------

    def add_listener(self, callback: Callable[[bytes], None]) -> int:
        with self._lock:
            self._token_seq += 1
            token = self._token_seq
            self._listeners[token] = callback
            logger.info("新增音频监听者 token=%s，当前 %d 路", token, len(self._listeners))
            return token

    def remove_listener(self, token: int):
        with self._lock:
            self._listeners.pop(token, None)
            logger.info("移除音频监听者 token=%s，剩余 %d 路", token, len(self._listeners))

    @property
    def listener_count(self) -> int:
        with self._lock:
            return len(self._listeners)

    # ---------------- 对讲（浏览器 -> 车端） ----------------

    def begin_talk(self, client_id: str) -> Dict:
        with self._lock:
            if self._ptt_owner is not None and self._ptt_owner != client_id:
                return {"ok": False, "reason": f"已有用户在说话（{self._ptt_owner}），请稍候"}
            self._ptt_owner = client_id
            if self._writer is None:
                try:
                    self._writer = create_writer(self.config, self.backend)
                except Exception as e:
                    self._ptt_owner = None
                    self._note_error(f"打开扬声器失败: {e}")
                    return {"ok": False, "reason": f"打开扬声器失败: {e}"}
        logger.info("客户端 %s 开始对讲", client_id)
        return {"ok": True}

    def write_talk(self, client_id: str, data: bytes) -> bool:
        if not data:
            return True
        with self._lock:
            if self._ptt_owner != client_id:
                return False
            writer = self._writer
        if writer is None:
            return False
        try:
            if self.config.talk_gain != 1.0:
                data = apply_gain(data, self.config.talk_gain)
            writer.write(data)
            self._talk_bytes += len(data)
            return True
        except Exception as e:
            self._note_error(f"播放失败: {e}")
            with self._lock:
                if self._writer is writer:
                    self._writer = None
            try:
                writer.close(drain=False)
            except Exception:
                pass
            return False

    def end_talk(self, client_id: str) -> bool:
        with self._lock:
            if self._ptt_owner != client_id:
                return False
            self._ptt_owner = None
            writer, self._writer = self._writer, None
        if writer:
            # 关 stdin 后让 aplay 把缓冲播完，别阻塞事件循环线程
            threading.Thread(target=self._drain_writer, args=(writer,), daemon=True).start()
        logger.info("客户端 %s 结束对讲", client_id)
        return True

    @staticmethod
    def _drain_writer(writer: PcmWriter):
        try:
            writer.close(drain=True)
        except Exception:
            pass

    def release_client(self, client_id: str):
        """客户端断线时调用：释放它可能持有的对讲权"""
        self.end_talk(client_id)

    # ---------------- 设备热切换 ----------------

    def set_devices(self, capture: Optional[str] = None, playback: Optional[str] = None) -> Dict:
        changed = []
        with self._lock:
            if capture is not None and capture != self.config.capture_device:
                self.config.capture_device = capture.strip()
                changed.append(f"麦克风={self.config.capture_device or 'default'}")
                self._close_reader_locked()
            if playback is not None and playback != self.config.playback_device:
                self.config.playback_device = playback.strip()
                changed.append(f"扬声器={self.config.playback_device or 'default'}")
        if changed:
            logger.info("音频设备切换: %s", ", ".join(changed))
        return {"ok": True, "changed": changed, "devices": self.device_selection()}

    def device_selection(self) -> Dict:
        return {
            "capture_device": self.config.capture_device,
            "playback_device": self.config.playback_device,
        }

    # ---------------- 状态 ----------------

    def _note_error(self, message: str):
        self._last_error = message
        self._last_error_at = time.time()
        logger.warning("音频错误: %s", message)

    def _close_reader_locked(self):
        reader, self._reader = self._reader, None
        if reader:
            try:
                reader.close()
            except Exception:
                pass

    def _update_level(self, pcm: bytes):
        level = rms_level(pcm)
        self._level = level
        self._peak = max(level, self._peak * 0.92)

    def capabilities(self) -> Dict:
        capture_devices = list_capture_devices()
        playback_devices = list_playback_devices()
        sim = self.backend == "sim"
        return {
            "enabled": bool(self.config.enabled),
            "backend": self.backend,
            "simulated": sim,
            "sample_rate": self.config.sample_rate,
            "channels": self.config.channels,
            "chunk_ms": self.config.chunk_ms,
            "chunk_bytes": self.config.chunk_bytes,
            "bytes_per_second": self.config.bytes_per_second,
            "capture_devices": capture_devices,
            "playback_devices": playback_devices,
            "capture_available": sim or bool(capture_devices and len(capture_devices) >= 1),
            "playback_available": sim or bool(playback_devices and len(playback_devices) >= 1),
            "devices": self.device_selection(),
            "error": self._last_error,
            "hints": self._hints(sim, capture_devices, playback_devices),
        }

    def _hints(self, sim: bool, capture_devices: List[Dict[str, str]],
               playback_devices: List[Dict[str, str]]) -> List[str]:
        hints: List[str] = []
        if sim:
            hints.append(
                "当前是模拟后端（无声卡）：监听听到的是 440Hz 测试音，对讲数据被丢弃。"
                "真机请安装 alsa-utils 并接上麦克风/扬声器。"
            )
            return hints
        if len(capture_devices) <= 1:
            hints.append("未发现独立声卡，只有系统默认设备；若听不到声音请检查 USB 麦克风。")
        if len(playback_devices) <= 1:
            hints.append("未发现独立声卡，只有系统默认扬声器；对讲可能没有输出。")
        if not _which("arecord"):
            hints.append("缺少 arecord：执行 sudo apt install -y alsa-utils")
        return hints

    def state(self) -> Dict:
        now = time.time()
        error = self._last_error
        if error and now - self._last_error_at > 120:
            error = None  # 老错误不再打扰
        return {
            "backend": self.backend,
            "simulated": self.backend == "sim",
            "monitor_listeners": self.listener_count,
            "ptt": self._ptt_owner is not None,
            "ptt_owner": self._ptt_owner,
            "level": round(self._level, 4),
            "peak": round(self._peak, 4),
            "monitor_frames": self._monitor_frames,
            "monitor_kbytes": round(self._monitor_bytes / 1024.0, 1),
            "talk_kbytes": round(self._talk_bytes / 1024.0, 1),
            "sample_rate": self.config.sample_rate,
            "chunk_ms": self.config.chunk_ms,
            "capture_active": self._reader is not None,
            "uptime_s": int(now - self._started_at),
            "error": error,
            "devices": self.device_selection(),
        }

    # ---------------- 采集主循环 ----------------

    def _capture_loop(self):
        idle_since: Optional[float] = None
        while not self._stop_evt.is_set():
            try:
                with self._lock:
                    has_listener = bool(self._listeners)
                    ptt = self._ptt_owner is not None

                if not has_listener or ptt:
                    # 没人听 / 有人说话：不占用麦克风
                    with self._lock:
                        if self._reader is not None:
                            if idle_since is None:
                                idle_since = time.time()
                            elif (
                                not ptt
                                and time.time() - idle_since > self.config.capture_idle_close_s
                            ):
                                self._close_reader_locked()
                                idle_since = None
                    if ptt:
                        idle_since = None
                    time.sleep(0.02)
                    continue

                idle_since = None
                with self._lock:
                    reader = self._reader
                if reader is None:
                    reader = create_reader(self.config, self.backend)
                    with self._lock:
                        self._reader = reader

                data = reader.read(self.config.chunk_bytes)
                if not data:
                    with self._lock:
                        self._close_reader_locked()
                    time.sleep(0.2)
                    continue

                if self.config.monitor_gain != 1.0:
                    data = apply_gain(data, self.config.monitor_gain)
                self._update_level(data)
                self._monitor_frames += 1
                self._monitor_bytes += len(data)

                with self._lock:
                    callbacks = list(self._listeners.values())
                for cb in callbacks:
                    try:
                        cb(data)
                    except Exception as e:  # 单个监听者出错不影响其他
                        logger.debug("音频下发失败: %s", e)

            except AudioUnavailable as e:
                self._note_error(str(e))
                with self._lock:
                    self._close_reader_locked()
                time.sleep(1.5)
            except Exception as e:  # 兜底，绝不让线程死掉
                self._note_error(f"采集异常: {e}")
                with self._lock:
                    self._close_reader_locked()
                time.sleep(1.0)


# --------------------------------------------------------------------------- #
# 全局单例
# --------------------------------------------------------------------------- #

_hub: Optional[IntercomHub] = None
_hub_lock = threading.Lock()


def get_hub(config: Optional[AudioConfig] = None) -> IntercomHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = IntercomHub(config)
        return _hub


def reset_hub():
    """测试用：销毁单例"""
    global _hub
    with _hub_lock:
        if _hub is not None:
            try:
                _hub.shutdown()
            except Exception:
                pass
        _hub = None


__all__ = [
    "AudioConfig",
    "AudioUnavailable",
    "IntercomHub",
    "get_hub",
    "reset_hub",
    "detect_backend",
    "list_capture_devices",
    "list_playback_devices",
    "apply_gain",
    "rms_level",
    "asdict",
]
