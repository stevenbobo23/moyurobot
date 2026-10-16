#!/usr/bin/env python
"""
音频对讲端到端测试（模拟后端，无需任何声卡）

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_audio_pipeline.py

覆盖：
1. 音频能力探测 / 配置加载（含 config/default.json 的 audio 段）
2. WebSocket 握手与 hello 协议
3. 监听通道：车端 PCM 下发（帧长、正弦波电平）
4. 对讲通道：浏览器 PCM 上行到"扬声器"、字节数统计
5. 对讲互斥：第二个人抢麦被拒
6. 半双工：有人说话时其他人收不到车端声音
7. 结束对讲后监听恢复
"""

import asyncio
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from moyurobot.core.audio import (  # noqa: E402
    AudioConfig,
    IntercomHub,
    list_capture_devices,
    list_playback_devices,
    rms_level,
)
from moyurobot.web.audio_ws import AudioWebSocketServer  # noqa: E402

import websockets  # noqa: E402

PORT = int(os.environ.get("AUDIO_TEST_PORT", "18081"))
URL = f"ws://127.0.0.1:{PORT}/audio"

RESULTS = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, bool(ok), detail))
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


async def collect(ws, seconds: float):
    """在 seconds 内收集所有消息，返回 (文本消息列表, 二进制帧列表)"""
    texts, binaries = [], []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        except Exception as e:  # 连接关闭
            texts.append(json.dumps({"type": "_closed", "message": str(e)}))
            break
        if isinstance(msg, (bytes, bytearray)):
            binaries.append(bytes(msg))
        else:
            texts.append(msg)
    return texts, binaries


def parse(texts):
    out = []
    for t in texts:
        try:
            out.append(json.loads(t))
        except Exception:
            pass
    return out


