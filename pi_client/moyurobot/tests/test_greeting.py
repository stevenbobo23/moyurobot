#!/usr/bin/env python
"""
自动打招呼测试：配置 / 状态机 / 音频解码 / 资源仲裁

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_greeting.py

不依赖 numpy / opencv / lerobot：检测器与摄像头都用假对象注入，
这样在开发机上也能把最容易被改坏的部分（冷却、限速、离开判定）钉死。
"""

import array
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from moyurobot.greeting.config import (  # noqa: E402
    GreetingConfig,
    in_active_hours,
    parse_active_hours,
)
from moyurobot.greeting.policy import (  # noqa: E402
    STATE_COOLDOWN,
    STATE_IDLE,
    STATE_SEEING,
    GreetPolicy,
)
from moyurobot.greeting.detector import Detection  # noqa: E402
from moyurobot.greeting.speech import SpeechLibrary, _apply_gain, decode_wav  # noqa: E402
from moyurobot.greeting.worker import FrameBroker, GreeterWorker  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))


def make_cfg(**overrides) -> GreetingConfig:
    """干净配置：不读磁盘、不读环境变量"""
    saved = dict(os.environ)
    try:
        for key in list(os.environ):
            if key.startswith("GREETING_"):
                os.environ.pop(key, None)
        cfg = GreetingConfig.from_dict({})
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg
    finally:
        os.environ.clear()
        os.environ.update(saved)


def local_ts(hour: int, minute: int = 0) -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))


def make_wav(path: Path, seconds: float = 0.5, rate: int = 24000,
             channels: int = 2, value: int = 8000) -> Path:
    frames = int(seconds * rate)
    samples = array.array("h", [value] * (frames * channels))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.tobytes())
    return path


# ---------------------------------------------------------------- 假硬件


class FakeCamera:
    def __init__(self, frame=None):
        self.reads = 0
        self.frame = frame if frame is not None else _FakeFrame()

    def async_read(self, timeout_ms=100):
        self.reads += 1
        return self.frame


class _FakeFrame:
    size = 640 * 480 * 3
    shape = (480, 640, 3)


class FakeDetector:
    backend = "fake"
    available = True
    error = ""

    def __init__(self, detection=None):
        self.detection = detection
        self.calls = 0

    def describe(self):
        return {"backend": "fake", "model": "", "error": "", "input_size": [320, 320]}

    def detect(self, frame):
        self.calls += 1
        return self.detection


class FakeHub:
    def __init__(self):
        self.ptt_active = False
        self.began = 0
        self.ended = 0
        self.data = b""

    def begin_talk(self, client_id):
        if self.ptt_active:
            return {"ok": False, "reason": f"已有用户在说话（{client_id}）"}
        self.ptt_active = True
        self.began += 1
        return {"ok": True}

    def write_talk(self, client_id, data):
        self.data += data
        return True

    def end_talk(self, client_id):
        self.ptt_active = False
        self.ended += 1
        return True


class FakeActionPlayer:
    def __init__(self, playing=False):
        self.playing = playing
        self.played = []

    def get_state(self):
        return {"playing": self.playing}

    def play(self, action_id):
        self.played.append(action_id)
        return {"success": True, "action_id": action_id}


# ---------------------------------------------------------------- 配置


def test_config_defaults():
    cfg = GreetingConfig.load()
    check("default.json 的 greeting 段被读到", cfg.active_hours == "08:00-22:00",
          f"active_hours={cfg.active_hours!r}")
    check("默认手势是点头", cfg.gesture == "nod", cfg.gesture)
    check("默认冷却 180 秒", cfg.cooldown_seconds == 180.0, str(cfg.cooldown_seconds))
    check("语音默认 auto", cfg.speech.mode == "auto", cfg.speech.mode)


