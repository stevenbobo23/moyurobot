#!/usr/bin/env python3
"""
Web HTTP 控制器

提供基于 Web 的机器人控制界面，支持：
- 手柄遥控
- 手势控制
- 人脸追踪
- 视频推流
"""

import os
import sys
import json
import time
import logging
import threading
import base64
from pathlib import Path
from typing import Optional, Dict, Any
from functools import wraps

import cv2
import numpy as np
from flask import Flask, Response, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('MoYuWeb')

# ============== 全局变量 ==============
robot_service = None
latest_frames = {}  # 缓存各摄像头的最新帧
frame_lock = threading.Lock()

# 速度控制
current_speed_index = 1  # 默认中速
speed_levels = [
    {"xy": 0.1, "theta": 30, "name": "慢速"},
    {"xy": 0.2, "theta": 60, "name": "中速"},
    {"xy": 0.3, "theta": 90, "name": "快速"},
]


def create_app(service=None, config: Optional[Dict[str, Any]] = None) -> Flask:
    """
    创建 Flask 应用
    
    Args:
        service: 机器人服务实例
        config: 应用配置
    
    Returns:
        Flask 应用实例
    """
    global robot_service
    robot_service = service
    
    # 获取模板和静态文件目录
    base_dir = Path(__file__).parent
    template_dir = base_dir / "templates"
    static_dir = base_dir / "static"
    
    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir)
    )
    
    # 应用配置
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "moyu_robot_secret_key")
    
    # 启用 CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # 认证配置
    auth_password = os.environ.get("WEB_PASSWORD", "moyu123")
    
    def require_auth(f):
        """需要认证的装饰器"""
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("authenticated"):
                if request.is_json:
                    return jsonify({"error": "未授权"}), 401
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated
    
    # ============== 页面路由 ==============
    
    @app.route("/")
    @require_auth
    def index():
        """主控制页面"""
        return render_template("index.html")
    
    @app.route("/login", methods=["GET", "POST"])
    def login():
        """登录页面"""
        if request.method == "POST":
            password = request.form.get("password", "")
            if password == auth_password:
                session["authenticated"] = True
                return redirect(url_for("index"))
            return render_template("login.html", error="密码错误")
        return render_template("login.html")
    
    @app.route("/logout")
    def logout():
        """登出"""
        session.pop("authenticated", None)
        return redirect(url_for("login"))
    
    @app.route("/waiting")
    def waiting():
        """等待页面（机器人连接中）"""
        return render_template("waiting.html")
    
    # ============== API 路由 ==============
    
    @app.route("/api/status")
    @require_auth
    def api_status():
        """获取机器人状态"""
        if robot_service is None:
            return jsonify({
                "connected": False,
                "message": "机器人服务未初始化"
            })
        
        try:
            status = robot_service.get_status()
            return jsonify({
                "connected": True,
                "status": status
            })
        except Exception as e:
            logger.error(f"获取状态失败: {e}")
            return jsonify({
                "connected": False,
                "message": str(e)
            })
    
    @app.route("/api/move", methods=["POST"])
    @require_auth
    def api_move():
        """控制移动"""
        global current_speed_index
        
        if robot_service is None:
            return jsonify({"error": "机器人服务未初始化"}), 503
        
        data = request.get_json()
        direction = data.get("direction", "stop")
        
        speed = speed_levels[current_speed_index]
        
        try:
            # 根据方向计算速度
            velocities = {
                "forward": (speed["xy"], 0, 0),
                "backward": (-speed["xy"], 0, 0),
                "left": (0, speed["xy"], 0),
                "right": (0, -speed["xy"], 0),
                "rotate_left": (0, 0, speed["theta"]),
                "rotate_right": (0, 0, -speed["theta"]),
                "stop": (0, 0, 0),
            }
            
            x, y, theta = velocities.get(direction, (0, 0, 0))
            robot_service.move(x, y, theta)
            
            return jsonify({
                "success": True,
                "direction": direction,
                "speed": speed["name"]
            })
        except Exception as e:
            logger.error(f"移动控制失败: {e}")
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/arm", methods=["POST"])
    @require_auth
    def api_arm():
        """控制机械臂"""
        if robot_service is None:
            return jsonify({"error": "机器人服务未初始化"}), 503
        
        data = request.get_json()
        action = data.get("action", "")
        
        try:
            if action == "reset":
                robot_service.reset_arm()
            elif action == "open_gripper":
                robot_service.set_gripper(100)
            elif action == "close_gripper":
                robot_service.set_gripper(0)
            else:
                return jsonify({"error": f"未知动作: {action}"}), 400
            
            return jsonify({
                "success": True,
                "action": action
            })
        except Exception as e:
            logger.error(f"机械臂控制失败: {e}")
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/speed", methods=["POST"])
    @require_auth
    def api_speed():
        """设置速度"""
        global current_speed_index
        
        data = request.get_json()
        action = data.get("action", "")
        
        if action == "increase":
            current_speed_index = min(current_speed_index + 1, len(speed_levels) - 1)
        elif action == "decrease":
            current_speed_index = max(current_speed_index - 1, 0)
        elif action == "set":
            level = data.get("level", 1)
            current_speed_index = max(0, min(level, len(speed_levels) - 1))
        
        return jsonify({
            "success": True,
            "speed": speed_levels[current_speed_index]["name"],
            "level": current_speed_index
        })
    
    @app.route("/api/video/<camera_name>")
    @require_auth
    def video_feed(camera_name: str):
        """视频流端点"""
        def generate():
            while True:
                with frame_lock:
                    frame = latest_frames.get(camera_name)
                
                if frame is not None:
                    # 编码为 JPEG
                    _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
                else:
                    # 生成占位图
                    placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
                    cv2.putText(placeholder, f"Camera: {camera_name}", (50, 240),
                               cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                    _, jpeg = cv2.imencode('.jpg', placeholder)
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
                
                time.sleep(0.033)  # ~30 FPS
        
        return Response(
            generate(),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )
    
    @app.route("/api/gesture", methods=["POST"])
    @require_auth
    def api_gesture():
        """处理手势控制"""
        if robot_service is None:
            return jsonify({"error": "机器人服务未初始化"}), 503
        
        data = request.get_json()
        gesture = data.get("gesture", "")
        landmarks = data.get("landmarks", [])
        
        try:
            # 根据手势执行动作
            gesture_actions = {
                "open_palm": lambda: robot_service.move(0, 0, 0),  # 停止
                "fist": lambda: robot_service.set_gripper(0),  # 关闭夹爪
                "pointing_up": lambda: robot_service.move(0.2, 0, 0),  # 前进
                "thumbs_up": lambda: robot_service.set_gripper(100),  # 打开夹爪
            }
            
            action = gesture_actions.get(gesture)
            if action:
                action()
                return jsonify({
                    "success": True,
                    "gesture": gesture
                })
            else:
                return jsonify({
                    "success": False,
                    "message": f"未知手势: {gesture}"
                })
        except Exception as e:
            logger.error(f"手势控制失败: {e}")
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/face_track", methods=["POST"])
    @require_auth  
    def api_face_track():
        """处理人脸追踪"""
        if robot_service is None:
            return jsonify({"error": "机器人服务未初始化"}), 503
        
        data = request.get_json()
        face_center = data.get("center", {})
        frame_size = data.get("frame_size", {"width": 640, "height": 480})
        
        try:
            # 计算偏移量并转换为运动指令
            center_x = face_center.get("x", 0.5)
            center_y = face_center.get("y", 0.5)
            
            # 转换为 -1 到 1 的范围
            offset_x = (center_x - 0.5) * 2
            offset_y = (center_y - 0.5) * 2
            
            # 根据偏移量控制旋转（追踪人脸）
            angular_speed = -offset_x * 30  # 左右追踪
            
            robot_service.move(0, 0, angular_speed)
            
            return jsonify({
                "success": True,
                "offset": {"x": offset_x, "y": offset_y},
                "angular_speed": angular_speed
            })
        except Exception as e:
            logger.error(f"人脸追踪失败: {e}")
            return jsonify({"error": str(e)}), 500
    
    return app


def update_frame(camera_name: str, frame: np.ndarray):
    """更新摄像头帧缓存"""
    global latest_frames
    with frame_lock:
        latest_frames[camera_name] = frame.copy()


def run_server(host: str = "0.0.0.0", port: int = 8080, service=None, 
               debug: bool = False, connect_robot: bool = True):
    """
    运行 Web 服务器
    
    Args:
        host: 监听地址
        port: 监听端口
        service: 机器人服务实例（如果为 None 则自动创建）
        debug: 是否启用调试模式
        connect_robot: 是否自动连接机器人
    """
    # 如果没有提供服务实例，创建默认服务
    if service is None and connect_robot:
        try:
            from moyurobot.core.robot_service import create_default_service, set_global_service
            
            # 从环境变量获取 robot_id，默认使用 "my_awesome_kiwi"（与 lerobot 原始校准文件匹配）
            robot_id = os.environ.get("ROBOT_ID", "my_awesome_kiwi")
            
            logger.info(f"创建机器人服务 (robot_id={robot_id})...")
            service = create_default_service(robot_id=robot_id)
            set_global_service(service)
            
            # 尝试连接机器人
            logger.info("正在连接机器人...")
            if service.connect():
                logger.info("✓ 机器人连接成功")
            else:
                logger.warning("⚠️ 机器人连接失败，将以离线模式启动")
                
        except ImportError as e:
            logger.warning(f"无法导入机器人服务模块: {e}")
            logger.warning("将以离线模式启动（仅 Web 界面）")
        except Exception as e:
            logger.warning(f"机器人服务初始化失败: {e}")
            logger.warning("将以离线模式启动")
    
    app = create_app(service=service)
    
    logger.info(f"启动 Web 服务器: http://{host}:{port}")
    # 禁用 reloader 避免重复连接机器人
    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)


if __name__ == "__main__":
    # 测试运行
    run_server(debug=True, connect_robot=True)

