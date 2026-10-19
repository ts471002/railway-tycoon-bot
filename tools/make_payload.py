"""把「快速版文件夹」压成安装版内嵌的 payload.tar.xz。

    python tools/make_payload.py

只做一件事：遍历 dist\\铁路大亨推测-快速版\\，打成 xz 的 tar。
用 xz 而不是 zip，是因为同样内容 zip 要 111MB，xz 只要 78MB。
"""
from __future__ import annotations

import os
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dist", "铁路大亨推测-快速版")
OUT = os.path.join(ROOT, "payload.tar.xz")
SKIP = {"logs", "data"}


def main() -> int:
    if not os.path.isdir(SRC):
        print(f"找不到 {SRC}，先跑 打包快速版.bat")
        return 1
    if os.path.exists(OUT):
        os.remove(OUT)
    t0 = time.time()
    n = 0
    with tarfile.open(OUT, "w:xz", preset=6) as tf:
        for base, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for f in files:
                p = os.path.join(base, f)
                tf.add(p, arcname=os.path.relpath(p, SRC))
                n += 1
    mb = os.path.getsize(OUT) / 1e6
    print(f"{n} 个文件 -> payload.tar.xz  {mb:.1f} MB  ({time.time() - t0:.0f} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