def test_parse_active_hours():
    check("08:00-22:00 → (480, 1320)", parse_active_hours("08:00-22:00") == (480, 1320))
    check("允许 8:00-22:00 这种不补零写法", parse_active_hours("8:00-22:00") == (480, 1320))
    check("支持跨零点 22:00-06:00", parse_active_hours("22:00-06:00") == (1320, 360))
    check("空字符串 = 全天", parse_active_hours("") is None)
    check("写错的格式退化成全天（不静默变永不触发）",
          parse_active_hours("早上到晚上") is None)


def test_in_active_hours():
    check("08:00-22:00 内 09:30 允许", in_active_hours("08:00-22:00", local_ts(9, 30)))
    check("08:00-22:00 前 07:59 不允许", not in_active_hours("08:00-22:00", local_ts(7, 59)))
    check("08:00-22:00 边界 08:00 允许", in_active_hours("08:00-22:00", local_ts(8, 0)))
    check("08:00-22:00 边界 22:00 不允许（左闭右开）",
          not in_active_hours("08:00-22:00", local_ts(22, 0)))
    check("跨零点 22:00-06:00 的 23:00 允许", in_active_hours("22:00-06:00", local_ts(23, 0)))
    check("跨零点 22:00-06:00 的 12:00 不允许",
          not in_active_hours("22:00-06:00", local_ts(12, 0)))
    check("时段留空 = 全天允许", in_active_hours("", local_ts(3, 0)))


def test_env_override():
    cfg = GreetingConfig.load(env={
        "GREETING_ACTIVE_HOURS": "22:00-06:00",
        "GREETING_COOLDOWN_SECONDS": "30",
        "GREETING_GESTURE": "shake_head",
        "GREETING_ENABLED": "0",
        "GREETING_TEXTS": "甲,乙|丙",
        "GREETING_MIN_PERSON_RATIO": "0.5",
    })
    check("环境变量覆盖 active_hours", cfg.active_hours == "22:00-06:00", cfg.active_hours)
    check("环境变量覆盖冷却秒数", cfg.cooldown_seconds == 30.0, str(cfg.cooldown_seconds))
    check("环境变量切换手势", cfg.gesture == "shake_head", cfg.gesture)
    check("GREETING_ENABLED=0 能关掉", cfg.enabled is False)
    check("文案支持逗号/竖线分隔", cfg.speech.texts == ["甲", "乙", "丙"], str(cfg.speech.texts))
    check("比例阈值被正确解析", abs(cfg.min_person_ratio - 0.5) < 1e-9)
    check("非法手势回退到 nod", GreetingConfig.from_dict({"gesture": "跳舞"}).gesture == "nod")

    # env 手滑写成越界值时要和 from_dict 一样被夹住：
    # 否则 min_person_ratio=3 会让检测永不触发，且日志里看不出任何异常
    bad = GreetingConfig.load(env={
        "GREETING_MIN_PERSON_RATIO": "3",
        "GREETING_CENTER_ZONE": "5",
        "GREETING_YOLO_CONF": "1.5",
        "GREETING_HOG_SCALE": "0.5",
    })
    check("env 比例阈值上限截断", bad.min_person_ratio == 1.0, str(bad.min_person_ratio))
    check("env center_zone 上限截断", bad.center_zone == 1.0, str(bad.center_zone))
    check("env yolo_conf 上限截断", bad.yolo_conf == 0.99, str(bad.yolo_conf))
    check("env hog_scale 下限抬到 1.01", abs(bad.hog_scale - 1.01) < 1e-9, str(bad.hog_scale))


def test_config_clamping():
    cfg = GreetingConfig.from_dict({"min_person_ratio": 5, "center_zone": 9,
                                    "detect_fps": 0, "max_per_hour": 0})
    check("比例阈值上限截断到 1.0", cfg.min_person_ratio == 1.0, str(cfg.min_person_ratio))
    check("center_zone 上限截断到 1.0", cfg.center_zone == 1.0, str(cfg.center_zone))
    check("detect_fps 有下限（不会 0 导致死循环）", cfg.detect_fps > 0, str(cfg.detect_fps))
    check("max_per_hour 至少 1", cfg.max_per_hour >= 1, str(cfg.max_per_hour))


# ---------------------------------------------------------------- 状态机


