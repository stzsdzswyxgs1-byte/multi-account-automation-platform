<#
03-windows-rdp-setup.ps1
日本电脑 (Windows 10 Pro) 一键配置：开 RDP 服务端 + 永不休眠 + 打印 Tailscale IP。

用法：
  以【管理员】身份打开 PowerShell，然后：
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\03-windows-rdp-setup.ps1

前提：
  - 已装好 Tailscale 并登录同一账号 (https://tailscale.com/download/windows，或 winget install tailscale.tailscale)。
  - 系统为 Win10 Pro。家庭版无 RDP 服务端，请改用 RustDesk (见 runbook 附录 A)。

⚠️ 绝对不要在本机启用 Tailscale 的 Exit Node / Use exit node —— 会改变业务出口 IP。
#>

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

Write-Host "==> [1/4] 检测系统版本" -ForegroundColor Cyan
$os = Get-CimInstance Win32_OperatingSystem
Write-Host "    $($os.Caption)"
if ($os.Caption -match "Home|家庭") {
  Write-Warning "检测到家庭版 (Home)，无 RDP 服务端。请改用 RustDesk 方案 (runbook 附录 A)。"
  return
}

Write-Host "==> [2/4] 开启 RDP 服务端 + 防火墙规则" -ForegroundColor Cyan
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name "fDenyTSConnections" -Value 0
Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
# 建议保持 NLA 开启 (更安全)；如客户端不支持可自行关闭。
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name "UserAuthentication" -Value 1

Write-Host "==> [3/4] 设置永不休眠 (断线可唤醒的关键)" -ForegroundColor Cyan
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0

Write-Host "==> [4/4] 读取本机 Tailscale IP" -ForegroundColor Cyan
$tsCandidates = @(
  "$Env:ProgramFiles\Tailscale\tailscale.exe",
  "${Env:ProgramFiles(x86)}\Tailscale\tailscale.exe",
  "tailscale.exe"
)
$ts = $tsCandidates | Where-Object { (Get-Command $_ -ErrorAction SilentlyContinue) -or (Test-Path $_) } | Select-Object -First 1
if ($ts) {
  $ip = & $ts ip -4 2>$null
  Write-Host "`n    本机 Tailscale IP: $ip" -ForegroundColor Green
  Write-Host "    手机 RDP 客户端就连这个 100.x.x.x 地址。"
} else {
  Write-Warning "未找到 tailscale.exe。请先装好并登录 Tailscale，再手动跑: tailscale ip -4"
}

Write-Host "`n✅ 配置完成。" -ForegroundColor Green
Write-Host "   提醒：" -ForegroundColor Yellow
Write-Host "   - Windows 登录密码务必用强密码 (RDP 在 tailnet 内可达)。"
Write-Host "   - 给本机打 ACL tag: tag:jp-pc，并确认【没有】启用 Exit Node。"
