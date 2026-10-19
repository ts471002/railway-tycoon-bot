@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在打包 快速版（文件夹，启动 1.2 秒）...
echo.
".venv\Scripts\pyinstaller.exe" "铁路大亨推测-快速版.spec" --noconfirm --distpath dist --workpath build_fast
if errorlevel 1 (
  echo.
  echo [失败] 打包出错，看上面的提示。
  pause
  exit /b 1
)
echo.
echo [完成] dist\铁路大亨推测-快速版\
pause
