#!/usr/bin/env python
"""
音频连接令牌测试（moyurobot/audio_token.py + 握手鉴权）

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_audio_token.py

背景：网页 (lerobot.mycodebro.cn) 和音频服务 (lerobotwws.mycodebro.cn) 是两个域名，
会话 Cookie 是 host-only 的、属于网页域名，浏览器不会把它带到音频域名的 WebSocket
握手上 —— 服务端读不到用户名就回「未登录」。把身份签成令牌放进 ws 地址的查询串，
是唯一不依赖跨域 Cookie 的做法。这里锁住令牌的签发/校验/过期/防篡改，以及握手侧
「令牌优先、Cookie 兜底」的取值顺序。
"""

import importlib.util
import os
import sys
import types
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _PROJECT_ROOT)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))


def _load_by_path(name, relative):
    path = os.path.join(_PROJECT_ROOT, relative)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_light(relative_module: str):
    """加载只用标准库的模块；重型模块回退到按路径加载"""
    try:
        return importlib.import_module(f"moyurobot.{relative_module}")
    except ImportError:
        return _load_by_path(f"moyurobot_{relative_module}", f"moyurobot/{relative_module}.py")


def _load_audio_stack():
    """加载 audio_ws + 它的依赖

    本机（Mac）没有 numpy/lerobot，直接 import moyurobot.core 会连带拉起它们，
    于是这里给 moyurobot.core / moyurobot.web 塞轻量包桩，再按路径加载真正用到的模块。
    """
    for name, rel in (("moyurobot.core", "moyurobot/core"), ("moyurobot.web", "moyurobot/web")):
        pkg = types.ModuleType(name)
        pkg.__path__ = [os.path.join(_PROJECT_ROOT, rel)]  # type: ignore[attr-defined]
        sys.modules[name] = pkg

    audio = _load_by_path("moyurobot.core.audio", "moyurobot/core/audio.py")
    audio_ws = _load_by_path("moyurobot.web.audio_ws", "moyurobot/web/audio_ws.py")
    return audio, audio_ws


audio_token = _load_light("audio_token")
public_url = _load_light("public_url")

audio_mod, audio_ws = _load_audio_stack()


# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #


class FakeWS:
    """只提供握手能拿到的信息：请求路径 + 请求头"""

    def __init__(self, path="/audio", cookie=None):
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        self.request = types.SimpleNamespace(path=path, headers=headers)
        self.remote_address = ("192.168.1.9", 51234)


class FakeHub:
    backend = "sim"
    listener_count = 0

    def __init__(self, config):
        self.config = config

    def state(self):
        return {}

    def capabilities(self):
        return {"capture_devices": [], "playback_devices": []}


class FakeRequest:
    def __init__(self, host="lerobot.mycodebro.cn", secure=True):
        self.host = host
        self.is_secure = secure
        self.headers = {"Host": host}


def make_server(auth_fn=None):
    cfg = audio_mod.AudioConfig.from_env({"ws_public_url": "", "enabled": True})
    return audio_ws.AudioWebSocketServer(hub=FakeHub(cfg), config=cfg, auth_fn=auth_fn)


def recording_auth(seen):
    """记录服务端最终拿到的 (user_id, username)"""
    def _fn(user_id, username):
        seen.append((user_id, username))
        if not username:
            return {"ok": False, "reason": "未登录：请先进入控制台页面再使用语音对讲"}
        return {"ok": True, "can_monitor": True, "can_talk": True, "reason": ""}
    return _fn


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #


def test_mint_verify_roundtrip():
    token = audio_token.mint("阿爆", "sess-1")
    claims = audio_token.verify(token)
    check("签发后可校验通过", claims is not None, str(claims))
    check(
        "用户名/会话 ID 原样带回来",
        bool(claims) and claims["username"] == "阿爆" and claims["user_id"] == "sess-1",
        str(claims),
    )
    check("令牌是三段式 v1 前缀", token.startswith("v1.") and token.count(".") == 2, token[:20] + "…")

    no_id = audio_token.verify(audio_token.mint("bob"))
    check("会话 ID 可省略", bool(no_id) and no_id["username"] == "bob" and no_id["user_id"] is None)

    check("未登录不签发令牌", audio_token.mint(None) == "" and audio_token.mint("") == "")


