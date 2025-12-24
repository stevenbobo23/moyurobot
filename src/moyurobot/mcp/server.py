#!/usr/bin/env python3
"""
MCP 控制服务器

通过 MCP 协议提供机器人控制功能，支持 AI 调用
"""

import sys
import os
import logging
import math
import random
import time
import cv2
import numpy as np
import base64
import requests
import json
from typing import Dict, Any, Optional
from pathlib import Path

from fastmcp import FastMCP

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('MoYuMCP')

# 修复 Windows 控制台的 UTF-8 编码
if sys.platform == 'win32':
    sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

# 创建 MCP 服务器
mcp = FastMCP("MoYuRobotController")

# 全局变量
current_speed_index = 1  # 默认中速
speed_levels = [
    {"xy": 0.1, "theta": 30, "name": "慢速"},
    {"xy": 0.2, "theta": 60, "name": "中速"},
    {"xy": 0.3, "theta": 90, "name": "快速"},
]


def get_service():
    """获取机器人服务实例"""
    from moyurobot.core.robot_service import (
        get_global_service, 
        create_default_service, 
        set_global_service
    )
    
    service = get_global_service()
    if service is None:
        logger.info("全局服务实例不存在，创建新的服务实例")
        try:
            # 从环境变量获取 robot_id，默认使用 my_awesome_kiwi（与校准文件匹配）
            robot_id = os.environ.get('ROBOT_ID', 'my_awesome_kiwi')
            logger.info(f"使用 robot_id: {robot_id}")
            service = create_default_service(robot_id=robot_id)
            if service.connect():
                logger.info("✓ 服务创建并连接成功")
                set_global_service(service)
            else:
                logger.warning("⚠️ 服务创建成功但连接失败，将以离线模式运行")
                set_global_service(service)
        except Exception as e:
            logger.error(f"创建服务失败: {e}")
            return None
    
    return service


def _smooth_arm_motion(service, target_positions: Dict[str, float], 
                       duration: float = 1.0, steps: int = 10) -> Dict[str, Any]:
    """平滑移动机械臂到目标位置"""
    try:
        try:
            current_state = service.robot.get_observation()
            current_positions = {
                key: current_state.get(key, 0) for key in target_positions.keys()
            }
            logger.info(f"当前位置: {current_positions}")
        except Exception as e:
            logger.warning(f"无法读取当前位置，使用默认值: {e}")
            current_positions = target_positions.copy()
        
        needs_movement = False
        for key in target_positions.keys():
            if abs(current_positions[key] - target_positions[key]) > 0.5:
                needs_movement = True
                break
        
        if not needs_movement:
            logger.info("目标位置与当前位置相同，无需移动")
            return {
                "success": True,
                "duration": 0,
                "steps": 0,
                "start_positions": current_positions,
                "target_positions": target_positions,
                "skipped": True,
                "reason": "当前位置已是目标位置"
            }
        
        step_duration = duration / steps
        
        arm_motors = []
        for key in target_positions.keys():
            motor_name = key.split('.')[0]
            if motor_name not in arm_motors:
                arm_motors.append(motor_name)
        
        temp_speed_ratio = 0.8
        max_speed = 2400
        temp_goal_speed = int(max_speed * temp_speed_ratio)
        temp_acceleration = max(10, int(100 * temp_speed_ratio))
        
        logger.info(f"临时提高舵机速度: Goal_Speed={temp_goal_speed}, Goal_Acc={temp_acceleration}")
        
        for motor in arm_motors:
            try:
                service.robot.bus.write("Goal_Acc", motor, temp_acceleration)
                service.robot.bus.write("Goal_Speed", motor, temp_goal_speed)
            except Exception as e:
                logger.warning(f"设置舵机 {motor} 临时速度失败: {e}")
        
        logger.info(f"开始平滑运动: {steps} 步, 每步 {step_duration:.3f}s")
        
        for i in range(steps + 1):
            ratio = i / steps
            
            interpolated_positions = {}
            for key in target_positions.keys():
                start_val = current_positions[key]
                end_val = target_positions[key]
                interpolated_positions[key] = start_val + (end_val - start_val) * ratio
            
            result = service.set_arm_position(interpolated_positions)
            
            if not result["success"]:
                logger.error(f"第 {i}/{steps} 步失败: {result.get('message', '未知错误')}")
                service._configure_arm_servo_speed(service.config.arm_servo_speed)
                return {
                    "success": False,
                    "error": f"平滑运动在第{i}步失败: {result.get('message', '未知错误')}",
                    "completed_steps": i,
                    "total_steps": steps
                }
            
            if i < steps:
                time.sleep(step_duration)
        
        logger.info(f"恢复原始舵机速度: {service.config.arm_servo_speed*100:.0f}%")
        service._configure_arm_servo_speed(service.config.arm_servo_speed)
        
        return {
            "success": True,
            "duration": duration,
            "steps": steps,
            "start_positions": current_positions,
            "target_positions": target_positions
        }
        
    except Exception as e:
        logger.error(f"平滑运动失败: {e}")
        try:
            service._configure_arm_servo_speed(service.config.arm_servo_speed)
        except:
            pass
        return {
            "success": False,
            "error": f"平滑运动执行异常: {str(e)}"
        }


