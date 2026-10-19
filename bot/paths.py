"""程序自己的位置。

源码运行时是项目根目录；打包成 exe 之后是 **exe 所在目录**，
这样把 exe 拷到 U 盘、桌面、任意文件夹，data/ 和 logs/ 都跟着 exe 走，
不会写到 PyInstaller 那个临时解压目录里（那个目录退出就没了）。

万一 exe 被放在没有写权限的地方（比如 Program Files），
自动退到 `%LOCALAPPDATA%\\铁路大亨推测`，程序照样能用。
"""
from __future__ import annotations

import os
import sys
import tempfile


def frozen() -> bool:
    """是不是从 PyInstaller 打出来的 exe 里跑。"""
    return bool(getattr(sys, "frozen", False))


def _base_dir() -> str:
    if frozen():
        # 注意：不能用 __file__。onefile 模式下它在临时解压目录里，
        # 要的是 exe 本身所在的位置。
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


_DATA: str | None = None


def data_root() -> str:
    """可写的数据根目录（data/、logs/ 都放在它下面）。"""
    global _DATA
    if _DATA:
        return _DATA
    base = _base_dir()
    if _writable(base):
        _DATA = base
    else:
        fallback = os.path.join(
            os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
            "铁路大亨推测",
        )
        os.makedirs(fallback, exist_ok=True)
        _DATA = fallback
    return _DATA


def resource_root() -> str:
    """只读资源根目录。

    源码运行时是项目根；onefile 的 exe 运行时是 PyInstaller 的解压目录 `_MEIPASS`。
    只要资源已经被 `--add-data` 打进去，从这里面读得到。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