def test_tamper_and_garbage():
    token = audio_token.mint("alice", "s1")
    version, body, signature = token.split(".")

    check("改签名验不过", audio_token.verify(f"{version}.{body}.{'A' * len(signature)}") is None)
    check("改负载验不过", audio_token.verify(f"{version}.{body[:-2]}xx.{signature}") is None)
    check("换版本号验不过", audio_token.verify(f"v2.{body}.{signature}") is None)
    check("空串验不过", audio_token.verify("") is None and audio_token.verify(None) is None)
    check("乱码验不过", audio_token.verify("not-a-token") is None)

    # 自己拿同一密钥重签一个别的用户，签名是对的 —— 但密钥只在服务端，改不出别的
    other = audio_token.verify(audio_token.mint("bob", "s2"))
    check("不同用户令牌互相隔离", bool(other) and other["username"] == "bob")


def test_expiry():
    now = 1_700_000_000.0
    token = audio_token.mint("alice", "s1", ttl=60, now=now)

    check("有效期内可用", audio_token.verify(token, now=now + 30) is not None)
    check("过期即失效", audio_token.verify(token, now=now + 61) is None)
    check("刚好到期边界视为过期", audio_token.verify(token, now=now + 60) is None)

    default_ttl = audio_token.token_ttl()
    check("默认有效期为正", default_ttl > 0, f"{default_ttl / 3600:.1f} 小时")


def test_secret():
    os.environ["AUDIO_TOKEN_SECRET"] = "shared-secret"
    try:
        check("配了密钥时来源为 env", audio_token.secret_source() == "env")
        a = audio_token.mint("alice", "s1")
        check("同一密钥可互相校验", audio_token.verify(a) is not None)

        os.environ["AUDIO_TOKEN_SECRET"] = "another-secret"
        check("换密钥后旧令牌失效", audio_token.verify(a) is None)
    finally:
        os.environ.pop("AUDIO_TOKEN_SECRET", None)

    check("没配密钥时来源为 ephemeral", audio_token.secret_source() == "ephemeral")
    b = audio_token.mint("alice", "s1")
    check("进程内随机密钥同样可自洽校验", audio_token.verify(b) is not None)


def test_token_from_path():
    token = audio_token.mint("alice", "s1")
    cases = [
        (f"/audio?token={token}", token, "标准写法"),
        ("/audio", "", "没有查询串"),
        ("/audio?", "", "空查询串"),
        ("/audio?token=", "", "空值"),
        ("/audio?other=1", "", "别的参数"),
        (f"/audio?other=1&token={token}", token, "混在其它参数里"),
        ("", "", "空路径"),
        (None, "", "None"),
    ]
    for path, want, desc in cases:
        got = audio_token.token_from_path(path)
        check(f"路径取令牌：{desc}", got == want, f"{path!r} -> {got[:14]}…" if got else f"{path!r} -> {got!r}")


def test_append_query():
    token = "v1.abc.def"
    check(
        "无查询串用 ?",
        public_url.append_query("wss://a.com/audio", "token", token) == f"wss://a.com/audio?token={token}",
    )
    check(
        "已有查询串用 &",
        public_url.append_query("wss://a.com/audio?x=1", "token", token) == f"wss://a.com/audio?x=1&token={token}",
    )
    check("值为空时原样返回", public_url.append_query("wss://a.com/audio", "token", "") == "wss://a.com/audio")
    check("空地址原样返回", public_url.append_query("", "token", token) == "")
    check(
        "保留锚点位置正确",
        public_url.append_query("wss://a.com/audio#frag", "token", token) == f"wss://a.com/audio?token={token}#frag",
    )


