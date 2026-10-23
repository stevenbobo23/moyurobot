#!/usr/bin/env python
"""SO 主臂 -> LeKiwi 小车遥操作（官方 ZMQ 通道，Mac/PC 端运行）

为什么不用 `lerobot-teleoperate`：
 1. 它不会给动作补 ``arm_`` 前缀。主臂产出 ``shoulder_pan.pos``，而 Pi 上的
    马达叫 ``arm_shoulder_pan``（见 lerobot/robots/lekiwi/lekiwi.py 的 motors）。
 2. 它不会补底盘速度。``LeKiwi.send_action()`` 里是
    ``base_goal_vel["x.vel"]`` 硬下标，缺 key 直接 KeyError。
 3. ``lerobot-record`` 的 CLI 只接受单个 ``--teleop``（``cfg.teleop`` 是单个
    TeleoperatorConfig），它内部支持的双 teleop（主臂 + 键盘）列表分支只有从
    Python 调用才走得到，且必须落数据集。

所以官方通道能走，但循环得自己写——就是这个文件的全部内容。

Pi 端（先起，会独占串口，你的 Web/MCP/语音服务得停）：
    python -m lerobot.robots.lekiwi.lekiwi_host \\
        --robot.id=my_awesome_kiwi \\
        --robot.port=/dev/ttyACM0 \\
        --host.connection_time_s=86400 \\
        --robot.cameras='{}'

    --host.connection_time_s 一定要改，默认 30 秒后 host 自己退出。
    --robot.cameras='{}' 关掉回传视频（纯遥操作不需要，两个相机约 25-30 Mbps）。

本端：
    python teleop/leader_zmq.py --ip 192.168.1.23 --port /dev/tty.usbmodem5A46xxxx

对着墙/空地先 5Hz 试方向，确认没镜像再放开：
    python teleop/leader_zmq.py --ip ... --port ... --hz 5
"""

from __future__ import annotations

import argparse
import sys
import time

import zmq
from lerobot.robots.lekiwi import LeKiwiClient, LeKiwiClientConfig
from lerobot.teleoperators.keyboard import KeyboardTeleop, KeyboardTeleopConfig
from lerobot.teleoperators.so_leader import SOLeader, SOLeaderTeleopConfig

ZERO_BASE = {"x.vel": 0.0, "y.vel": 0.0, "theta.vel": 0.0}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SO leader arm -> LeKiwi teleop over ZMQ")
    p.add_argument("--ip", required=True, help="树莓派局域网 IP（别用公网域名，RTT 90ms+）")
    p.add_argument("--port", required=True, help="主臂串口，如 /dev/tty.usbmodem5A46xxxx")
    p.add_argument("--id", default="leader_arm", help="主臂校准 id（与 lerobot-calibrate 时一致）")
    p.add_argument("--hz", type=float, default=30.0, help="发送频率，先 5 再往上加")
    p.add_argument(
        "--no-keyboard",
        action="store_true",
        help="不启用键盘控底盘（底盘速度恒为 0，仍需发送，否则 Pi 端 KeyError）",
    )
    p.add_argument(
        "--calibrate",
        action="store_true",
        help="主臂首次使用：走一遍交互式校准（之后校准结果存 ~/.cache/huggingface/lerobot）",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # cameras={} 必须和 Pi 端 --robot.cameras='{}' 对齐，省掉 JPEG 回传带宽
    robot = LeKiwiClient(LeKiwiClientConfig(remote_ip=args.ip, cameras={}))
    arm = SOLeader(SOLeaderTeleopConfig(port=args.port, id=args.id))
    kb = None if args.no_keyboard else KeyboardTeleop(KeyboardTeleopConfig(id="teleop_kb"))

    print(f"连接主臂 {args.port} ...")
    # connect(calibrate=True) 在未校准时会走交互式校准流程
    arm.connect(calibrate=args.calibrate)

    if kb is not None:
        print("启用键盘控底盘（WASD 平移 / ZX 旋转 / R F 调速 / Q 退出）")
        print("  首次运行 macOS 会要求「辅助功能 / 输入监控」权限，没给的话键盘无效但不影响手臂")
        kb.connect()

    print(f"连接 Pi 端 host {args.ip}:5555/5556 ...")
    try:
        # connect() 会阻塞等第一帧观测，默认 5s 超时
        robot.connect()
    except Exception as e:  # noqa: BLE001
        sys.exit(
            f"连不上 Pi 端。先在树莓派上起 lekiwi_host，并确认 5555/5556 端口可达。\n原始错误: {e}"
        )

    period = 1.0 / args.hz
    frames = 0
    t_report = time.perf_counter()
    last_arm: dict[str, float] = {}

    print(f"开始遥操作 @ {args.hz:.0f}Hz，Ctrl-C 结束")
    try:
        while True:
            loop_start = time.perf_counter()

            # 底盘：键盘没按就是 0；keys 必须全给，Pi 端硬下标取
            base = (
                robot._from_keyboard_to_base_action(kb.get_action())  # noqa: SLF001
                if kb is not None
                else dict(ZERO_BASE)
            )

            # 手臂：主臂给的是裸关节名，Pi 上马达带 arm_ 前缀，必须补
            last_arm = {f"arm_{k}": float(v) for k, v in arm.get_action().items()}

            robot.send_action({**last_arm, **base})

            # 排空观测队列但不去解码 JPEG，避免 TCP 背压把命令通道拖慢
            try:
                while True:
                    robot.zmq_observation_socket.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                pass

            frames += 1
            now = time.perf_counter()
            if now - t_report >= 2.0:
                print(f"  {frames / (now - t_report):5.1f} Hz", flush=True)
                frames, t_report = 0, now

            time.sleep(max(period - (time.perf_counter() - loop_start), 0.0))

    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，停车")
    finally:
        # 只归零底盘，手臂保持当前姿态——发 0 位置会把臂甩到零位
        if last_arm:
            try:
                robot.send_action({**last_arm, **ZERO_BASE})
            except Exception:  # noqa: BLE001
                pass
        for dev in (arm, kb, robot):
            if dev is not None:
                try:
                    dev.disconnect()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":
    main()
