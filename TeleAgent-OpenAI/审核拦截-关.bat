@echo off
chcp 936 >nul
title TeleAgent 审核拦截 - 关
net session >nul 2>&1
if errorlevel 1 (
  echo 需要管理员权限，正在申请提权...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0review-block.ps1" -Mode off
echo.
pause