def test_handshake_token_priority():
    """握手侧：令牌优先，Cookie 兜底"""
    seen = []
    server = make_server(auth_fn=recording_auth(seen))

    token = audio_token.mint("阿爆", "sess-token")
    ws = FakeWS(f"/audio?token={token}")
    auth = server._authorize(ws, raw_path=f"/audio?token={token}")
    check("带令牌即可通过", auth.get("ok") is True, str(auth.get("reason", "")))
    check("身份取自令牌", seen[-1] == ("sess-token", "阿爆"), str(seen[-1]))
    check("来源标记为 token", auth.get("auth_source") == "token", str(auth.get("auth_source")))

    # 两个域名的核心场景：没有 Cookie，只有令牌
    ws_nocookie = FakeWS(f"/audio?token={token}")
    check(
        "跨域名无 Cookie 也能过（本次问题的正解）",
        server._authorize(ws_nocookie, raw_path=f"/audio?token={token}").get("ok") is True,
    )

    # 同域/局域网：老页面不带令牌，退回 Cookie
    ws_cookie = FakeWS("/audio", cookie="moyu_user_id=sess-cookie; moyu_username=bob")
    auth = server._authorize(ws_cookie, raw_path="/audio")
    check("无令牌时退回 Cookie", seen[-1] == ("sess-cookie", "bob"), str(seen[-1]))
    check("来源标记为 cookie", auth.get("auth_source") == "cookie", str(auth.get("auth_source")))

    # 令牌过期 → 回退 Cookie（同一浏览器另一域名仍可用）
    expired = audio_token.mint("阿爆", "sess-token", ttl=-1)
    auth = server._authorize(ws_cookie, raw_path=f"/audio?token={expired}")
    check("过期令牌不拦截 Cookie 回退", seen[-1] == ("sess-cookie", "bob"), str(seen[-1]))

    # 都没有 → 未登录
    auth = server._authorize(FakeWS("/audio"), raw_path="/audio")
    check("都没有则判未登录", auth.get("ok") is False and "未登录" in auth.get("reason", ""), str(auth))
    check("未登录时来源为 none", auth.get("auth_source") == "none", str(auth.get("auth_source")))


def test_build_client_config_carries_token():
    server = make_server()
    req = FakeRequest("lerobot.mycodebro.cn")

    info = server.build_client_config(req, username="阿爆", user_id="sess-1")
    check(
        "ws_url 指向配置的音频域名",
        info["ws_url"].startswith("wss://lerobot.mycodebro.cn/audio?"),
        info["ws_url"].replace(audio_token.token_from_path(info["ws_url"]), "<令牌>"),
    )
    claims = audio_token.verify(audio_token.token_from_path(info["ws_url"]))
    check("ws_url 里的令牌可校验且身份一致",
          bool(claims) and claims["username"] == "阿爆" and claims["user_id"] == "sess-1", str(claims))
    check("展示用地址不含令牌", "?" not in info["ws_url_base"], info["ws_url_base"])
    check("标记已登录", info["logged_in"] is True)

    anon = server.build_client_config(req)
    check("未登录不签发令牌", anon["logged_in"] is False and "?" not in anon["ws_url"], anon["ws_url"])

    # 服务没起来时也要能签令牌，否则页面先连上会一直撞「未登录」
    audio_ws._server = None
    stopped = audio_ws.build_client_config(req, username="阿爆", user_id="sess-1")
    check(
        "音频服务未启动时仍带令牌",
        stopped["running"] is False
        and audio_token.verify(audio_token.token_from_path(stopped["ws_url"])) is not None,
        stopped["ws_url"].replace(audio_token.token_from_path(stopped["ws_url"]), "<令牌>"),
    )


def main():
    print("=== 音频连接令牌测试 ===")
    test_mint_verify_roundtrip()
    test_tamper_and_garbage()
    test_expiry()
    test_secret()
    test_token_from_path()
    test_append_query()
    test_handshake_token_priority()
    test_build_client_config_carries_token()

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
