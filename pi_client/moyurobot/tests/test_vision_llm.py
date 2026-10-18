#!/usr/bin/env python
"""视觉模型层测试：provider 选择 / 请求构造 / 响应解析 / 错误与回退

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_vision_llm.py

不联网：``requests.post`` 用假响应替换，只钉死最容易改坏的部分
（Key 优先级、payload 结构、回退顺序、错误文案）。
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from moyurobot import vision_llm  # noqa: E402

MANAGED_ENV = (
    "VISION_PROVIDER",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_VISION_MODEL",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_VISION_MODEL",
)

_passed = 0
_failed = []


def check(name, cond, detail=""):
    global _passed
    if cond:
        _passed += 1
        print("  ✓ %s" % name)
    else:
        _failed.append(name)
        print("  ✗ %s %s" % (name, detail))


class EnvPatch:
    """清空相关变量，逐个设置"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in MANAGED_ENV}
        for key in MANAGED_ENV:
            os.environ.pop(key, None)
        for key, value in self.kwargs.items():
            os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key in MANAGED_ENV:
            os.environ.pop(key, None)
            if self.saved.get(key) is not None:
                os.environ[key] = self.saved[key]
        return False


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def ok_payload(text="画面里有一顶帐篷"):
    return {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 123}}


class FakePost:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def with_post(fake):
    vision_llm.requests.post = fake


# ---------------------------------------------------------------- provider 选择

def test_provider_order():
    print("[provider 选择]")
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds"):
        check("只配 DeepSeek → deepseek", vision_llm.provider_order() == ["deepseek"])

    with EnvPatch(QWEN_API_KEY="sk-qw"):
        check("只配千问 → dashscope", vision_llm.provider_order() == ["dashscope"])

    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", QWEN_API_KEY="sk-qw"):
        check("两者都有 → DeepSeek 排第一",
              vision_llm.provider_order() == ["deepseek", "dashscope"],
              vision_llm.provider_order())

    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", QWEN_API_KEY="sk-qw", VISION_PROVIDER="dashscope"):
        check("强制 dashscope 生效", vision_llm.provider_order() == ["dashscope"])

    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", VISION_PROVIDER="deepseek"):
        check("强制 deepseek 生效", vision_llm.provider_order() == ["deepseek"])

    with EnvPatch():
        check("都没有 → 空列表", vision_llm.provider_order() == [])
        check("available() 为 False", vision_llm.available() is False)

    with EnvPatch(DEEPSEEK_KEY="sk-alias"):
        check("DEEPSEEK_KEY 别名可用", vision_llm.provider_order() == ["deepseek"])

    with EnvPatch(DASHSCOPE_API_KEY="sk-alias"):
        check("DASHSCOPE_API_KEY 别名可用", vision_llm.provider_order() == ["dashscope"])

    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", VISION_PROVIDER="openai"):
        check("未知 provider 退化成 auto", vision_llm.provider_order() == ["deepseek"])


def test_urls_and_models():
    print("[地址与模型名]")
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds"):
        check("默认 base 拼 /chat/completions",
              vision_llm.deepseek_url() == "https://api.deepseek.com/chat/completions")
        check("默认模型是 deepseek-flash", vision_llm.deepseek_model() == "deepseek-flash")

    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", DEEPSEEK_BASE_URL="https://proxy.example.com/"):
        check("尾斜杠不会拼出 //",
              vision_llm.deepseek_url() == "https://proxy.example.com/chat/completions")

    with EnvPatch(DEEPSEEK_API_KEY="sk-ds",
                  DEEPSEEK_BASE_URL="https://api.deepseek.com/chat/completions"):
        check("已经写全路径就不重复拼",
              vision_llm.deepseek_url() == "https://api.deepseek.com/chat/completions")

    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", DEEPSEEK_VISION_MODEL="deepseek-v4-flash"):
        check("模型名可覆盖", vision_llm.deepseek_model() == "deepseek-v4-flash")

    with EnvPatch(QWEN_API_KEY="sk-qw"):
        check("千问默认模型 qwen-vl-plus", vision_llm.dashscope_model() == "qwen-vl-plus")


# ---------------------------------------------------------------- 请求构造

