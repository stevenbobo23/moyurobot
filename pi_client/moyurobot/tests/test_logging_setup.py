#!/usr/bin/env python
"""
日志治理测试：去重 / 指数退避 / 限速 / 轮转

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_logging_setup.py

覆盖：
1. 同一条消息在窗口内只放行 1 条（模拟 lerobot 摄像头 30fps 刷屏）
2. 退避重报：放行间隔按 1s→2s→4s… 翻倍，长时间刷屏的日志量是 O(log 时间)
3. 不同摄像头（不同消息）互不影响
4. 长时间静默后重新按窗口起算，不会一直保持长间隔
5. 单 logger 限速：一秒内只放行 N 条，丢弃数会标注在下一条里
6. LOG_DEDUP_WINDOW_S=0 / LOG_RATE_LIMIT_PER_S=0 时整体失效（全部放行）
7. setup_logging 幂等、噪声 logger 级别生效
8. RotatingFileHandler 把日志文件大小压在 LOG_MAX_BYTES 以内
"""

import importlib.util
import io
import logging
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _PROJECT_ROOT)


def _load_logging_setup():
    """直接按文件路径加载 logging_setup

    不走 ``import moyurobot.core``：core/__init__ 会连带拉起 numpy/lerobot，
    而这个模块本身只用标准库，按路径加载可以让测试在没有机器人依赖的机器上也能跑。
    """
    try:
        from moyurobot import logging_setup as module  # noqa: F401
        return module
    except ImportError:
        path = os.path.join(_PROJECT_ROOT, "moyurobot", "logging_setup.py")
        spec = importlib.util.spec_from_file_location("moyurobot_logging_setup", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


logging_setup = _load_logging_setup()

RESULTS = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))


class FakeClock:
    """可控时钟，替换 logging_setup.time 用，避免测试依赖真实时间"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingHandler(logging.Handler):
    """把记录收集到内存，便于统计条数与内容"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def messages(self):
        return [r.getMessage() for r in self.records]


def make_logger(name: str, filters) -> tuple:
    """搭一个独立 logger + 内存 handler，过滤器直接挂在 logger 上"""
    logger = logging.getLogger(f"moyurobot.test.{name}")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    for f in filters:
        logger.addFilter(f)
    handler = RecordingHandler()
    logger.addHandler(handler)
    return logger, handler


def spam(logger, message, times, level=logging.WARNING):
    for _ in range(times):
        logger.log(level, message)


def test_dedup_single_message():
    clock = FakeClock()
    original = logging_setup.time
    logging_setup.time = clock
    try:
        f = logging_setup.DedupBackoffFilter(window_s=1.0, max_window_s=300.0)
        logger, handler = make_logger("dedup", [f])

        # 同一瞬间涌入 300 条同一句警告（模拟摄像头采集线程刷屏）
        spam(logger, "OpenCVCamera(/dev/video0) read failed", 300)
        check("窗口内同一消息只放行 1 条", len(handler.records) == 1,
              f"实际放行 {len(handler.records)} 条 / 输入 300 条")

        # 推进到退避窗口之后，应该出现带"已抑制"标注的重报
        clock.advance(1.5)
        spam(logger, "OpenCVCamera(/dev/video0) read failed", 1)
        annotated = len(handler.records) == 2 and "已抑制 299 条" in handler.records[-1].getMessage()
        check("退避到期后重报并标注被抑制的条数", annotated,
              handler.records[-1].getMessage()[-40:] if len(handler.records) > 1 else "无重报")
    finally:
        logging_setup.time = original


def test_backoff_is_logarithmic():
    """持续刷屏 10 分钟，放行条数应该是个位数"""
    clock = FakeClock()
    original = logging_setup.time
    logging_setup.time = clock
    try:
        f = logging_setup.DedupBackoffFilter(window_s=1.0, max_window_s=300.0)
        logger, handler = make_logger("backoff", [f])

        for _ in range(30 * 600):  # 30fps × 600s
            spam(logger, "read failed /dev/video0", 1)
            clock.advance(1.0 / 30)

        allowed = len(handler.records)
        check("30fps 刷 10 分钟，放行条数 < 15", allowed < 15,
              f"共 18000 条输入 -> 放行 {allowed} 条，抑制 {f.stats()['suppressed']} 条")
    finally:
        logging_setup.time = original


def test_different_messages_are_independent():
    clock = FakeClock()
    original = logging_setup.time
    logging_setup.time = clock
    try:
        f = logging_setup.DedupBackoffFilter(window_s=1.0, max_window_s=300.0)
        logger, handler = make_logger("independent", [f])

        for i in range(50):
            spam(logger, "read failed /dev/video0", 1)
            spam(logger, "read failed /dev/video2", 1)
            clock.advance(0.01)

        check("不同设备/不同消息各自计数", len(handler.records) == 2,
              f"video0 + video2 各 50 条 -> 放行 {len(handler.records)} 条")
    finally:
        logging_setup.time = original