@mcp.tool()
def calculator(python_expression: str) -> dict:
    """数学计算工具，执行 Python 表达式。可直接使用 math 和 random 模块。"""
    result = eval(python_expression, {"math": math, "random": random})
    logger.info(f"计算表达式: {python_expression}, 结果: {result}")
    return {"success": True, "result": result}


@mcp.tool()
def move_robot(direction: str, duration: float = 1.0) -> dict:
    """
    控制机器人移动（不包含旋转）
    
    Args:
        direction: 移动方向 ('forward', 'backward', 'left', 'right', 'stop')
        duration: 移动持续时间（秒），默认1.0秒
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"移动机器人 {direction} {duration}秒")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    valid_directions = ['forward', 'backward', 'left', 'right', 'stop']
    if direction not in valid_directions:
        return {
            "success": False,
            "error": f"无效的移动方向: {direction}。有效选项: {', '.join(valid_directions)}"
        }
    
    return service.move_robot_for_duration(direction, duration)


@mcp.tool()
def rotate_robot(direction: str, angle: float = 45.0) -> dict:
    """
    控制机器人旋转指定角度
    
    机器人旋转速度约为20度/秒
    
    Args:
        direction: 旋转方向 ('rotate_left', 'rotate_right')
        angle: 旋转角度（度），默认45度
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"旋转机器人 {direction} {angle}度")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    if direction not in ['rotate_left', 'rotate_right']:
        return {
            "success": False,
            "error": f"无效的旋转方向: {direction}。有效选项: 'rotate_left', 'rotate_right'"
        }
    
    if angle <= 0:
        return {"success": False, "error": f"旋转角度必须大于0，当前值: {angle}"}
    
    angular_speed = 20
    duration = angle / angular_speed
    
    logger.info(f"旋转计算: 角度={angle}°, 速度={angular_speed}°/s, 持续={duration:.2f}s")
    
    result = service.move_robot_for_duration(direction, duration)
    
    if result["success"]:
        result["rotation_angle"] = angle
        result["rotation_direction"] = direction
        result["angular_speed"] = angular_speed
        result["message"] = f"机器人已{direction}旋转{angle}度（耗时{duration:.2f}秒）"
    
    return result


