@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8899"
set "MAXP=%~2"
if "%MAXP%"=="" set "MAXP=40000"
set "NOPAUSE=0"
if /I "%~1"=="nopause" set "NOPAUSE=1"
if /I "%~2"=="nopause" set "NOPAUSE=1"
if /I "%~3"=="nopause" set "NOPAUSE=1"

echo ==========================================
echo   TeleAgent 网关 - 启动
echo ==========================================
echo.

REM ---------- 1) TeleAgent 是否在运行 ----------
tasklist /FI "IMAGENAME eq TeleAgent.exe" 2>nul | findstr /I "TeleAgent.exe" >nul
if errorlevel 1 (
    echo [!] TeleAgent 客户端未运行，网关拿不到 key。
    echo     请先启动 TeleAgent，再重跑本脚本。
    echo.
)

REM ---------- 1.5) Python 检查 ----------
set "PYEXE=%~dp0python\python.exe"
if not exist "!PYEXE!" set "PYEXE=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "!PYEXE!" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [X] 找不到 Python，网关无法启动。
        echo     请先运行 check.bat 查看完整环境，或安装 Python 3.8+ 后重试。
        echo.
        if "%NOPAUSE%"=="1" exit /b 1
        pause
        exit /b 1
    )
)
REM ---------- 2) 是否已在运行 ----------
call :check_port %PORT%
if "%LISTENING%"=="1" (
    echo [--] 网关已在运行 ^(端口 %PORT%^)，无需重复启动
    set "REUSED=1"
    goto :report
)

REM ---------- 3) 启动（独立窗口，关掉本窗口也不影响） ----------
echo [..] 正在启动网关 ^(端口 %PORT%，prompt 上限 40000^) ...
start "" /min "%~dp0start-gateway.cmd" %PORT% %MAXP%

set "REUSED=0"
set "READY=0"
for /L %%i in (1,1,60) do (
    ping -n 2 127.0.0.1 >nul
    call :check_port %PORT%
    if "!LISTENING!"=="1" (
        set "READY=1"
        goto :started
    )
)
:started
echo.

if "%READY%"=="0" (
    echo [X] 60 秒内没起来，看日志：
    if exist gateway.log ( powershell -NoProfile -Command "Get-Content gateway.log -Tail 15" )
    if exist gateway.log.err ( powershell -NoProfile -Command "Get-Content gateway.log.err -Tail 10" )
    echo.
    if "%NOPAUSE%"=="1" exit /b 1
    pause
    exit /b 1
)
echo [OK] 网关已启动

:report
echo.
echo 网关地址 : http://127.0.0.1:%PORT%/v1
echo API Key  : 任意值（如 teleagent）
echo 模型     : chat-flagship / chat-pro / chat-flash / chat-lite / chat-nano
if "%REUSED%"=="1" (echo prompt   : 沿用已运行实例，本次参数未生效) else (echo prompt   : 上限 %MAXP% 字符)
echo 日志     : %~dp0gateway.log
echo.
echo 停止用 stop.bat，重启用 restart.bat
echo 参数： start.bat [端口] [prompt上限]   例：start.bat 8899 80000
echo.
if "%NOPAUSE%"=="1" exit /b 0
pause
exit /b 0

REM ---------- 子过程：检查端口是否 LISTENING ----------
:check_port
set "LISTENING=0"
for /f "tokens=*" %%L in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "LISTENING=1"
exit /b 0
