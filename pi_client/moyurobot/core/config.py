#!/usr/bin/env python
"""
配置管理模块

提供应用配置的加载和管理功能
"""

import os
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CameraConfig:
    """摄像头配置"""
    name: str
    device_name_or_path: str  # 设备名称（如 "USB Camera"）或路径（如 "/dev/video0"）
    fps: int = 30
    width: int = 640
    height: int = 480
    rotate_180: bool = False


@dataclass
class RobotServiceConfig:
    """机器人服务配置"""
    robot_id: str = "moyu_robot"
    # 移动速度配置 (m/s 和 deg/s)
    linear_speed: float = 0.2
    angular_speed: float = 30.0
    # 机械臂舵机速度配置 (0.0-1.0，相对于最大速度的百分比)
    arm_servo_speed: float = 0.2
    # 机械臂扭矩限制 (0-1000)
    arm_torque_limit: int = 600
    # 安全配置
    command_timeout_s: float = 6.0
    max_loop_freq_hz: int = 30
    # 摄像头配置（设备名称，用于自动查找设备路径）
    front_camera_name: str = "T1 Webcam"
    wrist_camera_name: str = "USB Camera"


@dataclass
class WebServerConfig:
    """Web 服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8080
    session_timeout_seconds: int = 100
    vip_session_timeout_seconds: int = 600


@dataclass
class MCPConfig:
    """MCP 服务配置"""
    enabled: bool = True
    transport: str = "http"  # "stdio" 或 "http"
    host: str = "0.0.0.0"
    port: int = 8000
    endpoint_url: Optional[str] = None  # WebSocket 端点 URL


@dataclass
class StreamingConfig:
    """推流配置"""
    enabled: bool = False
    rtmp_url: str = ""
    rotate_180: bool = False


@dataclass
class AppConfig:
    """应用总配置"""
    robot: RobotServiceConfig = field(default_factory=RobotServiceConfig)
    web: WebServerConfig = field(default_factory=WebServerConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    cameras: Dict[str, CameraConfig] = field(default_factory=dict)
    log_dir: str = "/home/bobo/logs"
    
    def __post_init__(self):
        # 默认摄像头配置
        if not self.cameras:
            self.cameras = {
                "front": CameraConfig(
                    name="front",
                    device_name_or_path="T1 Webcam",
                    rotate_180=False
                ),
                "wrist": CameraConfig(
                    name="wrist", 
                    device_name_or_path="USB Camera",
                    rotate_180=True
                )
            }


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """加载配置文件
    
    Args:
        config_path: 配置文件路径，如果不指定则使用默认配置
        
    Returns:
        AppConfig 实例
    """
    if config_path and Path(config_path).exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
            
            # 解析配置
            robot_config = RobotServiceConfig(**config_data.get("robot", {}))
            web_config = WebServerConfig(**config_data.get("web", {}))
            mcp_config = MCPConfig(**config_data.get("mcp", {}))
            streaming_config = StreamingConfig(**config_data.get("streaming", {}))
            
            # 解析摄像头配置
            cameras = {}
            for cam_name, cam_data in config_data.get("cameras", {}).items():
                cameras[cam_name] = CameraConfig(**cam_data)
            
            return AppConfig(
                robot=robot_config,
                web=web_config,
                mcp=mcp_config,
                streaming=streaming_config,
                cameras=cameras if cameras else None,
                log_dir=config_data.get("log_dir", "/home/bobo/logs")
            )
            
        except Exception as e:
            logger.warning(f"加载配置文件失败: {e}，使用默认配置")
    
    return AppConfig()


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent.parent.parent


def get_config_dir() -> Path:
    """获取配置目录"""
    return get_project_root() / "config"

