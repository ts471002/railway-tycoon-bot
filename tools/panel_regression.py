"""回归测试：在真实游戏截图上合成右侧面板，测不同分辨率/位置能否读到。

    python tools/panel_regression.py

为什么要有它：读面板靠的是「位置 + 兜底扫描」，位置一改就容易在某种分辨率上失效，
而这种失效在本机（100% 缩放、固定分辨率）根本看不出来。这里把同一段游戏文字
摆到画面不同位置、不同尺寸下，跑一遍看能不能全部认出来。

底板是 assets/sample_frame.png（一张真实游戏截图），合成图落到 logs/test/。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont

import next_station as NS

BASE = Image.open(os.path.join(NS.ROOT, "assets", "sample_frame.png")).convert("RGB")
FONT = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)

LINES = ["未来3站将有食铺站数量最多", "旅程剩余站点：15站", "酒庄x5   食铺x5   商行x5"]


def make_panel(scale: float) -> Image.Image:
    """按 1600x900 基准做一个 600 宽的面板块，再按 scale 缩放。"""
    w, h = 600, 220
    im = Image.new("RGB", (w, h), (18, 20, 26))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, h - 1], outline=(70, 62, 44))
    y = 24
    for i, t in enumerate(LINES):
        f = FONT if i == 0 else ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 24)
        d.text((14, y), t, font=f, fill=(232, 210, 160) if i == 0 else (240, 232, 214))
        y += 52
    if abs(scale - 1.0) > 1e-6:
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return im


def frame(size, pos="right", panel_scale=None):
    """把面板贴到 size 大小的画面里；pos 决定贴哪。"""
    w, h = size
    s = panel_scale if panel_scale else w / 1600.0
    p = make_panel(s)
    im = BASE.resize(size, Image.LANCZOS).copy()
    if pos == "right":
        x, y = w - p.width - int(8 * s), int(28 * s)
    elif pos == "left":
        x, y = int(24 * s), int(24 * s)
    elif pos == "center":
        x, y = (w - p.width) // 2, int(h * 0.30)
    else:
        x, y = int(pos[0] * s), int(pos[1] * s)
    im.paste(p, (x, int(y)))
    return im


CASES = [
    ("1600x900 右（基准）", (1600, 900), "right", None),
    ("1920x1080 右", (1920, 1080), "right", None),
    ("1280x720 右（小窗）", (1280, 720), "right", None),
    ("2560x1440 右（2K）", (2560, 1440), "right", None),
    ("1920x1080 左（异常布局）", (1920, 1080), "left", None),
    ("1600x900 居中（异常布局）", (1600, 900), "center", None),
]


def main() -> int:
    os.makedirs(os.path.join(NS.ROOT, "logs", "test"), exist_ok=True)
    ok = 0
    for label, size, pos, ps in CASES:
        img = frame(size, pos, ps)
        img.save(os.path.join(NS.ROOT, "logs", "test", f"{pos}_{size[0]}.png"))
        NS.forget_panel()
        NS.reset_scan_backoff()
        NS._full_miss = 2           # 允许直接进全屏分块层
        t = time.time()
        r = NS.compute_img(img, allow_scan=True)
        dt = time.time() - t
        c = r.get("counts") or {}
        good = c.get("酒庄") == 5 and c.get("食铺") == 5 and c.get("商行") == 5
        ok += 1 if good else 0
        print(f"{'✓' if good else '✗'} {label:26s} {dt:5.2f}s  "
              f"{c or '无'}  阶段={NS.LAST.get('stage')}  行={NS.LAST.get('lines')}")
    print(f"\n{ok}/{len(CASES)} 通过")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
