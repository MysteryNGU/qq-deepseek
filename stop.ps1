# 停止：bot + 看守进程 + NapCat 的 QQ 进程
#
# 为什么要把「两个 PID」都杀掉：
#   .venv\Scripts\python.exe 是个启动器，它会再拉起一个 D:\python3\python.exe 子进程。
#   所以同一个 bot 在进程表里占两个 PID。只杀一个会留下孤儿进程，
#   它还在抢同一个聊天窗口 —— 表现出来就是「每句话回两遍」。
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host '正在停止…' -ForegroundColor Cyan

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*supervise.py*' }

if (-not $procs) {
    Write-Host '  bot 本来就没在跑' -ForegroundColor DarkGray
} else {
    # 先杀父再杀子，避免子进程被重新拉起
    foreach ($p in ($procs | Sort-Object ParentProcessId)) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $which = if ($p.CommandLine -match 'supervise') { '看守' } else { 'bot' }
        Write-Host ("  停掉 {0}: {1}" -f $which, $p.ProcessId) -ForegroundColor Gray
    }
}

Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*supervise.py*' }
if ($left) {
    Write-Host ("  还剩 {0} 个，再清一次" -f ($left | Measure-Object).Count) -ForegroundColor Yellow
    foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

$boot = Get-Process -Name 'NapCatWinBootMain' -ErrorAction SilentlyContinue
foreach ($b in $boot) {
    Stop-Process -Id $b.Id -Force -ErrorAction SilentlyContinue
    Write-Host ("  停掉 NapCat 启动器: " + $b.Id) -ForegroundColor Gray
}

# QQNT 是机器级路径 D:\Program Files\Tencent\QQNT，不能再按 napcat-onekey 匹配
$qqs = Get-Process -Name 'QQ' -ErrorAction SilentlyContinue |
       Where-Object { $_.Path -like '*Tencent\QQNT\QQ.exe' -or $_.Path -like '*napcat-onekey*' }
foreach ($q in $qqs) {
    Stop-Process -Id $q.Id -Force -ErrorAction SilentlyContinue
    Write-Host ("  停掉 NapCat 的 QQ: " + $q.Id) -ForegroundColor Gray
}
if (-not $qqs) { Write-Host '  NapCat 的 QQ 本来就没在跑' -ForegroundColor DarkGray }

Start-Sleep -Seconds 1
$p3001 = (Get-NetTCPConnection -State Listen -LocalPort 3001 -ErrorAction SilentlyContinue |
          Measure-Object).Count
if ($p3001 -gt 0) { Write-Host '  3001 还在（可能刚被杀，等几秒）' -ForegroundColor Yellow }
else { Write-Host '  已全部停止' -ForegroundColor Green }

Read-Host '按回车关闭'
