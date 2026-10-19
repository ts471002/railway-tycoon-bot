"""安装版：一个 exe，装一次，之后从桌面图标启动只要 1 秒。

为什么需要它：
    单文件版每次双击都要把 100MB 的自己解压到临时目录（6~7 秒），
    这是 PyInstaller onefile 的机制，改不掉。文件夹版启动 1.2 秒，
    但要整个文件夹一起拷，不方便发。
    安装版把两者合起来：发一个文件，装一次，以后就当普通软件用。

它做的事：
    1. 把内嵌的运行环境解压到 %LOCALAPPDATA%\\铁路大亨推测\\app\\
    2. 桌面 + 开始菜单建快捷方式
    3. 直接启动
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import threading
import time

APP_NAME = "铁路大亨推测"
APP_EXE = "铁路大亨推测.exe"


def _base_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)


def _payload_path() -> str:
    """内嵌的运行环境压缩包（PyInstaller 会解到 _MEIPASS）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    root = meipass or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "payload.tar.xz")


def _desktop_dir() -> str:
    """真实桌面路径（可能被 OneDrive 重定向，不能假设是 ~\\Desktop）。"""
    try:
        import win32com.client  # type: ignore

        return win32com.client.Dispatch("WScript" + ".Shell").SpecialFolders("Desktop")
    except Exception:
        pass
    for p in (os.path.join(os.path.expanduser("~"), "Desktop"),
              os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")):
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


def _make_shortcut(exe_path: str, where: str) -> bool:
    """在指定目录建快捷方式。成功返回 True。"""
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript" + ".Shell")
        lnk = shell.CreateShortCut(os.path.join(where, f"{APP_NAME}.lnk"))
        lnk.TargetPath = exe_path
        lnk.WorkingDirectory = os.path.dirname(exe_path)
        lnk.IconLocation = exe_path
        lnk.Description = "铁路大亨 · 下一站推测"
        lnk.Save()
        return os.path.exists(os.path.join(where, f"{APP_NAME}.lnk"))
    except Exception as e:  # noqa: BLE001
        _note_error(f"快捷方式失败 {where}: {type(e).__name__}: {e}")
        return False


def _cmd_launcher(exe_path: str, where: str) -> None:
    """快捷方式建不出来时的兜底：放一个双击就启动的 .cmd（黑框一闪而过）。"""
    try:
        with open(os.path.join(where, f"{APP_NAME}.cmd"), "w", encoding="gbk") as f:
            f.write("@echo off\r\n")
            f.write(f'start "" "{exe_path}"\r\n')
    except Exception:
        pass


def _note_error(msg: str) -> None:
    try:
        base = _base_dir()
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "install_error.txt"), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _payload_stamp() -> str:
    """用 payload 的指纹当版本号。

    以前用日期，结果同一天重新出包时，装过的机器会认为「已经装过」而
    继续跑旧程序，改了 bug 用户重装也还是老样子。md5 太慢，取 mtime+size 够了。
    """
    try:
        st = os.stat(_payload_path())
        return f"{int(st.st_mtime)}-{st.st_size}"
    except Exception:
        return time.strftime("%Y%m%d")