def test_analyze_deepseek_payload():
    print("[DeepSeek 请求构造]")
    fake = FakePost([FakeResponse(200, ok_payload())])
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds"):
        with_post(fake)
        result = vision_llm.analyze_image(b"\xff\xd8fakejpeg", "图里有什么")

    call = fake.calls[0]
    payload = call["json"]
    content = payload["messages"][0]["content"]
    check("打到 DeepSeek 官方端点",
          call["url"] == "https://api.deepseek.com/chat/completions", call["url"])
    check("Authorization 用 Bearer", call["headers"]["Authorization"] == "Bearer sk-ds")
    check("model=deepseek-flash", payload["model"] == "deepseek-flash", payload["model"])
    check("图片块在前、文字块在后",
          content[0]["type"] == "image_url" and content[1]["type"] == "text")
    check("图片用 data URL 内联",
          content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
    check("文字块是原始提问", content[1]["text"] == "图里有什么")
    check("返回 answer", result["answer"] == "画面里有一顶帐篷")
    check("返回 provider", result["provider"] == "deepseek")
    check("返回 model", result["model"] == "deepseek-flash")
    check("透传 usage", result["usage"].get("total_tokens") == 123)
    check("带耗时字段", isinstance(result["elapsed_ms"], int))


def test_content_blocks():
    print("[响应解析]")
    payload = {"choices": [{"message": {"content": [
        {"type": "text", "text": "帐篷"},
        {"type": "text", "text": "和折叠椅"},
    ]}}]}
    fake = FakePost([FakeResponse(200, payload)])
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds"):
        with_post(fake)
        result = vision_llm.analyze_image(b"x", "q")
    check("块数组 content 能拼起来", result["answer"] == "帐篷和折叠椅", result["answer"])

    fake = FakePost([FakeResponse(200, {"choices": []})])
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds"):
        with_post(fake)
        try:
            vision_llm.analyze_image(b"x", "q")
            check("空 choices 应报错", False)
        except vision_llm.VisionError as exc:
            check("空 choices 报错", "没有文本内容" in str(exc), str(exc))


def test_empty_input():
    print("[输入校验]")
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds"):
        try:
            vision_llm.analyze_image(b"", "q")
            check("空图片应报错", False)
        except vision_llm.VisionError as exc:
            check("空图片报错", "图片数据为空" in str(exc))

        fake = FakePost([FakeResponse(200, ok_payload())])
        with_post(fake)
        vision_llm.analyze_image(b"x", "   ")
        text_block = fake.calls[0]["json"]["messages"][0]["content"][1]["text"]
        check("空 prompt 自动填默认文案", "描述这张图片" in text_block, text_block)

    with EnvPatch():
        try:
            vision_llm.analyze_image(b"x", "q")
            check("没 Key 应报错", False)
        except vision_llm.VisionError as exc:
            check("没 Key 报错提到 DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY" in str(exc), str(exc))


def test_fallback_to_dashscope():
    print("[回退到千问]")
    fake = FakePost([FakeResponse(401, {"error": {"message": "invalid key"}}),
                     FakeResponse(200, ok_payload("千问答的"))])
    with EnvPatch(DEEPSEEK_API_KEY="sk-bad", QWEN_API_KEY="sk-qw"):
        with_post(fake)
        result = vision_llm.analyze_image(b"x", "q")

    check("DeepSeek 401 后回退千问", result["provider"] == "dashscope")
    check("第二个请求打到 DashScope",
          "dashscope.aliyuncs.com" in fake.calls[1]["url"], fake.calls[1]["url"])
    check("回退后 model=qwen-vl-plus", result["model"] == "qwen-vl-plus")
    check("回退后拿到千问的答案", result["answer"] == "千问答的")

    fake = FakePost([FakeResponse(500, {}, "server error"),
                     FakeResponse(500, {}, "server error")])
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", QWEN_API_KEY="sk-qw"):
        with_post(fake)
        try:
            vision_llm.analyze_image(b"x", "q")
            check("全部失败应报错", False)
        except vision_llm.VisionError as exc:
            check("全部失败报错含两个 provider",
                  "deepseek:" in str(exc) and "dashscope:" in str(exc), str(exc))


def test_error_mapping():
    print("[错误映射]")
    cases = [
        (FakeResponse(401, {"error": {"message": "bad key"}}), "鉴权失败"),
        (FakeResponse(429, {}), "请求过于频繁"),
        (FakeResponse(400, {}, "bad request"), "HTTP 400"),
        (FakeResponse(200, ValueError("not json"), "not-json"), "不是 JSON"),
        (FakeResponse(200, {"error": {"message": "quota exceeded"}}), "quota exceeded"),
    ]
    for response, expect in cases:
        fake = FakePost([response])
        with EnvPatch(DEEPSEEK_API_KEY="sk-ds", VISION_PROVIDER="deepseek"):
            with_post(fake)
            try:
                vision_llm.analyze_image(b"x", "q")
                check("应抛错: %s" % expect, False)
            except vision_llm.VisionError as exc:
                check("错误映射 → %s" % expect, expect in str(exc), str(exc))

    import requests as _requests
    fake = FakePost([_requests.exceptions.Timeout()])
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", VISION_PROVIDER="deepseek"):
        with_post(fake)
        try:
            vision_llm.analyze_image(b"x", "q", timeout=3)
            check("超时应报错", False)
        except vision_llm.VisionError as exc:
            check("超时报错文案", "超时" in str(exc), str(exc))

    fake = FakePost([_requests.exceptions.ConnectionError("down")])
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", VISION_PROVIDER="deepseek"):
        with_post(fake)
        try:
            vision_llm.analyze_image(b"x", "q")
            check("网络错误应报错", False)
        except vision_llm.VisionError as exc:
            check("网络错误文案", "网络请求失败" in str(exc), str(exc))


def test_status():
    print("[状态输出]")
    with EnvPatch(DEEPSEEK_API_KEY="sk-ds", QWEN_API_KEY="sk-qw"):
        info = vision_llm.status()
        check("status.provider 是 deepseek", info["provider"] == "deepseek")
        check("status.providers 含两个", info["providers"] == ["deepseek", "dashscope"])
        check("status 标出两个 Key 都配了",
              info["deepseek_key"] is True and info["dashscope_key"] is True)
        check("status 不泄漏 Key 内容", "sk-ds" not in json.dumps(info))

    with EnvPatch():
        info = vision_llm.status()
        check("无 Key 时 provider 为 None", info["provider"] is None)


def test_describe():
    print("[小工具]")
    check("长文本截断到 60 字",
          vision_llm.describe_response("字" * 100).endswith("…"))
    check("短文本原样", vision_llm.describe_response("帐篷") == "帐篷")
    check("换行被压成空格", vision_llm.describe_response("a\nb") == "a b")


def main():
    test_provider_order()
    test_urls_and_models()
    test_analyze_deepseek_payload()
    test_content_blocks()
    test_empty_input()
    test_fallback_to_dashscope()
    test_error_mapping()
    test_status()
    test_describe()

    print()
    if _failed:
        print("✗ %d 项失败 / 共 %d 项" % (len(_failed), len(_failed) + _passed))
        for name in _failed:
            print("   - %s" % name)
        return 1
    print("✓ 全部通过：%d 项" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
