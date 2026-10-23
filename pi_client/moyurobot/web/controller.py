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
import ssl
import threading
import uuid

# 添加项目根目录到路径
if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, '../../..'))
    sys.path.insert(0, project_root)

import cv2
from flask import Flask, jsonify, request, render_template, Response, make_response, redirect, url_for

# 先加载 pi_client/.env：下面这些模块在 import 阶段就会读环境变量
# （session 的会话超时、音频配置等），必须赶在它们前面
from moyurobot.env_file import load_env

load_env()

# 导入拆分出的模块
from moyurobot.web.session import (
    session_manager, 
    SESSION_COOKIE_NAME, 
    USERNAME_COOKIE_NAME,
    SESSION_TIMEOUT_SECONDS,
    VIP_SESSION_TIMEOUT_SECONDS,
    cookie_options,
)
from moyurobot.web import streaming
from moyurobot.web import audio_ws
from moyurobot.web import site_verify
from moyurobot.logging_setup import setup_logging
from moyurobot.greeting import worker as greeting_worker

# 全局变量
app = None
service = None
logger = None
mcp = None  # MCP 服务实例

# 运动控制开关，默认关闭（监控模式）
_movement_enabled = False


def _audio_authorize(user_id, username):
    """音频 WebSocket 的鉴权：登录后可听；当前控制者可说"""
    if not username:
        return {
            "ok": False,
            "reason": "未登录：请先进入控制台页面再使用语音对讲",
        }
    active = session_manager.is_session_active()
    is_owner = bool(user_id) and session_manager.active_user_id == user_id
    can_talk = bool(is_owner or not active)
    return {
        "ok": True,
        "username": username,
        "can_monitor": True,
        "can_talk": can_talk,
        "reason": "" if can_talk else f"当前控制权在 {session_manager.active_username} 手上",
    }


def _index_context(username, user_id=None):
    """渲染控制台所需的额外上下文（音频对讲配置 + 连接令牌）

    ``user_id`` 要由调用方把**路由里实际用的那个**传进来：首页第一次请求时响应头里的
    Set-Cookie 还没落到浏览器上，``request.cookies`` 里没有它，按 Cookie 取值会签出
    一个不带会话 ID 的令牌，用户就只能听、不能说。
    """
    try:
        audio_config = audio_ws.build_client_config(
            request, username=username, user_id=user_id or request.cookies.get(SESSION_COOKIE_NAME)
        )
    except Exception as e:
        logger.warning(f"构建音频配置失败: {e}")
        audio_config = {
            "available": False,
            "running": False,
            "ws_url": "",
            "sample_rate": 16000,
            "chunk_ms": 100,
            "error": str(e),
        }
    return {
        "username": username,
        "audio_config": audio_config,
        "audio_ws_url": audio_config.get("ws_url") or "",
        "audio_sample_rate": audio_config.get("sample_rate", 16000),
    }


