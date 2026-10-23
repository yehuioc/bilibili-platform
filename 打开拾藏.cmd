@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先在本项目运行 uv sync 以安装本地环境。
  pause
  exit /b 1
)
.venv\Scripts\python.exe -X utf8 -B tools\launch_library.py
pause
