"""后台窗口捕获：不抢屏幕、不抢鼠标、不需要窗口在前台。

为什么需要它：
    这个客户端的游戏窗口是 Unreal 渲染的，`PrintWindow` 四个标志位全部返回 0
    （窗口不处理 WM_PRINT），`BitBlt` 拿到的又是屏幕帧缓冲（会被其它窗口遮挡）。
    所以传统 GDI 截图只能"把游戏切到前台"，一跑脚本就抢用户的屏幕。

    Windows 10 1803+ 提供的 **Windows Graphics Capture**（WGC）直接从 DWM 取
    该窗口的合成结果，窗口在后台、被别的窗口盖住都能抓，且不移动光标、不激活窗口。
    实测：游戏在后台（IsWindowForeground=False）时仍能稳定取到 1602x932 的完整画面。

输入侧：
    WGC 只解决"看"，不解决"点"。后台点击走 PostMessage 投递鼠标消息，同样不移动
    物理光标；但客户端进程权限高于本进程时系统会直接拒绝（UIPI），
    表现为 `pywintypes.error: (5, 'PostMessage', '拒绝访问。')` —— 这种情况必须
    以管理员身份运行本工具，见 `BgInput.available()` 的提示。
"""
from __future__ import annotations

import ctypes
import threading
import time
from typing import Optional, Tuple

import numpy as np
import win32con
import win32gui
from PIL import Image

_WGC_ERROR = None
try:
    from windows_capture import WindowsCapture  # type: ignore
except Exception as e:  # pragma: no cover - 环境缺包时的降级路径
    WindowsCapture = None  # type: ignore
    _WGC_ERROR = e


def wgc_available() -> bool:
    return WindowsCapture is not None


def wgc_reason() -> str:
    if WindowsCapture is not None:
        return "ok"
    return f"未安装 windows-capture（{_WGC_ERROR}）。执行：.venv\\Scripts\\pip install windows-capture"


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _client_box_in_visible(hwnd: int) -> Tuple[int, int, int, int]:
    """客户区相对于"DWM 可见边界"的 (left, top, width, height)，单位像素。"""
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    r = _RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
    )
    if hr != 0:
        wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
    else:
        wl, wt = r.left, r.top
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    cw, ch = win32gui.GetClientRect(hwnd)[2:]
    return cx - wl, cy - wt, cw, ch


class BgCapture:
    """持续抓某个窗口的画面（窗口可在后台）。

    用法：
        cap = BgCapture(hwnd)
        img = cap.grab()          # PIL.Image，已裁到客户区
        cap.stop()
    """

    def __init__(self, hwnd: int, min_interval_ms: Optional[int] = None):
        if WindowsCapture is None:
            raise RuntimeError(wgc_reason())
        self.hwnd = hwnd
        self._latest: Optional[np.ndarray] = None
        self._stamp = 0.0
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._ctl = None
        self._box = None            # 客户区在可见边界里的裁剪框（缓存）
        self._box_size = (0, 0)

        # 注意：draw_border / minimum_update_interval 在部分系统上不被支持，
        # Win10 1809 之前、或远程桌面会话里会直接抛异常。这里一律不传，走默认值。
        kwargs = dict(cursor_capture=False, window_hwnd=hwnd)
        if min_interval_ms:
            kwargs["minimum_update_interval"] = min_interval_ms

        def build(**kw):
            cap = WindowsCapture(**kw)

            @cap.event
            def on_frame_arrived(frame, capture_control):  # noqa: ANN001
                with self._lock:
                    self._latest = frame.frame_buffer.copy()
                    self._stamp = time.time()

            @cap.event
            def on_closed():  # noqa: ANN001
                self._closed.set()

            return cap

        try:
            self._capture = build(**kwargs)
            self._ctl = self._capture.start_free_threaded()
        except Exception:
            if not min_interval_ms:
                raise
            # 去掉不兼容的参数重试一次
            self._capture = build(cursor_capture=False, window_hwnd=hwnd)
            self._ctl = self._capture.start_free_threaded()

    # ------------------------------------------------------------ 取帧
    def grab(self, timeout: float = 3.0, fresh: bool = False,
             max_wait: float = 0.0) -> Image.Image:
        """取一帧并裁成客户区尺寸。

        默认**不等新帧**：直接返回缓存里最新的那帧（毫秒级）。
        这一点很关键——WGC 只在窗口内容真的变化时才回调新帧，游戏画面静止时
        一等就是好几秒，之前每次抓屏 0.8~5s 全是这么来的。

        需要"确保是最新画面"时传 max_wait>0：最多等这么久去拿新帧，等不到
        也照样返回缓存帧，不会卡住。timeout 只在**一帧都还没拿到**时起作用。
        """
        with self._lock:
            buf = self._latest
        if buf is not None and max_wait <= 0:
            return self._to_client_image(buf)

        deadline = time.time() + (max_wait if buf is not None else timeout)
        start_stamp = self._stamp
        while True:
            with self._lock:
                buf, stamp = self._latest, self._stamp
            if buf is not None and (not fresh or stamp > start_stamp):
                return self._to_client_image(buf)
            if self._closed.is_set():
                raise RuntimeError("捕获会话已关闭（游戏窗口可能被关闭或最小化了）")
            if time.time() >= deadline:
                break
            time.sleep(0.01)

        with self._lock:
            has = self._latest is not None
        if has:
            return self._to_client_image(self._latest)
        raise RuntimeError(
            "后台捕获拿不到画面：窗口可能已最小化。WGC 只能抓『显示中』的窗口，"
            "被其它窗口遮挡没问题，但最小化到任务栏不行 —— 请让游戏窗口保持展开（可以放在后台）。"
        )

    def stamp(self) -> float:
        """最近一帧的时间戳（判断画面有没有更新过）。"""
        with self._lock:
            return self._stamp

    def _to_client_image(self, buf: np.ndarray) -> Image.Image:
        """WGC 帧 == 窗口的 DWM 可见边界（含标题栏、可见边框），按精确偏移裁到客户区。

        不能用"按比例缩放"算偏移：可见边框和 GetWindowRect 的不可见边框宽度不一样
        （实测：GetWindowRect 1616x939，可见边界 1602x932，客户区在可见边界内的
        偏移是 (1,31) 而不是按比例算出来的 (8,31)），横向会差 7px，
        导致点击整体偏移、点不到按钮。

        偏移算一次就缓存：DwmGetWindowAttribute + ClientToScreen 每次要几百微秒，
        而这个循环每秒要跑好几次。
        """
        h, w = buf.shape[:2]
        if self._box_size != (w, h):
            try:
                left, top, cw, ch = _client_box_in_visible(self.hwnd)
            except Exception:
                self._box = None
            else:
                if (cw, ch) == (w, h):
                    self._box = None
                else:
                    sx, sy = w / max(1, cw), h / max(1, ch)
                    box = (int(round(left * sx)), int(round(top * sy)),
                           int(round((left + cw) * sx)), int(round((top + ch) * sy)))
                    self._box = (max(0, box[0]), max(0, box[1]),
                                 min(w, box[2]), min(h, box[3]), cw, ch)
            self._box_size = (w, h)

        img = self._as_image(buf)
        if not self._box:
            return img
        x1, y1, x2, y2, cw, ch = self._box
        img = img.crop((x1, y1, x2, y2))
        if img.size != (cw, ch) and cw > 0 and ch > 0:
            img = img.resize((cw, ch), Image.BILINEAR)
        return img

    @staticmethod
    def _as_image(buf: np.ndarray) -> Image.Image:
        arr = buf[:, :, :3]  # BGR
        return Image.fromarray(np.ascontiguousarray(arr[:, :, ::-1]))

    def alive(self) -> bool:
        return not self._closed.is_set()

    def stop(self) -> None:
        try:
            if self._ctl is not None:
                self._ctl.stop()
        except Exception:
            pass
        try:
            self._ctl = None
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


