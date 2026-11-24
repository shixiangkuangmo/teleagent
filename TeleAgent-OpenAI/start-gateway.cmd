@echo off
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8899"
set "MAXP=%~2"
if "%MAXP%"=="" set "MAXP=40000"
set "TG_MAX_PROMPT=%MAXP%"
set "PYTHONIOENCODING=utf-8"

REM system prompt mode: replace / strip / filter
set "TG_SYSTEM=replace"
set "TG_SYSTEM_FILE=%~dp0prompt-safe.md"

set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "teleagent_gateway.py" --port %PORT% >> "gateway.log" 2>&1