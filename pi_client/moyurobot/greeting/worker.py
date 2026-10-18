#!/usr/bin/env python
"""自动打招呼的运行时

一个后台线程，串起「取帧 → 检测 → 判定 → 演出」：

- ``FrameBroker``：前置摄像头的**单一读者**。lerobot 的 ``async_read`` 是队列消费，
  多个消费者会互相偷帧；所有模块（检测、网页 MJPEG、云端拍照）都从这里取最近的帧。
- ``GreeterWorker``：状态机驱动，语音和动作各开一个短命线程，互不阻塞。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from .config import GreetingConfig
from .policy import GreetPolicy
from .speech import SpeechLibrary

logger = logging.getLogger(__name__)


class FrameBroker:
    """给多路消费者共享同一路摄像头

    ``max_age_s`` 内的帧直接复用，过期才真正读一次。这样检测器与网页 MJPEG /
    云端拍照不会互相抢帧，也不会因为两路都要读而把帧率打对折。
    """

    def __init__(self, camera, name: str = "front"):
        self.camera = camera
        self.name = name
        self._lock = threading.Lock()
        self._frame = None
        self._timestamp = 0.0
        self.error = ""

    def get_frame(self, max_age_s: float = 0.3, timeout_ms: int = 500):
        now = time.time()
        with self._lock:
            if self._frame is not None and (now - self._timestamp) <= max_age_s:
                return self._frame
            try:
                frame = self.camera.async_read(timeout_ms=timeout_ms)
            except Exception as e:
                self.error = str(e)
                logger.debug("读取摄像头 %s 失败: %s", self.name, e)
                return None
            if frame is None or getattr(frame, "size", 0) == 0:
                self.error = "读到的帧为空"
                return None
            self._frame = frame
            self._timestamp = time.time()
            self.error = ""
            return frame


class GreeterWorker:
    """后台打招呼线程"""

    def __init__(self, cfg: Optional[GreetingConfig] = None, *,
                 service=None, frame_source=None, detector=None, speech=None,
                 hub=None, action_player=None, clock=None,
                 sample_rate: int = 16000, channels: int = 1):
        self.cfg = cfg or GreetingConfig.load()
        self._clock = clock or time.time
        self.policy = GreetPolicy(self.cfg, clock=self._clock)

        self._service = service
        self._frame_source = frame_source
        self._detector = detector
        self._speech = speech
        self._hub = hub
        self._action_player = action_player
        self._sample_rate = sample_rate
        self._channels = channels
        self._components_ready = all(
            x is not None for x in (frame_source, detector, speech)
        )

        self.enabled = bool(self.cfg.enabled)
        self.frames_seen = 0
        self.persons_seen = 0
        self.last_obs: Dict[str, Any] = {}
        self.last_error = ""
        self.events: Deque[Dict[str, Any]] = deque(maxlen=12)
        self._warned_no_speech = False
        self._warned_no_camera = False

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._perf: Deque[threading.Thread] = deque(maxlen=8)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 组件

    def _ensure_components(self) -> None:
        """懒加载硬件相关组件（numpy/lerobot 只在真正跑起来时才 import）"""
        if self._components_ready:
            return
        with self._lock:
            if self._components_ready:
                return

            if self._service is None:
                try:
                    from moyurobot.core.robot_service import get_global_service
                    self._service = get_global_service()
                except Exception as e:
                    self.last_error = f"机器人服务不可用: {e}"

            if self._frame_source is None and self._service is not None:
                try:
                    robot = getattr(self._service, "robot", None)
                    camera = (robot.cameras or {}).get(self.cfg.camera) if robot else None
                    if camera is not None:
                        self._frame_source = FrameBroker(camera, self.cfg.camera)
                except Exception as e:
                    self.last_error = f"查找摄像头失败: {e}"

            if self._detector is None:
                try:
                    from .detector import PersonDetector
                    self._detector = PersonDetector(self.cfg)
                except Exception as e:
                    self.last_error = f"检测器初始化失败: {e}"

            if self._speech is None:
                if self._hub is None:
                    try:
                        from moyurobot.core.audio import AudioConfig, get_hub
                        audio_cfg = AudioConfig.load()
                        self._sample_rate = audio_cfg.sample_rate
                        self._channels = audio_cfg.channels
                        self._hub = get_hub(audio_cfg)
                    except Exception as e:
                        logger.debug("音频服务不可用: %s", e)
                        self._hub = None
                self._speech = SpeechLibrary(
                    self.cfg, hub=self._hub,
                    sample_rate=self._sample_rate, channels=self._channels,
                )

            self._components_ready = True

    @property
    def frame_source(self) -> Optional[FrameBroker]:
        self._ensure_components()
        return self._frame_source

    @property
    def detector(self):
        self._ensure_components()
        return self._detector

    @property
    def speech(self) -> Optional[SpeechLibrary]:
        self._ensure_components()
        return self._speech

    @property
    def action_player(self):
        if self._action_player is None:
            try:
                from moyurobot.core.actions import get_action_player
                if self._service is None:
                    from moyurobot.core.robot_service import get_global_service
                    self._service = get_global_service()
                self._action_player = get_action_player(self._service)
            except Exception as e:
                logger.debug("动作播放器不可用: %s", e)
                return None
        return self._action_player

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="greeter")
        self._thread.start()
        return True

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        if wait and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        enabled = bool(enabled)
        changed = enabled != self.enabled
        self.enabled = enabled
        self.cfg.enabled = enabled
        if changed:
            # 重新启用时清掉旧的累积，避免「开关一开就立刻招呼一次」
            self.policy.reset()
            logger.info("自动打招呼已%s", "启用" if enabled else "停用")
            self._event("toggle", "启用" if enabled else "停用")
        return {"success": True, "enabled": self.enabled, "changed": changed}

    # ---------------------------------------------------------------- 主循环

    def _event(self, kind: str, detail: str) -> None:
        self.events.append({
            "t": time.strftime("%H:%M:%S", time.localtime(self._clock())),
            "kind": kind,
            "detail": detail,
        })

    def _run(self) -> None:
        self._ensure_components()
        self._log_startup()
        while not self._stop.is_set():
            interval = self.cfg.detect_interval()
            try:
                if not self.enabled:
                    self._stop.wait(0.5)
                    continue

                broker = self._frame_source
                detector = self._detector
                if broker is None or detector is None or not detector.available:
                    self._retry_camera()
                    broker = self._frame_source
                if broker is None or detector is None or not detector.available:
                    if detector is None or not detector.available:
                        reason = "检测器不可用：" + (
                            (detector.error if detector is not None else "未初始化") or "未知原因"
                        )
                    else:
                        reason = "前置摄像头不可用"
                    self._warn_not_ready_once(reason)
                    self._stop.wait(2.0)
                    continue

                frame = broker.get_frame(max_age_s=max(0.05, interval * 0.8))
                if frame is None:
                    self._stop.wait(0.3)
                    continue

                self.frames_seen += 1
                detection = detector.detect(frame)
                present = detection is not None
                if present:
                    self.persons_seen += 1
                self.last_obs = {
                    "present": present,
                    "ratio": round(detection.ratio, 3) if detection else 0.0,
                    "centered": bool(detection.centered) if detection else True,
                    "score": round(detection.score, 3) if detection else 0.0,
                    "at": time.strftime("%H:%M:%S", time.localtime(self._clock())),
                }

                decision = self.policy.evaluate(
                    present=present,
                    ratio=detection.ratio if detection else 0.0,
                    centered=detection.centered if detection else True,
                )
                if decision.greet:
                    self._perform(decision.reason, frame)
            except Exception as e:  # 任何异常都不能让后台线程死掉
                self.last_error = f"{type(e).__name__}: {e}"
                logger.exception("打招呼循环异常: %s", e)
                self._stop.wait(1.0)

            self._stop.wait(interval)

        logger.info("自动打招呼线程已退出")

    def _log_startup(self) -> None:
        cfg = self.cfg
        det = self._detector
        backend = det.backend if det is not None else "none"
        speech = self._speech
        speech_desc = speech.describe() if speech is not None else {}
        logger.info(
            "自动打招呼：%s | 检测器=%s | 语音=%s | 时段=%s | 摄像头=%s | 冷却=%ss",
            "已启用" if self.enabled else "未启用（可在网页上开）",
            backend,
            speech_desc.get("resolved", "none"),
            cfg.active_hours or "全天",
            cfg.camera,
            int(cfg.cooldown_seconds),
        )
        if det is not None and det.error:
            logger.warning("自动打招呼：检测器降级/不可用原因 —— %s", det.error)
        if self.enabled and speech is not None and not speech.available():
            logger.warning(
                "自动打招呼：没有可用的语音。放几个 wav 到 %s，或配置 QWEN_API_KEY 让 TTS 兜底"
                "（可先跑 scripts/gen_greeting_voices.py 生成）",
                speech.describe().get("pool_dir", ""),
            )

    def _retry_camera(self) -> None:
        """摄像头后插上 / 机器人稍后才连上时，别让功能永远卡在"不可用" """
        if self._frame_source is not None:
            return
        if self._service is None:
            try:
                from moyurobot.core.robot_service import get_global_service
                self._service = get_global_service()
            except Exception:
                return
        if self._service is None:
            return
        try:
            robot = getattr(self._service, "robot", None)
            camera = (robot.cameras or {}).get(self.cfg.camera) if robot else None
            if camera is not None:
                self._frame_source = FrameBroker(camera, self.cfg.camera)
                self._warned_no_camera = False
                logger.info("摄像头 %s 已就绪，自动打招呼恢复", self.cfg.camera)
        except Exception as e:
            logger.debug("重试获取摄像头失败: %s", e)

    def _warn_not_ready_once(self, reason: str) -> None:
        if self._warned_no_camera:
            return
        self._warned_no_camera = True
        self.last_error = reason
        logger.warning("自动打招呼暂停：%s（修好后会自动恢复，无需重启）", reason)

    # ---------------------------------------------------------------- 演出

    def _perform(self, reason: str, frame=None) -> None:
        """语音与动作各开一个线程，检测循环不阻塞"""
        speech = self._speech
        speak_off = (self.cfg.speech.mode == "off")
        can_speak = speech is not None and speech.available()

        # 「打招呼」的主体是说话。没有声音却点个头会显得莫名其妙，
        # 所以默认宁可不做；只有显式 speech.mode=off 才算「只想动手」。
        if not can_speak and not speak_off:
            if not self._warned_no_speech:
                self._warned_no_speech = True
                self.last_error = (speech.last_error if speech is not None else "") or "没有可用的语音"
                logger.warning("检测到人但没有可用语音，本次不打招呼：%s", self.last_error)
            return

        self._save_snapshot(frame)
        text = speech.next_text() if can_speak else ""
        self._event("greet", f"{reason}｜{text or '仅动作'}")
        logger.info("👋 有人来了，打招呼（%s%s）", reason, "，仅动作" if not can_speak else "")

        if can_speak:
            # 把已选好的文案传进去，保证日志里记的和实际说的是同一句
            self._spawn(self._do_speech, speech, text)

        gesture = self.cfg.gesture
        if gesture and gesture != "none":
            self._spawn(self._do_gesture, gesture, self.cfg.gesture_delay_ms)

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True,
                                  name=f"greeter-{getattr(target, '__name__', 'task')}")
        self._perf.append(thread)
        thread.start()

    def _do_speech(self, speech: SpeechLibrary, text: Optional[str] = None) -> None:
        try:
            result = speech.play(text)
        except Exception as e:
            self.last_error = f"语音播放异常: {e}"
            logger.warning("打招呼语音失败: %s", e)
            return
        if result.get("ok"):
            self._event("speech", f"「{result.get('text')}」{result.get('seconds', 0)}s")
        else:
            detail = str(result.get("reason") or "未知原因")
            self.last_error = detail
            self._event("speech", f"跳过：{detail}")

    def _do_gesture(self, gesture: str, delay_ms: int) -> None:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        player = self.action_player
        if player is None:
            self._event("gesture", "机械臂不可用，跳过")
            return
        if self.cfg.respect_arm_busy:
            state = player.get_state() or {}
            if state.get("playing"):
                self._event("gesture", "机械臂正在执行其它动作，本次只出声")
                return
        try:
            result = player.play(gesture)
        except Exception as e:
            self.last_error = f"动作播放异常: {e}"
            logger.warning("打招呼动作失败: %s", e)
            return
        if result.get("success"):
            self._event("gesture", {"nod": "点头", "shake_head": "摇头"}.get(gesture, gesture))
        else:
            self._event("gesture", f"跳过：{result.get('message', '')}")

    def _save_snapshot(self, frame) -> None:
        if not self.cfg.snapshot_dir or frame is None:
            return
        try:
            import cv2
            folder = Path(self.cfg.snapshot_dir).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            name = time.strftime("greet_%Y%m%d_%H%M%S.jpg", time.localtime(self._clock()))
            cv2.imwrite(str(folder / name), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        except Exception as e:
            logger.debug("保存招呼快照失败: %s", e)

    # ---------------------------------------------------------------- 对外

    def trigger_now(self, text: Optional[str] = None) -> Dict[str, Any]:
        """手动来一次（网页「试一次」/ MCP 调试），绕过检测、时段与限速"""
        decision = self.policy.force()
        speech = self._speech
        chosen = text or (speech.next_text() if speech is not None else "")
        self._event("manual", chosen)

        if speech is None:
            return {"success": False, "error": "语音模块未就绪"}

        def _run_once():
            try:
                result = speech.play(chosen)
                if result.get("ok"):
                    self._event("speech", f"「{result.get('text')}」{result.get('seconds', 0)}s")
                else:
                    self._event("speech", f"跳过：{result.get('reason')}")
            except Exception as e:
                self.last_error = f"语音播放异常: {e}"
            if self.cfg.gesture and self.cfg.gesture != "none":
                self._do_gesture(self.cfg.gesture, self.cfg.gesture_delay_ms)

        self._spawn(_run_once)
        return {
            "success": True,
            "text": chosen,
            "gesture": decision.gesture if decision.gesture != "none" else "",
        }

    def status(self) -> Dict[str, Any]:
        snap = self.policy.snapshot()
        detector = self._detector
        speech = self._speech
        broker = self._frame_source
        return {
            "enabled": self.enabled,
            "running": self.running,
            "camera": self.cfg.camera,
            "camera_ready": broker is not None,
            "camera_error": (broker.error if broker is not None else "摄像头未找到"),
            "frames_seen": self.frames_seen,
            "persons_seen": self.persons_seen,
            "last_observation": dict(self.last_obs),
            "detector": detector.describe() if detector is not None else {"backend": "none"},
            "speech": speech.describe() if speech is not None else {"resolved": "none"},
            "config": {
                "active_hours": self.cfg.active_hours,
                "min_person_ratio": self.cfg.min_person_ratio,
                "center_zone": self.cfg.center_zone,
                "cooldown_seconds": self.cfg.cooldown_seconds,
                "max_per_hour": self.cfg.max_per_hour,
                "gesture": self.cfg.gesture,
                "detect_fps": self.cfg.detect_fps,
            },
            "events": list(self.events),
            "last_error": self.last_error,
            **snap,
        }


# ---------------------------------------------------------------- 全局单例

_worker: Optional[GreeterWorker] = None
_worker_lock = threading.Lock()


def start_greeter(service=None, cfg: Optional[GreetingConfig] = None) -> Optional[GreeterWorker]:
    """启动（幂等）。即使 enabled=false 也会起线程，好让网页能随时开关。"""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.running:
            return _worker
        worker = _worker or GreeterWorker(cfg=cfg, service=service)
        _worker = worker
        worker.start()
        return worker


def get_greeter() -> Optional[GreeterWorker]:
    return _worker


def stop_greeter() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.stop()
            _worker = None


def get_frame_broker(name: str = "front") -> Optional[FrameBroker]:
    """给网页 MJPEG / 云端拍照复用同一个摄像头读取器（避免互相偷帧）"""
    worker = _worker
    if worker is None:
        return None
    broker = worker.frame_source
    if broker is not None and broker.name == name:
        return broker
    return None