def test_policy_needs_accumulation():
    cfg = make_cfg(active_hours="")
    pol = GreetPolicy(cfg, clock=lambda: 1000.0)
    # trigger_seconds 默认 0.5：0.3s 一帧，第 3 帧刚好 0.6s
    decisions = [pol.evaluate(now=1000.0 + i * 0.3, present=True, ratio=0.6).greet
                 for i in range(4)]
    check("前两帧不触发（需要连击）", decisions[:2] == [False, False], str(decisions))
    check("第三帧触发", decisions[2] is True, str(decisions))
    check("触发后进入冷却", pol.snapshot(now=1000.6)["state"] == STATE_COOLDOWN)


def test_policy_tolerates_single_miss():
    cfg = make_cfg(active_hours="", trigger_frames=3, trigger_seconds=0.0)
    pol = GreetPolicy(cfg, clock=lambda: 2000.0)
    sequence = [True, True, False, True, True]  # 中间漏检一帧
    greeted = None
    for index, present in enumerate(sequence):
        decision = pol.evaluate(now=2000.0 + index * 0.25, present=present, ratio=0.6)
        if decision.greet:
            greeted = index
            break
    check("漏检一帧不会让累积归零（第 4 帧仍能触发）", greeted == 4, f"triggered_at={greeted}")


def test_policy_two_misses_reset():
    cfg = make_cfg(active_hours="", trigger_frames=2, trigger_seconds=0.0)
    pol = GreetPolicy(cfg, clock=lambda: 3000.0)
    pol.evaluate(now=3000.0, present=True, ratio=0.6)   # score 1
    pol.evaluate(now=3000.2, present=False, ratio=0.0)  # score 0.5
    pol.evaluate(now=3000.4, present=False, ratio=0.0)  # score 0
    decision = pol.evaluate(now=3000.6, present=True, ratio=0.6)  # score 1
    check("连续两次漏检后累积清零", not decision.greet and decision.state == STATE_SEEING,
          f"{decision.reason}/{decision.state}")


def test_policy_ratio_and_center():
    cfg = make_cfg(active_hours="", trigger_frames=1, trigger_seconds=0.0,
                   min_person_ratio=0.3, center_zone=0.7)
    pol = GreetPolicy(cfg, clock=lambda: 4000.0)
    far = pol.evaluate(now=4000.0, present=True, ratio=0.1)
    check("太远的人不触发", not far.greet and far.reason == "too_far", far.reason)

    pol.reset()
    off = pol.evaluate(now=4000.0, present=True, ratio=0.6, centered=False)
    check("不在画面中央的人不触发", not off.greet and off.reason == "off_center", off.reason)

    pol.reset()
    ok = pol.evaluate(now=4000.0, present=True, ratio=0.6, centered=True)
    check("距离够且居中则触发", ok.greet, ok.reason)

    cfg.center_zone = 1.0
    pol2 = GreetPolicy(cfg, clock=lambda: 4000.0)
    loose = pol2.evaluate(now=4000.0, present=True, ratio=0.6, centered=False)
    check("center_zone=1.0 时不限制位置", loose.greet, loose.reason)


def test_policy_cooldown():
    cfg = make_cfg(active_hours="", trigger_frames=1, trigger_seconds=0.0, cooldown_seconds=60)
    pol = GreetPolicy(cfg, clock=lambda: 5000.0)
    first = pol.evaluate(now=5000.0, present=True, ratio=0.6)
    during = pol.evaluate(now=5010.0, present=True, ratio=0.6)
    after = pol.evaluate(now=5061.0, present=True, ratio=0.6)
    check("第一次触发", first.greet)
    check("冷却期内不重复", not during.greet and during.reason == "cooldown", during.reason)
    check("冷却结束后可再次触发", after.greet, after.reason)


