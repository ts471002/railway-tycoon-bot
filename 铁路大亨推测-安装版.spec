# -*- mode: python ; coding: utf-8 -*-
"""安装版打包：把「快速版文件夹」压进一个 exe，装一次就行。

    .venv\\Scripts\\pyinstaller.exe 铁路大亨推测-安装版.spec --noconfirm

前置：先跑 `打包快速版.bat` 生成 dist\\铁路大亨推测-快速版\\，
再把它压成 payload.tar.xz（见 打包安装版.bat，会自动做这两步）。

产物：dist\\铁路大亨推测-安装版.exe
"""
a = Analysis(
    ["installer.py"],
    pathex=["."],
    binaries=[],
    datas=[("payload.tar.xz", ".")],     # 内嵌的运行环境
    hiddenimports=[
        "win32com.client",
        "win32com",
        "pythoncom",
        "pywintypes",
        "win32api",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "openvino", "paddle", "tensorrt", "matplotlib", "scipy",
        "pandas", "IPython", "pytest", "numpy", "cv2", "onnxruntime", "rapidocr",
        "windows_capture", "PIL",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="铁路大亨推测-安装版",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
