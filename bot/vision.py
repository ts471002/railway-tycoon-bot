"""图像工具：模板匹配（锚点校验）、区域裁剪、数字清洗。

刻意不用 OpenCV：只用 numpy + Pillow，装包轻、启动快。
匹配策略是"定点校验 + 小范围搜索"——校准之后每个控件位置是已知的，
只需要确认它还在原位、没有被弹窗顶掉，因此不需要全图搜索。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image

Rect = Tuple[int, int, int, int]  # x, y, w, h


def crop(img: Image.Image, rect: Rect) -> Image.Image:
    x, y, w, h = [int(v) for v in rect]
    return img.crop((x, y, x + w, y + h))


def to_gray(img: Image.Image, scale: float = 1.0) -> np.ndarray:
    if scale != 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.BILINEAR)
    return np.asarray(img.convert("L"), dtype=np.float32)


def to_rgb(img: Image.Image, scale: float = 1.0) -> np.ndarray:
    if scale != 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.BILINEAR)
    return np.asarray(img.convert("RGB"), dtype=np.float32)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """标准化互相关（NCC），0~1。对纯色/平铺区域返回 0，避免"哪里都像"。

    相比逐像素差值，NCC 对整体明暗变化不敏感，但对结构敏感，
    这正是界面锚点需要的：形状不对就是不对。
    """
    if a.shape != b.shape or a.size == 0:
        return 0.0
    fa = a.reshape(-1, a.shape[-1]) if a.ndim == 3 else a.reshape(-1, 1)
    fb = b.reshape(-1, b.shape[-1]) if b.ndim == 3 else b.reshape(-1, 1)
    fa = fa - fa.mean(axis=0)
    fb = fb - fb.mean(axis=0)
    den = float(np.sqrt((fa * fa).sum() * (fb * fb).sum()))
    if den < 1e-6:
        return 0.0
    return max(0.0, float((fa * fb).sum() / den))


@dataclass
class Match:
    x: int
    y: int
    score: float

    def __bool__(self) -> bool:
        return self.score > 0


def find_patch(
    img: Image.Image,
    patch: Image.Image,
    hint: Optional[Tuple[int, int]] = None,
    search: int = 24,
    scale: float = 0.5,
) -> Match:
    """在 hint 附近 search 像素内寻找 patch，找不到时退回全图粗搜。

    返回匹配位置（客户区坐标，patch 左上角）与相似度。
    """
    if patch.width == 0 or patch.height == 0:
        return Match(0, 0, 0.0)

    if scale != 1.0:
        tw, th = max(1, int(patch.width * scale)), max(1, int(patch.height * scale))
        patch_s = patch.resize((tw, th), Image.BILINEAR)
    else:
        tw, th = patch.width, patch.height
        patch_s = patch

    gp = to_rgb(patch_s)
    gi = to_rgb(img, scale)
    H, W = gi.shape[0], gi.shape[1]

    def score_at(x: int, y: int) -> float:
        if x < 0 or y < 0 or x + tw > W or y + th > H:
            return -1.0
        return similarity(gi[y:y + th, x:x + tw], gp)

    if hint is not None:
        hx, hy = int(hint[0] * scale), int(hint[1] * scale)
        best = Match(hint[0], hint[1], score_at(hx, hy))
        step = 2
        r = max(1, int(search * scale))
        for dy in range(-r, r + 1, step):
            for dx in range(-r, r + 1, step):
                s = score_at(hx + dx, hy + dy)
                if s > best.score:
                    best = Match(int((hx + dx) / scale), int((hy + dy) / scale), s)
        return best

    # 全图粗搜：4 像素步长，够用且快
    best = Match(0, 0, 0.0)
    for y in range(0, max(1, H - th), 4):
        for x in range(0, max(1, W - tw), 4):
            s = score_at(x, y)
            if s > best.score:
                best = Match(int(x / scale), int(y / scale), s)
    return best


_NUM = re.compile(r"[-+]?\d[\d, ]*(?:\.\d+)?")


def parse_number(text: str) -> Optional[float]:
    """把 OCR 出来的文字转成数字，支持 1,234 / 12.3万 / 4.5亿 / 3.2k。"""
    if not text:
        return None
    t = text.replace("，", ",").replace("。", ".").replace("O", "0").replace("o", "0")
    t = t.replace("万", "万").replace("亿", "亿")
    mult = 1.0
    if "亿" in t:
        mult = 1e8
    elif "万" in t or "w" in t.lower():
        mult = 1e4
    elif "k" in t.lower():
        mult = 1e3
    m = _NUM.search(t)
    if not m:
        return None
    raw = m.group(0).replace(",", "").replace(" ", "")
    try:
        return float(raw) * mult
    except ValueError:
        return None


def diff_ratio(a: Image.Image, b: Image.Image) -> float:
    """两帧差异比例，用于判断画面是否发生变化（等动画/等结算）。"""
    if a.size != b.size:
        return 1.0
    ga = np.asarray(a.convert("L").resize((80, 45)), dtype=np.float32)
    gb = np.asarray(b.convert("L").resize((80, 45)), dtype=np.float32)
    return float(np.abs(ga - gb).mean() / 255.0)
