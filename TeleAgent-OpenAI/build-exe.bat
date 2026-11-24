@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   TeleAgent 节点 - 打包 exe
echo ============================================
echo.

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY if exist "python\python.exe" set "PY=python\python.exe"
if not defined PY (
    echo [X] 未找到 Python，请先安装 Python 3.8+ 并勾选 Add to PATH。
    pause
    exit /b 1
)

echo [1/2] 检查 PyInstaller ...
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo       未安装，正在安装 ...
    %PY% -m pip install --upgrade pyinstaller
    if errorlevel 1 (
        echo [X] PyInstaller 安装失败。
        pause
        exit /b 1
    )
)

echo [2/2] 打包 ...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name TeleAgentNode ^
    --hidden-import teleagent_gateway ^
    --distpath dist --workpath build ^
    node_gui.py
if errorlevel 1 (
    echo.
    echo [X] 打包失败，请查看上面的错误。
    pause
    exit /b 1
)

echo.
echo [OK] 已生成：dist\TeleAgentNode.exe
echo      把这个 exe 拷到要跑节点的 Windows 机器，双击运行即可。
echo      首次运行会在同目录生成 pool-agent.json。
echo.
pause
