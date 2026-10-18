#!/usr/bin/env python
"""打招呼的语音

两条来源，按 ``greeting.speech.mode`` 选择：

- **本地音频池**（``assets/greetings/*.wav``）：零延迟、断网可用，营地里最可靠
- **云端 TTS**（DashScope）：文案可动态，合成结果按文本哈希缓存，同一句只调一次

播放走语音对讲那条**独占**通道（``IntercomHub.begin_talk``）：有人正在按住说话时
拿不到锁，天然实现「不插话」。音频统一转成车端配置的采样率/声道再送进去。
"""

from __future__ import annotations

import array
import hashlib
import io
import logging
import random
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import GreetingConfig

logger = logging.getLogger(__name__)

# 打招呼的「客户端 ID」。和浏览器的 session id 不会冲突，也方便在日志里区分
SPEAKER_ID = "__greeter__"
DEFAULT_CACHE_DIR = "~/.cache/moyurobot/greeting"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _apply_gain(pcm: bytes, gain: float) -> bytes:
    """线性增益，clamp 到 int16 范围"""
    if not pcm or abs(gain - 1.0) < 1e-3:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    max_value = 32767
    min_value = -32768
    for i, value in enumerate(samples):
        scaled = int(value * gain)
        if scaled > max_value:
            scaled = max_value
        elif scaled < min_value:
            scaled = min_value
        samples[i] = scaled
    return samples.tobytes()


def _to_mono(samples: array.array, channels: int) -> array.array:
    if channels <= 1:
        return samples
    frames = len(samples) // channels
    out = array.array("h", bytes(frames * 2))
    for i in range(frames):
        total = 0
        base = i * channels
        for c in range(channels):
            total += samples[base + c]
        out[i] = int(total / channels)
    return out