@mcp.tool()
def move_robot_with_custom_speed(x_vel: float, y_vel: float, theta_vel: float, 
                                  duration: float = 1.0) -> dict:
    """
    使用自定义速度控制机器人移动
    
    Args:
        x_vel: 前后速度 (m/s)，正值为前进
        y_vel: 左右速度 (m/s)，正值为左移
        theta_vel: 旋转速度 (deg/s)，正值为逆时针
        duration: 移动持续时间（秒）
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"自定义速度移动: x={x_vel}, y={y_vel}, theta={theta_vel}, duration={duration}s")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    return service.move_robot_with_custom_speed_for_duration(x_vel, y_vel, theta_vel, duration)


@mcp.tool()
def set_speed_level(level: str) -> dict:
    """
    设置机器人速度等级
    
    Args:
        level: 速度等级 ('slow', 'medium', 'fast')
        
    Returns:
        dict: 操作结果
    """
    global current_speed_index
    
    level_map = {"slow": 0, "medium": 1, "fast": 2}
    
    if level in level_map:
        current_speed_index = level_map[level]
        speed_name = speed_levels[current_speed_index]["name"]
        logger.info(f"速度等级设置为 {level} ({speed_name})")
        
        return {
            "success": True,
            "level": level,
            "speed_name": speed_name,
            "message": f"速度等级已设为{speed_name}"
        }
    else:
        return {
            "success": False,
            "error": f"无效的速度等级: {level}。请使用 'slow', 'medium', 或 'fast'。"
        }


@mcp.tool()
def get_robot_status() -> dict:
    """
    获取机器人当前状态
    
    Returns:
        dict: 机器人状态信息
    """
    service = get_service()
    speed_config = speed_levels[current_speed_index]
    
    if service is None:
        return {
            "success": False,
            "error": "机器人服务不可用",
            "speed_level": speed_config["name"],
            "speed_xy": speed_config["xy"],
            "speed_theta": speed_config["theta"],
            "mcp_service_active": True,
            "message": "MCP服务活跃但机器人服务不可用"
        }
    
    status = service.get_status()
    status.update({
        "speed_level": speed_config["name"],
        "speed_xy": speed_config["xy"],
        "speed_theta": speed_config["theta"],
        "mcp_service_active": True,
        "message": "MCP服务活跃且与机器人服务正常通信"
    })
    
    logger.info(f"机器人状态: {status}")
    return status


@mcp.tool()
def control_gripper(action: str) -> dict:
    """
    控制机器人夹爪开关
    
    Args:
        action: 夹爪动作 ('open' 打开到80度, 'close' 关闭到0度)
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"控制夹爪: {action}")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    if action == "open":
        gripper_position = 80
        action_desc = "打开"
    elif action == "close":
        gripper_position = 0
        action_desc = "关闭"
    else:
        return {
            "success": False,
            "error": f"无效的夹爪动作: {action}。请使用 'open' 或 'close'。"
        }
    
    arm_positions = {"arm_gripper.pos": gripper_position}
    result = service.set_arm_position(arm_positions)
    
    if result["success"]:
        result["message"] = f"夹爪已{action_desc}到{gripper_position}度"
        result["action"] = action
        result["position"] = gripper_position
    
    return result


