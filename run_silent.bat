@echo off
rem ============================================================
rem  校园网自动登录 - 开机自启（静默运行，不弹黑框）
rem  由 install_task.ps1 注册的计划任务调用此文件
rem ============================================================
setlocal

set "APP_DIR=%~dp0"
set "PYW=%APP_DIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%APP_DIR%.venv\Scripts\pythonw.exe"

if exist "%PYW%" (
    start "" /b "%PYW%" -m jitauth --silent
    exit /b 0
)

rem 兜底：用系统 pythonw（无黑框），没有就退回 python
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" /b pythonw.exe -m jitauth --silent
    exit /b 0
)

where python.exe >nul 2>nul
if %errorlevel%==0 (
    python.exe -m jitauth --silent
    exit /b %errorlevel%
)

exit /b 1
