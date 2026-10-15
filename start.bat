@echo off
rem All Agent Config 一键启动（Windows）
cd /d "%~dp0"

if not exist .venv\Scripts\aac.exe (
  echo 首次运行，正在准备环境…
  python -m venv .venv || (echo 需要 Python 3.11+：https://www.python.org/ & pause & exit /b 1)
  .venv\Scripts\pip install -q -e . || (echo 依赖安装失败 & pause & exit /b 1)
)

.venv\Scripts\aac.exe
