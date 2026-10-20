@echo off
setlocal EnableExtensions
chcp 65001 >nul
title All Agent Config
cd /d "%~dp0"

rem 统一 UTF-8，避免中文输出在 cp936 控制台下编码失败
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "LOG=%~dp0start.log"

echo ============================================
echo   All Agent Config
echo ============================================
>>"%LOG%" echo [%DATE% %TIME%] cwd=%CD%

rem ---------- 1/4 Python ----------
echo.
echo [1/4] 检测 Python 3.11+ ...
set "PY="
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.12"
if not defined PY (
  py -3 -c "import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python -c "import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)

if not defined PY (
  echo.
  echo [错误] 未找到可用的 Python 3.11 或更高版本。
  echo        若已安装，可能是没有加入 PATH，或命中了 Windows 商店的 python 占位符。
  echo.
  where winget >nul 2>&1
  if errorlevel 1 (
    echo [提示] 未找到 winget，无法自动安装。
    echo        请手动安装：https://www.python.org/downloads/
    echo        安装时务必勾选 "Add python.exe to PATH"。
    goto :failed
  )
  choice /c YN /n /m "是否现在用 winget 安装 Python 3.12？[Y/N] "
  if errorlevel 2 goto :failed
  echo 正在安装 Python 3.12，如弹出 UAC 请允许...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  rem winget 在「已安装」时也可能返回非零，统一以重新探测结果为准
  set "PY="
  py -3.12 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=py -3.12"
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set PY="%ProgramFiles%\Python312\python.exe"
  if not defined PY (
    echo [错误] Python 仍未就绪：安装可能被取消，或新 Python 尚未进入当前窗口。
    echo        请关闭本窗口后重新双击 start.bat；若仍不行请手动安装：
    echo        https://www.python.org/downloads/
    pause
    goto :failed
  )
)
>>"%LOG%" echo [%DATE% %TIME%] PY=%PY%
echo       使用 %PY%

rem ---------- 2/4 venv ----------
echo.
echo [2/4] 检查虚拟环境 ...
set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" (
  "%VPY%" -c "import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)" >nul 2>&1
  if errorlevel 1 (
    echo [提示] 现有 .venv 不可用（可能被移动或对应 Python 已删除），正在重建...
    move /y ".venv" ".venv.broken-%RANDOM%%RANDOM%" >nul 2>&1
    if errorlevel 1 (
      echo [错误] 无法重命名 .venv，请关闭占用它的程序后重试。
      goto :failed
    )
  )
)
if not exist "%VPY%" (
  echo       创建虚拟环境 .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败。
    goto :failed
  )
)
>>"%LOG%" echo [%DATE% %TIME%] VENV=%VPY%

rem ---------- 3/4 deps ----------
echo.
echo [3/4] 检查依赖 ...
"%VPY%" -c "import fastapi, uvicorn, yaml, cryptography, multipart" >nul 2>&1
if errorlevel 1 (
  echo       安装依赖（首次运行约 1-2 分钟）...
  "%VPY%" -m pip --version >nul 2>&1
  if errorlevel 1 "%VPY%" -m ensurepip --upgrade
  "%VPY%" -m pip install -q --disable-pip-version-check -e .
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    echo        手动命令：.venv\Scripts\python -m pip install -e .
    goto :failed
  )
)

rem ---------- 4/4 port ----------
echo.
echo [4/4] 检查端口 ...
if "%~1"=="" (
  netstat -ano | findstr /r /c:":8600 .*LISTENING" >nul 2>&1
  if not errorlevel 1 (
    echo [提示] 8600 端口已被占用，可能已经有一个实例在运行。
    choice /c YN /n /m "是否打开 http://127.0.0.1:8600 并退出？[Y/N] "
    if errorlevel 2 goto :run
    start "" "http://127.0.0.1:8600"
    goto :done
  )
)

:run
echo.
echo 启动中：浏览器会自动打开，关闭本窗口或按 Ctrl+C 结束。
"%VPY%" -m app.main %*
echo.
echo 程序已退出。
pause
goto :done

:failed
echo.
echo 启动失败，请把上面的错误信息截图反馈。
echo 诊断信息已写入：%LOG%
pause
exit /b 1

:done
endlocal
exit /b 0
