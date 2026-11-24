@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8899"

echo ==========================================
echo   TeleAgent 网关 - 关闭
echo ==========================================
echo.

set "KILLED=0"

REM ---------- 1) 按 pid 文件 ----------
if exist ".gateway.pid" (
    for /f "usebackq delims=" %%i in (".gateway.pid") do (
        tasklist /FI "PID eq %%i" 2>nul | findstr /I "python" >nul
        if not errorlevel 1 (
            echo [--] 停止 PID %%i ^(pid 文件^)
            taskkill /PID %%i /F >nul 2>&1
            set "KILLED=1"
        )
    )
    del /q ".gateway.pid" >nul 2>&1
)

REM ---------- 2) 按端口找实际监听进程 ----------
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    echo [--] 停止端口 %PORT% 上的 PID %%p
    taskkill /PID %%p /F >nul 2>&1
    set "KILLED=1"
)

REM ---------- 3) 兜底：清掉残留的网关 python 进程 ----------
for /f %%n in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*teleagent_gateway*' } | Measure-Object).Count" 2^>nul') do set "LEFT=%%n"
if not "%LEFT%"=="0" (
    echo [--] 清理残留网关进程 %LEFT% 个
    powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*teleagent_gateway*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
    set "KILLED=1"
)

ping -n 2 127.0.0.1 >nul

set "LISTENING=0"
for /f "tokens=*" %%L in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "LISTENING=1"

echo.
if "%LISTENING%"=="1" (
    echo [X] 端口 %PORT% 仍在监听，可能被其它程序占用
) else if "%KILLED%"=="1" (
    echo [OK] 网关已停止，端口 %PORT% 已释放
) else (
    echo [--] 网关本来就没在运行
)
echo.
if /I "%~2"=="nopause" exit /b 0
pause
exit /b 0
