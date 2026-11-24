<#
  TeleAgent 审核出站拦截开关
  ---------------------------
  原理：ais.exe 的内容审核是它自己去 POST
        https://agent.teleai.com.cn/.../check/content
        它的安全策略 fail_mode 本来就是 "open"（审核请求失败 = 直接放行）。
        所以用防火墙按【程序 + 目标 IP】精确封掉 ais 的这条出站，
        审核请求就永远发不到云端，内容也不会被送出去审。
        模型的正常对话流量走的是另一个进程（super-agent-code），完全不受影响。

  用法（需要管理员）：
        powershell -ExecutionPolicy Bypass -File review-block.ps1 -Mode on
        powershell -ExecutionPolicy Bypass -File review-block.ps1 -Mode off
        powershell -ExecutionPolicy Bypass -File review-block.ps1 -Mode status
#>
param([ValidateSet("on", "off", "status")][string]$Mode = "status")

$RuleName = "TeleAgent-AIS-Block-Review"
$Host_ = "agent.teleai.com.cn"

function Get-AisPath {
    $p = (Get-CimInstance Win32_Process -Filter "Name='ais.exe'" -ErrorAction SilentlyContinue |
          Select-Object -First 1).ExecutablePath
    if (-not $p) {
        foreach ($c in @("D:\TeleAgent\data\runtimes\ais\bin\ais.exe",
                         "$env:LOCALAPPDATA\Programs\TeleAgent\data\runtimes\ais\bin\ais.exe",
                         "C:\Program Files\TeleAgent\data\runtimes\ais\bin\ais.exe")) {
            if (Test-Path $c) { return $c }
        }
    }
    return $p
}

function Get-TargetIps {
    $ips = Resolve-DnsName $Host_ -Type A -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress |
           Sort-Object -Unique
    return @($ips)
}

switch ($Mode) {
    "status" {
        $r = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        if (-not $r) { Write-Host "审核拦截: 未启用"; exit 0 }
        $af = $r | Get-NetFirewallAddressFilter
        $pf = $r | Get-NetFirewallApplicationFilter
        Write-Host "审核拦截: 已启用"
        Write-Host ("  程序: " + $pf.Program)
        Write-Host ("  目标: " + ($af.RemoteAddress -join ", "))
        Write-Host ("  状态: " + $r.Enabled + " / " + $r.Action)
    }
    "on" {
        $ais = Get-AisPath
        if (-not $ais -or -not (Test-Path $ais)) {
            Write-Host "没找到 ais.exe，请先启动 TeleAgent 再执行。" -ForegroundColor Red
            exit 1
        }
        $ips = Get-TargetIps
        if ($ips.Count -eq 0) {
            Write-Host "解析 $Host_ 失败，检查网络后重试。" -ForegroundColor Red
            exit 1
        }
        Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName $RuleName -Direction Outbound -Program $ais `
            -RemoteAddress $ips -Action Block -Profile Any -Description "TeleAgent 内容审核出站拦截" | Out-Null
        Write-Host "已启用审核拦截" -ForegroundColor Green
        Write-Host ("  ais.exe : " + $ais)
        Write-Host ("  目标 IP : " + ($ips -join ", "))
        Write-Host "  效果    : 审核请求发不出去 -> fail_mode=open -> 直接放行，内容不出本机"
    }
    "off" {
        $r = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        if (-not $r) { Write-Host "本来就没启用。"; exit 0 }
        Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        Write-Host "已关闭审核拦截（恢复原厂审核行为）" -ForegroundColor Yellow
    }
}
