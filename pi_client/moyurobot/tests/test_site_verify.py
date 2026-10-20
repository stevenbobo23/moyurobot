#!/usr/bin/env python
"""域名校验文件测试：根路径可达 / 字节一致 / 拒绝越界

运行：
    cd pi_client
    PYTHONPATH=. python moyurobot/tests/test_site_verify.py

只依赖 flask，不需要 cv2 / lerobot。
"""

import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from flask import Flask  # noqa: E402

from moyurobot.web import site_verify  # noqa: E402

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


def make_client(base):
    app = Flask("verify-test")
    site_verify.register(app, base=base)
    return app.test_client()


def test_real_file():
    print("[仓库里的校验文件]")
    client = make_client(site_verify.PUBLIC_DIR)
    resp = client.get("/ChC4AVj1oJ.txt")
    check("根路径返回 200", resp.status_code == 200, str(resp.status_code))

    on_disk = (site_verify.PUBLIC_DIR / "ChC4AVj1oJ.txt").read_bytes()
    check("响应字节与文件完全一致", resp.data == on_disk,
          "%r != %r" % (resp.data[:20], on_disk[:20]))
    check("没有多余的换行/BOM", resp.data == resp.data.strip() and not resp.data.startswith(b"\xef\xbb\xbf"))
    check("长度是 32", len(resp.data) == 32, str(len(resp.data)))
    check("Content-Type 是 text/plain",
          resp.headers.get("Content-Type", "").startswith("text/plain"),
          resp.headers.get("Content-Type"))


def test_temp_dir():
    print("[临时目录：通用性]")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "Abc123.txt").write_bytes(b"hello-verify")
        (base / "notes.json").write_bytes(b'{"a":1}')
        client = make_client(base)

        resp = client.get("/Abc123.txt")
        check("任意名字的校验文件都能取到", resp.status_code == 200 and resp.data == b"hello-verify")

        check("不存在的文件 404", client.get("/NoSuchFile.txt").status_code == 404)
        check("只有 .txt 会暴露（.json 不能借 .txt 取到）", "null" not in client.get("/notes.txt").data.decode())
        check("非 .txt 后缀不匹配该路由", client.get("/notes.json").status_code == 404)

        check("空名字 404", client.get("/.txt").status_code == 404)
        check("带点的名字 404", client.get("/a.b.txt").status_code == 404)
        check("带斜杠的穿越被拒",
              client.get("/../secret.txt").status_code in (404, 308),
              str(client.get("/../secret.txt").status_code))
        check("URL 编码斜杠被拒",
              client.get("/..%2Fsecret.txt").status_code in (404, 308),
              str(client.get("/..%2Fsecret.txt").status_code))


def test_resolve():
    print("[resolve 单元]")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "Ok.txt").write_bytes(b"x")

        check("合法名返回路径", site_verify.resolve("Ok", base) == base / "Ok.txt")
        check("不存在返回 None", site_verify.resolve("Missing", base) is None)
        check("空名返回 None", site_verify.resolve("", base) is None)
        check("点号返回 None", site_verify.resolve("..", base) is None)
        check("斜杠返回 None", site_verify.resolve("a/b", base) is None)
        check("空格返回 None", site_verify.resolve("a b", base) is None)
        check("Unicode 返回 None", site_verify.resolve("校验", base) is None)
        check("超长名返回 None", site_verify.resolve("a" * 65, base) is None)
        check("64 字符名不因长度被拒", site_verify.resolve("a" * 64, base) is None)  # 文件不存在

        check("list_files 只列 .txt", site_verify.list_files(base) == ["Ok.txt"])


def test_empty_dir():
    print("[空目录]")
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(Path(tmp))
        check("空目录下请求 404", client.get("/Anything.txt").status_code == 404)
        check("list_files 返回空列表", site_verify.list_files(Path(tmp)) == [])


def main():
    test_real_file()
    test_temp_dir()
    test_resolve()
    test_empty_dir()

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
