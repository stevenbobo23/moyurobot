#!/usr/bin/env python
"""
机器人服务模块

提供对 LeKiwi 机器人的统一控制接口
"""

import logging
import os
import platform
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class RobotServiceConfig:
    """机器人服务配置"""
    robot_id: str = "moyu_robot"
    # 移动速度配置 (m/s 和 deg/s)
    linear_speed: float = 0.2
    angular_speed: float = 30.0
    # 机械臂舵机速度配置 (0.0-1.0，相对于最大速度的百分比)
    arm_servo_speed: float = 0.2
    # 机械臂扭矩限制 (0-1000，默认600)
    arm_torque_limit: int = 600
    # 安全配置
    command_timeout_s: float = 6.0
    max_loop_freq_hz: int = 30
    # 摄像头配置
    front_camera_name: str = "T1 Webcam"
    wrist_camera_name: str = "USB Camera"


def find_camera_by_name(camera_name: str) -> Optional[str]:
    """根据设备名称查找摄像头设备路径
    
    通过读取 /sys/class/video4linux/ 目录下的设备信息文件来获取设备名称
    
    Args:
        camera_name: 摄像头设备名称，例如 "USB Camera" 或 "T1 Webcam"
        
    Returns:
        设备路径，例如 "/dev/video3"，如果未找到则返回 None
    """
    if platform.system() != "Linux":
        return None
    
    sys_video_path = Path("/sys/class/video4linux")
    
    if not sys_video_path.exists():
        logger.warning("/sys/class/video4linux 目录不存在")
        return None
    
    try:
        device_map = {}
        
        for video_dir in sorted(sys_video_path.glob("video*")):
            name_file = video_dir / "name"
            if not name_file.exists():
                continue
            
            try:
                with open(name_file, 'r') as f:
                    device_name = f.read().strip()
                
                if not device_name:
                    continue
                
                video_num = video_dir.name.replace("video", "")
                if not video_num.isdigit():
                    continue
                
                device_path = f"/dev/video{video_num}"
                
                if not Path(device_path).exists():
                    continue
                
                if device_name not in device_map:
                    device_map[device_name] = []
                device_map[device_name].append(device_path)
                
            except (IOError, OSError) as e:
                logger.debug(f"读取设备 {video_dir.name} 信息失败: {e}")
                continue
        
        for device_name, paths in device_map.items():
            if camera_name.lower() in device_name.lower():
                if paths:
                    device_path = paths[0]
                    logger.info(f"找到摄像头设备: {device_name} -> {device_path}")
                    return device_path
        
        available_names = list(device_map.keys())
        logger.warning(f"未找到名称为 '{camera_name}' 的摄像头设备。可用设备: {available_names}")
        return None
        
    except Exception as e:
        logger.error(f"查找摄像头设备时出错: {e}")
        return None


