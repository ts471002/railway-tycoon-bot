@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 打包文件夹版...
".venv\Scripts\pyinstaller.exe" "铁路大亨推测-快速版.spec" --noconfirm --distpath dist --workpath build_fast
if errorlevel 1 goto err

echo.
echo [2/3] 压缩成 payload.tar.xz（约 2 分半）...
".venv\Scripts\python.exe" "tools\make_payload.py"
if errorlevel 1 goto err

echo.
echo [3/3] 生成安装版 exe...
".venv\Scripts\pyinstaller.exe" "铁路大亨推测-安装版.spec" --noconfirm --distpath dist --workpath build_inst
if errorlevel 1 goto err

echo.
echo [完成] dist\铁路大亨推测-安装版.exe
pause
exit /b 0

:err
echo.
echo [失败] 看上面的提示。
pause
exit /b 1
