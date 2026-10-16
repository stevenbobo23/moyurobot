#!/usr/bin/env python
"""加载 pi_client/.env

python-dotenv 一直是项目依赖，但此前只有 ``mcp/pipe.py`` 调过 ``load_dotenv()``。
网页服务（``python -m moyurobot.web.controller``）从未加载过它 —— 也就是说
README 里「cp config/env.example .env」这一步对网页服务一直是无效的，
``AUDIO_*``、API Key 之类的配置只有 shell / systemd 里 export 过的才生效。

统一从这里加载，路径写死到 pi_client/.env，避免受启动时工作目录影响。

注意：默认 ``override=False``，即**不覆盖**已有的环境变量 ——
systemd ``EnvironmentFile`` / shell 里显式 export 的值优先级更高。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 本文件在 pi_client/moyurobot/ 下，往上一层就是 pi_client/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_file_path() -> str:
    """pi_client/.env 的绝对路径（MOYU_ENV_FILE 可指向别的文件，方便测试）"""
    override = os.environ.get("MOYU_ENV_FILE", "").strip()
    return override or os.path.join(_PROJECT_ROOT, ".env")


def load_env(path: Optional[str] = None, *, override: bool = False) -> Optional[str]:
    """加载 .env，返回真正加载到的文件路径；没有可加载的文件时返回 None

    重复调用是安全的：已有环境变量不会被覆盖。
    """
    target = path or env_file_path()

    try:
        from dotenv import load_dotenv
    except ImportError:  # 缺依赖不该影响服务启动
        logger.debug("未安装 python-dotenv，跳过环境变量文件 %s", target)
        return None

    if not os.path.isfile(target):
        logger.debug("未找到环境变量文件 %s，跳过", target)
        return None

    try:
        load_dotenv(target, override=override)
    except Exception as e:
        logger.warning("加载环境变量文件失败 %s: %s", target, e)
        return None

    logger.info("已加载环境变量文件: %s", target)
    return target


__all__ = ["load_env", "env_file_path"]
