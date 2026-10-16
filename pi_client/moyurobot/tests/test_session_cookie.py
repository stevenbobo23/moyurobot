#!/usr/bin/env python
"""
会话 Cookie 参数测试（moyurobot/web/session.py 的 cookie_options）

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_session_cookie.py

背景：网页与音频服务是两个域名 / 两个端口，host-only 的 Cookie 不会跟着
WebSocket 握手跑到音频域名上，服务端就会判定"未登录"。这里锁住
SESSION_COOKIE_DOMAIN / SESSION_COOKIE_SECURE 的行为，避免哪天又被改回去。
"""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _PROJECT_ROOT)

_COOKIE_ENV = ("SESSION_COOKIE_DOMAIN", "SESSION_COOKIE_SECURE")


def _load_session():
    """按文件路径加载（session.py 只用标准库，不需要起 Flask）"""
    try:
        from moyurobot.web import session as module  # noqa: F401

        return module
    except ImportError:
        path = os.path.join(_PROJECT_ROOT, "moyurobot", "web", "session.py")
        spec = importlib.util.spec_from_file_location("moyurobot_web_session", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


session = _load_session()

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))


def with_env(**values):
    """临时设置环境变量，返回恢复函数"""
    old = {k: os.environ.get(k) for k in _COOKIE_ENV}

    def restore():
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    for key in _COOKIE_ENV:
        os.environ.pop(key, None)
    for key, value in values.items():
        os.environ[key] = value
    return restore


def test_default():
    restore = with_env()
    try:
        opts = session.cookie_options()
        check("默认不带 domain（host-only，IP 访问也对）", "domain" not in opts, str(opts))
        check("默认不带 secure", "secure" not in opts, str(opts))
        check("默认 httponly + Lax + 24h",
              opts.get("httponly") is True and opts.get("samesite") == "Lax"
              and opts.get("max_age") == 24 * 3600, str(opts))

        opts = session.cookie_options(http_only=False)
        check("用户名 Cookie 可被 JS 读取（httponly=False）", opts.get("httponly") is False, str(opts))
    finally:
        restore()


def test_cross_domain():
    restore = with_env(SESSION_COOKIE_DOMAIN=".mycodebro.cn")
    try:
        opts = session.cookie_options()
        check("跨域名场景带上父域 domain", opts.get("domain") == ".mycodebro.cn", str(opts))
        check("cookie_domain() 与 cookie_options() 一致",
              session.cookie_domain() == ".mycodebro.cn", session.cookie_domain())
    finally:
        restore()

    restore = with_env(SESSION_COOKIE_DOMAIN="  .mycodebro.cn  ")
    try:
        check("domain 前后空格被去掉", session.cookie_options().get("domain") == ".mycodebro.cn",
              str(session.cookie_options()))
    finally:
        restore()

    restore = with_env(SESSION_COOKIE_DOMAIN="   ")
    try:
        check("只写空白视作未设置", "domain" not in session.cookie_options(), str(session.cookie_options()))
    finally:
        restore()


def test_secure_flag():
    restore = with_env(SESSION_COOKIE_SECURE="1")
    try:
        check("SESSION_COOKIE_SECURE=1 -> secure=True",
              session.cookie_options().get("secure") is True, str(session.cookie_options()))
    finally:
        restore()

    restore = with_env(SESSION_COOKIE_SECURE="0")
    try:
        check("SESSION_COOKIE_SECURE=0 -> 不带 secure",
              "secure" not in session.cookie_options(), str(session.cookie_options()))
    finally:
        restore()


def main():
    print("=== 会话 Cookie 参数测试 ===")
    test_default()
    test_cross_domain()
    test_secure_flag()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n=== 结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        print("失败项:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
