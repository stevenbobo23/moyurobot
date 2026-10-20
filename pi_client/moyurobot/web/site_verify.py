#!/usr/bin/env python
"""平台域名校验文件（微信小程序业务域名 / 支付授权目录 / 各类站长校验）

微信等平台要求把形如 ``ChC4AVj1oJ.txt`` 的校验文件放在**站点根目录**并能直接访问，
内容必须一字不差。这里统一从 ``pi_client/public/`` 读，路由固定为 ``/<名字>.txt``：
以后换域名、换校验文件都只需把文件丢进 ``public/``，不用改代码。

安全约束：
- 只服务 ``public/`` 下**真实存在**的 ``.txt``，不列目录、不跟随越界路径；
- 文件名只允许字母、数字、``-``、``_``，且首字符必须是字母或数字，
  从根上排除 ``..``、斜杠和奇怪字符；
- 以 ``text/plain`` 返回，直接回原始字节，不经过文本编码转换。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from flask import abort, send_from_directory

logger = logging.getLogger(__name__)

# pi_client/moyurobot/web/site_verify.py -> pi_client/public
PUBLIC_DIR = Path(__file__).resolve().parents[2] / "public"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def resolve(name: str, base: Optional[Path] = None) -> Optional[Path]:
    """把请求里的名字换成 public 下的真实文件；不合法或不存在返回 None"""
    if not _NAME_RE.match(name or ""):
        return None
    directory = Path(base) if base is not None else PUBLIC_DIR
    candidate = directory / ("%s.txt" % name)
    try:
        if not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate


def list_files(base: Optional[Path] = None) -> list:
    """当前 public/ 里有哪些校验文件（日志与排查用）"""
    directory = Path(base) if base is not None else PUBLIC_DIR
    try:
        return sorted(p.name for p in directory.glob("*.txt") if p.is_file())
    except OSError:
        return []


def register(app, base: Optional[Path] = None) -> None:
    """挂到 Flask app 上"""
    directory = Path(base) if base is not None else PUBLIC_DIR

    @app.route("/<name>.txt")
    def site_verification(name):  # noqa: ANN001
        target = resolve(name, directory)
        if target is None:
            abort(404)
        # 直接回字节，避免任何换行/BOM 处理把校验串改掉
        return send_from_directory(str(target.parent), target.name,
                                  mimetype="text/plain")

    found = list_files(directory)
    if found:
        logger.info("域名校验文件已就绪（%s）: %s", directory, ", ".join(found))
    else:
        logger.info("域名校验文件目录为空（%s），把平台给的 .txt 丢进去即可", directory)
