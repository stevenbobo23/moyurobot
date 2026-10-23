#!/usr/bin/env python3
"""把从臂夹爪的闭合点再往下挪一点（在树莓派上跑）

问题：主臂和从臂都显示 0，从臂夹爪还是留一条缝。
原因：lerobot 的 0-100 归一化里，0 对应的是**该舵机自己校准的 range_min**：

    motors_bus.py: _unnormalize
        bounded_val = min(100.0, max(0.0, val))        # 值被夹在 0~100
        raw = int(bounded_val / 100 * (max_ - min_) + min_)

所以发 0 只会走到 range_min，**软件再往下压是压不动的**。如果校准时夹爪没
压到底，range_min 就停在"快闭合"的位置，这个缝是校准进来的。

两种解法：
  A（推荐）重跑 lerobot-calibrate，扫夹爪那一步把两片颚板压到贴死
  B（本脚本）直接把 Min_Position_Limit 往下挪 N 个 tick，并同步校准 json

用法：
    # 必须先停掉网页服务，串口是独占的
    sudo systemctl stop <你的服务>      # 或 Ctrl+C 掉 start_web.sh

    python3 tools/fix_follower_gripper.py --dry-run          # 只看当前值
    python3 tools/fix_follower_gripper.py --delta -60        # 闭合方向挪 60 tick
    python3 tools/fix_follower_gripper.py --delta -60 --apply

改完把夹爪拖到 0 看是否闭合，还差就再挪一点。一次别超过 100 tick，
顶到机械死点会让电机堵转发热。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

MOTOR_NAME = "arm_gripper"
MOTOR_ID = 6
MODEL = "sts3215"

# 至少保留这么多 tick 的行程，避免把 range 压成 0（lerobot 会直接报错）
MIN_RANGE_TICKS = 200


def calibration_json(robot_id: str) -> Path:
    from lerobot.utils.constants import HF_LEROBOT_CALIBRATION

    return HF_LEROBOT_CALIBRATION / "robots" / "lekiwi" / f"{robot_id}.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0", help="从臂串口（默认 /dev/ttyACM0）")
    ap.add_argument("--robot-id", default="my_awesome_kiwi", help="校准文件名用的 robot id")
    ap.add_argument("--delta", type=int, default=-60, help="挪多少 tick，负数是往闭合方向（默认 -60）")
    ap.add_argument("--apply", action="store_true", help="真正写入；不加只读不动")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写（等同于不加 --apply）")
    args = ap.parse_args()

    try:
        from lerobot.motors import Motor, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus

        bus = FeetechMotorsBus(
            port=args.port,
            motors={MOTOR_NAME: Motor(MOTOR_ID, MODEL, MotorNormMode.RANGE_0_100)},
        )
    except ImportError as e:
        # feetech-servo-sdk 是在 FeetechMotorsBus 构造时才 require 的，得一起兜住
        sys.exit(
            f"缺少 lerobot 的舵机依赖：{e}\n"
            "在树莓派上装：pip install 'lerobot[feetech]'（或 uv pip install 'lerobot[feetech]'）"
        )

    print(f"连接 {args.port} …")
    try:
        bus.connect()
    except Exception as e:  # noqa: BLE001
        sys.exit(
            f"连接失败：{e}\n"
            "常见原因：网页服务还占着串口（先停掉），或者端口不是 "
            f"{args.port}（ls /dev/ttyACM* 看一眼）"
        )

    try:
        cur_min = bus.read("Min_Position_Limit", MOTOR_NAME, normalize=False)
        cur_max = bus.read("Max_Position_Limit", MOTOR_NAME, normalize=False)
        cur_pos = bus.read("Present_Position", MOTOR_NAME, normalize=False)
        print(f"当前：min={cur_min}  max={cur_max}  range={cur_max - cur_min}  present={cur_pos}")

        new_min = cur_min + args.delta
        if new_min < 0:
            sys.exit(f"新 min={new_min} 越界（不能小于 0），把 --delta 调小一点")
        if cur_max - new_min < MIN_RANGE_TICKS:
            sys.exit(
                f"新行程只剩 {cur_max - new_min} tick（下限 {MIN_RANGE_TICKS}），"
                "说明夹爪行程本来就窄，别再往下压了，重跑校准更合适"
            )

        print(f"目标：min {cur_min} → {new_min}（行程 {cur_max - cur_min} → {cur_max - new_min}）")

        if not args.apply:
            print("\n只读模式，没有写入。确认后加 --apply")
            return

        # 1) 先备份校准 json，再改
        cj = calibration_json(args.robot_id)
        if cj.exists():
            bak = cj.with_suffix(f".json.bak-{datetime.now():%Y%m%d-%H%M%S}")
            shutil.copy2(cj, bak)
            print(f"已备份校准文件：{bak}")
            data = json.loads(cj.read_text())
            entry = data.get(MOTOR_NAME)
            if entry and "range_min" in entry:
                print(f"校准文件里的 range_min：{entry['range_min']} → {new_min}")
                entry["range_min"] = int(new_min)
                cj.write_text(json.dumps(data, indent=4))
            else:
                print(f"⚠ 校准文件里没有 {MOTOR_NAME}.range_min，跳过同步：{cj}")
        else:
            print(f"⚠ 没找到校准文件（{cj}），只改寄存器不动 json")

        # 2) 写寄存器
        bus.write("Min_Position_Limit", MOTOR_NAME, int(new_min), normalize=False)
        check = bus.read("Min_Position_Limit", MOTOR_NAME, normalize=False)
        print(f"写入完成，回读 min={check}")
        if check != new_min:
            sys.exit("回读不一致，写入没生效，先别动夹爪")

        print(
            "\n下一步：\n"
            "  1. 重启网页服务，把「夹爪」滑块直接拖到 0（绕过主臂）\n"
            "  2. 看是否闭死。还有缝就再跑一次 --delta -40\n"
            "  3. 闭合时听声音：一直嗡嗡响说明顶死了，往回退一点"
        )
    finally:
        try:
            bus.disconnect()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
