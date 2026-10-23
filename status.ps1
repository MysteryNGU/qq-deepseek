# 状态：端口、设备、登录的号
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$adb = Join-Path $root 'tools\platform-tools\adb.exe'
$py  = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

function Port-Up($p) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
    return ($c | Measure-Object).Count -gt 0
}

Write-Host '===== 状态 =====' -ForegroundColor Cyan

if (Port-Up 3001) { Write-Host '  QQ 协议端(3001): 在线' -ForegroundColor Green }
else { Write-Host '  QQ 协议端(3001): 离线（要扫码？）' -ForegroundColor Red }

if (Port-Up 6099) { Write-Host '  NapCat WebUI(6099): 可用 → http://127.0.0.1:6099/webui' -ForegroundColor Green }
else { Write-Host '  NapCat WebUI(6099): 不可用' -ForegroundColor DarkGray }

$bots = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*bot.py*' } | Measure-Object).Count
if ($bots -gt 0) { Write-Host ('  bot 进程: 在跑 (' + $bots + ')') -ForegroundColor Green }
else { Write-Host '  bot 进程: 没在跑' -ForegroundColor Red }

if (Test-Path $adb) {
    $devs = (& $adb devices) -join ' '
    if ($devs -match '\tdevice') { Write-Host '  手机(adb): 已连接' -ForegroundColor Green }
    else { Write-Host '  手机(adb): 未连接' -ForegroundColor Yellow }
} else {
    Write-Host '  手机(adb): 没装' -ForegroundColor DarkGray
}

Write-Host ''
Write-Host '  最近日志:' -ForegroundColor Gray
foreach ($name in @('conversation.log','runtime.log')) {
    $log = Join-Path $root ('data\' + $name)
    Write-Host ('  ' + $name + ':') -ForegroundColor Gray
    if (Test-Path $log) { Get-Content $log -Tail 5 -Encoding UTF8 }
    else { Write-Host '    （还没有日志）' -ForegroundColor DarkGray }
}

Read-Host '按回车关闭'