@mcp.tool()
def nod_head(times: int = 3, pause_duration: float = 0.3) -> dict:
    """
    控制机器人做点头动作
    
    通过控制腕关节弯曲实现点头效果
    
    Args:
        times: 点头次数，默认3次
        pause_duration: 每次动作停顿时间（秒），默认0.3秒
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"点头动作: {times}次, 停顿{pause_duration}s")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    try:
        results = []
        
        for i in range(times):
            logger.info(f"点头 {i+1}/{times}: 腕关节到60度")
            down_result = service.set_arm_position({"arm_wrist_flex.pos": 60})
            results.append({"cycle": i+1, "phase": "down", "position": 60, "success": down_result["success"]})
            
            if not down_result["success"]:
                return {
                    "success": False,
                    "error": f"点头第{i+1}次失败（向下）",
                    "completed_cycles": i,
                    "results": results
                }
            
            time.sleep(pause_duration)
            
            logger.info(f"点头 {i+1}/{times}: 腕关节到0度")
            up_result = service.set_arm_position({"arm_wrist_flex.pos": 0})
            results.append({"cycle": i+1, "phase": "up", "position": 0, "success": up_result["success"]})
            
            if not up_result["success"]:
                return {
                    "success": False,
                    "error": f"点头第{i+1}次失败（向上）",
                    "completed_cycles": i,
                    "results": results
                }
            
            if i < times - 1:
                time.sleep(pause_duration)
        
        logger.info(f"点头动作完成: {times}次")
        return {
            "success": True,
            "message": f"点头动作完成，共{times}次",
            "cycles": times,
            "pause_duration": pause_duration,
            "results": results
        }
        
    except Exception as e:
        logger.error(f"点头动作失败: {e}")
        return {"success": False, "error": f"点头动作异常: {str(e)}"}


@mcp.tool()
def reset_arm() -> dict:
    """
    将机械臂复位到初始位置（1秒平滑复位）
    
    Returns:
        dict: 操作结果
    """
    logger.info("机械臂复位到初始位置")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    target_positions = {
        "arm_shoulder_pan.pos": 0,
        "arm_shoulder_lift.pos": 0,
        "arm_elbow_flex.pos": 0,
        "arm_wrist_flex.pos": 0,
        "arm_wrist_roll.pos": 0
    }
    
    result = _smooth_arm_motion(service, target_positions, duration=1.0, steps=10)
    
    if result["success"]:
        result["message"] = f"机械臂已平滑复位（耗时{result['duration']}秒）"
        result["home_position"] = target_positions
    
    return result


@mcp.tool()
def stand_at_attention() -> dict:
    """
    控制机器人立正姿态（1秒平滑运动）
    
    Returns:
        dict: 操作结果
    """
    logger.info("设置立正姿态")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    attention_positions = {"arm_elbow_flex.pos": -90}
    
    result = _smooth_arm_motion(service, attention_positions, duration=1.0, steps=10)
    
    if result["success"]:
        result["message"] = f"机器人已设为立正姿态（耗时{result['duration']}秒）"
        result["attention_position"] = attention_positions
    
    return result


@mcp.tool()
def shake_head(times: int = 3, pause_duration: float = 0.3) -> dict:
    """
    控制机器人摇头动作
    
    通过控制腕关节旋转实现摇头效果
    
    Args:
        times: 摇头次数，默认3次
        pause_duration: 每次动作停顿时间（秒），默认0.3秒
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"摇头动作: {times}次, 停顿{pause_duration}s")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    try:
        results = []
        
        for i in range(times):
            logger.info(f"摇头 {i+1}/{times}: 腕旋转到-40度")
            left_result = service.set_arm_position({"arm_wrist_roll.pos": -40})
            results.append({"cycle": i+1, "phase": "left", "position": -40, "success": left_result["success"]})
            
            if not left_result["success"]:
                return {
                    "success": False,
                    "error": f"摇头第{i+1}次失败（向左）",
                    "completed_cycles": i,
                    "results": results
                }
            
            time.sleep(pause_duration)
            
            logger.info(f"摇头 {i+1}/{times}: 腕旋转到40度")
            right_result = service.set_arm_position({"arm_wrist_roll.pos": 40})
            results.append({"cycle": i+1, "phase": "right", "position": 40, "success": right_result["success"]})
            
            if not right_result["success"]:
                return {
                    "success": False,
                    "error": f"摇头第{i+1}次失败（向右）",
                    "completed_cycles": i,
                    "results": results
                }
            
            if i < times - 1:
                time.sleep(pause_duration)
        
        logger.info("摇头：回到中心位置")
        center_result = service.set_arm_position({"arm_wrist_roll.pos": 0})
        results.append({"cycle": "final", "phase": "center", "position": 0, "success": center_result["success"]})
        
        return {
            "success": True,
            "message": f"摇头动作完成，共{times}次",
            "cycles": times,
            "pause_duration": pause_duration,
            "results": results
        }
        
    except Exception as e:
        logger.error(f"摇头动作失败: {e}")
        return {"success": False, "error": f"摇头动作异常: {str(e)}"}