def test_reset_after_long_silence():
    clock = FakeClock()
    original = logging_setup.time
    logging_setup.time = clock
    try:
        f = logging_setup.DedupBackoffFilter(window_s=1.0, max_window_s=60.0)
        logger, handler = make_logger("silence", [f])
        msg = "camera gone"

        spam(logger, msg, 1)                    # 第 1 条放行
        clock.advance(2.0)
        spam(logger, msg, 1)                    # 退避窗口到期，第 2 条放行（间隔翻倍到 2s）
        after_second = len(handler.records)

        clock.advance(1.0)
        spam(logger, msg, 1)                    # 间隔未到，被抑制
        quiet_ok = len(handler.records) == after_second

        clock.advance(60 * 2 + 10)              # 静默超过 max_window_s * 2 → 视为新一轮
        spam(logger, msg, 1)                    # 放行
        spam(logger, msg, 1)                    # 新一轮窗口内，仍被抑制
        check("长时间静默后退避间隔重置为新窗口",
              after_second == 2 and quiet_ok and len(handler.records) == 3,
              f"前 2 条放行、间隔内被抑制、静默后共 {len(handler.records)} 条")
    finally:
        logging_setup.time = original


def test_rate_limit():
    clock = FakeClock()
    original = logging_setup.time
    logging_setup.time = clock
    try:
        f = logging_setup.RateLimitFilter(limit_per_s=5)
        logger, handler = make_logger("ratelimit", [f])

        # 每条都不同 —— 去重拦不住，靠限速兜
        for i in range(100):
            spam(logger, f"frame {i} stats", 1)
            clock.advance(0.002)

        allowed = len(handler.records)
        check("限速：一秒内最多放行 5 条", allowed == 5, f"放行 {allowed} 条 / 100 条")

        clock.advance(1.0)
        spam(logger, "next second", 1)
        check("限速丢弃数会标注在下一条上",
              "被限速丢弃" in handler.records[-1].getMessage(),
              handler.records[-1].getMessage())
    finally:
        logging_setup.time = original


def test_filters_can_be_disabled():
    f = logging_setup.DedupBackoffFilter(window_s=0)
    r = logging_setup.RateLimitFilter(limit_per_s=0)
    logger, handler = make_logger("disabled", [f, r])
    spam(logger, "same message", 200)
    check("开关置 0 时全部放行（不改变旧行为）", len(handler.records) == 200,
          f"放行 {len(handler.records)} 条")


def test_noisy_logger_level():
    old = os.environ.get("LOG_NOISY_LEVEL")
    os.environ["LOG_NOISY_LEVEL"] = "ERROR"
    try:
        logging_setup.setup_logging(force=True, log_to_file=False)
        level = logging.getLogger("lerobot.cameras.opencv.camera_opencv").level
        check("LOG_NOISY_LEVEL 生效（可静音摄像头刷屏）", level == logging.ERROR,
              f"lerobot 摄像头 logger level={logging.getLevelName(level)}")
    finally:
        if old is None:
            os.environ.pop("LOG_NOISY_LEVEL", None)
        else:
            os.environ["LOG_NOISY_LEVEL"] = old


def test_setup_logging_idempotent():
    logging_setup.setup_logging(force=True, log_to_file=False)
    count_once = len(logging.getLogger().handlers)
    logging_setup.setup_logging()  # 重复调用不应叠加 handler
    count_twice = len(logging.getLogger().handlers)
    check("setup_logging 幂等，不会重复叠加 handler", count_once == count_twice,
          f"{count_once} -> {count_twice}")


def test_rotating_file_handler_caps_disk():
    tmpdir = tempfile.mkdtemp(prefix="moyu-logtest-")
    old_env = {k: os.environ.get(k) for k in
               ("LOG_TO_FILE", "LOG_DIR", "LOG_FILE", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT",
                "LOG_RATE_LIMIT_PER_S")}
    real_stderr = sys.stderr
    try:
        os.environ.update({
            "LOG_TO_FILE": "1",
            "LOG_DIR": tmpdir,
            "LOG_FILE": "cap.log",
            "LOG_MAX_BYTES": str(64 * 1024),
            "LOG_BACKUP_COUNT": "2",
            # 关掉限速，让 1000 条测试日志全部落盘，真正触发滚动
            "LOG_RATE_LIMIT_PER_S": "0",
        })
        # 先把 stderr 换成内存缓冲，免得这 1000 条测试日志刷满终端
        sys.stderr = io.StringIO()
        logging_setup.setup_logging(force=True)

        probe = logging.getLogger("moyurobot.test.rotation")
        payload = "x" * 500
        for i in range(1000):  # 每条都不一样，绕过去重，确保真的写满
            probe.warning("payload %s %s", i, payload)

        for handler in logging.getLogger().handlers:
            handler.flush()

        files = sorted(f for f in os.listdir(tmpdir) if f.startswith("cap.log"))
        sizes = [os.path.getsize(os.path.join(tmpdir, f)) for f in files]
        limit = (2 + 1) * 64 * 1024  # maxBytes × (backupCount + 1)
        ok = sum(sizes) <= limit and 1 < len(files) <= 3
        check("日志文件按大小滚动且有硬上限", ok,
              f"{len(files)} 个文件 共 {sum(sizes) / 1024:.0f}KB（上限 {limit / 1024:.0f}KB）")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.stderr = real_stderr
        logging_setup.setup_logging(force=True, log_to_file=False)
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    print("=== 日志治理测试 ===")
    test_dedup_single_message()
    test_backoff_is_logarithmic()
    test_different_messages_are_independent()
    test_reset_after_long_silence()
    test_rate_limit()
    test_filters_can_be_disabled()
    test_noisy_logger_level()
    test_setup_logging_idempotent()
    test_rotating_file_handler_caps_disk()

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
