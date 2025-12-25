#!/usr/bin/env python
"""
摸鱼遥控车 Web HTTP 控制器

基于 lerobot lekiwi_http_controller 的完整移植版本，包含：
- 排队系统（用户会话管理）
- 手势控制
- 人脸控制
- 视频推流
- 机械臂控制
- MCP 服务支持（http/stdio 模式）
"""

import logging
import time
import sys
import os
import threading
import uuid

# 添加项目根目录到路径
if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '../../..'))
    sys.path.insert(0, project_root)

import cv2
from flask import Flask, jsonify, request, render_template, Response, make_response, redirect, url_for

# 导入拆分出的模块
from moyurobot.web.session import (
    session_manager, 
    SESSION_COOKIE_NAME, 
    USERNAME_COOKIE_NAME,
    SESSION_TIMEOUT_SECONDS,
    VIP_SESSION_TIMEOUT_SECONDS
)
from moyurobot.web import streaming

# 全局变量
app = None
service = None
logger = None
mcp = None  # MCP 服务实例

# 运动控制开关，默认关闭（监控模式）
_movement_enabled = False


def setup_routes():
    """设置HTTP路由"""
    global app, service, logger

    @app.route('/')
    def index():
        """主页面 - 提供简单的控制界面，仅允许一个活跃用户"""
        username = request.cookies.get(USERNAME_COOKIE_NAME)
        if not username:
            return redirect(url_for('login'))

        user_id = request.cookies.get(SESSION_COOKIE_NAME) or str(uuid.uuid4())
        
        # 尝试获取控制权
        if not session_manager.try_acquire_control(user_id, username, is_vip=False):
            # 需要排队
            wait_info = session_manager.get_waiting_info(username)
            return (
                render_template(
                    "waiting.html",
                    current_owner=wait_info["current_owner"],
                    waiting_users=wait_info["waiting_users"],
                    requesting_user=username,
                    remaining_seconds=wait_info["remaining_seconds"],
                    session_timeout=wait_info["session_timeout"],
                ),
                429,
                {"Content-Type": "text/html; charset=utf-8"},
            )

        response = make_response(render_template('index.html', username=username))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            user_id,
            max_age=24 * 3600,
            httponly=True,
            samesite='Lax'
        )
        return response

    @app.route('/vip', methods=['GET'])
    def vip():
        """VIP 页面 - 直接进入控制界面，无需等待，10分钟超时"""
        username = request.cookies.get(USERNAME_COOKIE_NAME)
        if not username:
            return redirect(url_for('login'))

        user_id = request.cookies.get(SESSION_COOKIE_NAME) or str(uuid.uuid4())
        
        # VIP 用户强制获取控制权
        session_manager.try_acquire_control(user_id, username, is_vip=True)

        response = make_response(render_template('index.html', username=username))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            user_id,
            max_age=24 * 3600,
            httponly=True,
            samesite='Lax'
        )
        return response

    @app.route('/wait', methods=['GET'])
    def wait():
        """等待页面 - 显示排队信息"""
        username = request.cookies.get(USERNAME_COOKIE_NAME)
        if not username:
            return redirect(url_for('login'))
        
        # 添加到等待列表
        session_manager.add_to_waiting_list(username)
        wait_info = session_manager.get_waiting_info(username)
        
        return render_template(
            "waiting.html",
            current_owner=wait_info["current_owner"],
            waiting_users=wait_info["waiting_users"],
            requesting_user=username,
            remaining_seconds=wait_info["remaining_seconds"],
            session_timeout=wait_info["session_timeout"],
        )
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """登录页面，要求输入用户名"""
        error = None
        if request.method == 'POST':
            username = (request.form.get('username') or '').strip()
            if not username:
                error = "用户名不能为空"
            else:
                resp = make_response(redirect(url_for('index')))
                resp.set_cookie(
                    USERNAME_COOKIE_NAME,
                    username,
                    max_age=24 * 3600,
                    httponly=False,
                    samesite='Lax'
                )
                return resp

        return render_template('login.html', error=error)

    @app.route('/exit_control', methods=['POST'])
    def exit_control():
        """退出控制 - 清除当前活跃用户，让其他人可以进入"""
        user_id = request.cookies.get(SESSION_COOKIE_NAME)
        username = request.cookies.get(USERNAME_COOKIE_NAME)
        
        if session_manager.release_control(user_id):
            logger.info(f"用户 {username} 已退出控制")
            return jsonify({
                "success": True,
                "message": "已退出控制"
            })
        else:
            return jsonify({
                "success": False,
                "message": "您不是当前活跃用户，无法退出控制"
            }), 403

    @app.route('/session_info', methods=['GET'])
    def session_info():
        """获取当前会话占用信息"""
        user_id = request.cookies.get(SESSION_COOKIE_NAME)
        return jsonify(session_manager.get_session_info(user_id))

    @app.route('/status', methods=['GET'])
    def get_status():
        """获取机器人状态"""
        if service is None:
            return jsonify({
                "connected": False,
                "movement_enabled": _movement_enabled,
                "message": "机器人服务未初始化"
            })
        
        status = service.get_status()
        status['movement_enabled'] = _movement_enabled
        return jsonify(status)

    @app.route('/startmove', methods=['GET', 'POST'])
    def start_move():
        """开启运动控制"""
        global _movement_enabled
        _movement_enabled = True
        logger.info("收到 /startmove 请求，已启用运动控制")
        return jsonify({
            "success": True,
            "message": "运动控制已启用"
        })

    @app.route('/stopmove', methods=['GET', 'POST'])
    def stop_move():
        """关闭运动控制"""
        global _movement_enabled
        _movement_enabled = False
        try:
            if service:
                service.stop_robot()
        except Exception:
            pass
        logger.info("收到 /stopmove 请求，已禁用运动控制")
        return jsonify({
            "success": True,
            "message": "运动控制已禁用"
        })

    @app.route('/control', methods=['POST'])
    def control_robot():
        """控制机器人移动"""
        global _movement_enabled
        
        if not _movement_enabled:
            return jsonify({
                "success": False,
                "message": "当前处于仅监控模式，找摸鱼管理员启用控制模式"
            })

        if service is None:
            return jsonify({
                "success": False,
                "message": "机器人服务未初始化"
            })

        try:
            data = request.get_json()
            if not data:
                return jsonify({
                    "success": False,
                    "message": "请求体不能为空"
                })

            # 处理预定义命令
            if "command" in data:
                duration = data.get("duration", 0)
                if duration > 0:
                    result = service.move_robot_for_duration(data["command"], duration)
                else:
                    result = service.execute_predefined_command(data["command"])
                return jsonify(result)
            
            # 处理机械臂位置控制
            elif any(key.endswith('.pos') for key in data.keys()):
                arm_positions = {k: v for k, v in data.items() if k.endswith('.pos')}
                result = service.set_arm_position(arm_positions)
                return jsonify(result)
            
            # 处理自定义速度
            elif any(key in data for key in ["x_vel", "y_vel", "theta_vel"]):
                duration = data.get("duration", 0)
                if duration > 0:
                    result = service.move_robot_with_custom_speed_for_duration(
                        data.get("x_vel", 0.0),
                        data.get("y_vel", 0.0),
                        data.get("theta_vel", 0.0),
                        duration
                    )
                else:
                    result = service.execute_custom_velocity(
                        data.get("x_vel", 0.0),
                        data.get("y_vel", 0.0),
                        data.get("theta_vel", 0.0)
                    )
                return jsonify(result)
            
            else:
                return jsonify({
                    "success": False,
                    "message": "无效的命令格式"
                })

        except Exception as e:
            logger.error(f"控制命令执行失败: {e}")
            return jsonify({
                "success": False,
                "message": str(e)
            })
    
    @app.route('/video_feed/<camera>')
    def video_feed(camera):
        """视频流端点"""
        def generate():
            """生成MJPEG视频流"""
            last_frame_time = 0
            min_interval = 0.1
            
            while True:
                try:
                    now = time.time()
                    if now - last_frame_time < min_interval:
                        time.sleep(0.01)
                        continue
                        
                    if service and service.robot and service.robot.is_connected and camera in service.robot.cameras:
                        try:
                            frame = service.robot.cameras[camera].async_read(timeout_ms=100)
                            if frame is not None and frame.size > 0:
                                height, width = frame.shape[:2]
                                new_width = int(width * 0.7)
                                new_height = int(height * 0.7)
                                frame_resized = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
                                
                                # RGB -> BGR
                                frame_encoded_ready = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
                                
                                ret, jpeg = cv2.imencode('.jpg', frame_encoded_ready, [cv2.IMWRITE_JPEG_QUALITY, 60])
                                if ret:
                                    jpeg_bytes = jpeg.tobytes()
                                    yield (b'--frame\r\n'
                                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg_bytes + b'\r\n')
                                    last_frame_time = time.time()
                                else:
                                    time.sleep(0.05)
                            else:
                                time.sleep(0.05)
                        except Exception as cam_e:
                            logger.debug(f"摄像头 {camera} 读取错误: {cam_e}")
                            time.sleep(0.1)
                    else:
                        time.sleep(0.1)
                except Exception as e:
                    logger.error(f"视频流错误: {e}")
                    time.sleep(0.1)
        
        return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')
    
    @app.route('/cameras')
    def get_cameras():
        """获取可用的摄像头列表"""
        cameras = []
        camera_status = {}
        
        if service and service.robot and service.robot.is_connected:
            for cam_name, cam in service.robot.cameras.items():
                try:
                    is_connected = cam.is_connected
                    test_frame = None
                    if is_connected:
                        try:
                            test_frame = cam.async_read(timeout_ms=100)
                            frame_available = test_frame is not None and test_frame.size > 0
                        except Exception as e:
                            frame_available = False
                            camera_status[cam_name] = str(e)
                    else:
                        frame_available = False
                        
                    cameras.append({
                        'name': cam_name,
                        'display_name': '前置摄像头' if cam_name == 'front' else '手腕摄像头',
                        'connected': is_connected,
                        'frame_available': frame_available,
                        'frame_shape': test_frame.shape if test_frame is not None else None
                    })
                    
                except Exception as e:
                    cameras.append({
                        'name': cam_name,
                        'display_name': '前置摄像头' if cam_name == 'front' else '手腕摄像头',
                        'connected': False,
                        'frame_available': False,
                        'error': str(e)
                    })
                    camera_status[cam_name] = str(e)
        
        return jsonify({
            'cameras': cameras, 
            'robot_connected': service.robot.is_connected if service and service.robot else False,
            'camera_status': camera_status
        })


