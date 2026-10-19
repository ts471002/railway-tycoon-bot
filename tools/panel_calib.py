"""手动校准：在一张定格画面上，用鼠标框出「旅程剩余站点」那一块。

为什么要有它：程序默认按 1600x900 量出来的比例去右上角找面板，找不到会自己
扫（竖条 / 全屏分块）。但游戏 UMG 的缩放规则不受我们控制——HUD 缩放、超宽屏、
窗口拖成奇怪比例，都可能让面板落在谁都想不到的地方。这时候让用户框一次，
比任何自动算法都准，而且是**一次性**的：框完存成比例，以后每帧只读那一小块。

框完立刻在框里跑一次识别，把认到的文字回显出来：
认对了才记下来；认错了当场就能看见，不会留个错的坐标继续坑人。
"""
from __future__ import annotations

import tkinter as tk

from PIL import Image, ImageTk

try:                      # 源码直接跑 tools/*.py 时是扁平导入
    import next_station as NS
except ImportError:       # 打包/包内导入时走带包名的
    import tools.next_station as NS

BG = "#171b23"
TXT = "#dfe7f5"
DIM = "#7f8ca3"
GOLD = "#ffb340"
GREEN = "#5bd18a"
RED = "#f2777a"
F = ("Microsoft YaHei UI", 9)

MAX_W, MAX_H = 1100, 600


def open_calibration(parent: tk.Misc, frame: Image.Image, on_done=None):
    """frame 必须是游戏那一帧的原图（不是缩放过的）。"""
    w, h = frame.size
    s = min(MAX_W / float(w), MAX_H / float(h), 1.0)
    vw, vh = max(1, int(w * s)), max(1, int(h * s))

    win = tk.Toplevel(parent)
    win.title("校准读取区域")
    win.configure(bg=BG)
    win.attributes("-topmost", True)
    win.resizable(False, False)

    tk.Label(win, text="在这张画面上，按住左键拖一个框，框住右侧那一整块："
                       "「未来3站…」和「旅程剩余站点：…」都要在里面",
             bg=BG, fg=TXT, font=F, justify="left", wraplength=vw).pack(
        padx=12, pady=(10, 4), anchor="w")

    cv = tk.Canvas(win, width=vw, height=vh, highlightthickness=0, bg="#000")
    cv.pack(padx=12)
    photo = ImageTk.PhotoImage(frame.resize((vw, vh), Image.BILINEAR))
    cv.create_image(0, 0, anchor="nw", image=photo)
    cv._photo = photo          # 防被回收

    st = {"x0": 0, "y0": 0, "rect": None, "box": None, "ok": False}

    lbl = tk.Label(win, text="还没框选", bg=BG, fg=DIM, font=F, justify="left",
                   wraplength=vw, anchor="w")
    lbl.pack(fill="x", padx=12, pady=(6, 0))

    def check():
        """在框里立刻认一次，让用户当场看到认得对不对。"""
        if not st["box"]:
            return
        box = NS._frac_to_px(st["box"], frame.size)
        try:
            lines, _ = NS.ocr_lines_fast(frame, scale=2.0, regions=[box])
            counts, _ = NS._checked_counts(lines, NS._load_journey())
        except Exception as e:
            lbl.configure(text=f"识别出错：{e}", fg=RED)
            st["ok"] = False
            return
        st["ok"] = bool(counts)
        txt = " | ".join(ln.text for ln in lines[:6]) or "（这一块里一个字都没认到）"
        if counts:
            lbl.configure(text=f"✓ 认到了：{txt}", fg=GREEN)
        else:
            lbl.configure(text=f"⚠ 没解析出「旅程剩余站点」，先看认到的字对不对：{txt}",
                          fg=GOLD)

    def press(e):
        st["x0"], st["y0"] = e.x, e.y
        if st["rect"]:
            cv.delete(st["rect"])
        st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline=GOLD, width=2)

    def drag(e):
        if st["rect"]:
            cv.coords(st["rect"], st["x0"], st["y0"], e.x, e.y)

    def release(e):
        x1, x2 = sorted((st["x0"], e.x))
        y1, y2 = sorted((st["y0"], e.y))
        if x2 - x1 < 20 or y2 - y1 < 14:
            st["box"] = None
            lbl.configure(text="框太小了，重新拖一个大一点的", fg=RED)
            return
        # 画面坐标 → 原图坐标 → 比例
        st["box"] = (max(0.0, x1 / s / w), max(0.0, y1 / s / h),
                     min(1.0, x2 / s / w), min(1.0, y2 / s / h))
        check()

    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", release)

    bar = tk.Frame(win, bg=BG)
    bar.pack(fill="x", padx=12, pady=(8, 10))

    def ok():
        if not st["box"]:
            lbl.configure(text="还没框呢", fg=RED)
            return
        NS.save_learned_panel(st["box"])
        NS.reset_scan_backoff()
        if on_done:
            on_done(bool(st["ok"]), st["box"])
        win.destroy()

    def reset():
        NS.forget_panel()
        NS.reset_scan_backoff()
        if on_done:
            on_done(False, None)
        win.destroy()

    tk.Button(bar, text="就用这个位置", command=ok, relief="flat",
              bg="#2a5d43", fg="#d8ffe8", font=F, padx=12, pady=5, bd=0,
              cursor="hand2", activebackground="#357a57").pack(side="left")
    tk.Button(bar, text="恢复默认", command=reset, relief="flat", bg="#242a36",
              fg=TXT, font=F, padx=12, pady=5, bd=0, cursor="hand2",
              activebackground="#2f3746").pack(side="left", padx=(8, 0))
    tk.Button(bar, text="取消", command=win.destroy, relief="flat", bg="#242a36",
              fg=DIM, font=F, padx=12, pady=5, bd=0, cursor="hand2",
              activebackground="#2f3746").pack(side="left", padx=(8, 0))

    win.update_idletasks()
    x = max(0, parent.winfo_x() + (parent.winfo_width() - win.winfo_width()) // 2)
    y = max(0, parent.winfo_y() + 40)
    win.geometry(f"+{x}+{y}")
    return win