@mcp.tool()
def twist_waist(times: int = 3, pause_duration: float = 0.3) -> dict:
    """
    控制机器人扭腰动作（平滑运动）
    
    通过控制肩膀水平旋转实现扭腰效果
    
    Args:
        times: 扭腰次数，默认3次
        pause_duration: 每次动作停顿时间（秒），默认0.3秒
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"扭腰动作: {times}次, 停顿{pause_duration}s (平滑运动)")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    try:
        results = []
        motion_duration = 1.0
        
        for i in range(times):
            logger.info(f"扭腰 {i+1}/{times}: 肩膀到-10度")
            left_result = _smooth_arm_motion(service, {"arm_shoulder_pan.pos": -10}, duration=motion_duration, steps=10)
            results.append({"cycle": i+1, "phase": "left", "position": -10, "success": left_result["success"]})
            
            if not left_result["success"]:
                return {
                    "success": False,
                    "error": f"扭腰第{i+1}次失败（向左）",
                    "completed_cycles": i,
                    "results": results
                }
            
            time.sleep(pause_duration)
            
            logger.info(f"扭腰 {i+1}/{times}: 肩膀到10度")
            right_result = _smooth_arm_motion(service, {"arm_shoulder_pan.pos": 10}, duration=motion_duration, steps=10)
            results.append({"cycle": i+1, "phase": "right", "position": 10, "success": right_result["success"]})
            
            if not right_result["success"]:
                return {
                    "success": False,
                    "error": f"扭腰第{i+1}次失败（向右）",
                    "completed_cycles": i,
                    "results": results
                }
            
            if i < times - 1:
                time.sleep(pause_duration)
        
        logger.info("扭腰：回到中心位置")
        center_result = _smooth_arm_motion(service, {"arm_shoulder_pan.pos": 0}, duration=motion_duration, steps=10)
        results.append({"cycle": "final", "phase": "center", "position": 0, "success": center_result["success"]})
        
        return {
            "success": True,
            "message": f"扭腰动作完成，共{times}次（平滑运动）",
            "cycles": times,
            "pause_duration": pause_duration,
            "motion_duration": motion_duration,
            "results": results
        }
        
    except Exception as e:
        logger.error(f"扭腰动作失败: {e}")
        return {"success": False, "error": f"扭腰动作异常: {str(e)}"}


@mcp.tool()
def control_arm_joint_limited(joint_name: str, position: float) -> dict:
    """
    控制机械臂单个关节到指定位置，限制在安全范围内（±50度）
    
    Args:
        joint_name: 关节名称 ('shoulder_pan', 'shoulder_lift', 'elbow_flex', 
                    'wrist_flex', 'wrist_roll', 'gripper')
        position: 目标位置（度），会被限制在安全范围内
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"控制关节 {joint_name} 到 {position}度 (受限模式)")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    joint_mapping = {
        "shoulder_pan": {"key": "arm_shoulder_pan.pos", "min_safe": -50, "max_safe": 50, "description": "肩膀水平"},
        "shoulder_lift": {"key": "arm_shoulder_lift.pos", "min_safe": -50, "max_safe": 50, "description": "肩膀垂直"},
        "elbow_flex": {"key": "arm_elbow_flex.pos", "min_safe": -50, "max_safe": 50, "description": "肘关节"},
        "wrist_flex": {"key": "arm_wrist_flex.pos", "min_safe": -50, "max_safe": 50, "description": "腕关节弯曲"},
        "wrist_roll": {"key": "arm_wrist_roll.pos", "min_safe": -50, "max_safe": 50, "description": "腕关节旋转"},
        "gripper": {"key": "arm_gripper.pos", "min_safe": 0, "max_safe": 50, "description": "夹爪"}
    }
    
    if joint_name not in joint_mapping:
        valid_joints = ", ".join(joint_mapping.keys())
        return {"success": False, "error": f"无效的关节名称: {joint_name}。有效选项: {valid_joints}"}
    
    joint_info = joint_mapping[joint_name]
    original_position = position
    clamped_position = max(joint_info["min_safe"], min(joint_info["max_safe"], position))
    
    if clamped_position != original_position:
        logger.warning(f"位置 {original_position} 被限制到 {clamped_position}")
    
    try:
        arm_positions = {joint_info["key"]: clamped_position}
        result = _smooth_arm_motion(service, arm_positions, duration=1.0, steps=10)
        
        if result["success"]:
            result["message"] = f"{joint_info['description']}已移动到{clamped_position}度"
            result["joint_name"] = joint_name
            result["joint_description"] = joint_info["description"]
            result["original_position"] = original_position
            result["actual_position"] = clamped_position
            result["safe_range"] = {"min": joint_info["min_safe"], "max": joint_info["max_safe"]}
            result["was_clamped"] = (clamped_position != original_position)
        
        return result
        
    except Exception as e:
        logger.error(f"关节控制失败: {e}")
        return {"success": False, "error": f"关节控制异常: {str(e)}"}