def test_policy_leave_keeps_cooldown():
    cfg = make_cfg(active_hours="", trigger_frames=1, trigger_seconds=0.0,
                   cooldown_seconds=60, leave_seconds=5)
    pol = GreetPolicy(cfg, clock=lambda: 6000.0)
    pol.evaluate(now=6000.0, present=True, ratio=0.6)          # 触发
    pol.evaluate(now=6010.0, present=False)                    # 无人，开始计时
    pol.evaluate(now=6016.0, present=False)                    # 超过 5 秒，算离开
    back = pol.evaluate(now=6020.0, present=True, ratio=0.6)   # 同一个人又回来
    check("离开后仍受冷却保护（不会被来回走动的人反复触发）",
          not back.greet and back.reason == "cooldown", back.reason)


def test_policy_rate_limit():
    cfg = make_cfg(active_hours="", trigger_frames=1, trigger_seconds=0.0,
                   cooldown_seconds=0, max_per_hour=2)
    pol = GreetPolicy(cfg, clock=lambda: 7000.0)
    results = []
    now = 7000.0
    for _ in range(4):
        decision = pol.evaluate(now=now, present=True, ratio=0.6)
        results.append((decision.greet, decision.reason))
        now += 1.0
    check("小时配额 2 次后拒绝", [g for g, _ in results] == [True, True, False, False],
          str(results))
    check("拒绝原因标注为速率限制", results[2][1] == "rate_limited", results[2][1])


def test_policy_inactive_hours():
    cfg = make_cfg(active_hours="08:00-22:00", trigger_frames=1, trigger_seconds=0.0)
    night = local_ts(23, 30)
    pol = GreetPolicy(cfg, clock=lambda: night)
    decision = pol.evaluate(now=night, present=True, ratio=0.6)
    check("不在时段内即使有人也不触发",
          not decision.greet and decision.reason == "inactive_hours", decision.reason)

    # 21:59 攒的分不该在 22:00 之后炸出来
    day_end = local_ts(21, 59)
    pol.evaluate(now=day_end, present=True, ratio=0.6)
    after = pol.evaluate(now=day_end + 120, present=True, ratio=0.6)
    check("跨时段边界不会把之前的累积带过去", not after.greet, after.reason)


def test_policy_force():
    cfg = make_cfg(active_hours="22:00-06:00", cooldown_seconds=999)
    night = local_ts(12, 0)
    pol = GreetPolicy(cfg, clock=lambda: night)
    decision = pol.force(now=night)
    check("force 绕过时段与检测", decision.greet and decision.reason == "manual", decision.reason)
    check("force 也会占用冷却", pol.snapshot(now=night)["state"] == STATE_COOLDOWN)
    check("force 计数 +1", pol.greet_count == 1, str(pol.greet_count))


def test_policy_state_reporting():
    cfg = make_cfg(active_hours="")
    pol = GreetPolicy(cfg, clock=lambda: 8000.0)
    idle = pol.snapshot(now=8000.0)
    pol.evaluate(now=8000.0, present=True, ratio=0.6)
    seeing = pol.snapshot(now=8000.0)
    check("没人是 idle", idle["state"] == STATE_IDLE, idle["state"])
    check("有人在累积是 seeing", seeing["state"] == STATE_SEEING, seeing["state"])
    check("快照里带时段是否生效", seeing["in_active_hours"] is True)
    check("快照里带小时配额使用量", "last_hour_count" in seeing)


# ---------------------------------------------------------------- 音频


def test_decode_wav():
    tmp = Path(tempfile.mkdtemp(prefix="moyu-greet-wav-"))
    try:
        src = make_wav(tmp / "a.wav", seconds=0.5, rate=24000, channels=2)
        pcm = decode_wav(src.read_bytes(), 16000, 1)
        expected = int(0.5 * 16000) * 2
        check("24k 立体声 → 16k 单声道", pcm is not None, "解码失败")
        if pcm:
            check("重采样后时长基本不变（±10%）",
                  abs(len(pcm) - expected) <= expected * 0.1,
                  f"{len(pcm)} vs {expected} 字节")
        check("坏数据不会抛异常", decode_wav(b"not a wav", 16000, 1) is None)
        check("空数据返回 None", decode_wav(b"", 16000, 1) is None)
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()


