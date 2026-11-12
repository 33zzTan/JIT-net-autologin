@echo off
rem ============================================================
rem  校园网自动登录 - 手动运行（可以看到运行日志）
rem  常用参数：
rem    --browser     自动登录时显示浏览器窗口
rem    --no-login    只连 WiFi + 打开登录网站，不自动登录
rem    --diagnose    抓取门户页面结构，便于排查表单字段
rem    --force       已联网也强制登录一次
rem    --skip-wifi   跳过 WiFi 检查（在当前不在 JIT-Stu 时测试用）
rem ============================================================
setlocal
set "APP_DIR=%~dp0"

set "PY=%APP_DIR%python.exe"
if not exist "%PY%" set "PY=%APP_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

pushd "%APP_DIR%"
"%PY%" -m jitauth %*
set "RC=%errorlevel%"
popd

echo.
echo 退出码: %RC%
pause
exit /b %RC%