# ---------------------------------------------------------------- 后台输入

class BgInput:
    """向窗口投递鼠标消息：不移动物理光标、不激活窗口。"""

    POST_ACCESS_DENIED = 5

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self._denied = False
        self._last_err = ""

    @staticmethod
    def _lparam(x: int, y: int) -> int:
        return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)

    def probe(self) -> Tuple[bool, str]:
        """试探能否向该窗口投递鼠标消息。

        不能用 WM_NULL —— 它是 UIPI 白名单里的消息，低权限进程发得出去，
        会给出"能投递"的假结论。这里必须用真实会走输入管线的 WM_MOUSEMOVE。
        """
        try:
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, self._lparam(0, 0))
            return True, "ok"
        except Exception as e:
            self._denied = True
            self._last_err = str(e)
            return False, self._hint()

    def _hint(self) -> str:
        return (
            "无法向游戏窗口投递消息（系统拒绝访问）。\n"
            "  原因：客户端以管理员/高完整性级别运行，Windows 的 UIPI 禁止低权限进程给它发消息。\n"
            "  解决：用『0_管理员运行.bat』以管理员身份启动本工具，然后重试。\n"
            f"  原始错误：{self._last_err}"
        )

    def click(self, x: int, y: int, delay: float = 0.06, hold: float = 0.30,
              activate: bool = False) -> None:
        """投递一整套鼠标消息：移动 → 按下 → 保持 → 抬起。

        实测结论（2026-10-19，Unreal 客户端）：
        * 纯 PostMessage **会**被游戏接收，游戏里能打出点击动效；
        * 但**不要**额外投递 WM_ACTIVATEAPP / WM_ACTIVATE —— 那会让 UE 切到
          "已激活"状态，反而不再处理这些消息，点击彻底失效；
        * 真正决定成败的是 lParam 里的客户区坐标必须与实际渲染分辨率一致；
        * UE 的 UMG 按钮多数按"按住时长"判定点击（OnClicked 需要按下帧和抬起帧
          落在同一控件的两个不同帧里），按下/抬起只隔几十毫秒时，如果客户端帧率
          偏低或这两帧被合并，就会只出悬停高亮而不触发 OnClicked。所以按住时间
          默认给到 300ms，并在按住期间补发带 MK_LBUTTON 的 MOUSEMOVE，保证
          控件在这段时间里持续处于"被按下"状态。
        """
        lp = self._lparam(x, y)
        try:
            if activate:
                win32gui.PostMessage(self.hwnd, win32con.WM_ACTIVATEAPP, 1, 0)
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, lp)
            time.sleep(0.05)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
            # 按住期间反复补发"按键仍按下"的移动消息，抵消 UE 按帧采样丢事件
            t_end = time.time() + max(0.0, hold)
            while time.time() < t_end:
                time.sleep(min(0.05, max(0.0, t_end - time.time())))
                win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE,
                                     win32con.MK_LBUTTON, lp)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, lp)
            time.sleep(delay)
        except Exception:
            self._denied = True
            raise RuntimeError(self._hint())

    def move(self, x: int, y: int) -> None:
        try:
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, self._lparam(x, y))
        except Exception:
            self._denied = True
            raise RuntimeError(self._hint())

    def key(self, vk: int) -> None:
        try:
            win32gui.PostMessage(self.hwnd, win32con.WM_KEYDOWN, vk, 0)
            time.sleep(0.03)
            win32gui.PostMessage(self.hwnd, win32con.WM_KEYUP, vk, 0)
        except Exception:
            self._denied = True
            raise RuntimeError(self._hint())
