# ============================================================
#  TeleAgent OpenAI 网关 · 一键配置
#  用法：  .\setup.ps1          启动
#          .\setup.ps1 -Stop    停止
#          .\setup.ps1 -Status  查看状态
# ============================================================
param(
    [switch]$Stop,
    [switch]$Status,
    [int]$Port = 8899,
    [int]$MaxPrompt = 40000
)

$ErrorActionPreference = 'Continue'
# 子进程（python）输出是 UTF-8，控制台默认按本地代码页解码会把中文显示成 ????
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Gateway = Join-Path $Here 'teleagent_gateway.py'
$PidFile = Join-Path $Here '.gateway.pid'
$LogFile = Join-Path $Here 'gateway.log'

function Find-Python {
    # 优先用包内自带的便携 Python（无需在目标机器安装 Python）
    $cands = @(
        (Join-Path $Here 'python\python.exe'),
        'C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe',
        (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
        (Get-Command python3.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    )
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

function Test-TeleAgent {
    $main = Get-CimInstance Win32_Process -Filter "Name='TeleAgent.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -notmatch '--type=' } | Select-Object -First 1
    if (-not $main) { return $null }
    $port = Get-NetTCPConnection -State Listen -LocalPort 4397 -ErrorAction SilentlyContinue | Select-Object -First 1
    return [pscustomobject]@{ Pid = $main.ProcessId; Port4397 = [bool]$port }
}

function Test-Gateway {
    param([int]$P)
    try {
        # 不用 Invoke-WebRequest：PowerShell 5.1 缺 -UseBasicParsing 时依赖 IE 引擎，
        # 在未配置 IE 的 Server 上必定抛异常 -> 永远判定网关没起来。
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$P/v1/models")
        $req.Timeout = 4000
        $req.Proxy = $null
        $resp = $req.GetResponse()
        $ok = ($resp.StatusCode -eq 200)
        $resp.Close()
        return $ok
    } catch { return $false }
}

function Get-GatewayPid {
    if (-not (Test-Path $PidFile)) { return $null }
    $v = (Get-Content $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ($v -match '^\d+$') {
        $p = Get-Process -Id ([int]$v) -ErrorAction SilentlyContinue
        if ($p) { return [int]$v }
    }
    return $null
}

# ───────────── 停止 ─────────────
if ($Stop) {
    $killed = @()
    $gp = Get-GatewayPid
    if ($gp) {
        Stop-Process -Id $gp -Force -ErrorAction SilentlyContinue
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        $killed += $gp
    }
    # pid 文件可能对不上（比如网关是手动启动的），再看端口上的实例
    $busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($busy -and ($killed -notcontains $busy.OwningProcess)) {
        if (Test-Gateway -P $Port) {
            Stop-Process -Id $busy.OwningProcess -Force -ErrorAction SilentlyContinue
            $killed += $busy.OwningProcess
        }
    }
    if ($killed.Count) {
        Write-Host "[OK] 网关已停止 (PID $($killed -join ', '))" -ForegroundColor Green
    } else {
        Write-Host "[--] 网关未在运行" -ForegroundColor Yellow
    }
    exit 0
}

# ───────────── 状态 ─────────────
if ($Status) {
    $ta  = Test-TeleAgent
    $gp  = Get-GatewayPid
    $ok  = Test-Gateway -P $Port
    Write-Host ""
    Write-Host "TeleAgent 进程 : " -NoNewline
    if ($ta) { Write-Host "PID $($ta.Pid)" -ForegroundColor Green } else { Write-Host "未运行" -ForegroundColor Red }
    Write-Host "4397 监听     : " -NoNewline
    if ($ta -and $ta.Port4397) { Write-Host "正常" -ForegroundColor Green } else { Write-Host "未监听" -ForegroundColor Red }
    Write-Host "网关进程      : " -NoNewline
    $busy2 = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    $showPid = if ($busy2) { $busy2.OwningProcess } else { $gp }
    if ($showPid) { Write-Host "PID $showPid" -ForegroundColor Green } else { Write-Host "未运行" -ForegroundColor Yellow }
    Write-Host "网关响应      : " -NoNewline
    if ($ok) { Write-Host "http://127.0.0.1:$Port/v1 可用" -ForegroundColor Green } else { Write-Host "无响应" -ForegroundColor Red }
    Write-Host ""
    exit 0
}

# ───────────── 启动 ─────────────
Write-Host ""
Write-Host "=======================================" -ForegroundColor Cyan
Write-Host "  TeleAgent OpenAI 网关 · 一键配置" -ForegroundColor Cyan
Write-Host "=======================================" -ForegroundColor Cyan
Write-Host ""

# 1) Python
$py = Find-Python
if (-not $py) {
    Write-Host "[X] 找不到 Python，请先安装或修改脚本里的 Find-Python 路径" -ForegroundColor Red
    exit 1
}
Write-Host "[1/6] Python : $py" -ForegroundColor Gray

# 2) 网关文件
if (-not (Test-Path $Gateway)) {
    Write-Host "[X] 缺少 teleagent_gateway.py，请放在同一目录" -ForegroundColor Red
    exit 1
}
Write-Host "[2/6] 网关脚本 : 已就绪" -ForegroundColor Gray

# 3) TeleAgent
$ta = Test-TeleAgent
if (-not $ta) {
    Write-Host "[!] TeleAgent 没在运行，正在启动..." -ForegroundColor Yellow
    $exe = 'C:\Program Files\TeleAgent\TeleAgent.exe'
    if (Test-Path $exe) {
        Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe -Parent)
        Write-Host "    等待 TeleAgent 就绪..." -NoNewline
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Seconds 1
            Write-Host "." -NoNewline
            $ta = Test-TeleAgent
            if ($ta -and $ta.Port4397) { break }
        }
        Write-Host ""
    }
    if (-not $ta) {
        Write-Host "[X] TeleAgent 启动失败，请手动打开后重试" -ForegroundColor Red
        exit 1
    }
}
if (-not $ta.Port4397) {
    Write-Host "[!] 4397 尚未监听，再等 15 秒..." -ForegroundColor Yellow
    Start-Sleep -Seconds 15
    $ta = Test-TeleAgent
    if (-not $ta.Port4397) {
        Write-Host "[X] 4397 未就绪（TeleAgent 可能还在初始化），稍后重跑本脚本" -ForegroundColor Red
        exit 1
    }
}
Write-Host "[3/6] TeleAgent : PID $($ta.Pid)，4397 正常" -ForegroundColor Green

# 4) 端口占用：已经有能用的网关就直接复用，不打断
$reuseGateway = $false
$existing = Get-GatewayPid
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue

if (Test-Gateway -P $Port) {
    $gatewayPid = if ($busy) { $busy.OwningProcess } else { $existing }
    Write-Host "[4/6] 端口 $Port : 已有网关在运行 (PID $gatewayPid)，直接复用" -ForegroundColor Green
    $reuseGateway = $true
}
elseif ($busy) {
    Write-Host "[X] 端口 $Port 被占用 (PID $($busy.OwningProcess))，但它不响应 /v1/models" -ForegroundColor Red
    Write-Host "    换个端口：.\setup.ps1 -Port 8900" -ForegroundColor Yellow
    exit 1
}
else {
    if ($existing) {
        Write-Host "[4/6] 清理残留网关进程 (PID $existing)" -ForegroundColor Yellow
        Stop-Process -Id $existing -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[4/6] 端口 $Port : 空闲" -ForegroundColor Gray
}

# 5) 启动网关（已在运行则跳过）
$ready = $true
if (-not $reuseGateway) {
    Write-Host "[5/6] 启动网关（prompt 上限 $MaxPrompt 字符；首次提 key 较慢，之后走缓存）..." -ForegroundColor Gray
    Remove-Item $LogFile -Force -ErrorAction SilentlyContinue
    # 传给网关子进程：prompt 裁剪上限（默认 40000，超出会丢弃中间历史）
    $env:TG_MAX_PROMPT = "$MaxPrompt"
    $env:PYTHONIOENCODING = 'utf-8'
    $proc = Start-Process -FilePath $py `
        -ArgumentList @($Gateway, '--port', $Port) `
        -WorkingDirectory $Here `
        -RedirectStandardOutput $LogFile `
        -RedirectStandardError "$LogFile.err" `
        -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content $PidFile -Encoding ASCII

    Write-Host "    等待网关就绪 " -NoNewline
    $ready = $false
    for ($i = 0; $i -lt 180; $i++) {
        Start-Sleep -Seconds 1
        if ($i % 5 -eq 0) { Write-Host "." -NoNewline }
        if (Test-Gateway -P $Port) { $ready = $true; break }
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) { break }
    }
    Write-Host ""

    if (-not $ready) {
        Write-Host "[X] 网关未能在 3 分钟内就绪，日志：" -ForegroundColor Red
        if (Test-Path $LogFile) { Get-Content $LogFile -Tail 15 | ForEach-Object { "    $_" } }
        if (Test-Path "$LogFile.err") { Get-Content "$LogFile.err" -Tail 10 | ForEach-Object { "    $_" } }
        exit 1
    }
}
else {
    Write-Host "[5/6] 复用已有网关，跳过启动" -ForegroundColor Gray
    Write-Host "    注意：prompt 上限沿用已运行实例，本次 -MaxPrompt 未生效" -ForegroundColor DarkGray
}

# 6) 冒烟测试（用 Python 而非 Invoke-RestMethod：后者在部分机器上会卡住）
Write-Host "[6/6] 冒烟测试..." -ForegroundColor Gray
$smokeFile = Join-Path $Here '.smoke.py'
$smokeOut  = Join-Path $Here '.smoke.json'
@'
import json, sys, urllib.request
out = {}
try:
    body = json.dumps({"model": "chat-lite",
                       "messages": [{"role": "user", "content": "回复两个字：就绪"}],
                       "stream": False}).encode()
    req = urllib.request.Request("http://127.0.0.1:__PORT__/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        j = json.loads(r.read())
    out = {"ok": True,
           "reply": j["choices"][0]["message"]["content"],
           "in": j["usage"]["prompt_tokens"],
           "out": j["usage"]["completion_tokens"]}
except Exception as e:
    out = {"ok": False, "err": str(e)[:160]}
# 写 UTF-8 文件交给 PowerShell 读，绕开控制台代码页导致的乱码
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
'@.Replace('__PORT__', "$Port") | Set-Content -Path $smokeFile -Encoding UTF8
try {
    & $py $smokeFile $smokeOut | Out-Null
    $res = Get-Content $smokeOut -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($res.ok) {
        Write-Host "    模型回复: $($res.reply)" -ForegroundColor Green
        Write-Host "    tokens : in=$($res.in) out=$($res.out)" -ForegroundColor Gray
    } else {
        Write-Host "    [!] 冒烟测试失败: $($res.err)" -ForegroundColor Yellow
        Write-Host "        网关已启动，可直接使用；失败多为首次调用较慢" -ForegroundColor Yellow
    }
} catch {
    Write-Host "    [!] 冒烟测试异常: $($_.Exception.Message)" -ForegroundColor Yellow
} finally {
    Remove-Item $smokeFile -Force -ErrorAction SilentlyContinue
    Remove-Item $smokeOut  -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "=======================================" -ForegroundColor Green
Write-Host "  配置完成" -ForegroundColor Green
Write-Host "=======================================" -ForegroundColor Green
Write-Host ""
Write-Host "网关地址 : " -NoNewline; Write-Host "http://127.0.0.1:$Port/v1" -ForegroundColor Cyan
Write-Host "API Key  : " -NoNewline; Write-Host "任意值（如 teleagent）" -ForegroundColor Cyan
Write-Host "可用模型 : " -NoNewline; Write-Host "chat-flagship(旗舰) / chat-pro(智能) / chat-flash(极速) / chat-lite(轻量) / chat-nano(轻速)" -ForegroundColor Cyan
Write-Host "prompt上限: " -NoNewline; Write-Host "$MaxPrompt 字符（超出会丢弃中间历史；用 -MaxPrompt 80000 可放宽）" -ForegroundColor Cyan
Write-Host ""
Write-Host "提示     : 上游对大 prompt 会超时（14 万字符→900s 返回空），" -ForegroundColor DarkGray
Write-Host "           所以网关会自动裁剪；超长对话建议开新会话。" -ForegroundColor DarkGray
Write-Host "           key 首次提取后写入 .session_key，重启网关秒开。" -ForegroundColor DarkGray
Write-Host ""
Write-Host "--- Python (openai SDK) ---" -ForegroundColor Magenta
Write-Host @"
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:$Port/v1", api_key="teleagent")
r = c.chat.completions.create(model="chat-flagship",
      messages=[{"role":"user","content":"你好"}])
print(r.choices[0].message.content)
"@ -ForegroundColor Gray
Write-Host "--- Node / JS ---" -ForegroundColor Magenta
Write-Host @"
const c = new OpenAI({ baseURL: "http://127.0.0.1:$Port/v1", apiKey: "teleagent" });
"@ -ForegroundColor Gray
Write-Host "--- curl ---" -ForegroundColor Magenta
Write-Host @"
curl http://127.0.0.1:$Port/v1/chat/completions -H "Content-Type: application/json" ``
  -d '{"model":"chat-lite","messages":[{"role":"user","content":"你好"}]}'
"@ -ForegroundColor Gray
Write-Host ""
Write-Host "管理命令 : " -NoNewline
Write-Host ".\setup.ps1 -Status   查看状态" -ForegroundColor Yellow
Write-Host "           " -NoNewline
Write-Host ".\setup.ps1 -Stop     停止网关" -ForegroundColor Yellow
Write-Host "日志     : $LogFile"
Write-Host ""