def _capture_front_camera_image_internal(filename: Optional[str] = None) -> dict:
    """内部辅助函数：获取前置摄像头图片并保存"""
    logger.info(f"获取前置摄像头图片: {filename}")
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    if not service.is_connected():
        return {"success": False, "error": "机器人未连接"}
    
    if "front" not in service.robot.cameras:
        return {"success": False, "error": "前置摄像头不可用"}
    
    front_camera = service.robot.cameras["front"]
    if not front_camera.is_connected:
        return {"success": False, "error": "前置摄像头未连接"}
    
    try:
        logger.info("读取前置摄像头帧...")
        frame = front_camera.async_read(timeout_ms=1000)
        
        if frame is None or frame.size == 0:
            return {"success": False, "error": "无法获取摄像头图片"}
        
        image_dir = Path.home() / "image"
        image_dir.mkdir(exist_ok=True)
        
        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            filename = f"front_camera_{timestamp}"
        
        safe_filename = "".join(c for c in filename if c.isalnum() or c in ('-', '_', '.'))
        if not safe_filename:
            safe_filename = f"front_camera_{int(time.time())}"
        
        file_path = image_dir / f"{safe_filename}.jpg"
        
        counter = 1
        original_path = file_path
        while file_path.exists():
            stem = original_path.stem
            file_path = image_dir / f"{stem}_{counter}.jpg"
            counter += 1
        
        logger.info("转换 BGR 到 RGB...")
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        success = cv2.imwrite(str(file_path), frame_rgb, [cv2.IMWRITE_JPEG_QUALITY, 95])
        
        if success:
            height, width = frame_rgb.shape[:2]
            file_size = file_path.stat().st_size
            
            logger.info(f"图片已保存: {file_path}")
            return {
                "success": True,
                "message": f"图片已保存到 {file_path}",
                "file_path": str(file_path),
                "filename": file_path.name,
                "image_info": {
                    "width": width,
                    "height": height,
                    "file_size_bytes": file_size,
                    "format": "JPEG",
                    "color_format": "RGB"
                },
                "capture_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            }
        else:
            return {"success": False, "error": f"保存图片失败: {file_path}"}
            
    except Exception as e:
        logger.error(f"获取摄像头图片失败: {e}")
        return {"success": False, "error": f"获取图片异常: {str(e)}"}


@mcp.tool()
def capture_and_analyze_with_qwen(question: str = "") -> dict:
    """
    拍照并分析图片内容，用户想看前方有什么时可以调用
    
    Args:
        question: 用户想了解的内容
        
    Returns:
        dict: 包含图片信息和AI分析结果
    """
    base_prompt = "用中文告诉我图片里有什么,回复内容50字以内"
    full_question = f"{base_prompt}。{question}" if question else base_prompt
    
    logger.info(f"拍照并分析: {full_question}")
    
    capture_result = _capture_front_camera_image_internal(None)
    
    if not capture_result["success"]:
        return capture_result
    
    try:
        image_path = capture_result["file_path"]
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
            image_base64 = base64.b64encode(image_data).decode('utf-8')
        
        api_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        api_key = os.environ.get("QWEN_API_KEY")
        
        if not api_key:
            return {"success": False, "error": "未配置 QWEN_API_KEY 环境变量"}
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "qwen-vl-plus",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                        {"type": "text", "text": full_question}
                    ]
                }
            ]
        }
        
        logger.info("调用千问 VL API 分析图片...")
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            response_data = response.json()
            
            if "choices" in response_data and len(response_data["choices"]) > 0:
                ai_content = response_data["choices"][0]["message"]["content"]
                
                logger.info("图片分析完成")
                return {
                    "success": True,
                    "user_question": question if question else "无",
                    "full_question": full_question,
                    "answer": ai_content,
                    "model": "qwen-vl-plus",
                    "image_file": capture_result['filename'],
                    "analysis_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                }
            else:
                return {"success": False, "error": "API响应格式异常"}
        else:
            return {"success": False, "error": f"API调用失败: {response.status_code}"}
            
    except requests.exceptions.Timeout:
        return {"success": False, "error": "API调用超时"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"网络请求失败: {str(e)}"}
    except Exception as e:
        logger.error(f"图片分析失败: {e}")
        return {"success": False, "error": f"分析异常: {str(e)}"}


@mcp.tool()
def control_multiple_arm_joints_limited(joint_positions: str) -> dict:
    """
    同时控制机械臂多个关节到指定位置，限制在安全范围内
    
    Args:
        joint_positions: 关节位置JSON字符串，如 '{"shoulder_pan": 30, "elbow_flex": -20}'
                        如果为空 "{}"，则为所有关节生成随机值
        
    Returns:
        dict: 操作结果
    """
    logger.info(f"控制多个关节 (受限模式): {joint_positions}")
    
    try:
        joint_positions_dict = json.loads(joint_positions)
        logger.info(f"解析JSON: {joint_positions_dict}")
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON解析失败: {str(e)}"}
    
    if not isinstance(joint_positions_dict, dict):
        return {"success": False, "error": f"参数类型错误，期望dict，得到{type(joint_positions_dict).__name__}"}
    
    service = get_service()
    if service is None:
        return {"success": False, "error": "机器人服务不可用"}
    
    joint_mapping = {
        "shoulder_pan": {"key": "arm_shoulder_pan.pos", "min_safe": -50, "max_safe": 50, "description": "肩膀水平"},
        "shoulder_lift": {"key": "arm_shoulder_lift.pos", "min_safe": -50, "max_safe": 50, "description": "肩膀垂直"},
        "elbow_flex": {"key": "arm_elbow_flex.pos", "min_safe": -50, "max_safe": 50, "description": "肘关节"},
        "wrist_flex": {"key": "arm_wrist_flex.pos", "min_safe": -50, "max_safe": 50, "description": "腕关节弯曲"},
        "wrist_roll": {"key": "arm_wrist_roll.pos", "min_safe": -50, "max_safe": 50, "description": "腕关节旋转"},
        "gripper": {"key": "arm_gripper.pos", "min_safe": 0, "max_safe": 50, "description": "夹爪"}
    }
    
    # 空字典时生成随机位置
    if len(joint_positions_dict) == 0:
        logger.info("参数为空，生成随机位置")
        joint_positions_dict = {}
        for joint_name, joint_info in joint_mapping.items():
            random_position = random.uniform(joint_info["min_safe"], joint_info["max_safe"])
            joint_positions_dict[joint_name] = round(random_position, 1)
        logger.info(f"随机关节位置: {joint_positions_dict}")
    
    arm_positions = {}
    position_info = {}
    clamp_warnings = []
    
    for joint_name, position in joint_positions_dict.items():
        if joint_name not in joint_mapping:
            valid_joints = ", ".join(joint_mapping.keys())
            return {"success": False, "error": f"无效的关节名称: {joint_name}。有效选项: {valid_joints}"}
        
        joint_info = joint_mapping[joint_name]
        original_position = position
        clamped_position = max(joint_info["min_safe"], min(joint_info["max_safe"], position))
        
        arm_positions[joint_info["key"]] = clamped_position
        position_info[joint_name] = {
            "description": joint_info["description"],
            "original_position": original_position,
            "actual_position": clamped_position,
            "safe_range": {"min": joint_info["min_safe"], "max": joint_info["max_safe"]},
            "was_clamped": (clamped_position != original_position)
        }
        
        if clamped_position != original_position:
            warning_msg = f"{joint_info['description']}: {original_position}°限制到{clamped_position}°"
            clamp_warnings.append(warning_msg)
            logger.warning(warning_msg)
    
    try:
        result = _smooth_arm_motion(service, arm_positions, duration=1.0, steps=10)
        
        if result["success"]:
            joint_count = len(joint_positions_dict)
            clamped_count = len(clamp_warnings)
            
            result["message"] = f"成功控制{joint_count}个关节（耗时{result['duration']}秒）"
            result["joint_positions"] = position_info
            result["joints_controlled"] = list(joint_positions_dict.keys())
            result["clamp_warnings"] = clamp_warnings
            result["clamped_joints_count"] = clamped_count
            
            if clamp_warnings:
                result["message"] += f"，其中{clamped_count}个关节位置被安全限制"
        
        return result
        
    except Exception as e:
        logger.error(f"多关节控制失败: {e}")
        return {"success": False, "error": f"多关节控制异常: {str(e)}"}


# 启动服务器
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='MoYu Robot MCP Controller Server')
    parser.add_argument('--transport', type=str, choices=['stdio', 'http'], default='stdio',
                        help='传输方式：stdio 或 http，默认 stdio')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='HTTP服务器监听地址，默认 0.0.0.0')
    parser.add_argument('--port', type=int, default=8000,
                        help='HTTP服务器监听端口，默认 8000')
    
    args = parser.parse_args()
    
    logger.info(f"启动 MCP 服务器，传输方式: {args.transport}...")
    
    logger.info("初始化机器人连接...")
    service = get_service()
    if service and service.is_connected():
        logger.info("✓ 机器人连接成功")
    else:
        logger.warning("⚠️ 机器人连接失败，MCP 服务器将以离线模式运行")
    
    if args.transport == "http":
        logger.info(f"启动 HTTP 服务器: {args.host}:{args.port}")
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        logger.info("启动 stdio 服务器")
        mcp.run(transport="stdio")