def _resample(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    """线性插值重采样（纯标准库，2 秒音频约几十毫秒，够用）"""
    if src_rate == dst_rate or not samples:
        return samples
    ratio = dst_rate / float(src_rate)
    out_len = max(1, int(round(len(samples) * ratio)))
    out = array.array("h", bytes(out_len * 2))
    last = len(samples) - 1
    for i in range(out_len):
        position = i / ratio
        left = int(position)
        if left >= last:
            out[i] = samples[last]
            continue
        frac = position - left
        out[i] = int(samples[left] * (1.0 - frac) + samples[left + 1] * frac)
    return out


def decode_wav(data: bytes, sample_rate: int, channels: int) -> Optional[bytes]:
    """把任意 wav 转成 16bit / sample_rate / channels 的 PCM

    返回 None 表示格式没法处理（例如 24bit），调用方应跳过这一条而不是播噪音。
    """
    if not data:
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            src_channels = reader.getnchannels()
            sampwidth = reader.getsampwidth()
            src_rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except wave.Error as e:
        logger.warning("不是合法的 wav: %s", e)
        return None

    if sampwidth == 1:
        # 8bit wav 是无符号的，转成有符号 16bit
        samples = array.array("h", bytes(len(frames) * 2))
        for i, byte in enumerate(frames):
            samples[i] = (byte - 128) * 256
    elif sampwidth == 2:
        usable = len(frames) - (len(frames) % 2)
        samples = array.array("h")
        samples.frombytes(frames[:usable])
    else:
        logger.warning("不支持的位深 %s bit，跳过", sampwidth * 8)
        return None

    samples = _to_mono(samples, src_channels)
    samples = _resample(samples, src_rate, sample_rate)

    if channels > 1:
        stereo = array.array("h", bytes(len(samples) * 2 * channels))
        for i, value in enumerate(samples):
            base = i * channels
            for c in range(channels):
                stereo[base + c] = value
        samples = stereo

    return samples.tobytes()


class SpeechLibrary:
    """语音来源 + 播放"""

    def __init__(self, cfg: GreetingConfig, hub=None,
                 sample_rate: int = 16000, channels: int = 1):
        self.cfg = cfg
        self.speech = cfg.speech
        self.hub = hub
        self.sample_rate = sample_rate
        self.channels = channels
        self.pool: List[Path] = []
        # label（给人看的原文） -> wav 路径
        self._label_to_path: Dict[str, Path] = {}
        self.last_error = ""
        self.last_text = ""
        self.play_count = 0
        self.refresh()

    # ---------------------------------------------------------------- 音频池

    def _audio_dir(self) -> Path:
        path = Path(self.speech.audio_dir)
        return path if path.is_absolute() else _project_root() / path

    def _read_manifest(self, folder: Path) -> Dict[str, str]:
        """可选的文件名 -> 原文映射，让界面/日志显示「你好呀～」而不是 01"""
        manifest = folder / "manifest.json"
        if not manifest.is_file():
            return {}
        try:
            import json
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if str(v).strip()}
        except Exception as e:
            logger.debug("读取 %s 失败: %s", manifest, e)
        return {}

    def refresh(self) -> None:
        folder = self._audio_dir()
        self.pool = []
        self._label_to_path = {}
        if folder.is_dir():
            self.pool = sorted(p for p in folder.glob("*.wav") if p.is_file())
            manifest = self._read_manifest(folder)
            for path in self.pool:
                self._label_to_path[manifest.get(path.stem, path.stem)] = path
        if self.pool:
            logger.info("打招呼：本地音频池 %d 条（%s）", len(self.pool), folder)
        else:
            logger.info("打招呼：本地音频池为空（%s），将使用云端 TTS", folder)

    # ---------------------------------------------------------------- 能力

    def _tts_ready(self) -> bool:
        if self.speech.mode in ("pool", "off"):
            return False
        try:
            from moyurobot import dashscope_tts
        except ImportError:
            return False
        return dashscope_tts.available()

    def available(self) -> bool:
        if self.speech.mode == "off":
            return False
        if self.pool:
            return True
        return self._tts_ready()

    def describe(self) -> Dict[str, Any]:
        mode = self.speech.mode
        if mode == "auto":
            resolved = "pool" if self.pool else ("tts" if self._tts_ready() else "none")
        else:
            resolved = mode
        return {
            "mode": mode,
            "resolved": resolved,
            "pool_count": len(self.pool),
            "pool_dir": str(self._audio_dir()),
            "tts_ready": self._tts_ready(),
            "tts_voice": self.speech.tts_voice,
            "cache_dir": self.cache_dir(),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "last_text": self.last_text,
            "last_error": self.last_error,
            "play_count": self.play_count,
        }

    def cache_dir(self) -> str:
        raw = self.speech.cache_dir or DEFAULT_CACHE_DIR
        return str(Path(raw).expanduser())

    # ---------------------------------------------------------------- 取音频

    def next_text(self) -> str:
        if self._label_to_path:
            return random.choice(list(self._label_to_path.keys()))
        return random.choice(self.speech.texts) if self.speech.texts else "你好呀～"

    def _pool_pcm(self, label: str) -> Optional[bytes]:
        path = self._label_to_path.get(label)
        if path is None:
            return None
        try:
            return decode_wav(path.read_bytes(), self.sample_rate, self.channels)
        except OSError as e:
            self.last_error = f"读取 {path.name} 失败: {e}"
            return None

    def _cache_path(self, text: str) -> Path:
        seed = f"{text}|{self.speech.tts_voice}|{self.sample_rate}|{self.channels}"
        digest = hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]
        return Path(self.cache_dir()) / f"{digest}.wav"

    def _tts_pcm(self, text: str) -> Optional[bytes]:
        cache = self._cache_path(text)
        try:
            if cache.exists() and cache.stat().st_size > 44:
                return decode_wav(cache.read_bytes(), self.sample_rate, self.channels)
        except OSError:
            pass

        try:
            from moyurobot import dashscope_tts
        except ImportError as e:
            self.last_error = f"缺少 TTS 模块: {e}"
            return None

        try:
            wav_bytes = dashscope_tts.synthesize(
                text,
                voice=self.speech.tts_voice,
                model=self.speech.tts_model,
                timeout=self.speech.timeout_s,
            )
        except Exception as e:
            self.last_error = f"TTS 失败: {e}"
            logger.warning("打招呼语音合成失败: %s", e)
            return None

        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(wav_bytes)
        except OSError as e:
            logger.debug("TTS 缓存写入失败: %s", e)

        pcm = decode_wav(wav_bytes, self.sample_rate, self.channels)
        if pcm is None:
            self.last_error = "TTS 返回的音频格式无法解析"
        return pcm

    def pcm_for(self, label: Optional[str] = None) -> Tuple[str, Optional[bytes]]:
        """拿到某句文案的 PCM

        ``label`` 为空则随机挑一句。指定了文案但池里没有（例如手动测试时临时输入）
        就先用 TTS 现合成；TTS 不可用则退回池里任意一条 —— 宁可口播一句别的，
        也别让「试一次」变成哑巴。
        """
        label = label or self.next_text()

        pcm = self._pool_pcm(label)
        if pcm:
            return label, pcm

        if self._tts_ready():
            return label, self._tts_pcm(label)

        fallback = self.next_text()
        if fallback != label:
            fallback_pcm = self._pool_pcm(fallback)
            if fallback_pcm:
                logger.info("文案「%s」不在音频池里，改播「%s」", label, fallback)
                return fallback, fallback_pcm

        self.last_error = (
            "既没有本地音频（%s），也没有可用的 TTS 密钥（QWEN_API_KEY）"
            % self._audio_dir()
        )
        return label, None

    # ---------------------------------------------------------------- 播放

    def _wait_intercom_idle(self) -> bool:
        """对讲中就不插话。返回 False 表示等不到空闲"""
        if not self.cfg.respect_intercom:
            return True
        deadline = time.time() + max(0.0, self.cfg.intercom_wait_s)
        while getattr(self.hub, "ptt_active", False):
            if time.time() >= deadline:
                return False
            time.sleep(0.2)
        return True

    def play(self, label: Optional[str] = None) -> Dict[str, Any]:
        if self.speech.mode == "off":
            return {"ok": False, "reason": "语音已关闭（speech.mode=off）"}

        hub = self.hub
        if hub is None:
            return {"ok": False, "reason": "音频服务不可用"}

        if not self._wait_intercom_idle():
            return {"ok": False, "reason": "对讲中，本次不插话", "skipped": True}

        text, pcm = self.pcm_for(label)
        if not pcm:
            return {"ok": False, "reason": self.last_error or "没有可播放的音频", "text": text}

        gained = _apply_gain(pcm, self.speech.gain)
        begun = hub.begin_talk(SPEAKER_ID)
        if not begun.get("ok"):
            return {"ok": False, "reason": begun.get("reason") or "扬声器被占用", "text": text}

        chunk = max(320, int(self.sample_rate * self.channels * 2 * 0.1))
        started = time.time()
        try:
            for offset in range(0, len(gained), chunk):
                if not hub.write_talk(SPEAKER_ID, gained[offset:offset + chunk]):
                    return {"ok": False, "reason": "写入扬声器失败", "text": text}
        finally:
            try:
                hub.end_talk(SPEAKER_ID)  # 内部会 drain，等播完才关
            except Exception as e:
                logger.debug("关闭扬声器失败: %s", e)

        self.last_text = text
        self.play_count += 1
        return {
            "ok": True,
            "text": text,
            "seconds": round(time.time() - started, 2),
            "bytes": len(gained),
        }
