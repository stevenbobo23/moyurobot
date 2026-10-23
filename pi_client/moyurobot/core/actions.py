#!/usr/bin/env python
"""
预设动作编排模块

从 config/actions.json 加载动作定义，在后台线程中按序执行。
支持热加载、中途急停、关节限位、平滑插值。

被 web/controller.py 和 mcp/server.py 共同复用。
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 关节短名 -> (完整键名, 最小限位, 最大限位, 中文描述)
JOINT_MAPPING: Dict[str, Dict[str, Any]] = {
    "shoulder_pan": {"key": "arm_shoulder_pan.pos", "min": -85, "max": 85, "desc": "肩部水平"},
    "shoulder_lift": {"key": "arm_shoulder_lift.pos", "min": -100, "max": 100, "desc": "肩部垂直"},
    "elbow_flex": {"key": "arm_elbow_flex.pos", "min": -100, "max": 100, "desc": "肘关节"},
    "wrist_flex": {"key": "arm_wrist_flex.pos", "min": -85, "max": 85, "desc": "腕关节弯曲"},
    "wrist_roll": {"key": "arm_wrist_roll.pos", "min": -70, "max": 70, "desc": "腕关节旋转"},
    "gripper": {"key": "arm_gripper.pos", "min": -10, "max": 90, "desc": "夹爪"},
}

ARM_HOME_POSITIONS: Dict[str, float] = {
    "arm_shoulder_pan.pos": 0,
    "arm_shoulder_lift.pos": 0,
    "arm_elbow_flex.pos": 0,
    "arm_wrist_flex.pos": 0,
    "arm_wrist_roll.pos": 0,
    "arm_gripper.pos": 0,
}

# 插值步进间隔（秒）。控制循环 30Hz，这里 20Hz 足够平滑
_INTERP_INTERVAL = 0.05
# 播放期间临时提升舵机速度（与 MCP _smooth_arm_motion 行为一致）
_PLAYBACK_SPEED_RATIO = 0.8


def _default_actions_path() -> Path:
    """定位 config/actions.json"""
    here = Path(__file__).resolve()
    # moyurobot/core/actions.py -> pi_client/config/actions.json
    candidate = here.parent.parent.parent / "config" / "actions.json"
    if candidate.exists():
        return candidate
    return here.parent.parent / "actions.json"


class ActionPlayer:
    """预设动作播放器

    单实例全局复用。play() 在后台线程执行动作序列，
    stop() 可随时中断，动作结束后自动恢复舵机默认速度。
    """

    def __init__(self, service, actions_path: Optional[Path] = None):
        self.service = service
        self.actions_path = Path(actions_path) if actions_path else _default_actions_path()

        self._actions: Dict[str, Dict[str, Any]] = {}
        self._actions_mtime: float = 0.0
        self._load_lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()

        # 播放状态（供前端/AI 查询）
        self._state: Dict[str, Any] = {
            "playing": False,
            "action_id": None,
            "action_name": None,
            "step_index": 0,
            "total_steps": 0,
            "started_at": None,
        }

    # ---------- 动作定义加载 ----------

    def _load_actions_locked(self) -> None:
        """读取 actions.json（调用方需持有 _load_lock）"""
        try:
            mtime = self.actions_path.stat().st_mtime
            if mtime == self._actions_mtime and self._actions:
                return  # 未变化
            with open(self.actions_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            actions = data.get("actions", {})
            if not isinstance(actions, dict) or not actions:
                logger.warning(f"{self.actions_path} 中没有定义任何动作")
                return
            self._actions = actions
            self._actions_mtime = mtime
            logger.info(f"已加载 {len(actions)} 个预设动作: {', '.join(actions.keys())}")
        except FileNotFoundError:
            logger.error(f"动作定义文件不存在: {self.actions_path}")
        except json.JSONDecodeError as e:
            logger.error(f"动作定义 JSON 解析失败: {e}")
        except Exception as e:
            logger.error(f"加载动作定义失败: {e}")

    def list_actions(self) -> List[Dict[str, Any]]:
        """列出所有可用动作（自动热加载）"""
        with self._load_lock:
            self._load_actions_locked()
        result = []
        for action_id, action in self._actions.items():
            result.append({
                "id": action_id,
                "name": action.get("name", action_id),
                "icon": action.get("icon", "🎬"),
                "description": action.get("description", ""),
                "steps": len(action.get("steps", [])),
            })
        return result

    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        with self._load_lock:
            self._load_actions_locked()
        return self._actions.get(action_id)

    # ---------- 状态查询 ----------

    def get_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    # ---------- 播放控制 ----------

    def play(self, action_id: str) -> Dict[str, Any]:
        """播放指定动作。若已有动作在播放，先停止再切换。"""
        action = self.get_action(action_id)
        if action is None:
            return {"success": False, "message": f"未找到动作: {action_id}"}

        if not self.service.is_connected():
            return {"success": False, "message": "机器人未连接"}

        # 若正在播放，先优雅停止（arm 停在当前位置）
        if self._thread is not None and self._thread.is_alive():
            self.stop(wait=True)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._play_worker,
            args=(action_id, action),
            daemon=True,
            name=f"action-{action_id}",
        )
        self._thread.start()
        return {
            "success": True,
            "message": f"开始执行动作: {action.get('name', action_id)}",
            "action_id": action_id,
            "total_steps": len(action.get("steps", [])),
        }

    def stop(self, wait: bool = False) -> Dict[str, Any]:
        """急停：中断当前动作，机械臂保持在当前位置"""
        was_playing = self._state.get("playing")
        self._stop_event.set()
        if wait and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        return {
            "success": True,
            "message": "已停止动作" if was_playing else "当前无动作在播放",
        }

    # ---------- 内部实现 ----------

    def _set_state(self, **kwargs) -> None:
        with self._state_lock:
            self._state.update(kwargs)

    def _boost_servo_speed(self, ratio: float) -> None:
        """播放期间临时提升舵机速度（照搬 MCP _smooth_arm_motion 的做法）"""
        if not self.service.is_connected():
            return
        max_speed = 2400
        goal_speed = int(max_speed * ratio)
        acceleration = max(10, int(100 * ratio))
        try:
            arm_motors = [m for m in self.service.robot.bus.motors if m.startswith("arm")]
            for motor in arm_motors:
                try:
                    self.service.robot.bus.write("Goal_Acc", motor, acceleration)
                    self.service.robot.bus.write("Goal_Speed", motor, goal_speed)
                except Exception as e:
                    logger.warning(f"设置舵机 {motor} 临时速度失败: {e}")
        except Exception as e:
            logger.warning(f"提升舵机速度失败: {e}")

    def _restore_servo_speed(self) -> None:
        """恢复默认舵机速度配置"""
        try:
            self.service._configure_arm_servo_speed(self.service.config.arm_servo_speed)
            # 与 set_arm_position 的缓存标记保持一致，避免下次重复配置
            self.service._arm_speed_configured = self.service.config.arm_servo_speed
        except Exception as e:
            logger.warning(f"恢复舵机速度失败: {e}")

    def _read_current_positions(self, keys: List[str]) -> Dict[str, float]:
        """从服务状态读取当前目标位置（不直接碰硬件总线）"""
        status = self.service.get_status()
        action_state = status.get("current_action", {})
        return {k: float(action_state.get(k, 0.0)) for k in keys}

    def _clamped_arm_positions(self, positions: Dict[str, float]) -> Dict[str, float]:
        """关节短名 -> 完整键名，并施加限位"""
        result = {}
        for joint, value in positions.items():
            info = JOINT_MAPPING.get(joint)
            if info is None:
                logger.warning(f"未知关节名: {joint}，忽略")
                continue
            clamped = max(info["min"], min(info["max"], float(value)))
            if clamped != value:
                logger.warning(f"关节 {info['desc']}({joint}) 目标 {value}° 超限，已截断到 {clamped}°")
            result[info["key"]] = clamped
        return result

    def _interpolated_move(self, target_positions: Dict[str, float], duration: float) -> bool:
        """线性插值移动到目标位置。返回 False 表示被急停中断。"""
        keys = list(target_positions.keys())
        start = self._read_current_positions(keys)

        # 无需移动则直接返回
        if all(abs(target_positions[k] - start[k]) < 0.5 for k in keys):
            return True

        steps = max(1, int(duration / _INTERP_INTERVAL))
        t0 = time.time()
        for i in range(1, steps + 1):
            if self._stop_event.is_set():
                return False
            ratio = min(1.0, (time.time() - t0) / duration) if duration > 0 else 1.0
            interp = {k: start[k] + (target_positions[k] - start[k]) * ratio for k in keys}
            result = self.service.set_arm_position(interp)
            if not result.get("success"):
                logger.error(f"插值下发失败: {result.get('message')}")
                return False
            # 按固定节奏推进
            target_t = t0 + duration * (i / steps)
            sleep_t = target_t - time.time()
            if sleep_t > 0:
                time.sleep(sleep_t)
        return not self._stop_event.is_set()

    def _sleep_interruptible(self, duration: float) -> bool:
        """可中断等待。返回 False 表示被急停。"""
        end = time.time() + duration
        while time.time() < end:
            if self._stop_event.is_set():
                return False
            time.sleep(min(0.05, max(0.0, end - time.time())))
        return not self._stop_event.is_set()

    def _execute_step(self, step: Dict[str, Any]) -> bool:
        """执行单个步骤。返回 False 表示被中断或失败。"""
        step_type = step.get("type")
        duration = float(step.get("duration", 0.5))

        if step_type == "arm":
            targets = self._clamped_arm_positions(step.get("positions", {}))
            if not targets:
                logger.warning("arm 步骤没有有效关节目标，跳过")
                return True
            return self._interpolated_move(targets, duration)

        elif step_type == "gripper":
            command = step.get("command")
            if command == "open":
                value = float(step.get("value", 55))
            elif command == "close":
                value = float(step.get("value", 0))
            else:
                value = float(step.get("value", 30))
            info = JOINT_MAPPING["gripper"]
            value = max(info["min"], min(info["max"], value))
            return self._interpolated_move({info["key"]: value}, duration)

        elif step_type == "move":
            command = step.get("command", "stop")
            result = self.service.move_robot_for_duration(command, duration)
            if not result.get("success"):
                logger.error(f"底盘移动失败: {result.get('message')}")
                return False
            return self._sleep_interruptible(duration)

        elif step_type == "wait":
            return self._sleep_interruptible(duration)

        elif step_type == "reset":
            return self._interpolated_move(dict(ARM_HOME_POSITIONS), duration)

        else:
            logger.warning(f"未知步骤类型: {step_type}，跳过")
            return True

    def _play_worker(self, action_id: str, action: Dict[str, Any]) -> None:
        """后台播放线程"""
        steps: List[Dict[str, Any]] = action.get("steps", [])
        self._set_state(
            playing=True,
            action_id=action_id,
            action_name=action.get("name", action_id),
            step_index=0,
            total_steps=len(steps),
            started_at=time.time(),
        )
        logger.info(f"▶ 开始播放动作 [{action_id}] {action.get('name', '')}，共 {len(steps)} 步")

        self._boost_servo_speed(_PLAYBACK_SPEED_RATIO)

        completed = 0
        interrupted = False
        try:
            for i, step in enumerate(steps):
                if self._stop_event.is_set():
                    interrupted = True
                    break
                self._set_state(step_index=i + 1)
                if not self._execute_step(step):
                    interrupted = True
                    break
                completed = i + 1
        except Exception as e:
            logger.error(f"动作播放异常: {e}")
            interrupted = True
        finally:
            self._restore_servo_speed()
            self._set_state(
                playing=False,
                step_index=completed,
            )
            if interrupted:
                logger.info(f"⏹ 动作 [{action_id}] 已中断（完成 {completed}/{len(steps)} 步），机械臂保持在当前位置")
            else:
                logger.info(f"✓ 动作 [{action_id}] 播放完成（{len(steps)} 步）")


# ---------- 全局实例管理 ----------

_player_instance: Optional[ActionPlayer] = None
_player_lock = threading.Lock()


def get_action_player(service) -> Optional[ActionPlayer]:
    """获取（或懒创建）全局 ActionPlayer 实例"""
    global _player_instance
    with _player_lock:
        if _player_instance is None:
            if service is None:
                return None
            _player_instance = ActionPlayer(service)
        return _player_instance