def test_gain_clipping():
    samples = array.array("h", [30000, -30000, 1000])
    out = _apply_gain(samples.tobytes(), 2.0)
    values = array.array("h")
    values.frombytes(out)
    check("增益不会溢出，超限值被夹到 32767", list(values) == [32767, -32768, 2000],
          str(list(values)))
    same = _apply_gain(samples.tobytes(), 1.0)
    check("gain=1.0 原样返回", same == samples.tobytes())


def _speech_with_pool(tmp: Path, mode: str = "pool") -> SpeechLibrary:
    make_wav(tmp / "greet_01.wav", seconds=0.4, rate=16000, channels=1)
    cfg = make_cfg()
    cfg.speech.mode = mode
    cfg.speech.audio_dir = str(tmp)
    cfg.speech.gain = 1.0
    return SpeechLibrary(cfg, hub=FakeHub(), sample_rate=16000, channels=1)


def test_speech_pool_playback():
    tmp = Path(tempfile.mkdtemp(prefix="moyu-greet-pool-"))
    try:
        lib = _speech_with_pool(tmp)
        check("本地音频池被扫到", lib.available() and len(lib.pool) == 1, str(lib.describe()))
        result = lib.play()
        check("播放成功", result.get("ok") is True, str(result))
        check("扬声器收到了 PCM", len(lib.hub.data) > 0, f"{len(lib.hub.data)} 字节")
        check("播完释放了喇叭", lib.hub.ended == 1 and not lib.hub.ptt_active)
        check("播放计数 +1", lib.play_count == 1)
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()


def test_speech_intercom_arbitration():
    tmp = Path(tempfile.mkdtemp(prefix="moyu-greet-arb-"))
    try:
        lib = _speech_with_pool(tmp)
        lib.hub.ptt_active = True
        lib.cfg.intercom_wait_s = 0.0
        result = lib.play()
        check("对讲中不插话（跳过而不是抢断）",
              result.get("ok") is False and result.get("skipped") is True, str(result))
        check("跳过时不占用喇叭", lib.hub.began == 0)
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()


def test_speech_off_mode():
    cfg = make_cfg()
    cfg.speech.mode = "off"
    lib = SpeechLibrary(cfg, hub=FakeHub(), sample_rate=16000, channels=1)
    result = lib.play()
    check("speech.mode=off 时不发声", result.get("ok") is False, str(result))


# ---------------------------------------------------------------- 帧共享


def test_frame_broker_cache():
    camera = FakeCamera()
    broker = FrameBroker(camera, "front")
    first = broker.get_frame(max_age_s=5.0)
    second = broker.get_frame(max_age_s=5.0)
    check("同一窗口内复用同一帧", first is second and camera.reads == 1,
          f"reads={camera.reads}")
    broker.get_frame(max_age_s=0.0)
    check("max_age=0 时强制重新读", camera.reads == 2, f"reads={camera.reads}")


# ---------------------------------------------------------------- 仲裁与状态


def _worker_with_fakes(tmp: Path, detection=None, playing=False):
    cfg = make_cfg(active_hours="", gesture_delay_ms=0)
    cfg.speech.mode = "pool"
    cfg.speech.audio_dir = str(tmp)
    make_wav(tmp / "greet_01.wav", seconds=0.2, rate=16000, channels=1)
    hub = FakeHub()
    speech = SpeechLibrary(cfg, hub=hub, sample_rate=16000, channels=1)
    player = FakeActionPlayer(playing=playing)
    worker = GreeterWorker(
        cfg,
        frame_source=FrameBroker(FakeCamera(), "front"),
        detector=FakeDetector(Detection(ratio=0.6, centered=True)),
        speech=speech,
        hub=hub,
        action_player=player,
    )
    return worker, player, hub