class RobotService:
    """机器人控制服务
    
    提供统一的机器人控制接口，可被 HTTP 控制器、MCP 服务等复用
    """
    
    def __init__(self, config: RobotServiceConfig):
        self.config = config
        self.robot = None
        self._robot_class = None
        self._robot_config_class = None
        
        # 运行状态
        self.running = False
        self.last_command_time = 0
        self.current_action = {
            "x.vel": 0.0,
            "y.vel": 0.0,
            "theta.vel": 0.0,
            "arm_shoulder_pan.pos": 0,
            "arm_shoulder_lift.pos": 0,
            "arm_elbow_flex.pos": 0,
            "arm_wrist_flex.pos": 0,
            "arm_wrist_roll.pos": 0,
            "arm_gripper.pos": 0,
        }
        self.control_thread = None
        self._lock = threading.Lock()
        
        # 延迟导入标记
        self._lerobot_imported = False
        
    def _import_lerobot(self):
        """延迟导入 lerobot 模块"""
        if self._lerobot_imported:
            return True
            
        try:
            from lerobot.robots.lekiwi.config_lekiwi import LeKiwiConfig
            from lerobot.robots.lekiwi.lekiwi import LeKiwi
            from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig
            from lerobot.cameras.configs import Cv2Rotation
            
            self._robot_class = LeKiwi
            self._robot_config_class = LeKiwiConfig
            self._camera_config_class = OpenCVCameraConfig
            self._cv2_rotation = Cv2Rotation
            self._lerobot_imported = True
            return True
            
        except ImportError as e:
            logger.error(f"无法导入 lerobot 模块: {e}")
            logger.error("请确保已安装 lerobot[lekiwi] 依赖")
            return False
    
    def _create_robot(self):
        """创建机器人实例"""
        if not self._import_lerobot():
            return None
        
        # 查找摄像头
        front_path = find_camera_by_name(self.config.front_camera_name)
        if front_path is None:
            front_path = "/dev/video0"
            logger.warning(f"未找到 '{self.config.front_camera_name}'，使用默认路径: {front_path}")
        
        wrist_path = find_camera_by_name(self.config.wrist_camera_name)
        if wrist_path is None:
            wrist_path = "/dev/video3"
            logger.warning(f"未找到 '{self.config.wrist_camera_name}'，使用默认路径: {wrist_path}")
        
        # 创建摄像头配置
        cameras_config = {
            "front": self._camera_config_class(
                index_or_path=front_path,
                fps=30,
                width=640,
                height=480,
                rotation=self._cv2_rotation.NO_ROTATION
            ),
            "wrist": self._camera_config_class(
                index_or_path=wrist_path,
                fps=30,
                width=640,
                height=480,
                rotation=self._cv2_rotation.ROTATE_180
            )
        }
        
        robot_config = self._robot_config_class(
            id=self.config.robot_id,
            cameras=cameras_config
        )
        
        return self._robot_class(robot_config)
    
    def connect(self, calibrate: bool = False) -> bool:
        """连接机器人
        
        Args:
            calibrate: 是否进行校准，默认 False（跳过校准）
        """
        try:
            if self.robot and self.robot.is_connected:
                logger.info("机器人已经连接")
                return True
            
            logger.info("正在连接机器人...")
            
            if self.robot is None:
                self.robot = self._create_robot()
                if self.robot is None:
                    return False
            
            # 跳过校准（calibrate=False）
            self.robot.connect(calibrate=calibrate)
            
            # 读取当前机械臂位置
            current_state = self.robot.get_observation()
            with self._lock:
                for key in self.current_action:
                    if key.endswith('.pos') and key in current_state:
                        self.current_action[key] = current_state[key]
            
            # 启动控制循环
            if not self.running:
                self.running = True
                self.control_thread = threading.Thread(target=self._control_loop, daemon=True)
                self.control_thread.start()
            
            logger.info("✓ 机器人连接成功")
            return True
            
        except Exception as e:
            logger.error(f"机器人连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开机器人连接"""
        try:
            self.running = False
            if self.robot and self.robot.is_connected:
                self.robot.disconnect()
            logger.info("机器人断开连接成功")
        except Exception as e:
            logger.error(f"断开机器人连接失败: {e}")
    
    def is_connected(self) -> bool:
        """检查机器人是否连接"""
        return self.robot is not None and self.robot.is_connected
    
    def get_status(self) -> Dict[str, Any]:
        """获取机器人状态"""
        try:
            with self._lock:
                return {
                    "success": True,
                    "connected": self.is_connected(),
                    "running": self.running,
                    "current_action": self.current_action.copy(),
                    "last_command_time": self.last_command_time
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
                "connected": False,
                "running": self.running
            }
    
    def execute_predefined_command(self, command: str) -> Dict[str, Any]:
        """执行预定义的移动命令"""
        if not self.is_connected():
            return {
                "success": False,
                "message": "机器人未连接，请检查硬件连接后重启服务"
            }
        
        try:
            with self._lock:
                self.current_action.update({
                    "x.vel": 0.0,
                    "y.vel": 0.0,
                    "theta.vel": 0.0
                })

                if command == "forward":
                    self.current_action["x.vel"] = self.config.linear_speed
                elif command == "backward":
                    self.current_action["x.vel"] = -self.config.linear_speed
                elif command == "left":
                    self.current_action["y.vel"] = self.config.linear_speed
                elif command == "right":
                    self.current_action["y.vel"] = -self.config.linear_speed
                elif command == "rotate_left":
                    self.current_action["theta.vel"] = self.config.angular_speed
                elif command == "rotate_right":
                    self.current_action["theta.vel"] = -self.config.angular_speed
                elif command == "stop":
                    pass
                else:
                    logger.warning(f"未知命令: {command}")
                    return {
                        "success": False,
                        "message": f"未知命令: {command}"
                    }

                self.last_command_time = time.time()
            
            return {
                "success": True,
                "message": f"执行命令: {command}",
                "current_action": self.current_action.copy()
            }

        except Exception as e:
            logger.error(f"执行命令失败: {e}")
            return {
                "success": False,
                "message": str(e)
            }
    
    def execute_custom_velocity(self, x_vel: float, y_vel: float, theta_vel: float) -> Dict[str, Any]:
        """执行自定义速度命令"""
        if not self.is_connected():
            return {
                "success": False,
                "message": "机器人未连接，请检查硬件连接后重启服务"
            }
        
        try:
            with self._lock:
                self.current_action.update({
                    "x.vel": x_vel,
                    "y.vel": y_vel,
                    "theta.vel": theta_vel
                })
                self.last_command_time = time.time()
            
            return {
                "success": True,
                "message": "自定义速度命令已设置",
                "current_action": self.current_action.copy()
            }
            
        except Exception as e:
            logger.error(f"设置自定义速度失败: {e}")
            return {
                "success": False,
                "message": str(e)
            }
    
    def move_robot_for_duration(self, command: str, duration: float) -> Dict[str, Any]:
        """移动机器人指定时间"""
        result = self.execute_predefined_command(command)
        if not result["success"]:
            return result
        
        if command != "stop" and duration > 0:
            def stop_after_duration():
                end_time = time.time() + duration
                while time.time() < end_time:
                    with self._lock:
                        self.last_command_time = time.time()
                    time.sleep(0.1)
                self.execute_predefined_command("stop")
            
            stop_thread = threading.Thread(target=stop_after_duration, daemon=True)
            stop_thread.start()
        
        return {
            "success": True,
            "command": command,
            "duration": duration,
            "message": f"机器人{command}移动{duration}秒"
        }
    
    def move_robot_with_custom_speed_for_duration(self, x_vel: float, y_vel: float,
                                                 theta_vel: float, duration: float) -> Dict[str, Any]:
        """使用自定义速度移动机器人指定时间"""
        result = self.execute_custom_velocity(x_vel, y_vel, theta_vel)
        if not result["success"]:
            return result
        
        if duration > 0:
            def stop_after_duration():
                end_time = time.time() + duration
                while time.time() < end_time:
                    with self._lock:
                        self.last_command_time = time.time()
                    time.sleep(0.1)
                self.execute_predefined_command("stop")
            
            stop_thread = threading.Thread(target=stop_after_duration, daemon=True)
            stop_thread.start()
        
        return {
            "success": True,
            "x_vel": x_vel,
            "y_vel": y_vel,
            "theta_vel": theta_vel,
            "duration": duration,
            "message": f"机器人自定义速度移动{duration}秒"
        }
    
    def set_arm_position(self, arm_positions: Dict[str, float]) -> Dict[str, Any]:
        """设置机械臂位置"""
        if not self.is_connected():
            return {
                "success": False,
                "message": "机器人未连接，请检查硬件连接后重启服务"
            }
        
        try:
            if hasattr(self, '_arm_speed_configured') and self._arm_speed_configured == self.config.arm_servo_speed:
                pass
            else:
                self._configure_arm_servo_speed(self.config.arm_servo_speed)
                self._arm_speed_configured = self.config.arm_servo_speed
            
            with self._lock:
                for joint, position in arm_positions.items():
                    if joint in self.current_action:
                        self.current_action[joint] = position
                
                self.last_command_time = time.time()
            
            return {
                "success": True,
                "message": f"机械臂位置已更新（舵机速度: {self.config.arm_servo_speed*100:.0f}%）",
                "arm_positions": arm_positions,
                "servo_speed_percent": self.config.arm_servo_speed * 100,
                "current_action": self.current_action.copy()
            }
            
        except Exception as e:
            logger.error(f"设置机械臂位置失败: {e}")
            return {
                "success": False,
                "message": str(e)
            }
    
    def _configure_arm_servo_speed(self, speed_ratio: float = 0.2):
        """配置机械臂舵机速度"""
        if not self.is_connected():
            return
            
        speed_ratio = max(0.05, min(1.0, speed_ratio))
        max_speed = 2400
        goal_speed = int(max_speed * speed_ratio)
        max_acceleration = 50
        acceleration = max(5, int(max_acceleration * speed_ratio * 0.5))
        torque_limit = getattr(self.config, 'arm_torque_limit', 600)
        
        arm_motors = [motor for motor in self.robot.bus.motors if motor.startswith("arm")]
        
        for motor in arm_motors:
            try:
                self.robot.bus.write("Goal_Acc", motor, acceleration)
                self.robot.bus.write("Goal_Speed", motor, goal_speed)
                self.robot.bus.write("P_Coefficient", motor, 8)
                self.robot.bus.write("Torque_Limit", motor, torque_limit)
            except Exception as e:
                logger.warning(f"设置舵机 {motor} 速度/扭矩失败: {e}")
        
        logger.info(f"机械臂舵机配置已更新: 速度={speed_ratio*100:.0f}%, 扭矩限制={torque_limit}/1000")
    
    def stop_robot(self):
        """停止机器人"""
        return self.execute_predefined_command("stop")
    
    def move(self, x: float, y: float, theta: float):
        """
        移动机器人
        
        Args:
            x: 前后速度 (m/s)
            y: 左右速度 (m/s)
            theta: 旋转速度 (deg/s)
        """
        return self.execute_custom_velocity(x, y, theta)
    
    def reset_arm(self):
        """重置机械臂到初始位置"""
        initial_positions = {
            "arm_shoulder_pan.pos": 0,
            "arm_shoulder_lift.pos": 0,
            "arm_elbow_flex.pos": 0,
            "arm_wrist_flex.pos": 0,
            "arm_wrist_roll.pos": 0,
            "arm_gripper.pos": 50,  # 半开状态
        }
        return self.set_arm_position(initial_positions)
    
    def set_gripper(self, value: int):
        """
        设置夹爪开合度
        
        Args:
            value: 0-100，0为完全关闭，100为完全打开
        """
        return self.set_arm_position({"arm_gripper.pos": value})
    
    def _control_loop(self):
        """机器人控制主循环"""
        logger.info("机器人控制循环已启动")
        
        while self.running and self.is_connected():
            try:
                loop_start_time = time.time()
                
                if (time.time() - self.last_command_time) > self.config.command_timeout_s:
                    with self._lock:
                        self.current_action.update({
                            "x.vel": 0.0,
                            "y.vel": 0.0,
                            "theta.vel": 0.0
                        })

                with self._lock:
                    action_to_send = self.current_action.copy()
                
                self.robot.send_action(action_to_send)
                
                elapsed = time.time() - loop_start_time
                sleep_time = max(1.0 / self.config.max_loop_freq_hz - elapsed, 0)
                time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"控制循环错误: {e}")
                time.sleep(0.1)

        logger.info("机器人控制循环已停止")


# 全局服务实例
_global_service: Optional[RobotService] = None
_service_lock = threading.Lock()


def get_global_service() -> Optional[RobotService]:
    """获取全局服务实例"""
    global _global_service
    with _service_lock:
        return _global_service


def set_global_service(service: RobotService):
    """设置全局服务实例"""
    global _global_service
    with _service_lock:
        _global_service = service


def create_default_service(robot_id: str = "moyu_robot") -> RobotService:
    """创建默认配置的服务实例"""
    config = RobotServiceConfig(
        robot_id=robot_id,
        linear_speed=0.2,
        angular_speed=30.0,
        arm_servo_speed=0.2,
        command_timeout_s=6,
        max_loop_freq_hz=30
    )
    return RobotService(config)

