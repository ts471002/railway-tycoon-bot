@echo off
cd /d "%~dp0"
set "PYW=.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "tools\next_station_gui.py"
exit
