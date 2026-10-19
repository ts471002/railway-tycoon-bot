"""让进程按「物理像素」工作。

Windows 的显示缩放（125% / 150% / 200%）会给**没有声明 DPI 感知**的进程
做一层虚拟化：程序问窗口多大，系统回答的是缩放前的逻辑尺寸。

后果非常具体（真实踩过）：
    显示器 1920×1080、缩放 150%，游戏全屏
    → win32gui.GetClientRect() 返回 1280×720
    → 抓屏按 1280×720 建帧池，拿到的图是 1920×1080 被压/裁过的
    → 程序以为"画面是 1280×720"，读区域、点击坐标全部对不上
    → 校准也没用，因为校准用的是同一张错图

必须在**创建任何窗口/抓屏会话之前**调用 `enable()`。

注意顺序：Per-Monitor V2 → Per-Monitor → System，逐级降级；
老系统（Win7/Win8）没有前两个 API，最后那个永远能用。
"""
from __future__ import annotations

import ctypes
import sys

_applied: str | None = None


def enable() -> str:
    """声明 DPI 感知，返回实际生效的级别（重复调用只执行一次）。"""
    global _applied
    if _applied is not None:
        return _applied
    if sys.platform != "win32":
        _applied = "非 Windows"
        return _applied

    try:
        # PER_MONITOR_AWARE_V2 = -4（Win10 1703+）
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _applied = "per-monitor-v2"
            return _applied
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Win8.1+
        _applied = "per-monitor"
        return _applied
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()        # Vista+
        _applied = "system"
        return _applied
    except Exception:
        _applied = "失败"
        return _applied


def scale(hwnd: int = 0) -> float:
    """显示器缩放倍数（1.0 = 100%，1.5 = 150%）。"""
    try:
        if sys.platform != "win32":
            return 1.0
        if hwnd:
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        else:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        return max(1.0, float(dpi) / 96.0)
    except Exception:
        return 1.0


def screen() -> tuple[int, int]:
    """主屏的物理分辨率。"""
    try:
        u = ctypes.windll.user32
        try:
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        return (0, 0)


def info() -> str:
    w, h = screen()
    return f"DPI 感知 {enable()} · 缩放 {scale():.2f}× · 主屏 {w}×{h}"
