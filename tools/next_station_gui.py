"""铁路大亨 · 下一站推测面板。

双击 `铁路大亨推测.bat` 弹出小窗，常驻屏幕角落：

    ┌──────────────────────────────────┐
    │ 铁路大亨 · 下一站          ● 在线 │
    │  下一站                           │
    │  商行                     100.0%  │
    │  酒庄  ░░░░░░░░░░░░░░░     0.0%  │
    │  食铺  ░░░░░░░░░░░░░░░     0.0%  │
    │  商行  ███████████████   100.0%  │
    │ ────────────────────────────      │
    │  后续站点   第2站   第3站   第4站 │
    │  酒庄        0%      0%     50%  │
    │  食铺        0%    100%     50%  │
    │  商行      100%      0%      0%  │
    │ ────────────────────────────      │
    │  剩余 3 站 · 酒庄x0 食铺x1 商行x2 │
    │  提示 未来3站中商行最多           │
    │  本局 开局 15 站 · 已走 12 站     │
    └──────────────────────────────────┘

两条原则：
  * 后台抓屏，不抢前台、不动鼠标；
  * **只在「旅程剩余站点」的数字变了之后才刷新**，平时画面怎么变都不动。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback

if getattr(sys, "frozen", False):
    # exe 里跑：模块都打进 exe 了，sys.path 不用动
    HERE = ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(HERE)
    sys.path.insert(0, HERE)
    sys.path.insert(0, ROOT)

import tkinter as tk  # noqa: E402

import numpy as np  # noqa: E402

import next_station as NS  # noqa: E402
from bot import journey as J  # noqa: E402

ROOT = NS.ROOT  # 可写目录（exe 放在没权限的地方时会退到 LOCALAPPDATA）
POS_FILE = os.path.join(ROOT, "data", "gui_pos.json")
LOG_FILE = os.path.join(ROOT, "logs", "gui.log")

BG = "#0e1015"
CARD = "#181c24"
LINE = "#2a3040"
GOLD = "#e6c07b"
GOLD_BG = "#3a3325"
TXT = "#eef1f6"
DIM = "#8790a3"
RED = "#e06c75"
GREEN = "#7ec699"
TRACK = "#242a36"

F_TITLE = ("Microsoft YaHei UI", 10)
F_SMALL = ("Microsoft YaHei UI", 9)
F_BIG = ("Microsoft YaHei UI", 30, "bold")
F_PCT = ("Microsoft YaHei UI", 18, "bold")
F_MID = ("Microsoft YaHei UI", 10)
F_CELL = ("Microsoft YaHei UI", 10, "bold")
F_HEAD = ("Microsoft YaHei UI", 9)

# 判定「画面变了」用的区域：右侧信息面板（提示 + 剩余站点）和策略卡区。
# 坐标是 1600x900 客户区下的基准值，实际按帧尺寸等比换算（换分辨率/全屏也对得上）。
# 面板是纯 UI，阈值压得低；策略卡区盖住了 3D 场景，画面一直在动，
# 所以给它单独一个高得多的阈值——只有"整块被弹窗盖住"这种大变化才算。
WATCH_BOXES = [NS.PANEL_BOX, (230, 430, 1470, 650)]
DIFF_THRESHOLD = 5.0     # 面板区
DIFF_CARDS = 18.0        # 策略卡区
SIG_W, SIG_H = 160, 44
FRESH_BOARD_MIN = 8   # 新一局开局的站点总数至少这么多（残局一般只剩一两站）
MAX_AHEAD = 3  # 往后最多再展示 3 站


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _tip(widget, text: str) -> None:
    """极简悬停提示（tk 自带的没有）。"""
    state = {"win": None}

    def show(_e=None):
        if state["win"]:
            return
        x = widget.winfo_rootx() + widget.winfo_width() // 2 - 60
        y = widget.winfo_rooty() + widget.winfo_height() + 6
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        tk.Label(win, text=text, bg="#242a36", fg=TXT, font=F_SMALL,
                 padx=8, pady=4).pack()
        state["win"] = win

    def hide(_e=None):
        if state["win"]:
            state["win"].destroy()
            state["win"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


def _diag(img) -> str:
    """读不到时把「画面多大、在读哪一块、认到几行」一起报出来。

    换分辨率/换机器、装在别的目录之后读不到，第一件要确认的就是这几个数——
    有它们才能判断是区域偏了（认到很多行但没那句话），还是画面本身没到那个界面
    （一行都没认到）。
    """
    try:
        w, h = img.size
        b = NS._frac_to_px(NS.panel_frac(), img.size)
        where = "已校准位置" if NS.load_learned_panel() else "标准位置（还没校准过）"
        st = NS.LAST.get("stage") or "-"
        n = NS.LAST.get("lines")
        extra = f" · 认到 {n} 行" if isinstance(n, int) else ""
        return (f"画面 {w}×{h} · 读 {b[0]},{b[1]}→{b[2]},{b[3]} · {where} · {st}{extra}\n"
                f"一直这样的话：点「校准」，框一次那条「旅程剩余站点」，位置就记下来了")
    except Exception:
        return ""


def _signature(img) -> np.ndarray:
    """把关心的区域缩成小灰度图，用来判断界面有没有变。

    第一块用**实际在读的那块面板**（校准过就是校准的位置），
    否则面板挪过位置之后，盯着一个空背景看，画面变了也发现不了。
    """
    parts = []
    boxes = [NS._frac_to_px(NS.panel_frac(), img.size)]
    boxes += [NS.scale_box(b, img.size) for b in WATCH_BOXES[1:]]
    for bx, by, bx2, by2 in boxes:
        c = img.crop((bx, by, bx2, by2)).convert("L").resize((SIG_W, SIG_H), 1)
        parts.append(np.asarray(c, dtype=np.int16))
    return np.concatenate([p.ravel() for p in parts])


def _moved(cur, sig) -> bool:
    """面板区小幅变化 or 策略卡区大幅变化，都算"画面变了"。"""
    if sig is None:
        return True
    n = SIG_W * SIG_H
    if float(np.abs(cur[:n] - sig[:n]).mean()) > DIFF_THRESHOLD:
        return True
    return float(np.abs(cur[n:] - sig[n:]).mean()) > DIFF_CARDS


def _counts_key(counts: dict) -> tuple:
    """只用三种数量描述一帧画面——剩余站数没变就不刷新。"""
    return tuple(int(counts.get(t, 0) or 0) for t in J.TYPES)


class Worker(threading.Thread):
    """后台循环：抓帧 → 看面板变没变 → 变了才 OCR → 数字变了才推给界面。"""

    def __init__(self, q: queue.Queue):
        super().__init__(daemon=True)
        self.q = q
        self.stop_flag = threading.Event()
        self.force = threading.Event()
        self.reset_flag = threading.Event()
        self.pause = threading.Event()   # 校准/诊断时先停一下，别跟界面抢抓屏
        self.last_key = None      # 上一次推给界面的剩余构成
        self.last_cards = None
        self.miss = 0             # 连续几帧没读到（避免单帧读错就清屏）
        self.cards_at = 0.0       # 上次读策略卡的时间（限流用）
        self.last_stamp = 0.0     # 上次处理过的帧时间戳
        self.last_img = None      # 最近一帧原图（校准/诊断直接拿来用，不用再抓一次）
        self.size_checked = False

    # ------------------------------------------------------------ 重置
    def _check_size(self, g, img) -> None:
        """抓到的画面和窗口客户区对不对得上。

        对不上基本只有一个原因：显示器缩放不是 100%，而进程拿到的是被系统
        虚拟化过的逻辑尺寸——画面会被压/裁，读区域和点击坐标全错，
        这时候点多少次「校准」都没用。所以宁可当场把两个数报出来。
        """
        try:
            cw, ch = g.client_size()
        except Exception:
            return
        fw, fh = img.size
        if not cw or not ch or (cw, ch) == (fw, fh):
            return
        self.q.put(("warn",
                    f"⚠ 画面尺寸对不上：窗口 {cw}×{ch}，抓到 {fw}×{fh}（差 "
                    f"{max(cw / max(1, fw), ch / max(1, fh)):.2f} 倍）。"
                    f"把「设置 → 系统 → 显示 → 缩放」改成 100% 再启动一次；"
                    f"不改缩放的话，把游戏改成窗口化 1920×1080 也能用。"))

    def _restart(self, g, img) -> np.ndarray:
        """重置 = 这一局结束了，从头开始新的一局。

        只清空记录，绝不把「现在屏幕上剩几个」当成新一局的开局——那多半是
        上一局的残局（比如剩 3 站）。清空后盯着画面，等它真的动过、或者读到
        一个跟清空前不一样的数、并且连续两帧一致，才把那一帧确立为开局。
        """
        sig0 = _signature(img)
        try:
            NS.reset_game()
        except Exception as e:
            self.q.put(("err", f"重置失败：{e}"))
            return sig0
        self.last_key = None
        self.last_cards = None
        self.miss = 0
        self.q.put(("pending", ""))

        before = sum((NS.probe_counts(img) or {}).values())
        t0 = time.time()
        last, hits = None, 0
        while not self.stop_flag.is_set():
            if self.reset_flag.is_set():  # 等待期间又点了一次
                return sig0
            self.stop_flag.wait(0.30)
            try:
                frame, _ = g.grab()
            except Exception:
                continue
            c = NS.probe_counts(frame)
            s = sum(c.values())
            if s <= 0:
                if time.time() - t0 > 60:
                    self.q.put(("pending", "还没读到新一局（游戏要停在能看到「旅程剩余站点」的界面）"))
                    t0 = time.time()
                continue
            hits = hits + 1 if s == last else 1
            last = s
            if hits < 2:
                continue
            moved = _moved(_signature(frame), sig0)
            if not moved and s == before and time.time() - t0 < 20:
                continue  # 还是重置前那个画面，再等等
            if time.time() - t0 >= 20 and s < FRESH_BOARD_MIN:
                # 等了 20 秒画面还是老的、数字又明显是残局（剩一两站）：
                # 宁可继续等，也不把残局当成新一局的开局。
                self.q.put(("pending", "已重置 · 等新一局开始（现在画面上的数字还是上一局的残局）"))
                t0 = time.time()
                continue
            r = NS.begin_game(c)
            self.last_key = None
            self.q.put(("reset", r))
            return _signature(frame)
        return sig0

    def run(self) -> None:
        g = NS.Grabber()
        sig = None
        try:
            while not self.stop_flag.is_set():
                if self.pause.is_set():
                    self.stop_flag.wait(0.2)
                    self.last_stamp = 0.0
                    continue
                try:
                    # 画面根本没更新（WGC 只在内容变化时才给新帧）就什么都不用做。
                    # 但上一次没读到时必须重试：识别新局要"连续两帧同一个数"，
                    # 静止画面下如果也跳过，就永远凑不齐两票。
                    st = g.stamp()
                    if (st and st == self.last_stamp
                            and not self.force.is_set() and self.miss == 0):
                        self.stop_flag.wait(0.12)
                        continue
                    img, _hwnd = g.grab()
                    self.last_img = img
                    self.last_stamp = st or self.last_stamp
                    if not self.size_checked:
                        self.size_checked = True
                        self._check_size(g, img)
                except Exception as e:
                    self.q.put(("err", str(e)))
                    self.stop_flag.wait(2.0)
                    continue

                if self.reset_flag.is_set():
                    self.reset_flag.clear()
                    sig = self._restart(g, img)
                    self.last_stamp = 0.0
                    self.miss = 0
                    continue

                cur = _signature(img)
                changed = _moved(cur, sig)
                forced = self.force.is_set()
                if not changed and not forced:
                    # 画面没动：抓帧和比对都是毫秒级的，快轮询才能"走动一站立刻跟上"
                    self.stop_flag.wait(0.12)
                    continue
                if changed and not forced:
                    # 画面明显变了：可能是刚切到铁路界面。重新给兜底一个机会，
                    # 否则上一次在商店界面连扫几次失败，退避会拖到 60 秒后才重试。
                    NS.reset_scan_backoff()
                self.force.clear()

                self.q.put(("busy", ""))
                try:
                    # 前两帧读不到先不算数（可能只是刚切界面/正在转场）；
                    # 连着读不到才允许去右上角重新找面板，那一步要跑两次识别。
                    parsed = NS.compute_img(img, allow_scan=self.miss >= 2)
                except Exception as e:
                    self.q.put(("err", str(e)))
                    self.stop_flag.wait(0.3)
                    continue

                counts = parsed.get("counts") or {}
                if not counts:
                    # 单帧读错（比如读到 >6 的数被作废）不立刻清屏，连续几次才报错。
                    # 同一帧最多试两次：读不到就把 sig 记下，别在没面板的界面上空转。
                    self.miss += 1
                    if self.miss >= 2:
                        sig = cur
                    if self.last_key is not None and self.miss < 4:
                        self.q.put(("same", ""))
                    else:
                        self.q.put(("err", "没读到「旅程剩余站点」那一行\n"
                                    + _diag(img)))
                    self.stop_flag.wait(0.3)
                    continue
                self.miss = 0
                sig = cur

                key = _counts_key(counts)
                if key != self.last_key or forced:
                    self.last_key = key
                    self.last_cards = tuple(c["title"] for c in (parsed.get("cards") or []))
                    self.q.put(("ok", parsed))
                else:
                    # 数字没变但画面变了：多半是切到了策略卡界面。
                    # 单独读卡片区（一次识别），限流 2 秒，免得场景动画一直触发。
                    now = time.time()
                    if now - self.cards_at > 2.0 and parsed.get("probs"):
                        self.cards_at = now
                        cards = NS.read_cards(img, parsed["probs"])
                        titles = tuple(c["title"] for c in cards)
                        if titles and titles != self.last_cards:
                            self.last_cards = titles
                            parsed["cards"] = cards
                            self.q.put(("cards", parsed))
                            self.stop_flag.wait(0.2)
                            continue
                    self.q.put(("same", ""))
                self.stop_flag.wait(0.2)
        finally:
            g.stop()


def _close_splash() -> None:
    """关掉打包版 exe 的启动画面。

    只有单文件版才有 splash（靠环境变量 `_PYI_SPLASH_IPC` 通信）；
    文件夹版和源码运行都没有这个变量，直接跳过——否则会打一串没用的报错。
    """
    if "_PYI_SPLASH_IPC" not in os.environ:
        return
    try:
        import pyi_splash  # type: ignore

        pyi_splash.close()
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.worker = Worker(self.q)
        self.has_data = False

        root.title("铁路大亨 · 下一站")
        root.configure(bg=BG)
        root.geometry(self._start_geom())
        root.attributes("-topmost", True)
        root.minsize(430, 470)

        self._build()
        self._warmup()
        self.worker.start()
        root.after(150, self._pump)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 窗口已经画出来了，启动图可以退了
        root.after(60, _close_splash)

    def _warmup(self) -> None:
        """提前把 OCR 模型加载 + 空跑一次。

        模型加载要 2~4 秒，放在第一次真正读数之前完成，界面才不会一开局就"卡一下"。
        """
        def work() -> None:
            try:
                NS.reader()
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 位置
    def _start_geom(self) -> str:
        try:
            with open(POS_FILE, encoding="utf-8") as f:
                p = json.load(f)
                return f"462x600+{p['x']}+{p['y']}"
        except Exception:
            pass
        sw, sh = 1920, 1080
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        except Exception:
            pass
        return f"462x600+{max(0, sw - 482)}+{max(0, sh - 660)}"

    def _save_pos(self) -> None:
        try:
            os.makedirs(os.path.dirname(POS_FILE), exist_ok=True)
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()}, f)
        except Exception:
            pass

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=16, pady=(10, 2))
        tk.Label(head, text="铁路大亨 · 下一站", bg=BG, fg=DIM, font=F_TITLE).pack(side="left")
        self.dot = tk.Label(head, text="● 启动中", bg=BG, fg=DIM, font=F_SMALL)
        self.dot.pack(side="right")
        # 双击状态点 = 忘掉记住的面板位置重新找一遍。
        # 换分辨率/换显示器之后如果一直读不到，点这里比重新装还快。
        self.dot.bind("<Double-Button-1>", lambda _e: self._relocate())
        _tip(self.dot, "双击：重新定位面板位置")

        card = tk.Frame(self.root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=12, pady=6)

        # ---- 下一站 ----
        tk.Label(card, text="下一站", bg=CARD, fg=DIM, font=F_SMALL).pack(anchor="w", padx=16, pady=(10, 0))
        big = tk.Frame(card, bg=CARD)
        big.pack(fill="x", padx=14)
        self.big_type = tk.Label(big, text="?", bg=CARD, fg=GOLD, font=F_BIG)
        self.big_type.pack(side="left")
        self.big_pct = tk.Label(big, text="", bg=CARD, fg=GOLD, font=F_PCT)
        self.big_pct.pack(side="right", pady=(10, 0))

        self.bars = {}
        for t in J.TYPES:
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=16, pady=2)
            tk.Label(row, text=t, bg=CARD, fg=TXT, font=F_MID, width=3, anchor="w").pack(side="left")
            cv = tk.Canvas(row, width=196, height=10, bg=CARD, highlightthickness=0)
            cv.pack(side="left", padx=(8, 10))
            cv.create_rectangle(0, 1, 196, 9, fill=TRACK, width=0)
            fill = cv.create_rectangle(0, 1, 0, 9, fill=GOLD, width=0)
            pct = tk.Label(row, text="—", bg=CARD, fg=DIM, font=F_SMALL, width=6, anchor="e")
            pct.pack(side="right")
            self.bars[t] = (cv, fill, pct)

        self.lbl_verdict = tk.Label(card, text="", bg=CARD, fg=DIM, font=F_SMALL, anchor="w")
        self.lbl_verdict.pack(fill="x", padx=16, pady=(2, 0))

        self.sep_top = tk.Frame(card, bg=LINE, height=1)
        self.sep_top.pack(fill="x", padx=16, pady=(8, 6))

        # ---- 后续站点矩阵（整块可隐藏：读不到数据时不显示空表） ----
        self.ahead_box = tk.Frame(card, bg=CARD)
        self.ahead_box.pack(fill="x", padx=16)

        ahead = tk.Frame(self.ahead_box, bg=CARD)
        ahead.pack(fill="x")
        tk.Label(ahead, text="后续站点", bg=CARD, fg=DIM, font=F_SMALL, anchor="w").pack(side="left")
        self.lbl_ahead_note = tk.Label(ahead, text="", bg=CARD, fg=DIM, font=F_SMALL, anchor="e")
        self.lbl_ahead_note.pack(side="right")

        grid = tk.Frame(self.ahead_box, bg=CARD)
        grid.pack(fill="x", pady=(4, 0))
        grid.columnconfigure(0, minsize=52, weight=0)
        for i in range(1, MAX_AHEAD + 1):
            grid.columnconfigure(i, minsize=84, weight=1)

        self.g_head = []
        for i in range(1, MAX_AHEAD + 1):
            lb = tk.Label(grid, text=f"第{i + 1}站", bg=CARD, fg=DIM, font=F_HEAD,
                          width=7, pady=3)
            lb.grid(row=0, column=i, padx=2, pady=1, sticky="nsew")
            self.g_head.append(lb)

        self.g_cells = {}   # (row_index, col) -> Label
        self.g_name = []
        for r, t in enumerate(J.TYPES):
            name = tk.Label(grid, text=t, bg=CARD, fg=TXT, font=F_SMALL, anchor="w", width=4)
            name.grid(row=r + 1, column=0, sticky="w", padx=(4, 0), pady=3)
            self.g_name.append(name)
            for i in range(1, MAX_AHEAD + 1):
                lb = tk.Label(grid, text="—", bg=CARD, fg=DIM, font=F_CELL,
                              width=7, pady=3)
                lb.grid(row=r + 1, column=i, padx=2, pady=1, sticky="nsew")
                self.g_cells[(r, i)] = lb

        self.sep_bot = tk.Frame(card, bg=LINE, height=1)
        self.sep_bot.pack(fill="x", padx=16, pady=(8, 8))

        # ---- 底部信息 ----
        self.lbl_rest = tk.Label(card, text="剩余 —", bg=CARD, fg=TXT, font=F_MID,
                                 anchor="w", justify="left", wraplength=400)
        self.lbl_rest.pack(fill="x", padx=16)
        self.lbl_hint = tk.Label(card, text="提示 —", bg=CARD, fg=DIM, font=F_SMALL,
                                 anchor="w", justify="left", wraplength=400)
        self.lbl_hint.pack(fill="x", padx=16, pady=(3, 0))
        self.lbl_card = tk.Label(card, text="", bg=CARD, fg=GOLD,
                                 font=("Microsoft YaHei UI", 9, "bold"),
                                 anchor="w", justify="left", wraplength=400)
        self.lbl_card.pack(fill="x", padx=16, pady=(4, 0))
        self.lbl_base = tk.Label(card, text="", bg=CARD, fg="#8fa3c8", font=F_SMALL,
                                 anchor="w", justify="left", wraplength=400)
        self.lbl_base.pack(fill="x", padx=16, pady=(4, 8))

        # ---- 按钮 ----
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=12, pady=(0, 8))
        self._button(foot, "立即重读", self._force, BG="#242a36", fg=TXT).pack(side="left")
        self._button(foot, "展开游戏", self._restore_game, BG="#242a36", fg=TXT).pack(side="left", padx=(6, 0))
        self._button(foot, "校准", self._calibrate, BG="#2a3a52", fg="#cfe0ff").pack(side="left", padx=(6, 0))
        self._button(foot, "诊断", self._diag_now, BG="#2a3a52", fg="#cfe0ff").pack(side="left", padx=(6, 0))
        self.btn_reset = self._button(foot, "重置", self._reset, BG="#3a2b2b", fg="#f0c6a0")
        self.btn_reset.pack(side="left", padx=(6, 0))

        self.top_var = tk.BooleanVar(value=True)
        tk.Checkbutton(foot, text="置顶", variable=self.top_var, command=self._toggle_top,
                       bg=BG, fg=DIM, selectcolor=CARD, font=F_SMALL,
                       activebackground=BG, activeforeground=TXT, bd=0,
                       highlightthickness=0).pack(side="left", padx=(6, 0))

        self.lbl_time = tk.Label(foot, text="", bg=BG, fg=DIM, font=F_SMALL)
        self.lbl_time.pack(side="right")

    def _button(self, parent, text, cmd, BG="#242a36", fg=TXT):
        return tk.Button(parent, text=text, command=cmd, relief="flat", bg=BG, fg=fg,
                         font=F_SMALL, activebackground="#2f3746", activeforeground=TXT,
                         bd=0, padx=10, pady=5, cursor="hand2")

    def _toggle_top(self) -> None:
        self.root.attributes("-topmost", bool(self.top_var.get()))

    # ------------------------------------------------------------ 动作
    def _force(self) -> None:
        self.worker.force.set()
        self.dot.configure(text="● 读取中", fg=DIM)

    def _relocate(self) -> None:
        """忘掉已经记住的面板位置，重新在右上角找一遍。

        什么时候用：换了分辨率/显示器、改了游戏 HUD 缩放之后，如果一直
        「没读到旅程剩余站点」，就是这个位置对不上了。
        """
        NS.forget_panel()
        self.worker.force.set()
        self.dot.configure(text="● 重新定位…", fg=GOLD)
        self.lbl_base.configure(text="已清掉记住的位置，正在重新找面板…（约 4 秒）")

    def _reset(self) -> None:
        """重置：这一局结束，从下一局的开局重新开始。"""
        self.worker.reset_flag.set()
        self._show_pending("已清空本局记录，等新一局开始…")

    # ------------------------------------------------------------ 校准 / 诊断
    def _calibrate(self) -> None:
        """框一次「旅程剩余站点」那一块，之后每帧只读这里。

        自动找面板会先试标准位置、再扫竖条、最后全屏分块；三层都不中的时候
        （超宽屏、HUD 缩放、窗口被拖成奇怪比例）就不再猜了，让用户框一次。
        存的是比例，换分辨率/换机器照样能用。
        """
        img = getattr(self.worker, "last_img", None)
        if img is None:
            self.lbl_base.configure(text="还没抓到过画面，先让游戏动一下（或点「立即重读」）")
            return
        try:
            import panel_calib as PC
        except Exception:
            try:
                from tools import panel_calib as PC
            except Exception as e:
                self.lbl_base.configure(text=f"校准界面加载失败：{e}")
                return
        self.worker.pause.set()
        self.dot.configure(text="● 校准中", fg=GOLD)

        def done(ok: bool, frac) -> None:
            self.worker.pause.clear()
            self.worker.force.set()
            if frac is None:
                self.lbl_base.configure(text="已恢复默认位置，正在重新找面板…")
                self.dot.configure(text="● 重新定位…", fg=GOLD)
            elif ok:
                self.lbl_base.configure(text="已记住这个位置，以后每帧只读这里")
                self.dot.configure(text="● 已校准", fg=GREEN)
            else:
                self.lbl_base.configure(text="位置已保存，但刚才没解析出数字——"
                                             "如果一直读不到，再框一次把范围放宽些")

        PC.open_calibration(self.root, img, on_done=done)

    def _diag_now(self) -> None:
        """把这一帧、读取区域、识别到的原文打包存下来，出问题直接发回来。"""
        img = getattr(self.worker, "last_img", None)
        if img is None:
            self.lbl_base.configure(text="还没抓到过画面，先让游戏动一下（或点「立即重读」）")
            return
        self.worker.pause.set()
        self.dot.configure(text="● 诊断中", fg=DIM)

        def work() -> None:
            try:
                d = NS.save_diag(img, note="用户手动点「诊断」")
            except Exception as e:
                self.worker.pause.clear()
                self.q.put(("err", f"诊断失败：{e}"))
                return
            self.worker.pause.clear()
            self.worker.force.set()
            try:
                os.startfile(d)      # 直接打开文件夹，用户能看见东西在哪
            except Exception:
                pass
            self.lbl_base.configure(text=f"诊断已存到 {d}（画面+读取区域+识别原文）")
            self.dot.configure(text="● 在线", fg=GREEN)

        threading.Thread(target=work, daemon=True).start()

    def _restore_game(self) -> None:
        """游戏窗口最小化时把它展开——不激活、不抢前台。"""
        self.dot.configure(text="● 展开中", fg=DIM)

        def work() -> None:
            try:
                import ctypes

                import win32con
                import win32gui

                h = NS.find_unreal_any_state()
                if not h:
                    raise RuntimeError("没找到游戏窗口")
                if not win32gui.IsIconic(h):
                    self.worker.force.set()
                    return
                ctypes.windll.user32.ShowWindowAsync(h, win32con.SW_SHOWNOACTIVATE)
                time.sleep(0.3)
                if win32gui.IsIconic(h):
                    ctypes.windll.user32.PostMessageW(
                        h, win32con.WM_SYSCOMMAND, win32con.SC_RESTORE, 0)
                    time.sleep(0.4)
                if win32gui.IsIconic(h):
                    raise RuntimeError("游戏以管理员身份运行，普通权限展不开它")
                self.worker.force.set()
            except Exception as e:
                self.q.put(("err", f"展开窗口失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 刷新
    def _pump(self) -> None:
        touched = False
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    self._show(payload)
                    self.lbl_time.configure(text=time.strftime("%H:%M:%S"))
                    touched = True
                elif kind == "cards":
                    self._show_cards(payload)
                    touched = True
                elif kind == "busy":
                    self.dot.configure(text="● 读取中", fg=DIM)
                elif kind == "same":
                    self.dot.configure(text="● 在线", fg=GREEN)
                elif kind == "reset":
                    self._on_reset(payload)
                    touched = True
                elif kind == "pending":
                    self._show_pending(payload)
                    touched = True
                elif kind == "warn":
                    # 尺寸对不上这类问题不影响出结果，但必须让人看见原因
                    self.lbl_base.configure(text=payload, fg="#f0a35e")
                    touched = True
                else:
                    self._show_error(payload)
                    touched = True
        except queue.Empty:
            pass
        if touched:
            self._fit()
        self.root.after(140, self._pump)

    def _fit(self) -> None:
        """窗口尺寸跟着内容走（提示文字换行长度会变，缩放后的字号也不同）。"""
        self.root.update_idletasks()
        h = max(470, self.root.winfo_reqheight())
        w = max(430, self.root.winfo_reqwidth(), self.root.winfo_width())
        if abs(h - self.root.winfo_height()) > 2 or abs(w - self.root.winfo_width()) > 2:
            self.root.geometry(f"{w}x{h}+{self.root.winfo_x()}+{self.root.winfo_y()}")

    def _show(self, parsed: dict) -> None:
        counts = parsed.get("counts") or {}
        probs = parsed.get("probs") or {}
        if not counts or not probs:
            self._show_error("没读到「旅程剩余站点」那一行")
            return

        self.has_data = True
        self.dot.configure(text="● 在线", fg=GREEN)

        nxt = probs[1]
        best = max(nxt, key=lambda k: nxt[k])
        p = nxt[best]
        self.big_type.configure(text=best, fg=GOLD)
        self.big_pct.configure(text=f"{p * 100:.1f}%", fg=GOLD)

        for t in J.TYPES:
            cv, fill, lbl = self.bars[t]
            cv.coords(fill, 0, 1, 196 * max(0.0, min(1.0, nxt[t])), 9)
            cv.itemconfigure(fill, fill=GOLD if t == best else "#4b5468")
            lbl.configure(text=f"{nxt[t] * 100:.1f}%", fg=TXT if t == best else DIM)

        self.lbl_verdict.configure(text=self._verdict(p, best, nxt))

        self._fill_ahead(probs)

        j = NS._load_journey()
        station = int((parsed.get("meta") or {}).get("station") or 0) + 1
        fixed = parsed.get("counts_fixed") or []
        self.lbl_rest.configure(
            text=f"第 {station} 站 · 剩余 {sum(counts.values())} 站 · {J.describe_counts(counts)}"
                 + (f"（已纠正 {'/'.join(fixed)}）" if fixed else ""))

        hint = parsed.get("hint_obj")
        meta = parsed.get("meta") or {}
        extra = ""
        old = [x for x in (meta.get("used") or []) if x[0] < 0]
        if old:
            extra = f"（另 {len(old)} 条旧提示仍生效）"
        if meta.get("conflict"):
            extra += " ⚠ 约束冲突，已退回本站提示"
        self.lbl_hint.configure(text="提示 " + (hint.describe() if hint else "未读到") + extra)

        self._show_cards(parsed)

        base = j.get("base") or {}
        if base:
            self.lbl_base.configure(
                text=f"本局开局 {sum(base.values())} 站 · 已走 {j.get('i', 0)} 站"
                     f" · 每类≤{NS.MAX_PER_TYPE}", fg="#8fa3c8")
        else:
            self.lbl_base.configure(text="还没开局：点「重置」，然后开新的一局", fg="#8fa3c8")

        log(f"{J.describe_counts(counts)} | {parsed.get('hint')} | {best} {p * 100:.1f}%")

    def _ahead_visible(self, on: bool) -> None:
        """有数据才显示「后续站点」那一块，读不到时不留空表。"""
        if on and not self.ahead_box.winfo_ismapped():
            self.ahead_box.pack(fill="x", padx=16, after=self.sep_top)
        elif not on and self.ahead_box.winfo_ismapped():
            self.ahead_box.pack_forget()

    def _fill_ahead(self, probs: dict) -> None:
        """后面几站：三种类型做行、站点做列的矩阵，每列最大的那个高亮。"""
        self._ahead_visible(bool(probs))
        total = len(probs)
        for i in range(1, MAX_AHEAD + 1):
            show = i + 1 <= total
            if show:
                self.g_head[i - 1].grid()
            else:
                self.g_head[i - 1].grid_remove()
            for r in range(len(J.TYPES)):
                cell = self.g_cells[(r, i)]
                if not show:
                    cell.grid_remove()
                    continue
                cell.grid()
                p = (probs[i + 1] or {}).get(J.TYPES[r], 0.0)
                cell.configure(text=f"{p * 100:.0f}%")

        for i in range(1, MAX_AHEAD + 1):
            if i + 1 > total:
                continue
            col = probs[i + 1] or {}
            top = max(col.values()) if col else 0.0
            winners = [t for t in J.TYPES if abs(col.get(t, 0.0) - top) < 1e-12]
            for r, t in enumerate(J.TYPES):
                cell = self.g_cells[(r, i)]
                if len(winners) == 1 and t in winners and top > 0:
                    cell.configure(bg=GOLD_BG, fg=GOLD)
                elif col.get(t, 0.0) <= 0:
                    cell.configure(bg=CARD, fg="#4b5468")
                else:
                    cell.configure(bg=CARD, fg=TXT)

        n = total - 1
        self.lbl_ahead_note.configure(text=f"往后 {n} 站" if n > 0 else "已经是最后几站")

    def _show_cards(self, parsed: dict) -> None:
        cards = parsed.get("cards") or []
        if cards:
            top = cards[0]
            self.lbl_card.configure(
                text=f"策略卡 → 选「{top['title']}」 ({top['p'] * 100:.0f}%)")
        else:
            self.lbl_card.configure(text="")

    @staticmethod
    def _verdict(p: float, best: str, probs: dict) -> str:
        if p >= 0.999:
            return f"只能是【{best}】"
        if p >= 0.95:
            return f"基本锁定【{best}】"
        if p >= 0.65:
            return f"很可能是【{best}】，但别当 100% 用"
        if p >= 0.50:
            return f"偏向【{best}】，还有变数"
        if len(set(round(v, 3) for v in probs.values())) == 1:
            return "三种一样，这站没法预判"
        return "信息不足，三种差不多"

    def _show_pending(self, msg: str = "") -> None:
        """重置之后、新一局开局之前的空白状态：不显示上一局残局的数字。"""
        self.has_data = False
        self.dot.configure(text="● 已重置", fg=GOLD)
        self.big_type.configure(text="…", fg=DIM)
        self.big_pct.configure(text="")
        self.lbl_verdict.configure(text="")
        for t in J.TYPES:
            cv, fill, lbl = self.bars[t]
            cv.coords(fill, 0, 1, 0, 9)
            lbl.configure(text="—", fg=DIM)
        self._ahead_visible(False)
        self.lbl_rest.configure(text="本局记录已清空")
        self.lbl_hint.configure(
            text=msg or "等新一局开始：把游戏推进到能看到「旅程剩余站点」的界面")
        self.lbl_card.configure(text="")
        self.lbl_base.configure(text="重置 = 这一局结束，下一局从开局那一刻重新算", fg="#8fa3c8")

    def _on_reset(self, r: dict) -> None:
        base = r["base"]
        total = r["total"]
        log(f"新一局开局 {total} 站 {J.describe_counts(base)}")
        self.dot.configure(text="● 在线", fg=GREEN)
        self.lbl_base.configure(
            text=f"新一局 · 开局 {total} 站（{J.describe_counts(base)}）· 已走 0 站")
        self.worker.force.set()

    def _show_error(self, msg: str) -> None:
        rows = (msg or "未知错误").splitlines() or ["未知错误"]
        short = rows[0][:70]
        detail = rows[1] if len(rows) > 1 else ""
        if self.has_data:
            # 已经读到过：单帧失手很常见（OCR 把数字切碎、界面在切换），
            # 先保留上一次的数字，别整块清空吓人。
            self.dot.configure(text="● 本帧没读到", fg=GOLD)
            self.lbl_hint.configure(text=f"沿用上一次结果 · {short}")
            return
        self.dot.configure(text="● 读不到", fg=RED)
        self.big_type.configure(text="?", fg=RED)
        self.big_pct.configure(text="")
        self.lbl_verdict.configure(text="")
        for t in J.TYPES:
            cv, fill, lbl = self.bars[t]
            cv.coords(fill, 0, 1, 0, 9)
            lbl.configure(text="—", fg=DIM)
        self._ahead_visible(False)
        self.lbl_rest.configure(text="读不到画面")
        self.lbl_hint.configure(text=short)
        self.lbl_card.configure(text="")
        self.lbl_base.configure(
            text=detail or "停到有「旅程剩余站点」的界面就会自动出结果（窗口别最小化）",
            fg="#8fa3c8")

    def _on_close(self) -> None:
        self._save_pos()
        self.worker.stop_flag.set()
        self.root.destroy()


def main() -> int:
    try:
        # 同上：建窗口前必须先声明 DPI 感知，否则 150% 缩放时抓到的画面会是错的
        from bot.dpi import enable as _dpi_enable
        from bot.dpi import scale as _dpi_scale

        _dpi_enable()
        root = tk.Tk()
        try:
            # 声明感知之后 Tk 不会自动按缩放放大字号，这里补一点；
            # 但界面是按固定像素排的（进度条 196px、换行 400px），放太大反而撑破布局，
            # 所以最多放到 1.25 倍。
            k = min(1.25, _dpi_scale(root.winfo_id()))
            root.tk.call("tk", "scaling", k * 96.0 / 72.0)
        except Exception:
            pass
        App(root)
        root.mainloop()
        return 0
    except Exception:
        log("CRASH\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