def test_worker_trigger_now():
    tmp = Path(tempfile.mkdtemp(prefix="moyu-greet-worker-"))
    try:
        worker, player, hub = _worker_with_fakes(tmp)
        result = worker.trigger_now("测试一下")
        check("手动触发返回成功", result.get("success") is True, str(result))

        deadline = time.time() + 3.0
        while time.time() < deadline and not player.played:
            time.sleep(0.05)
        check("手动触发会出声", len(hub.data) > 0, f"{len(hub.data)} 字节")
        check("手动触发会做点头动作", player.played == ["nod"], str(player.played))
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()


def test_worker_respects_busy_arm():
    tmp = Path(tempfile.mkdtemp(prefix="moyu-greet-busy-"))
    try:
        worker, player, hub = _worker_with_fakes(tmp, playing=True)
        worker.trigger_now("机械臂忙")
        time.sleep(0.4)
        check("机械臂忙时只出声不动手", player.played == [], str(player.played))
        check("机械臂忙时语音照常", len(hub.data) > 0)
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()


def test_worker_skips_when_no_speech():
    """没有可用语音时不该只点个头（那看起来更奇怪）"""
    tmp = Path(tempfile.mkdtemp(prefix="moyu-greet-nospeak-"))
    try:
        cfg = make_cfg(active_hours="", gesture_delay_ms=0)
        cfg.speech.mode = "pool"          # 池里没有 wav，也没有 TTS
        cfg.speech.audio_dir = str(tmp)
        hub = FakeHub()
        player = FakeActionPlayer()
        worker = GreeterWorker(
            cfg,
            frame_source=FrameBroker(FakeCamera(), "front"),
            detector=FakeDetector(Detection(ratio=0.6, centered=True)),
            speech=SpeechLibrary(cfg, hub=hub, sample_rate=16000, channels=1),
            hub=hub, action_player=player,
        )
        worker._perform("ok")
        time.sleep(0.3)
        check("没有语音时整次招呼跳过（不做哑巴点头）", player.played == [], str(player.played))
        check("跳过原因被记下来", bool(worker.last_error), worker.last_error)

        # 显式关语音 = 只想动手，这时应该做动作
        cfg.speech.mode = "off"
        worker2 = GreeterWorker(
            cfg,
            frame_source=FrameBroker(FakeCamera(), "front"),
            detector=FakeDetector(None),
            speech=SpeechLibrary(cfg, hub=hub, sample_rate=16000, channels=1),
            hub=hub, action_player=player,
        )
        worker2._perform("ok")
        time.sleep(0.3)
        check("speech.mode=off 时只做动作", player.played == ["nod"], str(player.played))
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()


def test_worker_status_and_toggle():
    tmp = Path(tempfile.mkdtemp(prefix="moyu-greet-status-"))
    try:
        worker, _, _ = _worker_with_fakes(tmp)
        status = worker.status()
        check("状态里有检测器信息", status["detector"]["backend"] == "fake")
        check("状态里有语音来源", status["speech"]["resolved"] == "pool", str(status["speech"]))
        check("状态默认带时段", status["config"]["active_hours"] == "", status["config"])

        worker.set_enabled(False)
        check("可以运行时关闭", worker.status()["enabled"] is False)
        worker.set_enabled(True)
        check("可以运行时打开", worker.status()["enabled"] is True)
    finally:
        for path in tmp.glob("*"):
            path.unlink()
        tmp.rmdir()


def main() -> int:
    test_config_defaults()
    test_parse_active_hours()
    test_in_active_hours()
    test_env_override()
    test_config_clamping()

    test_policy_needs_accumulation()
    test_policy_tolerates_single_miss()
    test_policy_two_misses_reset()
    test_policy_ratio_and_center()
    test_policy_cooldown()
    test_policy_leave_keeps_cooldown()
    test_policy_rate_limit()
    test_policy_inactive_hours()
    test_policy_force()
    test_policy_state_reporting()

    test_decode_wav()
    test_gain_clipping()
    test_speech_pool_playback()
    test_speech_intercom_arbitration()
    test_speech_off_mode()

    test_frame_broker_cache()
    test_worker_trigger_now()
    test_worker_respects_busy_arm()
    test_worker_skips_when_no_speech()
    test_worker_status_and_toggle()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n=== 结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        print("失败项:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
