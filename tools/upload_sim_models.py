#!/usr/bin/env python3
"""把仿真模型上传到腾讯云 COS（走 CDN 分发）

只依赖标准库：COS 的 v5 签名用 HMAC-SHA1 手算，免得为了传 14 个文件装 coscli。

用法：
    python3 tools/upload_sim_models.py            # 上传
    python3 tools/upload_sim_models.py --dry-run  # 只看会传什么

依赖环境变量：COS_SECRET_ID / COS_SECRET_KEY / COS_REGION / COS_BUCKET（已在本机设好）。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

# COS 上的目录前缀；sim.js 里的 MODEL_BASE 要和它对齐
PREFIX = "moyurobot/sim"

# 要上传的本地目录（URDF 里用相对路径引用 assets/*.stl，所以目录结构必须原样保留）
MODEL_DIR = Path(__file__).resolve().parent.parent / (
    "pi_client/moyurobot/web/static/sim/models"
)


def cos_config() -> dict[str, str]:
    need = ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_REGION", "COS_BUCKET")
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit(f"缺少环境变量: {', '.join(missing)}")
    region = os.environ["COS_REGION"]
    bucket = os.environ["COS_BUCKET"]
    return {
        "secret_id": os.environ["COS_SECRET_ID"],
        "secret_key": os.environ["COS_SECRET_KEY"],
        "host": f"{bucket}.cos.{region}.myqcloud.com",
    }


def sign(cfg: dict[str, str], method: str, key: str, headers: dict[str, str]) -> str:
    """COS 请求签名（q-sign-algorithm=sha1）"""
    now = int(time.time())
    key_time = f"{now - 60};{now + 3600}"

    def _hmac(key_bytes: bytes | str, msg: str) -> str:
        if isinstance(key_bytes, str):
            key_bytes = key_bytes.encode()
        return hmac.new(key_bytes, msg.encode(), hashlib.sha1).hexdigest()

    sign_key = _hmac(cfg["secret_key"], key_time)

    # 参与签名的 header 必须小写、按字典序，value 需 urlencode
    items = sorted((k.lower(), quote(str(v), safe="-_.~")) for k, v in headers.items())
    header_list = ";".join(k for k, _ in items)
    header_str = "&".join(f"{k}={v}" for k, v in items)

    http_string = f"{method.lower()}\n{quote('/' + key, safe='/')}\n\n{header_str}\n"
    string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    signature = _hmac(sign_key, string_to_sign)

    return (
        f"q-sign-algorithm=sha1&q-ak={cfg['secret_id']}"
        f"&q-sign-time={key_time}&q-key-time={key_time}"
        f"&q-header-list={header_list}&q-url-param-list=&q-signature={signature}"
    )


def guess_type(path: Path) -> str:
    return {
        ".stl": "model/stl",
        ".urdf": "application/xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
    }.get(path.suffix.lower(), "application/octet-stream")


def put_object(cfg: dict[str, str], key: str, path: Path) -> None:
    body = path.read_bytes()
    headers = {
        "Host": cfg["host"],
        "Content-Type": guess_type(path),
        "Content-Length": str(len(body)),
        # 文件名自带版本号（v1/v2），内容不会原地改，可放心长缓存。
        # 不加这条的话 CDN 用默认策略，回源频率不可控。
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    req = urllib.request.Request(
        url=f"https://{cfg['host']}/{quote(key, safe='/')}",
        data=body,
        method="PUT",
        headers={**headers, "Authorization": sign(cfg, "put", key, headers)},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not MODEL_DIR.is_dir():
        sys.exit(f"找不到模型目录: {MODEL_DIR}")

    files = sorted(p for p in MODEL_DIR.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"待上传 {len(files)} 个文件，合计 {total / 1e6:.1f} MB")

    if args.dry_run:
        for p in files:
            rel = p.relative_to(MODEL_DIR).as_posix()
            print(f"  {PREFIX}/{rel}  ({p.stat().st_size / 1e3:.0f} KB)")
        return

    cfg = cos_config()
    ok = 0
    for p in files:
        rel = p.relative_to(MODEL_DIR).as_posix()
        key = f"{PREFIX}/{rel}"
        try:
            put_object(cfg, key, p)
            ok += 1
            print(f"  ✓ {key}  ({p.stat().st_size / 1e3:.0f} KB)")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            print(f"  ✗ {key}  HTTP {e.code}: {detail}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {key}  {type(e).__name__}: {e}")

    print(f"\n完成 {ok}/{len(files)}")


if __name__ == "__main__":
    main()
