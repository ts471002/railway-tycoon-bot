"""打包入口：源码直接跑、打成 exe 跑，都从这里进。

PyInstaller 从这一行开始解析依赖，所以 GUI 用到的模块都得在这条链上。
"""
from __future__ import annotations

import os
import sys


def _fix_std_streams() -> None:
    """--windowed 打包后没有控制台，sys.stdout/stderr 是 None。

    有些库（日志、tqdm）会往里写，一写就 AttributeError 把程序带崩。
    塞个空设备进去，写什么都无害。
    """
    devnull = os.devnull
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(devnull, "w", encoding="utf-8"))
            except Exception:
                pass
    if sys.stdin is None:
        try:
            sys.stdin = open(devnull, "r", encoding="utf-8")
        except Exception:
            pass


def _close_splash() -> None:
    """打包版的启动画面，用完就关。

    只有单文件版带 splash（靠环境变量 `_PYI_SPLASH_IPC` 通信）；
    文件夹版和源码运行没有这个变量，直接跳过。
    """
    if "_PYI_SPLASH_IPC" not in os.environ:
        return
    try:
        import pyi_splash  # type: ignore

        pyi_splash.close()
    except Exception:
        pass


def selftest() -> int:
    """自检：抓一帧 + 认一遍全屏文字，结果写到 logs/selftest.txt。

    用来确认「exe 在别的机器上到底能不能认字」——把那个文件发回来就知道。
    加 `--frame 某张截图.png` 可以不吃实时画面，直接拿一张图跑完整流程。
    """
    import time
    import traceback

    from bot.paths import data_root

    _close_splash()
    log_dir = os.path.join(data_root(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    out = os.path.join(log_dir, "selftest.txt")
    lines_out = [f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}", f"exe 目录 {data_root()}"]

    frame = None
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--frame" and i + 1 < len(argv):
            frame = argv[i + 1]

    ok = True
    try:
        import next_station as NS
        from PIL import Image

        if frame:
            img = Image.open(frame).convert("RGB")
            info = f"本地图片 {frame}"
        else:
            t0 = time.time()
            img, info = NS.grab_game()
            lines_out.append(f"抓帧 {(time.time() - t0) * 1000:.0f}ms")
        lines_out.append(f"客户区 {img.size}  来源 {info}")
        try:
            from bot.dpi import info as _dpi_info

            import win32gui

            from next_station import game_hwnd

            hwnd = game_hwnd()
            rect = win32gui.GetClientRect(hwnd) if hwnd else None
            lines_out.append("系统环境：" + _dpi_info())
            if rect:
                ww, wh = rect[2], rect[3]
                lines_out.append(f"窗口客户区 {ww}×{wh}  抓到 {img.size[0]}×{img.size[1]}"
                                 + ("  一致" if (ww, wh) == img.size else "  ⚠ 不一致"))
        except Exception as e:  # noqa: BLE001
            lines_out.append(f"系统环境：读取失败 {e}")
        t0 = time.time()
        ocr, name = NS.ocr_lines(img)
        lines_out.append(f"OCR[{name}] 全屏 {(time.time() - t0) * 1000:.0f}ms  共 {len(ocr)} 行")
        for ln in sorted(ocr, key=lambda l: (l.box[1], l.box[0]))[:80]:
            lines_out.append(f"  {ln.box}  {ln.text}")
        try:
            import panel_calib  # noqa: F401
            lines_out.append("校准界面：可用")
        except Exception as e:  # noqa: BLE001
            lines_out.append(f"校准界面：不可用（{e}）")

        # 兜底是分层的（标准位置 → 竖条 → 全屏分块），要连续失败几次才会往下走。
        # 自检就是为了尽快看到结论，所以不等退避，最多连试 4 轮。
        t0 = time.time()
        parsed = {}
        for tries in range(1, 5):
            NS.reset_scan_backoff()
            parsed = NS.compute_img(img)
            if parsed.get("counts"):
                break
        w, h = img.size
        frac = NS.panel_frac()
        box = NS._frac_to_px(frac, img.size)
        lines_out.append(f"面板读数 第{tries}轮 {(time.time() - t0) * 1000:.0f}ms  "
                         f"{parsed.get('counts')}  阶段 {NS.LAST.get('stage')}")
        # 这两行是排查「别的机器读不到」的关键：画面多大、到底在读哪一块、位置哪来的
        lines_out.append(f"画面 {w}x{h}  读的区域 {box}  比例 {tuple(round(v, 4) for v in frac)}")
        lines_out.append("位置来源：" + ("已对齐（学过实际位置）"
                                    if NS.load_learned_panel() else "标准位置（没对齐过）"))
        try:
            img.crop(box).save(os.path.join(log_dir, "selftest_panel.png"))
            lines_out.append("已把读的那一块存成 selftest_panel.png，看它就知道区域对不对")
        except Exception:
            pass
        if not parsed.get("counts"):
            # 没读到就再报一次「三条候选带各自认到什么」——面板到底在哪，看这个最直接
            lines_out.append("三条候选带（找面板用）各自认到的文字：")
            for i, bx in enumerate(NS._strip_boxes(img.size), 1):
                try:
                    seg, _ = NS.ocr_lines_fast(img, scale=2.0, regions=[bx])
                    txt = " / ".join(l.text for l in
                                     sorted(seg, key=lambda l: (l.box[1], l.box[0]))[:8])
                except Exception as e:  # noqa: BLE001
                    txt = f"识别出错 {e}"
                lines_out.append(f"  第{i}条 {tuple(int(v) for v in bx)}: {txt or '(空)'}")
        img.save(os.path.join(log_dir, "selftest_frame.png"))
        lines_out.append("结论：抓屏 + OCR 正常" if parsed.get("counts")
                         else "结论：抓屏 + OCR 正常，但这张画面里没有「旅程剩余站点」")
    except Exception:
        ok = False
        lines_out.append("出错：\n" + traceback.format_exc())
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out))
    return 0 if ok else 1


def main() -> int:
    # 必须在建窗口、开抓屏会话之前：否则 150% 缩放的显示器上，
    # 系统会把窗口尺寸报成逻辑值（1920×1080 变成 1280×720），抓到的帧也是错的。
    from bot.dpi import enable as _dpi_enable

    _dpi_enable()

    _fix_std_streams()
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    # 源码运行时 tools/ 不在包路径上，next_station_gui 里用的是 `import next_station`
    if os.path.isdir(os.path.join(here, "tools")):
        sys.path.insert(0, os.path.join(here, "tools"))
        sys.path.insert(0, here)

    if "--selftest" in sys.argv:
        return selftest()

    from tools.next_station_gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