def setup_routes():
    """设置HTTP路由"""
    global app, service, logger

    # 平台域名校验文件（/<名字>.txt ← pi_client/public/），必须挂在根路径
    site_verify.register(app)

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

        response = make_response(render_template('index.html', **_index_context(username, user_id)))
        response.set_cookie(SESSION_COOKIE_NAME, user_id, **cookie_options())
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

        response = make_response(render_template('index.html', **_index_context(username, user_id)))
        response.set_cookie(SESSION_COOKIE_NAME, user_id, **cookie_options())
        return response

    @app.route('/sim', methods=['GET'])
    def sim():
        """机械臂仿真页 - 只读可视化 + 主臂 Web Serial 联动，不占用排队席位

        往从臂推指令同样走 /control，所以真要让从臂动，仍然需要在控制台拿到
        控制权并启用运动控制。
        """
        username = request.cookies.get(USERNAME_COOKIE_NAME)
        if not username:
            return redirect(url_for('login'))
        return make_response(render_template('sim.html'))

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
                # 用户名 Cookie 允许 JS 读取，故不开 httponly；domain/secure 与其它 Cookie 保持一致
                resp.set_cookie(USERNAME_COOKIE_NAME, username, **cookie_options(http_only=False))
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
    
    # ---------- 预设动作编排 ----------

    def _get_action_player():
        """懒创建全局 ActionPlayer（依赖 service）"""
        if service is None:
            return None
        try:
            from moyurobot.core.actions import get_action_player
            return get_action_player(service)
        except Exception as e:
            logger.error(f"创建动作播放器失败: {e}")
            return None

    @app.route('/actions', methods=['GET'])
    def list_actions():
        """列出所有预设动作"""
        player = _get_action_player()
        if player is None:
            return jsonify({"success": False, "actions": [], "message": "机器人服务未初始化"})
        return jsonify({
            "success": True,
            "actions": player.list_actions(),
            "playing": player.get_state(),
        })

    @app.route('/action/run', methods=['POST'])
    def run_action():
        """执行预设动作"""
        if not _movement_enabled:
            return jsonify({
                "success": False,
                "message": "当前处于仅监控模式，找摸鱼管理员启用控制模式"
            })
        player = _get_action_player()
        if player is None:
            return jsonify({"success": False, "message": "机器人服务未初始化"})
        try:
            data = request.get_json(silent=True) or {}
            action_id = data.get("action_id")
            if not action_id:
                return jsonify({"success": False, "message": "缺少 action_id 参数"})
            result = player.play(action_id)
            return jsonify(result)
        except Exception as e:
            logger.error(f"执行预设动作失败: {e}")
            return jsonify({"success": False, "message": str(e)})

    @app.route('/action/stop', methods=['POST'])
    def stop_action():
        """急停当前正在执行的预设动作（机械臂保持当前位置）"""
        player = _get_action_player()
        if player is None:
            return jsonify({"success": False, "message": "机器人服务未初始化"})
        try:
            result = player.stop(wait=False)
            result["playing"] = player.get_state()
            return jsonify(result)
        except Exception as e:
            logger.error(f"停止预设动作失败: {e}")
            return jsonify({"success": False, "message": str(e)})

    # ---------- 语音对讲 ----------

    @app.route('/audio/info', methods=['GET'])
    def audio_info():
        """音频对讲能力探测：后端、设备、WebSocket 地址（已带登录令牌）、权限"""
        username = request.cookies.get(USERNAME_COOKIE_NAME)
        user_id = request.cookies.get(SESSION_COOKIE_NAME)
        try:
            info = audio_ws.build_client_config(request, username=username, user_id=user_id)
        except Exception as e:
            return jsonify({"available": False, "error": str(e)})
        server = audio_ws.get_server()
        hub = None
        try:
            from moyurobot.core.audio import get_hub
            hub = get_hub()
        except Exception:
            hub = None
        auth = _audio_authorize(user_id, username)
        info["capabilities"] = hub.capabilities() if hub else {}
        info["permissions"] = {
            "can_monitor": auth.get("can_monitor", False),
            "can_talk": auth.get("can_talk", False),
            "reason": auth.get("reason", ""),
            "logged_in": bool(username),
        }
        info["server"] = server.describe_state() if server else {"running": False}
        response = jsonify(info)
        # ws_url 里带着登录令牌，别让浏览器/反代把它缓存下来再发给别人
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route('/greeting/status')
    def greeting_status():
        """自动打招呼状态：检测器、语音来源、当前状态与最近事件"""
        greeter = greeting_worker.get_greeter()
        if greeter is None:
            return jsonify({
                "enabled": False,
                "running": False,
                "error": "自动打招呼未启动（可能用的是 --no-greeting）",
            })
        response = jsonify(greeter.status())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route('/greeting/enabled', methods=['POST'])
    def greeting_set_enabled():
        """运行时开关。写在内存里，重启后回到配置文件的默认值。"""
        greeter = greeting_worker.get_greeter()
        if greeter is None:
            return jsonify({"success": False, "error": "自动打招呼未启动"}), 503
        payload = request.get_json(silent=True) or {}
        if "enabled" not in payload:
            return jsonify({"success": False, "error": "缺少 enabled 字段"}), 400
        result = greeter.set_enabled(bool(payload["enabled"]))
        return jsonify(result)

    @app.route('/greeting/test', methods=['POST'])
    def greeting_test():
        """立刻打一次招呼（不看画面、不看时段、不占冷却），方便现场调试"""
        greeter = greeting_worker.get_greeter()
        if greeter is None:
            return jsonify({"success": False, "error": "自动打招呼未启动"}), 503
        payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or "").strip() or None
        result = greeter.trigger_now(text)
        return jsonify(result)

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
                            # 前置摄像头可能被自动打招呼的检测器一起用，走共享读取器避免互相偷帧
                            broker = greeting_worker.get_frame_broker(camera)
                            if broker is not None:
                                frame = broker.get_frame(max_age_s=min_interval * 0.5, timeout_ms=200)
                            else:
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