def run_server(host="0.0.0.0", port=8080, robot_id="my_awesome_kiwi", mcp_mode=None, mcp_port=8000):
    """启动HTTP服务器，可选同时启动MCP服务
    
    Args:
        host: 服务器主机地址
        port: HTTP服务器端口
        robot_id: 机器人ID
        mcp_mode: MCP模式 ('http' 或 'stdio')，None表示不启用MCP
        mcp_port: MCP HTTP服务器端口（仅在mcp_mode='http'时有效）
    """
    global app, service, logger, mcp
    
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    template_dir = os.path.join(current_dir, 'templates')
    static_dir = os.path.join(current_dir, 'static')
    
    app = Flask(__name__, 
                template_folder=template_dir,
                static_folder=static_dir)
    
    # 设置 secret key（生产环境必须通过环境变量配置）
    app.secret_key = os.environ.get("FLASK_SECRET_KEY")
    if not app.secret_key:
        import secrets
        app.secret_key = secrets.token_hex(32)
        logger.warning("⚠️ 未配置 FLASK_SECRET_KEY，使用随机生成的密钥（重启后会话将失效）")
    
    # 创建服务实例
    try:
        from moyurobot.core.robot_service import create_default_service, set_global_service
        service = create_default_service(robot_id)
        set_global_service(service)
    except ImportError as e:
        logger.warning(f"无法导入机器人服务模块: {e}")
        service = None
    except Exception as e:
        logger.warning(f"创建机器人服务失败: {e}")
        service = None
    
    # 如果启用MCP模式，导入MCP服务
    if mcp_mode:
        try:
            from moyurobot.mcp.server import mcp as mcp_server
            mcp = mcp_server
            logger.info(f"MCP 服务已加载，模式: {mcp_mode}")
        except ImportError as e:
            logger.error(f"无法导入MCP服务模块: {e}")
            mcp = None
    
    # 设置路由
    setup_routes()
    
    logger.info(f"正在启动摸鱼遥控车 HTTP 控制器，地址: http://{host}:{port}")
    if mcp_mode:
        logger.info(f"MCP 模式: {mcp_mode}, MCP 端口: {mcp_port if mcp_mode == 'http' else 'N/A'}")
    
    # 启动时自动连接机器人
    if service:
        if service.connect():
            logger.info("✓ 机器人连接成功")
            # 连接成功后启动推流
            streaming.start_streaming(service)
        else:
            logger.warning("⚠️ 机器人连接失败，将以离线模式启动HTTP服务")
    
    logger.info("使用浏览器访问控制界面，或通过API发送控制命令")
    
    # 定义Flask运行函数
    def run_flask():
        try:
            app.run(
                host=host,
                port=port,
                debug=False,
                threaded=True,
                use_reloader=False
            )
        except Exception as e:
            logger.error(f"HTTP服务启动失败: {e}")

    try:
        if mcp_mode and mcp:
            # 在后台线程启动Flask
            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()
            
            # 在主线程运行MCP
            logger.info(f"Starting MCP server in {mcp_mode} mode")
            if mcp_mode == "http":
                mcp.run(transport="http", host=host, port=mcp_port)
            else:
                # stdio mode
                mcp.run(transport="stdio")
        else:
            # 仅运行Flask（主线程）
            run_flask()
            
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    finally:
        cleanup()


