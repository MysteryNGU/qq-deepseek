# Outer watchdog: survives loss of every Python process in the Bot runtime.
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = '',
    [int]$RetryBaseSeconds = 2
)
$ErrorActionPreference = 'Continue'
$Root = [IO.Path]::GetFullPath($Root)
if (-not $Python) { $Python = Join-Path $Root '.venv\Scripts\python.exe' }
$logDirectory = Join-Path $Root 'data\logs'
$controlDirectory = Join-Path $Root 'data\life-control'
[IO.Directory]::CreateDirectory($logDirectory) | Out-Null
[IO.Directory]::CreateDirectory($controlDirectory) | Out-Null
$stopRequest = Join-Path $controlDirectory 'stop.requested'
$watchState = Join-Path $controlDirectory 'watchdog.json'
$utf8 = New-Object Text.UTF8Encoding($false)

function Write-RunLog([string]$Name, [string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    try {
        $path = Join-Path $logDirectory $Name
        if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 4MB) {
            Move-Item -LiteralPath $path -Destination ($path + '.1') -Force -ErrorAction Stop
        }
        [IO.File]::AppendAllText($path, $line + [Environment]::NewLine, $utf8)
    } catch { Write-Warning "Log write failed: $($_.Exception.Message)" }
}

$hash = [Security.Cryptography.SHA256]::Create()
try { $key = [BitConverter]::ToString($hash.ComputeHash($utf8.GetBytes($Root.ToLowerInvariant()))).Replace('-', '') }
finally { $hash.Dispose() }
$mutex = New-Object Threading.Mutex($false, ('Local\QQBotWatchdog-' + $key))
$ownsMutex = $false
$oldEncoding = $env:PYTHONIOENCODING
$oldConsoleEncoding = [Console]::OutputEncoding
try {
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { Write-RunLog 'watchdog.log' 'Watchdog already running; no duplicate start.'; return }
    Remove-Item -LiteralPath $stopRequest -Force -ErrorAction SilentlyContinue
    [IO.File]::WriteAllText($watchState, (@{pid=$PID;root=$Root;started=(Get-Date).ToString('o')} | ConvertTo-Json -Compress), $utf8)
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = $utf8
    $failures = 0
    Write-RunLog 'watchdog.log' "Watchdog started pid=$PID root=$Root"
    while (-not (Test-Path -LiteralPath $stopRequest)) {
        $started = Get-Date
        Write-RunLog 'watchdog.log' 'Starting Python supervisor.'
        $global:LASTEXITCODE = 1
        & $Python -u -B (Join-Path $Root 'supervise.py') 2>&1 | ForEach-Object {
            Write-RunLog 'supervisor-console.log' ([string]$_)
        }
        $code = $LASTEXITCODE
        $seconds = ((Get-Date) - $started).TotalSeconds
        Write-RunLog 'watchdog.log' "Python supervisor exited exit_code=$code uptime=$([math]::Round($seconds,1))s"
        if (Test-Path -LiteralPath $stopRequest) {
            Write-RunLog 'watchdog.log' 'Explicit stop requested; recovery disabled.'
            break
        }
        if ($code -eq 0) {
            Write-RunLog 'watchdog.log' 'Supervisor exited normally; recovery not needed.'
            break
        }
        if ($seconds -ge 60) { $failures = 0 }
        $failures++
        $delay = [int][math]::Min(60, [math]::Max(1,$RetryBaseSeconds) * [math]::Pow(2,[math]::Min($failures-1,5)))
        Write-RunLog 'watchdog.log' "Unexpected exit; retrying in ${delay}s (failure $failures)."
        for ($elapsed = 0; $elapsed -lt $delay -and -not (Test-Path -LiteralPath $stopRequest); $elapsed++) {
            Start-Sleep -Seconds 1
        }
    }
} finally {
    $env:PYTHONIOENCODING = $oldEncoding
    [Console]::OutputEncoding = $oldConsoleEncoding
    if ($ownsMutex) {
        Write-RunLog 'watchdog.log' 'Watchdog stopped.'
        Remove-Item -LiteralPath $watchState -Force -ErrorAction SilentlyContinue
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
