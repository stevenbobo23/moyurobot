#!/usr/bin/env python
"""
会话管理模块

提供用户会话和排队系统管理
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Cookie 名称
SESSION_COOKIE_NAME = "moyu_user_id"
USERNAME_COOKIE_NAME = "moyu_username"

# 会话超时配置（从环境变量读取）
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "100"))
VIP_SESSION_TIMEOUT_SECONDS = int(os.environ.get("VIP_SESSION_TIMEOUT_SECONDS", "600"))


@dataclass
class ActiveUser:
    """活跃用户信息"""
    id: Optional[str] = None
    username: Optional[str] = None
    start_time: float = 0.0
    is_vip: bool = False


class SessionManager:
    """会话管理器
    
    管理用户会话、排队和控制权限
    """
    
    def __init__(self):
        self._active_user = ActiveUser()
        self._waiting_users: List[str] = []
        self._lock = threading.Lock()
    
    def get_timeout_seconds(self) -> int:
        """获取当前活跃用户的超时时间"""
        if self._active_user.is_vip:
            return VIP_SESSION_TIMEOUT_SECONDS
        return SESSION_TIMEOUT_SECONDS
    
    def is_session_active(self) -> bool:
        """检查当前会话是否有效"""
        with self._lock:
            if self._active_user.id is None:
                return False
            
            elapsed = time.time() - self._active_user.start_time
            timeout = self.get_timeout_seconds()
            return elapsed < timeout
    
    def get_remaining_seconds(self) -> int:
        """获取当前会话剩余时间（秒）"""
        with self._lock:
            if self._active_user.id is None:
                return 0
            
            elapsed = time.time() - self._active_user.start_time
            timeout = self.get_timeout_seconds()
            remaining = timeout - elapsed
            return max(0, int(remaining))
    
    def is_active_user(self, user_id: str) -> bool:
        """检查指定用户是否是当前活跃用户"""
        with self._lock:
            return (
                self._active_user.id == user_id and
                self.is_session_active()
            )
    
    def try_acquire_control(self, user_id: str, username: str, is_vip: bool = False) -> bool:
        """尝试获取控制权
        
        Args:
            user_id: 用户会话ID
            username: 用户名
            is_vip: 是否是VIP用户
            
        Returns:
            True 如果成功获取控制权，False 如果需要排队
        """
        with self._lock:
            now = time.time()
            
            # 检查当前会话是否有效
            if self._active_user.id is not None:
                elapsed = now - self._active_user.start_time
                timeout = VIP_SESSION_TIMEOUT_SECONDS if self._active_user.is_vip else SESSION_TIMEOUT_SECONDS
                
                if elapsed < timeout and self._active_user.id != user_id:
                    # 会话有效且不是当前用户，需要排队
                    if username and username not in self._waiting_users:
                        self._waiting_users.append(username)
                    return False
            
            # 可以获取控制权
            self._active_user.id = user_id
            self._active_user.username = username
            self._active_user.start_time = now
            self._active_user.is_vip = is_vip
            
            # 从等待列表中移除
            if username in self._waiting_users:
                self._waiting_users.remove(username)
            
            return True
    
    def release_control(self, user_id: str) -> bool:
        """释放控制权
        
        Args:
            user_id: 用户会话ID
            
        Returns:
            True 如果成功释放，False 如果不是当前用户
        """
        with self._lock:
            if self._active_user.id != user_id:
                return False
            
            username = self._active_user.username
            
            self._active_user.id = None
            self._active_user.username = None
            self._active_user.start_time = 0.0
            self._active_user.is_vip = False
            
            if username and username in self._waiting_users:
                self._waiting_users.remove(username)
            
            return True
    
    def get_session_info(self, user_id: str) -> Dict:
        """获取会话信息
        
        Args:
            user_id: 用户会话ID
            
        Returns:
            会话信息字典
        """
        with self._lock:
            now = time.time()
            
            has_active = self._active_user.id is not None
            elapsed = now - self._active_user.start_time if has_active else 0
            timeout = self.get_timeout_seconds()
            is_active = has_active and elapsed < timeout
            
            waiting_view = [
                u for u in self._waiting_users 
                if u != self._active_user.username
            ]
            
            is_current_user = is_active and user_id == self._active_user.id
            remaining = timeout - elapsed if is_current_user else 0
            
            return {
                "is_active_user": bool(is_current_user),
                "remaining_seconds": max(0, int(remaining)),
                "current_owner": self._active_user.username if is_active else None,
                "session_timeout": timeout,
                "is_vip": self._active_user.is_vip if is_active else False,
                "waiting_users": waiting_view,
            }
    
    def get_waiting_info(self, username: str) -> Dict:
        """获取等待信息
        
        Args:
            username: 用户名
            
        Returns:
            等待信息字典
        """
        with self._lock:
            now = time.time()
            
            has_active = self._active_user.id is not None
            elapsed = now - self._active_user.start_time if has_active else 0
            timeout = self.get_timeout_seconds()
            is_active = has_active and elapsed < timeout
            
            waiting_view = [
                u for u in self._waiting_users 
                if u != self._active_user.username
            ]
            
            remaining = max(0, int(timeout - elapsed)) if is_active else 0
            
            return {
                "current_owner": self._active_user.username if is_active else None,
                "waiting_users": waiting_view,
                "remaining_seconds": remaining,
                "session_timeout": timeout,
            }
    
    def add_to_waiting_list(self, username: str):
        """添加用户到等待列表"""
        with self._lock:
            if username and username not in self._waiting_users:
                self._waiting_users.append(username)
    
    @property
    def active_username(self) -> Optional[str]:
        """获取当前活跃用户名"""
        return self._active_user.username
    
    @property
    def active_user_id(self) -> Optional[str]:
        """获取当前活跃用户ID"""
        return self._active_user.id


# 全局会话管理器实例
session_manager = SessionManager()

