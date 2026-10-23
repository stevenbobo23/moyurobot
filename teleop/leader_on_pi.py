#!/usr/bin/env python
"""主臂 -> 从臂 遥操作（主臂直接插在树莓派上，复用现有服务，不写协议栈）

这是三条路里最省事的那条：主臂和从臂都挂在 Pi 的 USB 上，脚本在 Pi 本地
读主臂、走 127.0.0.1 打现有 /control，控制链完全复用 robot_service 里那个
30Hz 循环，一行 Pi 端业务代码都不用改。

用法（在 Pi 上，和 web 服务同一个 venv）：
    # 先看清楚主臂是哪个口，别和从臂搞混
    ls -l /dev/serial/by-id/

    python teleop/leader_on_pi.py \\
        --port /dev/serial/by-id/usb-1a86_USB_Single_Serial_XXXX-if00 \\
        --id leader_arm

    # 首次：主臂还没校准
    python teleop/leader_on_pi.py --port ... --id leader_arm --calibrate

    # 慢速验方向（脚本会先把 /control 打到 5Hz）
    python teleop/leader_on_pi.py --port ... --hz 5

前置条件：
 1. web 服务已在 8080 跑着（scripts/start_all.sh）
 2. 服务端 config/default.json 里 "arm_servo_speed" 改成 1.0 并重启
    —— 默认 0.2 是给网页滑块用的，遥操作会明显跟不上（详见下方说明）
 3. 主臂校准用独立 id，和从臂的 my_awesome_kiwi 分开，互不覆盖

已知冲突：
 - 打招呼（greeting）在 respect_arm_busy 里只检查 ActionPlayer，不检查遥操作。
   遥操作期间它如果触发"点头"，会通过 ActionPlayer 直接写机械臂，和你的手抢
   控制权。正式用之前把 greeting 关掉，或者等它改成也看 last_command_time。
"""

from __future__ import annotations

import argparse
import sys
import time

import requests
from lerobot.teleoperators.so_leader import SOLeader, SOLeaderTeleopConfig


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SO 主臂 -> 从臂（Pi 本地，走现有 /control）")
    p.add_argument("--port", required=True, help="主臂串口，建议用 /dev/serial/by-id/... 防插拔换号")
    p.add_argument("--id", default="leader_arm", help="主臂校准 id（和从臂的 id 分开）")
    p.add_argument("--host", default="http://127.0.0.1:8080", help="本地 web 服务地址")
    p.add_argument("--hz", type=float, default=30.0, help="发送频率，先 5 再往上")
    p.add_argument("--calibrate", action="store_true", help="首次：走交互式校准")
    p.add_argument("--no-startmove", action="store_true", help="不自动打 /startmove（网页已经点过就加这个）")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    sess = requests.Session()

    # 先探一下服务，省得连了主臂才发现后端没起
    try:
        st = sess.get(f"{args.host}/status", timeout=2).json()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"连不上 {args.host}，web 服务起了吗？原始错误: {e}")

    if not st.get("connected"):
        print("警告：服务报告机器人未连接，机械臂指令会全部被拒（检查 USB / 重启服务）")

    if not args.no_startmove and not st.get("movement_enabled"):
        # /control 前面有 _movement_enabled 门禁，不打开会一直回「仅监控模式」
        r = sess.post(f"{args.host}/startmove", timeout=2).json()
        print(f"已启用运动控制: {r.get('message', r)}")

    print(f"连接主臂 {args.port} ...")
    arm = SOLeader(SOLeaderTeleopConfig(port=args.port, id=args.id))
    arm.connect(calibrate=args.calibrate)
    print(f"主臂就绪（校准 id={arm.id}），按 Ctrl-C 结束")

    period = 1.0 / args.hz
    frames = errors = 0
    t_report = t_rtt = time.perf_counter()
    rtt_sum = 0.0

    try:
        while True:
            loop_start = time.perf_counter()

            # 主臂给的是裸关节名，Pi 上马达带 arm_ 前缀，必须补
            action = {f"arm_{k}": float(v) for k, v in arm.get_action().items()}

            t0 = time.perf_counter()
            try:
                # 短超时：宁可丢这一帧，也不让网络抖动把整个循环拖住
                resp = sess.post(f"{args.host}/control", json=action, timeout=(0.05, 0.15)).json()
                if not resp.get("success"):
                    errors += 1
                    if errors % 30 == 1:
                        print(f"  服务端拒绝: {resp.get('message')}", flush=True)
                else:
                    rtt_sum += time.perf_counter() - t0
            except requests.RequestException:
                errors += 1

            frames += 1
            now = time.perf_counter()
            if now - t_report >= 2.0:
                hz = frames / (now - t_report)
                ok = frames - errors
                rtt = (rtt_sum / ok * 1000) if ok else 0.0
                print(
                    f"  {hz:5.1f} Hz | 本地往返 {rtt:4.1f} ms | 失败 {errors}",
                    flush=True,
                )
                frames = errors = 0
                rtt_sum = 0.0
                t_report = now

            time.sleep(max(period - (time.perf_counter() - loop_start), 0.0))

    except KeyboardInterrupt:
        print("\n结束遥操作")
    finally:
        # 不要发 0 位置——那会把臂甩到零位。松开手就让从臂停在当前姿态。
        try:
            arm.disconnect()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
