"""读当前画面，推算下一站是什么（推理核心 + 命令行调试入口）。

游戏右侧面板每站都会给出两条信息：

    未来3站将有食铺站数量最多          ← 提示（约束）
    旅程剩余站点：15站
    酒庄x5  食铺x5  商行x5             ← 硬约束（剩余每种各几个）

第二条是"底牌"：它说明剩下的这一路上，三种店铺各还剩几个。
把「剩余数量」+「未来 3 站的相对多少」+「不会连续三站相同」合起来，
就能精确算出下一站的概率：枚举开头几个位置的所有填法，每种填法后面
剩下的位置有多少种合法排法，按权重加权平均即可。不是猜，是把约束穷举完。

图形界面在 tools/next_station_gui.py。

用法（平时不需要，调试时用）：
    python tools/next_station.py             # 读一次，打印结论
    python tools/next_station.py dump        # 打印画面里所有 OCR 文字（认错字时用）
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from bot import journey as J  # noqa: E402
from bot.ocr import Reader, get_ocr  # noqa: E402
from bot.paths import data_root  # noqa: E402

# 源码运行时 = 项目根；打包成 exe 之后 = exe 所在目录（见 bot/paths.py）
ROOT = data_root()
LOGS = os.path.join(ROOT, "logs")

MAX_PER_TYPE = 6   # 每类站点一局里最多 6 个，读出来 >6 一定是认错了
HINT_KW = ("最多", "最少", "相同")
HINT_LEAD = ("未来", "接下来", "之后", "各站")
CARD_COND = re.compile(r"如果下一站(不是|是)?【(.+?)】")
JOURNEY = os.path.join(ROOT, "data", "journey_state.json")
ARCHIVE = os.path.join(ROOT, "data", "journey_archive")


# ------------------------------------------------------------------ 窗口
def _unreal_windows() -> list:
    import win32gui

    found = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == "UnrealWindow":
                found.append(h)
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return found


def find_unreal_any_state() -> int:
    """不管尺寸，只要是 UnrealWindow 就返回。"""
    w = _unreal_windows()
    return w[0] if w else 0


def game_hwnd() -> int:
    """优先返回一个没被最小化的 Unreal 窗口。"""
    import win32gui

    for h in _unreal_windows():
        try:
            if not win32gui.IsIconic(h):
                return h
        except Exception:
            continue
    return find_unreal_any_state()


def restore_no_activate(hwnd: int) -> bool:
    """把最小化的窗口展开，但不激活、不抢前台。"""
    import win32con
    import win32gui

    try:
        win32gui.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        time.sleep(0.35)
        if win32gui.IsIconic(hwnd):
            win32gui.SetWindowPos(hwnd, 0, 100, 100, 1616, 939,
                                  win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER
                                  | win32con.SWP_SHOWWINDOW)
            time.sleep(0.35)
    except Exception:
        return False
    return not win32gui.IsIconic(hwnd)


class Grabber:
    """常驻抓屏会话：反复抓帧比每次新建快得多（新建一次约 0.8s）。"""

    def __init__(self) -> None:
        self.cap = None
        self.hwnd = 0

    def grab(self, restore: bool = True, timeout: float = 4.0) -> tuple[Image.Image, int]:
        import win32gui

        from bot.bgcapture import BgCapture

        hwnd = game_hwnd()
        if not hwnd:
            raise RuntimeError("没找到游戏窗口，游戏是不是没开？")
        if win32gui.IsIconic(hwnd):
            if not restore or not restore_no_activate(hwnd):
                raise RuntimeError(
                    "游戏窗口被最小化了，后台抓屏抓不到最小化窗口。\n"
                    "把它从任务栏展开一次就行（可以放在后台、被别的窗口挡住）。"
                )
        if self.cap is None or self.hwnd != hwnd or not self.cap.alive():
            self.stop()
            self.cap = BgCapture(hwnd)
            self.hwnd = hwnd
        return self.cap.grab(timeout=timeout), hwnd

    def stamp(self) -> float:
        """最近一帧的时间戳。没变就说明画面没更新，连转 PIL 都省了。"""
        return self.cap.stamp() if self.cap is not None else 0.0

    def client_size(self) -> tuple[int, int]:
        """窗口客户区的真实尺寸（DPI 感知生效后是物理像素）。

        和抓到的帧比一下就能发现"尺寸被系统缩放虚拟化了"这类问题——
        那种情况下画面会被压/裁，读区域也跟着全错。
        """
        try:
            import win32gui

            hwnd = game_hwnd()
            if not hwnd:
                return (0, 0)
            r = win32gui.GetClientRect(hwnd)
            return (int(r[2]), int(r[3]))
        except Exception:
            return (0, 0)

    def stop(self) -> None:
        if self.cap is not None:
            try:
                self.cap.stop()
            except Exception:
                pass
        self.cap = None
        self.hwnd = 0


def grab_game(restore: bool = True) -> tuple[Image.Image, dict]:
    """兼容旧接口：抓一帧就释放会话（命令行单次用）。"""
    g = Grabber()
    try:
        img, hwnd = g.grab(restore=restore)
    finally:
        g.stop()
    return img, {"hwnd": hwnd, "client": img.size}


# ------------------------------------------------------------------ OCR
_READER = None


def reader() -> Reader:
    """OCR 引擎只建一次（模型加载要 1~2 秒）。"""
    global _READER
    if _READER is None:
        _READER = Reader(get_ocr("auto"))
    return _READER


def ocr_lines(img: Image.Image):
    global _FRAME_SIZE

    _FRAME_SIZE = img.size
    r = reader()
    return r.backend.recognize(img), r.name


# 坐标基准：这里的数字都是在「游戏客户区 1600x900」下量出来的。
# 实际抓到的帧可能是 1280x720、1920x1080 或别的尺寸（换分辨率、全屏、改窗口大小），
# 所以下面这些框一律按比例换算到实际帧尺寸，绝不能当绝对像素直接用。
REF_W, REF_H = 1600, 900
PANEL_BOX = (1000, 30, 1600, 250)     # 右侧「未来3站…／旅程剩余站点」那一块
CARD_BOX = (230, 430, 1470, 640)      # 策略卡三选一那一块（只在选卡界面读）

# 说明：上面那个框是在 1600x900 下量出来的，而游戏的 UMG 不保证随分辨率等比缩放
# （换分辨率、换显示器、改 HUD 缩放时，面板的实际位置会飘）。所以读不到时会退到
# 「右上角两条竖条」里去找，找到就把真实位置按比例记下来 —— 见 _strip_boxes/_learn_from。

PANEL_CACHE = os.path.join(ROOT, "data", "panel_box.json")

_FRAME_SIZE = (REF_W, REF_H)          # 当前这一帧的尺寸，供 _in_panel 判断用
_LEARNED = None                        # 学到的面板比例框；None=没读过，()=读过但没有
_ACTIVE_FRAC = (PANEL_BOX[0] / REF_W, PANEL_BOX[1] / REF_H,
                PANEL_BOX[2] / REF_W, PANEL_BOX[3] / REF_H)


def _frac(box) -> tuple[float, float, float, float]:
    """基准坐标 → 比例（0~1）。存盘、跨分辨率复用一律用比例，不用像素。"""
    return (box[0] / REF_W, box[1] / REF_H, box[2] / REF_W, box[3] / REF_H)


def _frac_to_px(f, size) -> tuple[int, int, int, int]:
    w, h = size
    return (max(0, int(round(f[0] * w))), max(0, int(round(f[1] * h))),
            min(w, int(round(f[2] * w))), min(h, int(round(f[3] * h))))


def scale_box(box, size) -> tuple[int, int, int, int]:
    """把基准坐标的框按实际帧尺寸等比放大/缩小，并夹在画面里面。"""
    return _frac_to_px(_frac(box), size)


def load_learned_panel():
    """读上次学到的面板位置（比例框）。没有就返回 None。"""
    global _LEARNED
    if _LEARNED is None:
        try:
            with open(PANEL_CACHE, encoding="utf-8") as f:
                d = json.load(f)
            v = d.get("frac")
            if isinstance(v, list) and len(v) == 4:
                _LEARNED = tuple(float(x) for x in v)
            else:
                _LEARNED = ()
        except Exception:
            _LEARNED = ()
    return _LEARNED or None


def save_learned_panel(f: tuple) -> None:
    global _LEARNED
    _LEARNED = tuple(float(x) for x in f)
    try:
        os.makedirs(os.path.dirname(PANEL_CACHE), exist_ok=True)
        with open(PANEL_CACHE, "w", encoding="utf-8") as fp:
            json.dump({"frac": list(_LEARNED),
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, fp)
    except Exception:
        pass


def forget_panel() -> None:
    """忘掉学到的位置（换分辨率/换机器后想重新学一遍时用）。"""
    global _LEARNED
    _LEARNED = ()
    try:
        os.remove(PANEL_CACHE)
    except Exception:
        pass


def panel_frac():
    """当前该用哪个比例框：学到过就用学到的，否则用 1600x900 下量的标准位置。"""
    return load_learned_panel() or _frac(PANEL_BOX)


def panel_regions(img: Image.Image):
    return [_frac_to_px(panel_frac(), img.size)]


def card_regions(img: Image.Image):
    return [scale_box(CARD_BOX, img.size)]


def ocr_lines_fast(img: Image.Image, scale: float = 2.0, regions=None):
    """只识别指定的小块区域（默认只认右侧面板），比全屏快一个数量级；坐标会平移回整帧。

    放大 2 倍的目的是让小字（22px）落到识别模型舒服的尺寸，否则「x1」容易被拆错。
    缩放用 BILINEAR：2 倍放大时和 LANCZOS 的识别结果一致，但快 3~5 倍；
    真正的大头是识别模型本身，它的输入缩放由 Det.limit_type=max 兜住，不会把图吹大。
    """
    global _FRAME_SIZE
    from bot.ocr import OcrLine

    _FRAME_SIZE = img.size
    # 分辨率自适应：窗口比基准（1600 宽）小的时候把图放得更大一点，
    # 让「22px 小字放大到约 44px」这个尺度保持一致；窗口更大就不用再吹。
    _w = img.size[0] or REF_W
    scale *= max(1.0, min(1.8, REF_W / _w))
    r = reader()
    out = []
    for bx, by, bx2, by2 in (regions or panel_regions(img)):
        crop = img.crop((bx, by, bx2, by2))
        if abs(scale - 1.0) > 1e-6:
            crop = crop.resize((max(1, int(crop.width * scale)),
                                max(1, int(crop.height * scale))), Image.BILINEAR)
        for ln in r.backend.recognize(crop):
            x, y, w, h = ln.box
            out.append(OcrLine(ln.text, ln.conf,
                               (x / scale + bx, y / scale + by, w / scale, h / scale)))
    return out, r.name


# ------------------------------------------------------------------ 解析
def _in_panel(ln) -> bool:
    """这个文字框在不在当前正在读的那块面板里（按比例判断，换分辨率不失真）。"""
    w, h = _FRAME_SIZE
    x, y, _bw, _bh = ln.box
    fx = x / max(1, w)
    fy = y / max(1, h)
    f = _ACTIVE_FRAC
    return fx >= f[0] - 0.012 and f[1] - 0.012 <= fy <= f[3] + 0.01


def _merge_row(row) -> str:
    """把同一行的 OCR 碎块按 x 拼回去，并切掉框与框之间重复的那几个字。

    OCR 常见的坏习惯：右边那块会把左边块的最后一个字再框进来一次。
    「食铺x1」+「商行x2」被拼成「食铺x11商行x2」，数字就凭空多了一位——
    「1 读成 11、2 读成 22」基本都是这么来的，不是字认错，是同一个字被数了两次。

    做法：按两块框的横向重叠宽度估算右边块开头被重复了几个字（字宽 ≈ 框宽/字数），
    只有当切掉的字和已拼好的尾巴完全一致时才真切，避免误伤真正的数字。
    """
    out = ""
    prev_end = None
    for ln in row:
        t = ln.text.replace(" ", "")
        if not t:
            continue
        x, _y, w, _h = ln.box
        overlap = (prev_end - x) if prev_end is not None else 0.0
        if overlap > 1 and w > 0:
            char_w = w / len(t)
            k = 0
            while k + 1 <= len(t) - 1 and (k + 1) * char_w <= overlap + char_w * 0.75:
                k += 1
            for kk in range(k, 0, -1):
                if out.endswith(t[:kk]):
                    t = t[kk:]
                    break
        out += t
        prev_end = x + w if prev_end is None else max(prev_end, x + w)
    return out


def parse_counts_from_lines(lines) -> dict:
    """「旅程剩余站点：15站 / 酒庄 食铺 商行 / x5 x5 x5」抠出三种各剩几个。

    游戏有两种排布：一行写全（酒庄x5 食铺x5 商行x5），或者类型一行、数字一行。
    而且 OCR 经常把「食铺商行」并成一个框，所以这里按字符位置估算每个站名的
    横向中心，再找它正下方最近的那个 x5。
    """
    for ln in lines:
        c = J.parse_counts(ln.text)
        if c:
            return c

    # 同一行被 OCR 拆成「旅程剩余站点：」+「酒庄x4食铺x2」+「商行x4」三块很常见，
    # 所以把同一水平线上的碎块按 x 拼回去再解析一次（拼的时候要切掉重叠的字）。
    for ln in lines:
        if "剩余" not in ln.text.replace(" ", ""):
            continue
        y0 = ln.box[1]
        row = sorted([x for x in lines if abs(x.box[1] - y0) <= 12],
                     key=lambda x: x.box[0])
        c = J.parse_counts(_merge_row(row))
        if c:
            return c

    types, vals = [], []
    for ln in lines:
        if not _in_panel(ln):
            continue
        raw = ln.text.replace(" ", "").strip()
        x, y, w, h = ln.box
        if not raw:
            continue
        if any(k in raw for k in ("剩余", "站点", "未来", "接下来", "最多", "最少", "相同")):
            continue
        if len(raw) <= 8:
            for ty in J.TYPES:
                i = raw.find(ty)
                if i >= 0:
                    frac = (i + len(ty) / 2) / len(raw)
                    types.append((ty, x + w * frac, y))
        m = re.fullmatch(r"[x×X*＊]?\s*(\d{1,2})", raw)
        if m:
            vals.append((int(m.group(1)), x + w / 2, y))

    if len(vals) < 3:
        return {}
    out = {}
    for ty, cx, cy in types:
        if ty in out:
            continue
        cand = [(abs(vx - cx), n) for n, vx, vy in vals if vy >= cy - 8]
        if cand:
            out[ty] = min(cand)[1]
    return out if len(out) == 3 else {}


def parse_hint_from_lines(lines) -> str:
    """把提示句拼回来（OCR 常把它拆成「未来3站将有」+「食铺站数量最多」两行）。"""
    parts = []
    for ln in sorted(lines, key=lambda l: (l.box[1], l.box[0])):
        t = ln.text.replace(" ", "").strip()
        if not t or re.search(r"\d{2,}", t):
            continue
        if not _in_panel(ln):
            continue
        # 统计行（「旅程剩余站点：酒庄x5…」）不是提示，先剔掉。
        # 注意不能笼统地用「站点」判断：提示本身也会写「各站点数量相同」。
        if "剩余" in t or "旅程" in t or J.parse_counts(t):
            continue
        if any(k in t for k in HINT_KW) or any(k in t for k in HINT_LEAD):
            parts.append(t)
    return "".join(parts)


def parse_screen(lines) -> dict:
    hint = parse_hint_from_lines(lines)
    counts = parse_counts_from_lines(lines)

    rest = None
    for ln in lines:
        r = J.parse_remaining_stations(ln.text)
        if r:
            rest = r
            break

    return {
        "counts": counts,
        "hint": hint,
        "hint_obj": J.parse_hint(hint),
        "rest": rest,
    }


# ------------------------------------------------------- 数字纠错（1→11 之类）
def _digit_variants(n: int, cap: int = MAX_PER_TYPE) -> set[int]:
    """一个数字被 OCR 读错时的候选修正值。

    最常见的错法是同一个字被数了两遍（1→11、2→22），所以先去重位；
    超过 cap 的值不可能是真的，再把每一位单独当候选、以及去掉首位后的值。
    """
    v = {n}
    s = str(n)
    if 11 <= n <= 99 and n % 11 == 0:   # 11→1, 22→2, 66→6
        v.add(n // 11)
    if n > cap:
        for ch in s:                     # 16 → 1 / 6
            d = int(ch)
            if 0 <= d <= cap:
                v.add(d)
        if len(s) >= 2:                  # 16 → 6（首位是噪声）
            rest = s[1:]
            if rest.isdigit() and int(rest) <= cap:
                v.add(int(rest))
    return v


def repair_counts(counts: dict, max_total=None, prev=None) -> tuple[dict, list]:
    """用硬约束把 OCR 认错的数字纠回来，返回 (修正后的 counts, 改了哪几类)。

    约束来自游戏本身，比 OCR 可信得多：
      * 每一种数量 ≤ MAX_PER_TYPE（一局里每类最多 6 个），超了一律算读错；
      * 三种数量之和 ≤ 上一帧的剩余站数（剩余只会变少，不会变多）；
      * 每一种数量 ≤ 上一帧同名数量（每站只消耗一个）；
      * max_total 给出时，总和的硬上限。
    候选值只有「原值」和「去掉可疑位后的值」，最多 27 种组合，
    挑「改动最少、最接近原读数、且满足全部约束」的那一组。
    """
    if not counts:
        return counts, []
    ts = list(J.TYPES)
    raw = [int(counts.get(t, 0) or 0) for t in ts]
    cands = [_digit_variants(n) for n in raw]

    def ok(combo) -> bool:
        if any(v < 0 or v > MAX_PER_TYPE for v in combo):
            return False
        if max_total is not None and sum(combo) > max_total:
            return False
        if prev:
            if any(c > int(prev.get(t, 99) or 0) for c, t in zip(combo, ts)):
                return False
        return True

    if ok(raw):
        return counts, []

    best = None
    for combo in itertools.product(*cands):
        if not ok(combo):
            continue
        cost = sum(1 for a, b in zip(combo, raw) if a != b)
        drift = sum(abs(a - b) for a, b in zip(combo, raw))
        key = (cost, drift, sum(combo))
        if best is None or key < best[0]:
            best = (key, combo)

    if best is None:
        return {}, []  # 修不了：当作这一帧没读到，让上层重读或丢弃
    fixed = dict(zip(ts, best[1]))
    changed = [t for t, a in zip(ts, raw) if fixed[t] != a]
    return fixed, changed


# ------------------------------------------------------------------ 旅程历史
# 提示是「未来 3 站」，而每走一站窗口就往前挪一格 —— 所以上一站的提示
# 并没有失效，它还有两站在未来。要做到这一点，必须记住两件事：
#   1) 每一站当时给出的提示
#   2) 每一站实际是什么类型（从剩余构成的差值得出）
def _load_journey() -> dict:
    try:
        with open(JOURNEY, encoding="utf-8") as f:
            j = json.load(f)
        out = {"n": int(j.get("n") or 0),
               "i": j.get("i"),
               "rest": j.get("rest"),
               "base": j.get("base") or {},
               "snap": j.get("snap") or {},
               "hints": j.get("hints") or {},
               "pending": bool(j.get("pending")),
               "up": j.get("up") or {}}
    except Exception:
        return {"n": 0, "i": 0, "rest": -1, "base": {}, "snap": {}, "hints": {},
                "pending": False, "up": {}}

    # 兼容旧格式（只有 n/snap/hints）：用已经记到的最小剩余站数反推进度
    if out["i"] is None or out["rest"] is None:
        try:
            out["rest"] = min(int(k) for k in out["snap"]) if out["snap"] else out["n"]
        except ValueError:
            out["rest"] = out["n"]
        out["i"] = max(0, out["n"] - out["rest"])
    out["i"] = int(out["i"])
    out["rest"] = int(out["rest"])
    return out


def _save_journey(j: dict) -> None:
    try:
        os.makedirs(os.path.dirname(JOURNEY), exist_ok=True)
        tmp = JOURNEY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=1)
        os.replace(tmp, JOURNEY)
    except Exception:
        pass


def update_journey(parsed: dict, force_reset: bool = False) -> dict:
    """把当前这一帧的站号/提示记进历史。

    「本局开局」（base）是这一局最开始那一刻的三种剩余数量，之后每种数量
    都只能比它小；超过它的读数先纠错、纠不了就丢掉这一帧。换新局靠两条路：
    手动「重置」，或者 _checked_counts 里连续两帧读到明显更大的总数。
    """
    counts = parsed.get("counts") or {}
    if not counts:
        return _load_journey()
    s = sum(counts.values())

    j = _load_journey()
    if force_reset or j["n"] <= 0 or not j.get("base"):
        j = {"n": s, "i": 0, "rest": s, "base": dict(counts), "snap": {}, "hints": {}}
    elif s > j["n"]:
        return j  # 不可能超过本局开局总数（真换局的话前面已经识别掉了）
    elif s > j["rest"]:
        if s - j["rest"] < 5:
            return j  # 只多一两站 = OCR 抖动
        j = {"n": s, "i": 0, "rest": s, "base": dict(counts), "snap": {}, "hints": {}}
    elif s < j["rest"]:
        j["i"] += j["rest"] - s  # 一次跳过几站也照记，缺的快照会让旧提示自动弃用
        j["rest"] = s

    j.pop("up", None)
    j.pop("pending", None)
    j["snap"][str(s)] = counts
    h = parsed.get("hint_obj")
    if h and h.kind != J.KIND_NONE:
        j["hints"][str(j["i"])] = {"span": h.span, "kind": h.kind,
                                   "target": h.target, "raw": parsed.get("hint") or ""}
    _save_journey(j)
    return j


def _archive(j: dict, why: str = "") -> None:
    """把一局的记录整份存到 data/journey_archive/，以后想翻回来还在。"""
    if not j or not j.get("n") or not j.get("base"):
        return
    try:
        os.makedirs(ARCHIVE, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + (f"_{why}" if why else "") + ".json"
        with open(os.path.join(ARCHIVE, name), "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def clear_journey(why: str = "reset") -> dict:
    """把本局记录清空，回到「还没开局」（清空前先存档，防止以前的记录没了）。"""
    _archive(_load_journey(), why)
    empty = {"n": 0, "i": 0, "rest": -1, "base": {}, "snap": {}, "hints": {},
             "pending": True}
    _save_journey(empty)
    return empty


def begin_game(counts: dict) -> dict:
    """以这一帧作为新一局的开局（重置之后真正开始的那一下）。"""
    s = sum(counts.values())
    _archive(_load_journey(), "before_new")
    j = {"n": s, "i": 0, "rest": s, "base": dict(counts),
         "snap": {str(s): dict(counts)}, "hints": {}}
    _save_journey(j)
    return {"ok": True, "pending": False, "base": dict(counts), "total": s}


def _confirm_new_run(s: int) -> bool:
    """连续两帧读到同一个「比本局开局还大」的总数，就认定是换了一局。

    单帧大跳仍然当 OCR 认错丢掉，所以不会因为偶尔读成 11 就把历史清空。
    """
    j = _load_journey()
    up = j.get("up") or {}
    hits = int(up.get("hits") or 0) + 1 if int(up.get("s") or 0) == s else 1
    if hits >= 2:
        clear_journey()
        return True
    j["up"] = {"s": s, "hits": hits}
    _save_journey(j)
    return False


def reset_game(img: Image.Image | None = None) -> dict:
    """点「重置」：这一局到此为止。

    只把记录清空，**不**拿现在屏幕上的数字当下一局的开局——那多半是上一局
    的残局（剩 3 站之类）。真正的新局由 begin_game 在开局那一下确立。
    """
    clear_journey()
    return {"ok": True, "pending": True, "base": {}, "total": 0}


def _sane_counts(c: dict) -> bool:
    if not c or len(c) != 3:
        return False
    vals = list(c.values())
    return (1 <= sum(vals) <= 3 * MAX_PER_TYPE
            and all(0 <= v <= MAX_PER_TYPE for v in vals))


def probe_counts(img: Image.Image) -> dict:
    """不带历史约束地读一次剩余构成（重置后确认新局开局用）。

    只套「每类最多 6 个」这一条：超了先纠（11→1、16→6），纠不动才算没读到。
    """
    for scale in (2.0, 3.0):
        c = parse_counts_from_lines(ocr_lines_fast(img, scale=scale)[0])
        c = resolve_counts(c)
        if _sane_counts(c):
            return c
    c = resolve_counts(parse_counts_from_lines(ocr_lines(img)[0]))
    return c if _sane_counts(c) else {}


def resolve_counts(c: dict) -> dict:
    """把读到的构成套一遍「每类 ≤ MAX_PER_TYPE」，能纠就纠，纠不了一律作废。"""
    if not c:
        return {}
    if _sane_counts(c):
        return c
    fixed, _ = repair_counts(c)
    return fixed if _sane_counts(fixed) else {}


def _station_type(j: dict, p: int):
    """第 p 站（1-based）实际是什么类型：靠前后两帧剩余构成的差值反推。"""
    n = j["n"]
    cur = j["snap"].get(str(n - p))
    prev = j["snap"].get(str(n - p + 1))
    if not cur or not prev:
        return None
    if sum(prev.values()) - sum(cur.values()) != 1:
        return None
    dec = [t for t in J.TYPES if prev.get(t, 0) - cur.get(t, 0) == 1]
    return dec[0] if len(dec) == 1 else None


def build_hints(j: dict, i: int) -> tuple[list, list, dict]:
    """收集仍然管到未来的提示，转成相对「下一站」的窗口 + 历史。

    返回 (hints, past, meta)。past 是已走过站的类型（最早的在前），
    只保留连续已知的那一段；历史不全的旧提示会被剔除并记在 meta 里。
    """
    # 连续已知的历史长度（最多 2 站就够 span=3 用）
    known: list = []
    for back in range(1, 3):
        t = _station_type(j, i - back + 1) if i - back + 1 >= 1 else None
        if t is None:
            break
        known.append(t)
    known.reverse()  # 最早的在前

    hints, used, dropped = [], [], []
    for key, rec in (j.get("hints") or {}).items():
        try:
            i2 = int(key)
        except ValueError:
            continue
        start = i2 - i
        span = int(rec.get("span") or 3)
        if start + span <= 0:
            continue  # 这个窗口已经完全落在身后了
        if -start > len(known):
            dropped.append(rec.get("raw") or "")
            continue  # 历史缺帧，判断不了，宁可不约束
        hints.append(J.Hint(start, span, rec.get("kind"), rec.get("target")))
        used.append((start, rec.get("raw") or "", span))

    meta = {"used": used, "dropped": dropped, "past": known, "station": i}
    return hints, known, meta


def parse_cards(lines, probs: dict) -> list[dict]:
    """识别「策略卡」界面：每张卡都是"如果下一站是/不是某类型"。

    返回按命中概率从高到低排好的列表。
    """
    if not probs:
        return []
    titles = [ln for ln in lines if "·" in ln.text and ln.box[1] < 700]
    seeds = [ln for ln in lines if "如果下一站" in ln.text.replace(" ", "")]
    if not seeds:
        return []

    cards = []
    for sd in seeds:
        sx, sy = sd.box[0] + sd.box[2] / 2, sd.box[1]
        col = [ln for ln in lines
               if abs(ln.box[0] + ln.box[2] / 2 - sx) < 130 and sy - 15 <= ln.box[1] <= sy + 130]
        col.sort(key=lambda l: (l.box[1], l.box[0]))
        text = "".join(l.text.replace(" ", "") for l in col)
        m = CARD_COND.search(text)
        if not m:
            continue
        neg, target = m.group(1) == "不是", m.group(2)
        if target not in probs:
            continue
        p = (1 - probs[target]) if neg else probs[target]

        title = ""
        best_dy = 1e9
        for t in titles:
            dx = abs(t.box[0] + t.box[2] / 2 - sx)
            dy = sy - t.box[1]
            if dx < 90 and 0 < dy < 120 and dy < best_dy:
                best_dy, title = dy, t.text.replace(" ", "")

        rew = re.search(r"获得\d+件【(.+?)】", text)
        cards.append({
            "title": title or ("非" + target if neg else target),
            "target": target,
            "negated": neg,
            "reward": rew.group(1) if rew else "",
            "p": p,
        })

    cards.sort(key=lambda c: -c["p"])
    return cards


# ------------------------------------------------------------------ 报结果
def _bar(p: float, width: int = 22) -> str:
    n = max(0, min(width, round(p * width)))
    return "█" * n + "·" * (width - n)


def _checked_counts(lines, j: dict) -> tuple[dict, list]:
    """解析剩余构成，并用硬规则纠掉认错的数字。

    硬规则有两条：每类 ≤ MAX_PER_TYPE（6 个），且不超过本局开局值。
    任何一条过不了，要么纠回来，要么整帧丢掉。
    """
    raw = parse_counts_from_lines(lines)
    if not raw:
        return {}, []
    over = []
    if not _sane_counts(raw):
        # 出现了 >6 的数：一定是读错，先按上限纠，纠不了就丢这一帧
        raw, over = repair_counts(raw)
        if not _sane_counts(raw):
            return {}, []
    base = j.get("base") or {}
    if not base:
        return raw, over
    n = sum(base.values())
    s = sum(raw.values())
    if s >= n + 3:
        # 比本局开局还多出好几站：要么是换了一局，要么整行认错了。
        # 连续两帧读到同一个更大的数才认新局，单帧大跳一律丢。
        if _confirm_new_run(s):
            return raw, over
        return {}, []
    fixed, changed = repair_counts(raw, max_total=n, prev=base)
    return fixed, sorted(set(over) | set(changed))


# 兜底用的三条「竖条」。
# 为什么是竖的不是横的：检测模型把长边压到 640，横着拉宽 → 字变小 → 认不出；
# 竖着拉高只要宽度不超标，长边还是宽度，字的大小完全不变。
# 三条从左往右覆盖「面板贴右」「面板在中间」「UI 用固定像素、窗口一宽就相对靠左」
# 三种布局。多数机器第一条就中，后面的根本不会跑。
#
# 宽度必须按画面比例给，不能写死像素：
#   识别模型看到的字高 ≈ 画面字高 × 640 / 竖条宽度，
# 竖条宽度写成固定的 700 时，小窗口（1024 宽）的字就被压到 13px、直接认不出；
# 改成 0.42×宽度 之后这个值恒定在 21px 左右，1600/1920/2560/4K 都一样。
_STRIP_W_FRAC = 0.42
_STRIP_START = (1.0, 0.52, 0.26)     # 三条竖条的左边界（占画面宽度的比例）


def _strip_boxes(size):
    w, h = size
    sw = max(320.0, min(float(w), w * _STRIP_W_FRAC))
    sh = float(h) * 0.55
    out = []
    for fx in _STRIP_START:
        x2 = float(w) if fx >= 1.0 else min(float(w), w * fx + sw)
        x1 = max(0.0, (w - sw) if fx >= 1.0 else w * fx)
        out.append((x1, 0.0, min(float(w), x2), sh))
    return out


# 最后一层兜底：把整个画面切成 3×2 块挨个认。
# 竖条只覆盖「右半边」，万一游戏 UI 不按常规摆（超宽屏、HUD 缩放、窗口被拖成
# 奇怪比例、面板跑到中间），竖条永远找不到。分块之后每块宽度约 0.36×画面宽，
# 字高仍然在 24px 左右，认得出来，代价是要跑 6 次识别（约 2.5 秒）。
# 只在前两层连续失败、并且隔 15 秒以上才跑一次，正常使用根本碰不到。
_TILE_COLS, _TILE_ROWS = 3, 2
_TILE_H = 0.78                       # 只扫上面 78%：面板不会在屏幕最底下


def _tile_boxes(size):
    w, h = size
    tw = float(w) / _TILE_COLS
    th = float(h) * _TILE_H / _TILE_ROWS
    out = []
    for r in range(_TILE_ROWS):
        for c in range(_TILE_COLS):
            x1 = max(0.0, c * tw - w * 0.04)
            x2 = min(float(w), (c + 1) * tw + w * 0.04)
            y1 = max(0.0, r * th - h * 0.02)
            y2 = min(float(h), (r + 1) * th + h * 0.02)
            out.append((x1, y1, x2, y2))
    return out


def _box_frac(box, size):
    w, h = size
    return (box[0] / w, box[1] / h, box[2] / w, box[3] / h)


# 兜底要跑两次 OCR，不是常规路径：上一次没找到就歇一会儿再来，而且越试越久。
# 否则游戏停在商店/大地图界面时，后台会一直满负荷跑 OCR，风扇狂转还卡。
_SCAN_EVERY = 4.0
_scan_at = 0.0
_scan_wait = _SCAN_EVERY

# 全屏分块那层更贵（6 次识别），单独退避，起步 15 秒。
_FULL_EVERY = 15.0
_full_at = 0.0
_full_miss = 0          # 连续失败几次，够了才轮到全屏分块

# 最近一次读取的现场信息（给界面上的「诊断」和错误提示用）
LAST = {}


def _may_scan() -> bool:
    global _scan_at
    now = time.time()
    if now - _scan_at < _scan_wait:
        return False
    _scan_at = now
    return True


def _may_full() -> bool:
    global _full_at
    now = time.time()
    if now - _full_at < _FULL_EVERY:
        return False
    _full_at = now
    return True


def _scan_failed() -> None:
    """扫描没找到：下次等更久（4s → 12s → 36s → 封顶 60s）。"""
    global _scan_wait
    _scan_wait = min(60.0, _scan_wait * 3.0)


def _scan_worked() -> None:
    global _scan_wait
    _scan_wait = _SCAN_EVERY


def reset_scan_backoff() -> None:
    """画面明显变了（用户切到铁路界面了）就立刻允许重新找，不用等退避。"""
    global _scan_at, _scan_wait, _full_at
    _scan_at = 0.0
    _scan_wait = _SCAN_EVERY
    _full_at = 0.0


def _is_count_line(ln) -> bool:
    t = ln.text.replace(" ", "")
    if not t:
        return False
    return "剩余" in t or "旅程" in t or bool(J.parse_counts(t))


def _learn_from(lines, img: Image.Image, frac) -> None:
    """在兜底区域里认到面板之后，把真实位置缩到最小记下来。

    记的是比例（0~1），换分辨率、换窗口大小、换机器都能直接复用。
    下一帧只读这一小条：既快，又不会把旁边的商店按钮/日志误当成提示。
    """
    w, h = img.size
    rows = [ln for ln in lines if _is_count_line(ln)]
    if not rows:
        return
    y0 = min(ln.box[1] for ln in rows)
    y2 = max(ln.box[1] + ln.box[3] for ln in rows)
    lh = max(10.0, float(y2 - y0))
    top = max(0.0, y0 - lh * 3.4)      # 计数行上方还有「未来3站…」提示，一起圈进来
    bottom = min(float(h), y2 + lh * 1.4)
    # 左右留一点余量，别把数字贴死在边界上；但整体宽度必须控制住，
    # 太宽的裁剪会让识别模型把字压小（见 _strip_boxes 上面的说明）。
    left = max(0.0, min(ln.box[0] for ln in rows) - w * 0.10)
    right = min(float(w), max(frac[2] * w, max(ln.box[0] + ln.box[2] for ln in rows)
                             + w * 0.06))
    if (right - left) / w > 0.46:
        left = max(0.0, right - w * 0.46)
    f = (left / w, top / h, right / w, min(1.0, bottom / h))
    if f[2] - f[0] < 0.15 or f[3] - f[1] < 0.05:
        return
    save_learned_panel(f)


def read_panel(img: Image.Image, j: dict | None = None, allow_scan: bool = True):
    """只读右侧面板取剩余构成。

    三级策略，一命中就停：
      1. 上次学到的位置（通常就是第 1 次读时学到的，之后一直用它，最快）；
      2. 1600x900 量出来的标准位置；
      3. 标准位置全落空 → 在右上角铺一层等大的小块逐块试。
    第 3 级存在的理由：游戏 UI 不保证跟分辨率等比缩放，换了分辨率/显示器/
    HUD 缩放之后，面板可能整体挪了位置，死坐标读出来就是一片背景。
    一旦在第 3 级找到，就把真实位置按比例记下来，以后直接走第 1 级。
    """
    global _ACTIVE_FRAC, _full_miss
    j = j if j is not None else _load_journey()
    name = ""
    hit = None
    tried = set()
    seen = 0            # 主区域认到了几行字（诊断用：0 行=识别本身没跑起来）

    plans = []
    learned = load_learned_panel()
    if learned:
        plans.append(learned)
    plans.append(_frac(PANEL_BOX))

    for frac in plans:
        key = tuple(round(v, 4) for v in frac)
        if key in tried:
            continue
        tried.add(key)
        _ACTIVE_FRAC = frac
        lines, name = ocr_lines_fast(img, scale=2.0,
                                     regions=[_frac_to_px(frac, img.size)])
        seen = max(seen, len(lines))
        counts, changed = _checked_counts(lines, j)
        if counts:
            hit = (frac, lines, counts, changed, False)
            break

    stage = "标准位置" if hit else ""
    if hit and learned and tuple(round(v, 4) for v in hit[0]) == tuple(
            round(v, 4) for v in learned):
        stage = "校准位置"
    if hit is None and allow_scan and _may_scan():
        for box in _strip_boxes(img.size):
            frac = _box_frac(box, img.size)
            _ACTIVE_FRAC = frac
            lines, name = ocr_lines_fast(img, scale=2.0, regions=[box])
            seen = max(seen, len(lines))
            counts, changed = _checked_counts(lines, j)
            if counts:
                _learn_from(lines, img, frac)
                _scan_worked()
                _full_miss = 0
                hit = (load_learned_panel() or frac, lines, counts, changed, True)
                stage = "竖条扫描"
                break
        else:
            _scan_failed()
            _full_miss += 1          # 竖条也没中，攒够次数才轮到全屏分块
            stage = "竖条扫描失败"

    # 第三层：整个画面切块挨个认。竖条只覆盖右半边，布局再怪一点就永远找不到。
    if hit is None and allow_scan and _full_miss >= 2 and _may_full():
        for box in _tile_boxes(img.size):
            frac = _box_frac(box, img.size)
            _ACTIVE_FRAC = frac
            lines, name = ocr_lines_fast(img, scale=2.0, regions=[box])
            seen = max(seen, len(lines))
            counts, changed = _checked_counts(lines, j)
            if counts:
                _learn_from(lines, img, frac)
                _scan_worked()
                _full_miss = 0
                hit = (load_learned_panel() or frac, lines, counts, changed, True)
                stage = "全屏分块找到"
                break
        else:
            _full_miss += 1
            stage = "全屏分块也没找到"
    LAST.update({
        "size": tuple(img.size),
        "backend": name,
        "lines": seen,
        "stage": stage or "主区域",
        "learned": bool(load_learned_panel()),
        "box": _frac_to_px(_ACTIVE_FRAC, img.size),
    })

    if hit is None:
        _ACTIVE_FRAC = _frac(PANEL_BOX)
        return [], name, {}, []

    frac, lines, counts, changed, learned_now = hit
    if not changed:
        return lines, name, counts, changed

    # 数字过不了约束（比如把 x1 认成 x11）：按同一块位置放大 3 倍复核一次
    _ACTIVE_FRAC = frac
    lines3, _ = ocr_lines_fast(img, scale=3.0, regions=[_frac_to_px(frac, img.size)])
    c3, ch3 = _checked_counts(lines3, j)
    if c3 and not ch3:
        return lines3, name, c3, ch3
    if c3 and counts and len(ch3) < len(changed):
        return lines3, name, c3, ch3
    return lines, name, counts, changed


def read_cards(img: Image.Image, probs: dict) -> list:
    """只读策略卡那块区域（确认是选卡界面时才调，省一次识别）。"""
    lines, _ = ocr_lines_fast(img, scale=2.0, regions=card_regions(img))
    return parse_cards(lines, probs.get(1, {})) if probs else []


def save_diag(img: Image.Image, note: str = "") -> str:
    """把「这一帧长什么样、程序在读哪一块、读到了什么字」打包存成一个文件夹。

    别的机器上读不到时，让用户点一下「诊断」把这个文件夹发过来就够了：
    画面尺寸、读取区域、识别出的原文、OCR 引擎，全在里面，不用猜。
    """
    from PIL import ImageDraw

    stamp = time.strftime("%Y%m%d_%H%M%S")
    d = os.path.join(LOGS, "diag_" + stamp)
    os.makedirs(d, exist_ok=True)
    w, h = img.size

    try:
        img.save(os.path.join(d, "1_画面.png"))
    except Exception:
        pass

    frac = panel_frac()
    box = _frac_to_px(frac, img.size)
    try:
        crop = img.crop(box)
        crop.resize((crop.width * 2, crop.height * 2), Image.BILINEAR).save(
            os.path.join(d, "2_读取区域.png"))
    except Exception:
        crop = None

    try:
        mark = img.copy()
        dr = ImageDraw.Draw(mark)
        dr.rectangle(box, outline=(255, 179, 64), width=3)
        dr.rectangle(scale_box(PANEL_BOX, img.size), outline=(90, 160, 255), width=2)
        mark.save(os.path.join(d, "3_区域标注.png"))
    except Exception:
        pass

    lines, name = [], ""
    try:
        lines, name = ocr_lines_fast(img, scale=2.0, regions=[box])
    except Exception as e:
        lines = []
        name = f"识别失败：{e}"

    with open(os.path.join(d, "4_识别文字.txt"), "w", encoding="utf-8") as f:
        f.write(f"# 区域 {box} 共 {len(lines)} 行\n")
        for ln in lines:
            f.write(f"{ln.conf:.3f}\t{ln.box}\t{ln.text}\n")

    counts = {}
    try:
        counts, changed = _checked_counts(lines, _load_journey())
    except Exception:
        changed = []

    with open(os.path.join(d, "0_说明.txt"), "w", encoding="utf-8") as f:
        f.write("铁路大亨推测 · 读取诊断\n")
        f.write(f"时间       {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        if note:
            f.write(f"备注       {note}\n")
        f.write(f"画面尺寸   {w} × {h}\n")
        f.write(f"读取区域   {box}（比例 {tuple(round(v, 4) for v in frac)}）\n")
        f.write(f"标准区域   {scale_box(PANEL_BOX, img.size)}\n")
        f.write(f"是否已校准 {'是' if load_learned_panel() else '否（用的标准位置）'}\n")
        f.write(f"最近阶段   {LAST.get('stage', '-')}\n")
        f.write(f"OCR 引擎   {name or '-'}，本区域认到 {len(lines)} 行\n")
        f.write(f"解析出的剩余 {counts or '无'}\n")
        f.write(f"纠错记录   {changed or '无'}\n")
        f.write("\n若「认到 0 行」：识别引擎本身没跑起来（模型缺失/路径异常）。\n"
                "若认到很多行但没有「旅程剩余站点」：读取区域偏了，点「校准」框一次。\n")
    return d


def compute_img(img: Image.Image, with_cards: bool = False,
                allow_scan: bool = True) -> dict:
    """从一帧画面直接算结论（窗口版走这条）。

    默认**只读右侧面板**：整屏识别要 3~8 秒，而真正需要的几行字全在面板里；
    走到别的界面读不到就等下一帧，不拿整屏去兜底（那正是以前卡顿的来源）。
    """
    j = _load_journey()
    lines, name, counts, changed = read_panel(img, j, allow_scan=allow_scan)
    if with_cards and counts:
        extra, _ = ocr_lines_fast(img, scale=2.0, regions=card_regions(img))
        lines = lines + extra
    parsed = compute(lines, counts=counts or None)
    parsed["ocr"] = name
    parsed["counts_fixed"] = changed
    return parsed


def compute(lines, counts=None) -> dict:
    parsed = parse_screen(lines)
    if counts is not None:
        parsed["counts"] = counts
    counts = parsed["counts"]
    if counts:
        counts = resolve_counts(counts)   # 每类最多 6 个：超了先纠，纠不动就不算
        parsed["counts"] = counts
    hints, past, meta = [], [], {"used": [], "dropped": [], "past": [], "station": 0}
    probs = {}
    cards = []
    if counts:
        j = update_journey(parsed)
        meta["station"] = j.get("i", 0)
        hints, past, meta = build_hints(j, meta["station"])
        depth = min(4, sum(counts.values()))
        if not J.consistent(counts, hints, depth, past):
            # 约束互相矛盾（多半是某帧 OCR 认错）：退回只用当前这条提示
            cur = parsed["hint_obj"]
            hints = [cur] if cur else []
            past = []
            meta["conflict"] = True
        probs = J.prefix_probs(counts, hints, depth=depth, past=past)
        if probs:
            cards = parse_cards(lines, probs[1])
    parsed["probs"] = probs
    parsed["cards"] = cards
    parsed["hints"] = hints
    parsed["past"] = past
    parsed["meta"] = meta
    return parsed


def build_report(parsed: dict) -> str:
    counts = parsed["counts"]
    hint = parsed.get("hint_obj")
    probs = parsed.get("probs") or {}
    out = []

    if not counts:
        out.append(" 没读到「旅程剩余站点」那一行，无法精算。")
        out.append(" 先停到能看到右侧「未来3站…／旅程剩余站点：…」的界面，")
        out.append(" 或者用 dump 看看 OCR 到底读到了什么。")
        return "\n".join(out)

    total = sum(counts.values())
    out.append(f" 剩余 {total} 站：{J.describe_counts(counts)}"
               + (f"   （顶部「剩余{parsed['rest']}站」）" if parsed.get("rest") else ""))
    base = _load_journey().get("base") or {}
    if base:
        j = _load_journey()
        out.append(f" 本局：已走 {j.get('i', 0)} 站"
                   f"（开局 {sum(base.values())} 站：{J.describe_counts(base)}）"
                   f" · 每类最多 {MAX_PER_TYPE} 个、数量只会变少，超出就是认错，已自动纠正")
    else:
        out.append(" 本局：还没开局 · 点「重置」再开新的一局，开局那一下才开始记账")
    out.append(f" 本站提示：{hint.describe() if hint else '没读到（按无提示算）'}")

    meta = parsed.get("meta") or {}
    used = meta.get("used") or []
    old = [x for x in used if x[0] < 0]
    if old:
        out.append(f" 仍在生效的旧提示 {len(old)} 条（窗口还没走完）：")
        for start, raw, span in old:
            out.append(f"   · 「{raw}」→ 其中 {start + span} 站还在未来")
    if meta.get("dropped"):
        out.append(f" ⚠ 有 {len(meta['dropped'])} 条旧提示因中间缺帧、无法核对，已忽略。")
    out.append("")

    if meta.get("conflict"):
        out.append(" ⚠ 历史提示与当前剩余数量凑不出任何排列（多半是 OCR 认错），已退回只用本站提示。")
        out.append("")

    if not probs:
        out.append(" 没有可用的剩余站数。")
        return "\n".join(out)

    nxt = probs[1]
    best = max(nxt, key=lambda k: nxt[k])
    p = nxt[best]
    out.append(f" 下一站  →  【{best}】 {p*100:.1f}%")
    for t in J.TYPES:
        mark = " ←" if t == best else ""
        out.append(f"   {t}  {nxt[t]*100:5.1f}%  {_bar(nxt[t])}{mark}")
    out.append("")
    out.append(f" 判断：{_verdict(p, best, nxt)}")

    cards = parsed.get("cards") or []
    if cards:
        out.append("")
        out.append(f" 策略卡建议：选「{cards[0]['title']}」"
                   + (f"（{cards[0]['reward']}）" if cards[0]["reward"] else ""))
        for i, c in enumerate(cards):
            cond = f"下一站{'不是' if c['negated'] else '是'}{c['target']}"
            mark = "  ← 选这张" if i == 0 else ""
            out.append(f"   {c['title']:<10s} {cond:<12s} {c['p']*100:5.1f}%{mark}")

    if len(probs) > 1:
        out.append("")
        out.append(" 再往后（所有仍生效的提示一起约束，酒/食/商）：")
        for i in sorted(probs)[1:]:
            row = "  ".join(f"{probs[i][t]*100:5.1f}%" for t in J.TYPES)
            out.append(f"   第{i}站   {row}")
    return "\n".join(out)


def _verdict(p: float, best: str, probs: dict) -> str:
    if p >= 0.95:
        return f"基本锁定【{best}】"
    if p >= 0.65:
        return f"很可能是【{best}】，但别当 100% 用"
    if p >= 0.50:
        return f"偏向【{best}】，还有变数"
    if len(set(round(v, 3) for v in probs.values())) == 1:
        return "三种完全一样，这站没法预判"
    return "信息不足，三种差不多"


# ------------------------------------------------------------------ 命令
def cmd_dump() -> int:
    img, gw = grab_game()
    img.save(os.path.join(LOGS, "next_station_frame.png"))
    lines, name = ocr_lines(img)
    print(f" 窗口 {gw['hwnd']} 客户区 {gw['client']}  OCR={name}  {len(lines)} 行")
    print(f" 截图 logs/next_station_frame.png\n")
    for ln in sorted(lines, key=lambda l: (l.box[1], l.box[0])):
        print(f"  {str(ln.box):24s} {ln.text}")
    return 0


def one_shot() -> int:
    img, _gw = grab_game()
    parsed = compute_img(img)
    print(f" [OCR {parsed.get('ocr', '?')}]\n")
    print(build_report(parsed))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("cmd", nargs="?", default="auto", choices=["auto", "dump"])
    a = ap.parse_args()

    from bot.dpi import enable as _dpi_enable

    _dpi_enable()      # 抓屏前声明 DPI 感知，否则缩放 150% 的屏上尺寸会算错

    os.makedirs(LOGS, exist_ok=True)
    if a.cmd == "dump":
        return cmd_dump()
    return one_shot()


if __name__ == "__main__":
    sys.exit(main())