def _install(progress, base: str) -> str:
    """解压运行环境到指定目录，返回可执行文件路径。已经装过同一版就直接复用。"""
    app_dir = os.path.join(base, "app")
    exe_path = os.path.join(app_dir, APP_EXE)
    stamp = os.path.join(base, "installed.txt")
    version = _payload_stamp()

    installed = False
    try:
        with open(stamp, encoding="utf-8") as f:
            installed = f.read().strip() == version and os.path.exists(exe_path)
    except Exception:
        installed = False

    if not installed:
        progress("正在解压运行环境…", 0)
        os.makedirs(app_dir, exist_ok=True)
        payload = _payload_path()
        with tarfile.open(payload, "r:xz") as tf:
            members = tf.getmembers()
            total = len(members) or 1
            for i, m in enumerate(members):
                try:
                    tf.extract(m, app_dir, filter="data")
                except TypeError:      # 老版本 Python 没有 filter 参数
                    tf.extract(m, app_dir)
                if i % 40 == 0:
                    progress("正在解压运行环境…", i / total)
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(version)

    progress("正在创建桌面图标…", 0.95)
    desk = _desktop_dir()
    ok = _make_shortcut(exe_path, desk)
    _make_shortcut(exe_path, os.path.join(
        os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"))
    if not ok:
        _cmd_launcher(exe_path, desk)
    progress("装好了，正在启动…", 1.0)
    return exe_path


def main() -> int:
    # 静默安装（测试/批量部署用）：程序名 --silent "目标目录"
    if "--silent" in sys.argv:
        i = sys.argv.index("--silent")
        target = sys.argv[i + 1] if len(sys.argv) > i + 1 else _base_dir()
        import traceback

        try:
            exe = _install(lambda *a: None, target)
            msg = f"OK {exe}"
        except Exception:
            msg = "FAIL\n" + traceback.format_exc()
        try:
            with open(os.path.join(target, "install_log.txt"), "w", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
        return 0 if msg.startswith("OK") else 1

    import ctypes

    # 安装界面也要按物理像素排，否则 150% 缩放的屏上窗口会被系统再放大一次、糊掉
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    import tkinter as tk
    from tkinter import filedialog, ttk

    BG, CARD, TXT, DIM, GOLD = "#0e1015", "#181c24", "#eef1f6", "#8790a3", "#e6c07b"
    F = "Microsoft YaHei UI"

    root = tk.Tk()
    root.title(f"{APP_NAME} · 安装")
    root.configure(bg=BG)
    W, H = 500, 300
    root.geometry("%dx%d+%d+%d" % (W, H, (root.winfo_screenwidth() - W) // 2,
                                   (root.winfo_screenheight() - H) // 2))
    root.resizable(False, False)
    root.attributes("-topmost", True)

    tk.Label(root, text=APP_NAME, bg=BG, fg=TXT, font=(F, 15, "bold")).pack(pady=(22, 2))
    tk.Label(root, text="装到本机后从桌面图标启动，不用每次等解压",
             bg=BG, fg=DIM, font=(F, 9)).pack()

    tk.Label(root, text="安装位置", bg=BG, fg=DIM, font=(F, 9), anchor="w").pack(
        fill="x", padx=34, pady=(18, 5))

    row = tk.Frame(root, bg=BG)
    row.pack(fill="x", padx=34)
    var = tk.StringVar(value=_base_dir())
    ent = tk.Entry(row, textvariable=var, bg=CARD, fg=TXT, insertbackground=TXT,
                   relief="flat", font=(F, 9), highlightthickness=1,
                   highlightbackground="#2a3040", highlightcolor=GOLD)
    ent.pack(side="left", fill="x", expand=True, ipady=6, ipadx=6)

    def choose() -> None:
        cur = var.get().strip() or _base_dir()
        start = os.path.dirname(cur) if os.path.basename(cur).lower() in (
            APP_NAME.lower(), "app") else cur
        p = filedialog.askdirectory(title="选择安装位置", initialdir=start,
                                    mustexist=True)
        if not p:
            return
        p = os.path.normpath(p)
        # 选的是父目录就自动补一层程序名，避免直接铺在别人选的目录里
        if os.path.basename(p).lower() != APP_NAME.lower():
            p = os.path.join(p, APP_NAME)
        var.set(p)

    tk.Button(row, text="浏览…", command=choose, bg=CARD, fg=TXT, relief="flat",
              font=(F, 9), activebackground="#242a36", activeforeground=TXT,
              width=7, cursor="hand2", bd=0).pack(side="left", padx=(8, 0), ipady=5)

    btn = tk.Button(root, text="开始安装", bg=GOLD, fg="#1a1509", relief="flat",
                    font=(F, 10, "bold"), activebackground="#f0d29a",
                    activeforeground="#1a1509", padx=24, pady=6, bd=0,
                    cursor="hand2")
    btn.pack(pady=(18, 0))

    msg = tk.Label(root, text="选好位置就开始，装完会自动打开", bg=BG, fg=DIM, font=(F, 9))
    msg.pack(pady=(12, 4))
    bar = ttk.Progressbar(root, length=380, mode="determinate", maximum=100)
    bar.pack()

    def progress(text: str, frac: float) -> None:
        msg.configure(text=text, fg=GOLD)
        bar["value"] = max(0, min(100, frac * 100))
        root.update_idletasks()

    result: dict = {}

    def work(target: str) -> None:
        try:
            result["exe"] = _install(progress, target)
        except Exception as e:  # noqa: BLE001
            result["err"] = f"{type(e).__name__}: {e}"
            import traceback

            _note_error("安装失败\n" + traceback.format_exc())

    def start() -> None:
        target = var.get().strip().strip('"')
        if not target:
            msg.configure(text="先选一个安装位置", fg="#e06c75")
            return
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            msg.configure(text=f"这个位置建不了目录：{e}", fg="#e06c75")
            return
        btn.configure(state="disabled", text="安装中…")
        ent.configure(state="disabled")
        msg.configure(text="准备中…", fg=GOLD)
        threading.Thread(target=work, args=(target,), daemon=True).start()

    btn.configure(command=start)

    def poll() -> None:
        if "exe" in result:
            try:
                subprocess.Popen([result["exe"]], cwd=os.path.dirname(result["exe"]))
            except Exception:
                pass
            root.after(700, root.destroy)
            return
        if "err" in result:
            msg.configure(text="安装失败：" + result["err"][:70], fg="#e06c75")
            btn.configure(state="normal", text="重试")
            ent.configure(state="normal")
            return
        root.after(120, poll)

    root.after(150, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
