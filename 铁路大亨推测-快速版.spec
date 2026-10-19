# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（文件夹版 / 快速启动）。

单文件版每次启动都要把 100MB 解压到临时目录，代价是 7 秒；
这个版本把依赖摊在文件夹里，启动只要 1~2 秒。

    .venv\\Scripts\\pyinstaller.exe 铁路大亨推测-快速版.spec --noconfirm --clean

产物：dist\\铁路大亨推测-快速版\\ 整个文件夹（exe 在里面）。
发给别人时把这个文件夹压成 zip，对方解压后双击里面的 exe。
"""
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = []
datas += collect_data_files("rapidocr")

binaries = []
binaries += collect_dynamic_libs("onnxruntime")

hiddenimports = [
    "next_station",
    "next_station_gui",
    "panel_calib",
    "bot.dpi",
    "bot.journey",
    "bot.ocr",
    "bot.bgcapture",
    "bot.vision",
    "bot.paths",
    "rapidocr.inference_engine.onnxruntime",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "win32gui",
    "win32con",
    "win32api",
    "windows_capture",
]

excludes = [
    "torch", "torchvision", "openvino", "paddle", "paddleocr", "tensorrt",
    "mnn", "matplotlib", "scipy", "pandas", "IPython", "pytest", "setuptools",
    "tkinter.test", "test",
]

a = Analysis(
    ["launcher.py"],
    pathex=[".", "tools"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# 同样剔掉 cv2 那个 30MB 的 ffmpeg（只做静态截图，用不到视频解码）
a.binaries = [b for b in a.binaries if "ffmpeg" not in b[0].lower()]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # ← 关键：依赖不塞进 exe，摊在文件夹里
    name="铁路大亨推测",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="铁路大亨推测-快速版",
)