def cleanup():
    """清理资源"""
    global service
    streaming.stop_streaming()
    if service:
        service.disconnect()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="摸鱼遥控车 HTTP 控制器")
    parser.add_argument(
        "--robot-id", 
        type=str, 
        default="my_awesome_kiwi",
        help="机器人 ID 标识符（默认: my_awesome_kiwi）"
    )
    parser.add_argument(
        "--host", 
        type=str, 
        default="0.0.0.0",
        help="服务器主机地址（默认: 0.0.0.0）"
    )
    parser.add_argument(
        "--port", 
        type=int, 
        default=8080,
        help="服务器端口（默认: 8080）"
    )
    parser.add_argument(
        "--enable-stream",
        action="store_true",
        help="启用 RTMP 视频推流"
    )
    parser.add_argument(
        "--stream-url",
        type=str,
        default=None,
        help="RTMP 推流地址（也可通过 RTMP_STREAM_URL 环境变量配置）"
    )
    parser.add_argument(
        "--rotate-180",
        action="store_true",
        help="将推流画面旋转 180 度"
    )
    parser.add_argument(
        "--tuiliu",
        action="store_true",
        help="一键开启推流（使用默认配置）"
    )
    parser.add_argument(
        "--mcp-mode",
        type=str,
        choices=['stdio', 'http'],
        default=None,
        help="启用MCP服务器模式：stdio或http"
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=8000,
        help="MCP服务器HTTP端口（仅在mcp-mode=http时有效）"
    )
    
    args = parser.parse_args()
    
    # 应用推流配置（命令行参数优先于环境变量）
    if args.enable_stream or args.tuiliu:
        streaming.update_config(enabled=True)
    if args.stream_url:
        streaming.update_config(url=args.stream_url)
    if args.rotate_180:
        streaming.update_config(rotate=True)
    
    # 推流启用但未配置 URL 时警告
    if streaming.STREAMING_ENABLED and not streaming.STREAM_URL:
        print("⚠️ 推流已启用但未配置 RTMP_STREAM_URL，推流功能将无法使用")
    
    # 如果不是stdio模式，才打印欢迎信息到stdout
    # stdio模式下，stdout被用于MCP通信，不能打印杂乱信息
    if args.mcp_mode != 'stdio':
        print("=== 摸鱼遥控车 HTTP 控制器 ===")
        print(f"机器人 ID: {args.robot_id}")
        print(f"服务地址: http://{args.host}:{args.port}")
        if args.mcp_mode:
            print(f"MCP 模式: {args.mcp_mode}")
            if args.mcp_mode == 'http':
                print(f"MCP 端口: {args.mcp_port}")
        print("功能特性:")
        print("  - 网页控制界面")
        print("  - REST API 接口")
        print("  - 排队系统")
        print("  - 手势/人脸控制")
        print("  - 键盘控制支持")
        if args.mcp_mode:
            print("  - MCP 服务支持")
        if streaming.STREAMING_ENABLED:
            print("  - RTMP 视频推流")
        print("按 Ctrl+C 停止服务")
        print("=========================")
    
    try:
        run_server(args.host, args.port, args.robot_id, args.mcp_mode, args.mcp_port)
    except KeyboardInterrupt:
        if args.mcp_mode != 'stdio':
            print("\n收到键盘中断，正在关闭服务...")
    except Exception as e:
        if args.mcp_mode == 'stdio':
            import sys
            sys.stderr.write(f"启动失败: {e}\n")
        else:
            print(f"\n启动失败: {e}")

