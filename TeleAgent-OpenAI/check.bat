@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================
echo   TeleAgent 网关 - 环境自检
echo ==========================================
echo.

set "PROBLEM=0"

REM ---------- 1. 系统架构 ----------
if /I "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
    echo [OK] 系统架构 : x64
) else (
    echo [X ] 系统架构 : %PROCESSOR_ARCHITECTURE%  ^(需要 64 位 Windows^)
    set /a PROBLEM+=1
)

REM ---------- 2. 管理员权限 ----------
whoami /groups 2>nul | findstr /C:"S-1-16-12288" >nul
if not errorlevel 1 (
    echo [OK] 运行权限 : 管理员
) else (
    echo [!] 运行权限 : 非管理员  ^(读 TeleAgent 进程内存会失败，请右键“以管理员身份运行”^)
    set /a PROBLEM+=1
)

REM ---------- 3. Python ----------
set "PY=%~dp0python\python.exe"
if exist "%PY%" (
    echo [OK] Python   : 包内便携版  ^(无需安装^)
) else (
    set "PY=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if exist "!PY!" (
        echo [!] Python   : 包内便携版缺失，改用 DSH 运行时  !PY!
    ) else (
        where python >nul 2>&1
        if not errorlevel 1 (
            for /f "delims=" %%p in ('where python') do if not defined PY2 set "PY2=%%p"
            set "PY=!PY2!"
            echo [!] Python   : 包内便携版缺失，改用 PATH 中的  !PY!
        ) else (
            echo [X ] Python   : 完全没有  ^(包内 python 文件夹丢了？重新解压整个包^)
            set /a PROBLEM+=1
        )
    )
)

REM ---------- 4. TeleAgent 进程 ----------
tasklist /FI "IMAGENAME eq TeleAgent.exe" 2>nul | findstr /I "TeleAgent.exe" >nul
if not errorlevel 1 (
    echo [OK] TeleAgent : 进程在运行
) else (
    echo [X ] TeleAgent : 未运行  ^(凭证只能从本机 TeleAgent 客户端取，必须先安装并登录^)
    set /a PROBLEM+=1
)

REM ---------- 5. 4397 端口 ----------
netstat -ano | findstr /R /C:":4397 .*LISTENING" >nul
if not errorlevel 1 (
    echo [OK] 4397     : 已监听
) else (
    echo [!] 4397     : 未监听  ^(TeleAgent 可能还在启动或未登录^)
    set /a PROBLEM+=1
)

REM ---------- 6. 8899 端口 ----------
netstat -ano | findstr /R /C:":8899 .*LISTENING" >nul
if not errorlevel 1 (
    echo [i] 8899     : 已有程序监听  ^(可能就是网关本身，正常^)
) else (
    echo [OK] 8899     : 空闲
)

REM ---------- 7. 文件完整性 ----------
set "MISSING="
for %%f in (teleagent_gateway.py teleagent_client.py start-gateway.cmd) do (
    if not exist "%%f" set "MISSING=!MISSING! %%f"
)
if defined MISSING (
    echo [X ] 关键文件 : 缺少!MISSING!
    set /a PROBLEM+=1
) else (
    echo [OK] 关键文件 : 齐全
)

REM ---------- 8. Python 版本 ----------
if defined PY (
    for /f "tokens=2" %%v in ('"!PY!" -V 2^>^&1') do echo [i] Python版本: %%v
)

echo.
echo ==========================================
if "%PROBLEM%"=="0" (
    echo   自检通过，可以运行 start.bat
) else (
    echo   发现 %PROBLEM% 项问题，按上面 [X] 提示处理后重试
)
echo ==========================================
echo.
pause