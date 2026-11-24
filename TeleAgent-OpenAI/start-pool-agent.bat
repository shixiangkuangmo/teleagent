@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist pool-agent.json (
  echo 缺少 pool-agent.json。
  echo 请复制 pool-agent.example.json 为 pool-agent.json，并填入面板里创建的 node_id 与 token。
  pause
  exit /b 1
)

:loop
python pool_agent.py --config pool-agent.json
echo.
echo 代理已退出，5 秒后自动重连...
timeout /t 5 /nobreak >nul
goto loop