async def main():
    print("=" * 68)
    print("摸鱼遥控车 · 语音对讲端到端测试（模拟后端）")
    print("=" * 68)

    # ---------------- 1. 配置与能力 ----------------
    cfg_file = AudioConfig.load()
    check("配置加载 config/default.json → audio 段", cfg_file.sample_rate == 16000,
          f"rate={cfg_file.sample_rate} chunk={cfg_file.chunk_ms}ms port={cfg_file.ws_port}")

    cfg = AudioConfig(
        backend="sim",
        sample_rate=16000,
        channels=1,
        chunk_ms=100,
        ws_port=PORT,
        ws_host="127.0.0.1",
        sim_realtime=True,
        require_auth=False,
        capture_idle_close_s=0.5,
    )
    check("包长换算 chunk_bytes = rate*ms*2", cfg.chunk_bytes == 3200, f"{cfg.chunk_bytes} bytes")

    cap_devices = list_capture_devices()
    play_devices = list_playback_devices()
    check("设备枚举可用（至少返回默认项）",
          len(cap_devices) >= 1 and len(play_devices) >= 1,
          f"麦克风 {len(cap_devices)} 项 / 扬声器 {len(play_devices)} 项")

    hub = IntercomHub(cfg)
    caps = hub.capabilities()
    check("能力探测标记为模拟后端", caps["simulated"] and caps["backend"] == "sim",
          f"backend={caps['backend']}")

    hub.start()
    server = AudioWebSocketServer(hub=hub, config=cfg, host="127.0.0.1", port=PORT)
    started = server.start()
    check("WebSocket 音频服务启动", started, server.start_error or f"port={PORT}")
    if not started:
        return summarise()

    try:
        # ---------------- 2. 握手 ----------------
        async with websockets.connect(URL) as client_a:
            hello_raw = await asyncio.wait_for(client_a.recv(), timeout=5)
            hello = json.loads(hello_raw)
            check("收到 hello 并带上采样率/包长",
                  hello.get("type") == "hello" and hello.get("sample_rate") == 16000,
                  f"backend={hello.get('backend')} chunk_ms={hello.get('chunk_ms')}")
            check("握手带出可切换设备列表",
                  isinstance(hello.get("options", {}).get("capture_devices"), list),
                  f"{len(hello.get('options', {}).get('capture_devices', []))} 项")

            # ---------------- 3. 监听通道 ----------------
            await client_a.send(json.dumps({"type": "monitor", "on": True}))
            texts, binaries = await collect(client_a, 0.6)
            msgs = parse(texts)
            ack = [m for m in msgs if m.get("type") == "monitor"]
            check("监听开启被确认", bool(ack) and ack[0].get("on") is True)
            check("收到车端 PCM 帧", len(binaries) >= 2, f"{len(binaries)} 帧 / 0.6s")
            check("帧长与协议一致",
                  all(len(b) == cfg.chunk_bytes for b in binaries) if binaries else False,
                  f"期望 {cfg.chunk_bytes} 字节")

            if binaries:
                level = rms_level(binaries[0])
                check("模拟音源电平合理（440Hz 正弦 ≈0.18）", 0.08 < level < 0.35, f"rms={level:.3f}")

            state_msgs = [m for m in msgs if m.get("type") == "state"]
            check("服务端定期广播状态（含电平）",
                  bool(state_msgs) and "level" in state_msgs[-1],
                  f"{len(state_msgs)} 条状态消息")
            check("监听人数统计正确", hub.listener_count == 1, f"listeners={hub.listener_count}")

            # ---------------- 4. 对讲互斥 ----------------
            async with websockets.connect(URL) as client_b:
                await asyncio.wait_for(client_b.recv(), timeout=5)  # hello
                await client_b.send(json.dumps({"type": "monitor", "on": True}))
                await collect(client_b, 0.3)

                await client_a.send(json.dumps({"type": "ptt", "on": True}))
                texts, _ = await collect(client_a, 0.4)
                ptt_msgs = [m for m in parse(texts) if m.get("type") == "ptt"]
                check("A 抢到对讲权", bool(ptt_msgs) and ptt_msgs[0].get("on") is True)
                check("对讲时占用扬声器（writer 已创建）", hub._writer is not None)

                await client_b.send(json.dumps({"type": "ptt", "on": True}))
                texts_b, _ = await collect(client_b, 0.5)
                errs = [m for m in parse(texts_b) if m.get("type") == "error"]
                check("B 抢麦被拒绝（互斥生效）",
                      bool(errs) and "说话" in errs[0].get("message", ""),
                      errs[0].get("message", "")[:40] if errs else "无错误消息")
                check("B 未拿到对讲权", hub.state()["ptt"] and hub.state()["ptt_owner"] is not None)

                # ---------------- 5. 半双工 ----------------
                _, heard_by_b = await collect(client_b, 0.5)
                check("A 说话期间 B 收不到车端声音（半双工）", len(heard_by_b) == 0,
                      f"{len(heard_by_b)} 帧")

                # ---------------- 6. 上行播放 ----------------
                payload = (b"\x00\x10" * (cfg.chunk_bytes // 2))  # 恒定 4096 的方波
                sent_frames = 4
                for _ in range(sent_frames):
                    await client_a.send(payload)
                await asyncio.sleep(0.4)
                writer = hub._writer
                written = getattr(writer, "bytes_written", 0)
                check("上行 PCM 已写入车端扬声器",
                      written == cfg.chunk_bytes * sent_frames,
                      f"{written} / {cfg.chunk_bytes * sent_frames} 字节")
                check("对讲流量有统计", hub.state()["talk_kbytes"] > 0,
                      f"talk={hub.state()['talk_kbytes']} KB")

                # ---------------- 7. 结束对讲 ----------------
                await client_a.send(json.dumps({"type": "ptt", "on": False}))
                texts, _ = await collect(client_a, 0.5)
                off = [m for m in parse(texts) if m.get("type") == "ptt"]
                check("结束对讲被确认", bool(off) and off[0].get("on") is False)
                check("对讲权已释放（等待排空后）", wait_ptt_released(hub))

                _, resumed = await collect(client_b, 0.6)
                check("对讲结束后监听恢复下发", len(resumed) >= 2, f"{len(resumed)} 帧")

            # B 已断开，A 仍在监听
            await asyncio.sleep(0.3)
            check("B 断开后监听者只剩 A", hub.listener_count == 1,
                  f"listeners={hub.listener_count}")

        # A 也断开后应完全释放
        await asyncio.sleep(0.3)
        check("全部断开后监听者归零", hub.listener_count == 0,
              f"listeners={hub.listener_count}")
        check("服务端记录到断开（客户端数归零）", server.client_count == 0,
              f"clients={server.client_count}")

    finally:
        server.stop()
        hub.shutdown()

    return summarise()


def wait_ptt_released(hub, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not hub.state()["ptt"]:
            return True
        time.sleep(0.05)
    return False


def summarise():
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("-" * 68)
    print(f"结果: {passed}/{total} 通过")
    if passed != total:
        print("失败项:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  ❌ {name} {detail}")
    print("=" * 68)
    return passed == total


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
