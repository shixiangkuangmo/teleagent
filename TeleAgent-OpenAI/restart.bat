@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PORT=%~1"

echo ==========================================
echo   TeleAgent 网关 - 重启
echo ==========================================
echo.
echo [1/2] 关闭 ...
call "%~dp0stop.bat" %PORT% nopause
echo.
echo [2/2] 启动 ...
echo.
call "%~dp0start.bat" %PORT%

exit /b %ERRORLEVEL%
