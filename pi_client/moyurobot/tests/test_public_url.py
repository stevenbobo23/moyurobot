#!/usr/bin/env python
"""
对外 ws 地址解析测试（moyurobot/public_url.py）

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_public_url.py

覆盖：
1. 反代/多域名场景：AUDIO_WS_PUBLIC_URL 写浏览器地址（https://x/）也能用
2. 只写域名时自动补 /audio，且不会自作主张补 :8081
3. 显式端口、显式路径原样保留；多余的尾部斜杠去掉
4. {host} / {port} / {path} 占位符按当前请求替换
5. 没配置时按当前访问方式推导（局域网 http / 反代 https / 自签 https 带端口）
6. auto / none / - / 留空 = 不配置；协议不认识时退回推导而不是崩掉
"""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _PROJECT_ROOT)


def _load_public_url():
    """按文件路径加载，避免连带拉起 numpy/lerobot（本模块只用标准库）"""
    try:
        from moyurobot import public_url as module  # noqa: F401
        return module
    except ImportError:
        path = os.path.join(_PROJECT_ROOT, "moyurobot", "public_url.py")
        spec = importlib.util.spec_from_file_location("moyurobot_public_url", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


public_url = _load_public_url()

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))


class FakeRequest:
    """只带 host / is_secure 的假请求，够用且不用起 Flask"""

    def __init__(self, host="", secure=False):
        self.host = host
        self.is_secure = secure
        self.headers = {"Host": host}


def test_user_scenario():
    """网页在 lerobot.mycodebro.cn，音频走 lerobotwws.mycodebro.cn"""
    page = FakeRequest("lerobot.mycodebro.cn", secure=True)
    cases = [
        ("wss://lerobotwws.mycodebro.cn/audio", "wss://lerobotwws.mycodebro.cn/audio"),
        ("https://lerobotwws.mycodebro.cn/", "wss://lerobotwws.mycodebro.cn/audio"),
        ("https://lerobotwws.mycodebro.cn", "wss://lerobotwws.mycodebro.cn/audio"),
        ("lerobotwws.mycodebro.cn", "wss://lerobotwws.mycodebro.cn/audio"),
        ("lerobotwws.mycodebro.cn:443/audio", "wss://lerobotwws.mycodebro.cn:443/audio"),
    ]
    for raw, want in cases:
        url, source = public_url.resolve_ws_url(raw, request=page, port=8081)
        check(f"配置 {raw!r} -> {want}", url == want and source == "config", f"实际 {url} ({source})")


def test_trailing_slash_and_placeholder():
    page = FakeRequest("robot.example.com:8080", secure=False)
    url, _ = public_url.resolve_ws_url("wss://audio.example.com/audio/", request=page)
    check("多余尾部斜杠被去掉", url == "wss://audio.example.com/audio", url)

    url, _ = public_url.resolve_ws_url("wss://{host}/audio", request=page)
    check("{host} 占位符替换成网页域名", url == "wss://robot.example.com/audio", url)

    url, _ = public_url.resolve_ws_url(
        "wss://audio.example.com:{port}{path}", request=page, port=9000, path="/audio"
    )
    check("{port}/{path} 占位符可用", url == "wss://audio.example.com:9000/audio", url)


def test_auto_derivation():
    # 局域网 http：音频端口和网页端口不同，必须带 :8081
    url, source = public_url.resolve_ws_url("", request=FakeRequest("192.168.1.50:8080"))
    check("局域网 http -> ws://ip:8081/audio", url == "ws://192.168.1.50:8081/audio", url)
    check("来源标记为 auto", source == "auto", source)

    # 反代 https（标准端口）：再拼 :8081 是错的，按同域同路径推导
    url, _ = public_url.resolve_ws_url("", request=FakeRequest("lerobot.mycodebro.cn", secure=True))
    check("反代 https -> wss://域名/audio", url == "wss://lerobot.mycodebro.cn/audio", url)

    # 自签 https 直接暴露 8443/8081：保留音频监听端口
    url, _ = public_url.resolve_ws_url("", request=FakeRequest("192.168.1.50:8443", secure=True))
    check("自签 https 带端口 -> wss://ip:8081/audio", url == "wss://192.168.1.50:8081/audio", url)

    # 请求都拿不到时兜底本机
    url, _ = public_url.resolve_ws_url("", request=None)
    check("没有请求信息时兜底 127.0.0.1", url == "ws://127.0.0.1:8081/audio", url)


def test_disable_tokens_and_errors():
    page = FakeRequest("lerobot.mycodebro.cn", secure=True)
    for raw in ("", "  ", "auto", "AUTO", "none", "-", "default"):
        url, source = public_url.resolve_ws_url(raw, request=page)
        check(f"{raw!r} 视为未配置", source == "auto" and url == "wss://lerobot.mycodebro.cn/audio", url)

    # 协议写错不能崩，退回自动推导
    url, source = public_url.resolve_ws_url("ftp://audio.example.com/audio", request=page)
    check("协议不认识时退回自动推导", source == "auto" and url.startswith("wss://lerobot."), url)

    # 只给域名的话，协议跟着网页走
    url, _ = public_url.resolve_ws_url("audio.example.com", request=FakeRequest("10.0.0.5:8080"))
    check("裸域名按网页协议推导", url == "ws://audio.example.com/audio", url)


def test_host_port_split():
    check("拆分 host:port", public_url.split_host_port("a.com:8080") == ("a.com", 8080))
    check("拆分纯域名", public_url.split_host_port("a.com") == ("a.com", None))
    check("拆分 IPv6", public_url.split_host_port("[::1]:8080") == ("[::1]", 8080))
    check("空串安全", public_url.split_host_port("") == ("", None))


def main():
    print("=== 对外 ws 地址解析测试 ===")
    test_user_scenario()
    test_trailing_slash_and_placeholder()
    test_auto_derivation()
    test_disable_tokens_and_errors()
    test_host_port_split()

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
