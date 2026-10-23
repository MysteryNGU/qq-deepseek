# 一键启动：NapCat（QQ）+ adb 连模拟器 + bot
# 双击 一键启动.bat 就行，不用看这个文件。
param([switch]$NoBot)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$adb    = Join-Path $root 'tools\platform-tools\adb.exe'
$py     = Join-Path $root '.venv\Scripts\python.exe'
$napdir = Join-Path $root 'napcat-onekey\NapCat.52230.Shell'

if (-not (Test-Path $py)) { $py = 'python' }

function Port-Up($p) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
    return ($c | Measure-Object).Count -gt 0
}

function Say($t, $color = 'Gray') { Write-Host $t -ForegroundColor $color }

Say '============================================' Cyan
Say '  大肥鱼 一键启动' Cyan
Say '============================================' Cyan
Say ''

# ---------- 1. adb / 模拟器 ----------
Say '[1/3] 手机' Cyan
if (Test-Path $adb) {
    $mumu = Get-Process -Name 'MuMuNxMain' -ErrorAction SilentlyContinue
    if ($mumu) {
        $devs = & $adb devices
        if ($devs -notmatch '\tdevice') {
            & $adb connect 127.0.0.1:7555 | Out-Null
            $devs = & $adb devices
        }
        if ($devs -match '\tdevice') { Say '  模拟器已连上' Green }
        if ($devs -match '\tdevice') {
            $kb = & $adb shell pm list packages com.android.adbkeyboard
            if ($kb -notmatch 'com.android.adbkeyboard') {
                & $adb install -r (Join-Path $root 'tools\ADBKeyboard.apk') | Out-Null
            }
            & $adb shell ime enable com.android.adbkeyboard/.AdbIME | Out-Null
            & $adb shell ime set com.android.adbkeyboard/.AdbIME | Out-Null
        }
        else { Say '  模拟器在，但 adb 没连上（可以不管，聊天不受影响）' Yellow }
    } else {
        Say '  模拟器没开（手机功能会看不到，聊天不受影响）' Yellow
    }
} else {
    Say '  没装 adb（手机功能不可用，聊天不受影响）' Yellow
}

# ---------- 2. NapCat ----------
Say ''
Say '[2/3] QQ 协议端（NapCat）' Cyan
if (Port-Up 3001) {
    Say '  已经在跑，跳过' Green
} else {
    if (-not (Test-Path $napdir)) {
        Say "  找不到 NapCat 目录：$napdir" Red
        Read-Host '按回车退出'
        exit 1
    }
    Say '  正在启动…（会开一个最小化的窗口，那里能看到二维码）'
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList '/c', "cd /d `"$napdir`" && napcat.bat" `
        -WindowStyle Minimized
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if (Port-Up 3001) { $ok = $true; break }
    }
    if ($ok) {
        Say '  已启动（3001 端口通了）' Green
    } else {
        Say '  还没起来：大概率要扫码登录' Yellow
        Say '  去看那个最小化的窗口，或者开 http://127.0.0.1:6099/webui' Yellow
        Read-Host '  扫码完按回车继续（也可以先跳过，起来再继续）'
        for ($i = 0; $i -lt 60; $i++) {
            if (Port-Up 3001) { break }
            Start-Sleep -Seconds 1
        }
        if (Port-Up 3001) { Say '  连上了' Green } else { Say '  还是没通，先启动 bot，它自己会一直重试' Yellow }
    }
}

# ---------- 3. bot ----------
Say ''
Say '[3/3] 聊天机器人' Cyan
Say '  启动 bot（这个窗口别关，关了她就停了）' Gray
Say '  Ctrl+C 停止' Gray
Say ''
$running = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*bot.py*' } | Measure-Object).Count
if ($running -gt 0) {
    Say '  bot 已经在跑了，不重复启动' Green
    Say '  想重启就先双击「停止.bat」' Gray
    Read-Host '按回车关闭'
    exit 0
}

if ($NoBot) { Say '（-NoBot：只起服务，不起 bot）' Yellow; Read-Host '按回车关闭'; exit 0 }
& $py supervise.py

Say ''
Say 'bot 已退出。' Yellow
Read-Host '按回车关闭'
