# teleagent-dashboard launcher + supervisor (HKCU Run autostart)
# ASCII only for PS 5.1 compatibility (no-BOM UTF-8 with CJK would garble).
# Behavior:
#   - Idempotent: exits if port 8590 is already listening (another instance alive)
#   - Supervises node server.js in the foreground; restarts it if it exits/crashes
#   - Rapid failures back off (5s * count); gives up after 6 rapid failures
#   - A run lasting over 5 minutes resets the failure counter (healthy run, new incident)
# Deploy hot-reload trick: updating code then killing node is enough -
# the supervisor restarts it with the new code within ~10 seconds.
$dir = $PSScriptRoot
$logDir = Join-Path $dir 'data'
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir 'autostart.log'
$PORT = 8590
$MAX_RAPID_FAILS = 6

function Write-KaLog([string]$msg) {
  Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

function Test-DashboardUp {
  return [bool](Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue)
}

# Idempotent guard: another instance is already serving
if (Test-DashboardUp) {
  Write-KaLog "already-running(port $PORT up), supervisor exits"
  exit 0
}

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) { $node = 'D:\TeleAgent\data\runtimes\node\node.exe' }
if (-not (Test-Path -LiteralPath $node)) {
  Write-KaLog "node not found, autostart failed"
  exit 1
}

Set-Location -LiteralPath $dir
Write-KaLog "supervisor: starting (node=$node)"

$rapidFails = 0
while ($true) {
  if (Test-DashboardUp) {
    # another instance won the race; stop supervising
    Write-KaLog "port $PORT taken over by another instance, supervisor exits"
    exit 0
  }
  $t0 = Get-Date
  Write-KaLog "launching node server.js (rapidFails=$rapidFails)"
  & $node server.js *>> $log
  $code = $LASTEXITCODE
  $ran = ((Get-Date) - $t0).TotalSeconds
  if ($ran -gt 300) { $rapidFails = 0 } else { $rapidFails++ }
  Write-KaLog "node exited code=$code after $([int]$ran)s (rapidFails=$rapidFails)"
  if ($rapidFails -ge $MAX_RAPID_FAILS) {
    Write-KaLog "supervisor giving up after $rapidFails rapid failures; check server logs"
    exit 1
  }
  Start-Sleep -Seconds (5 * $rapidFails)
}
