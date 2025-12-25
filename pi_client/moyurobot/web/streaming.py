#!/usr/bin/env python
"""
视频推流模块

提供 RTMP 视频推流功能
"""

import logging
import os
import subprocess
import threading
import time
from typing import Optional

import cv2

logger = logging.getLogger(__name__)

# 推流配置（从环境变量读取）
STREAMING_ENABLED = os.environ.get("STREAMING_ENABLED", "false").lower() == "true"
STREAM_URL = os.environ.get("RTMP_STREAM_URL", "")
STREAM_ROTATE_180 = os.environ.get("STREAM_ROTATE_180", "false").lower() == "true"

# 推流状态
_stream_process: Optional[subprocess.Popen] = None
_stream_thread: Optional[threading.Thread] = None
_stream_running = False
_stream_lock = threading.Lock()


def start_streaming(robot_service):
    """启动视频推流
    
    Args:
        robot_service: 机器人服务实例，用于获取摄像头
    """
    global _stream_process, _stream_thread, _stream_running
    
    if not STREAMING_ENABLED:
        logger.info("推流功能已禁用，跳过启动")
        return
    
    if not STREAM_URL:
        logger.warning("未配置 RTMP_STREAM_URL，无法启动推流")
        return
    
    with _stream_lock:
        if _stream_running:
            logger.info("推流已在运行中")
            return
        
        if not robot_service or not robot_service.robot.is_connected:
            logger.warning("机器人未连接，无法启动推流")
            return
        
        if 'wrist' not in robot_service.robot.cameras:
            logger.warning("手腕摄像头不可用，无法启动推流")
            return
        
        _stream_running = True
    
    def stream_worker():
        global _stream_process, _stream_running
        try:
            camera = robot_service.robot.cameras['wrist']
            
            # 获取摄像头参数
            sample_frame = camera.async_read(timeout_ms=1000)
            if sample_frame is None:
                logger.error("无法获取摄像头帧")
                return
            
            height, width = sample_frame.shape[:2]
            fps = 15
            
            # ffmpeg 命令
            ffmpeg_cmd = [
                'ffmpeg',
                '-y',
                '-f', 'rawvideo',
                '-vcodec', 'rawvideo',
                '-pix_fmt', 'bgr24',
                '-s', f'{width}x{height}',
                '-r', str(fps),
                '-i', '-',
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-f', 'flv',
                STREAM_URL
            ]
            
            logger.info(f"启动推流: {STREAM_URL[:50]}...")
            _stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            frame_interval = 1.0 / fps
            last_frame_time = 0
            
            while _stream_running:
                try:
                    now = time.time()
                    if now - last_frame_time < frame_interval:
                        time.sleep(0.001)
                        continue
                    
                    frame = camera.async_read(timeout_ms=100)
                    if frame is not None:
                        # RGB -> BGR
                        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                        
                        if STREAM_ROTATE_180:
                            frame_bgr = cv2.rotate(frame_bgr, cv2.ROTATE_180)
                        
                        _stream_process.stdin.write(frame_bgr.tobytes())
                        last_frame_time = now
                    else:
                        time.sleep(0.01)
                        
                except Exception as e:
                    logger.error(f"推流帧错误: {e}")
                    break
                    
        except Exception as e:
            logger.error(f"推流线程错误: {e}")
        finally:
            stop_streaming()
    
    _stream_thread = threading.Thread(target=stream_worker, daemon=True)
    _stream_thread.start()
    logger.info("推流线程已启动")


def stop_streaming():
    """停止视频推流"""
    global _stream_process, _stream_running
    
    with _stream_lock:
        _stream_running = False
        
        if _stream_process:
            try:
                _stream_process.stdin.close()
                _stream_process.terminate()
                _stream_process.wait(timeout=5)
            except Exception:
                pass
            _stream_process = None
            
    logger.info("推流已停止")


def is_streaming() -> bool:
    """检查是否正在推流"""
    return _stream_running


def update_config(enabled: bool = None, url: str = None, rotate: bool = None):
    """更新推流配置
    
    Args:
        enabled: 是否启用推流
        url: RTMP 推流地址
        rotate: 是否旋转180度
    """
    global STREAMING_ENABLED, STREAM_URL, STREAM_ROTATE_180
    
    if enabled is not None:
        STREAMING_ENABLED = enabled
    if url is not None:
        STREAM_URL = url
    if rotate is not None:
        STREAM_ROTATE_180 = rotate