def _build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """构建 HTTPS 用的 SSLContext

    两件事同时处理：

    1. **truststore 污染**（``NoneType.get_unverified_chain`` 的真凶）。
       有些环境（pip-system-certs、某些发行版的 sitecustomize、容器镜像）会
       调用 ``truststore.inject_into_ssl()``，把 ``ssl.SSLContext`` 整体换成
       truststore 的包装类。那个类是在**客户端**场景设计的：握手完要去校验
       对端证书链，而服务端根本没有对端证书，内部一路取到 None，最后爆在
       ``sslobj.get_unverified_chain()`` —— 就是
       ``'NoneType' object has no attribute 'get_unverified_chain'``。
       这里从 MRO 取回标准库实现，临时替换 ``ssl.SSLContext`` 建好上下文，
       建完立刻还原，让进程里其它客户端（requests/urllib3）继续吃系统信任库。
       生效的那份经常是 pip 的 vendored 副本
       （``pip._vendor.truststore._api``）而不是顶层 truststore 包，所以检测
       必须用子串判断，不能写死等值比较。

    2. **不走 werkzeug 的元组路径**。传 ``(cert, key)`` 时 werkzeug 会自己
       wrap 一层 ``_SSLContext`` 子类接管 wrap_socket；直接给 SSLContext 实例
       走的是标准库路径，少一层变数。
    """
    log = logger or logging.getLogger(__name__)

    for label, path in (("证书", cert_path), ("私钥", key_path)):
        if not path:
            raise ValueError(f"{label}路径为空")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{label}文件不存在: {path}")

    # truststore 是否把 ssl.SSLContext 换掉了。
    # 注意不能写死成 == "truststore"：实际生效的那份往往来自 pip 的 vendored
    # 副本，模块名是 pip._vendor.truststore._api，只能用子串判断。
    patched_cls = ssl.SSLContext
    patched_by = getattr(patched_cls, "__module__", "") or ""
    plain_cls = None
    if "truststore" in patched_by:
        # truststore 的 SSLContext 在 CPython 下继承自标准库那个类，从 MRO 取回原版。
        # 光拿类还不够——ssl.py 里 SSLContext 的属性 setter 是按**模块全局名**
        # SSLContext 解析的，全局名还是包装类时赋值会无限递归（minimum_version
        # 必踩），所以必须同时把 ssl.SSLContext 这个名字临时换回原版。
        for base in patched_cls.__mro__[1:]:
            if getattr(base, "__module__", "") == "ssl":
                plain_cls = base
                break
        if plain_cls is None:
            log.warning("ssl.SSLContext 被 %s 替换，但 MRO 里找不到标准库原版", patched_by)

    try:
        if plain_cls is not None:
            ssl.SSLContext = plain_cls
            log.warning(
                "ssl.SSLContext 已被 %s 替换（truststore 注入），服务端临时切回标准库实现",
                patched_by,
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert_path, key_path)
    finally:
        if plain_cls is not None:
            ssl.SSLContext = patched_cls  # 还原，客户端继续用系统信任库

    # 把 SAN 和有效期打出来：局域网访问提示证书不受信任，十次有九次是
    # 访问用的 IP 不在 SAN 里（脚本按 IP 生成，换了 IP 就得重新签）
    try:
        info = ssl._ssl._test_decode_cert(cert_path)  # noqa: SLF001 私有 API，解不出来不影响启动
        san = [v for k, v in info.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
        log.info("HTTPS 证书 SAN: %s", ", ".join(san) or "(空，浏览器会校验证书失败)")
        log.info("HTTPS 证书有效期: %s ~ %s", info.get("notBefore"), info.get("notAfter"))
    except Exception as e:  # noqa: BLE001
        log.debug("证书信息解析失败（不影响启动）: %s", e)

    return ctx


def run_server(host="0.0.0.0", port=8080, robot_id="my_awesome_kiwi", mcp_mode=None, mcp_port=8000,
               audio_enabled=True, audio_port=None, ssl_cert=None, ssl_key=None,
               greeting_enabled=True):
    """启动HTTP服务器，可选同时启动MCP服务
    
    Args:
        host: 服务器主机地址
        port: HTTP服务器端口
        robot_id: 机器人ID
        mcp_mode: MCP模式 ('http' 或 'stdio')，None表示不启用MCP
        mcp_port: MCP HTTP服务器端口（仅在mcp_mode='http'时有效）
        audio_enabled: 是否启动语音对讲 WebSocket 服务
        audio_port: 语音对讲 WebSocket 端口（默认取 AUDIO_WS_PORT 或 8081）
        ssl_cert / ssl_key: 可选，启用 HTTPS（浏览器要麦克风权限必须走安全上下文）
        greeting_enabled: 是否启动自动打招呼（检测到人就说话 + 点头）
    """
    global app, service, logger, mcp
    
    # 统一日志配置：同一条消息去重 + 指数退避重报 + 单 logger 限速，
    # 避免 lerobot 摄像头在后台线程里按帧率刷屏把 SD 卡写满。
    setup_logging()
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
    
    # 启动语音对讲服务（独立端口，不影响 HTTP/MCP）
    if audio_enabled:
        try:
            server = audio_ws.start_audio_server(
                auth_fn=_audio_authorize,
                host=None,   # 默认监听 0.0.0.0，可用 AUDIO_WS_HOST 覆盖
                port=audio_port,
            )
            if server:
                st = server.describe_state()
                logger.info(
                    "✓ 语音对讲已就绪: ws://%s:%s%s（后端=%s）",
                    st["host"], st["port"], st["path"], audio_ws.get_config().backend,
                )
            else:
                err = audio_ws.get_server()
                logger.warning(
                    "⚠️ 语音对讲未启动: %s（可用 --no-audio 或 AUDIO_ENABLED=0 关闭）",
                    getattr(err, "start_error", "未知原因"),
                )
        except Exception as e:
            logger.error(f"语音对讲启动失败: {e}")
    else:
        logger.info("语音对讲已禁用（--no-audio）")

    # 启动自动打招呼（与语音对讲共用喇叭，内部有独占仲裁）
    if greeting_enabled:
        try:
            greeter = greeting_worker.start_greeter(service)
            if greeter is not None:
                st = greeter.status()
                logger.info(
                    "✓ 自动打招呼已就绪：%s（检测器=%s，语音=%s，时段=%s）",
                    "启用" if st["enabled"] else "未启用，可在网页打开",
                    st["detector"].get("backend", "none"),
                    st["speech"].get("resolved", "none"),
                    st["config"].get("active_hours") or "全天",
                )
        except Exception as e:
            logger.error(f"自动打招呼启动失败: {e}")
    else:
        logger.info("自动打招呼已禁用（--no-greeting）")
    
    # 定义Flask运行函数
    ssl_context = None
    if ssl_cert and ssl_key:
        try:
            ssl_context = _build_ssl_context(ssl_cert, ssl_key)
            logger.info("HTTPS 已启用（Web Serial / 麦克风等需要安全上下文的能力可用）")
        except Exception:
            # 证书坏了不该连控制台一起打不开：降级回 HTTP，但把原因打全
            logger.exception("加载证书失败，降级为 HTTP 启动")
            logger.warning("HTTP 模式下 Web Serial、麦克风、剪贴板等 API 会被浏览器屏蔽")
            ssl_context = None
    
    def run_flask():
        try:
            app.run(
                host=host,
                port=port,
                debug=False,
                threaded=True,
                use_reloader=False,
                ssl_context=ssl_context
            )
        except Exception:
            logger.exception("HTTP服务启动失败")

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
    try:
        greeting_worker.stop_greeter()
    except Exception as e:
        logger.warning(f"关闭自动打招呼失败: {e}")
    try:
        audio_ws.stop_audio_server()
    except Exception as e:
        logger.warning(f"关闭语音对讲失败: {e}")
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
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="禁用语音对讲（监听车端声音 / 按住说话）"
    )
    parser.add_argument(
        "--no-greeting",
        action="store_true",
        help="禁用自动打招呼（检测到人后说话 + 点头）"
    )
    parser.add_argument(
        "--audio-port",
        type=int,
        default=None,
        help="语音对讲 WebSocket 端口（默认 8081，也可用 AUDIO_WS_PORT 配置）"
    )
    parser.add_argument(
        "--ssl-cert",
        type=str,
        default=None,
        help="HTTPS 证书路径（与 --ssl-key 同时提供；浏览器申请麦克风权限需要安全上下文）"
    )
    parser.add_argument(
        "--ssl-key",
        type=str,
        default=None,
        help="HTTPS 私钥路径"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help="日志级别（DEBUG/INFO/WARNING/ERROR），默认取环境变量 LOG_LEVEL"
    )
    parser.add_argument(
        "--log-file",
        action="store_true",
        help="同时写入滚动日志文件（LOG_DIR/LOG_FILE，默认 ~/logs/moyurobot.log，单文件上限 LOG_MAX_BYTES）"
    )
    
    args = parser.parse_args()

    # 日志配置：命令行参数优先，其余取环境变量（LOG_LEVEL / LOG_DEDUP_WINDOW_S / ...）
    setup_logging(
        level=args.log_level,
        log_to_file=True if args.log_file else None,
        force=bool(args.log_level or args.log_file),
    )
    
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
        if not args.no_audio:
            print(f"  - 语音对讲（监听/对讲，WebSocket 端口 {args.audio_port or '8081'}）")
        if not args.no_greeting:
            print("  - 自动打招呼（有人进入视野 → 说话 + 点头）")
        print("按 Ctrl+C 停止服务")
        print("=========================")
    
    try:
        run_server(
            args.host, args.port, args.robot_id, args.mcp_mode, args.mcp_port,
            audio_enabled=not args.no_audio,
            audio_port=args.audio_port,
            ssl_cert=args.ssl_cert,
            ssl_key=args.ssl_key,
            greeting_enabled=not args.no_greeting,
        )
    except KeyboardInterrupt:
        if args.mcp_mode != 'stdio':
            print("\n收到键盘中断，正在关闭服务...")
    except Exception as e:
        if args.mcp_mode == 'stdio':
            import sys
            sys.stderr.write(f"启动失败: {e}\n")
        else:
            print(f"\n启动失败: {e}")

