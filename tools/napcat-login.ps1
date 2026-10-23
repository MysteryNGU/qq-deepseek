# Own the QR window, without holding NapCat's PNG open or controlling other viewers.
param(
    [string]$NapCatDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'napcat-onekey\NapCat.52230.Shell'),
    [int]$ReadyPort = 3001,
    [int]$OwnerProcessId = 0,
    [switch]$Library
)
$ErrorActionPreference = 'Stop'

function Test-QrLoginReady([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.ConnectAsync('127.0.0.1', $Port)
        return ($pending.Wait(150) -and $client.Connected)
    } catch { return $false }
    finally { $client.Dispose() }
}

function Get-QrLoginRevision([string]$Path, [datetime]$NotBefore) {
    $file = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($file -and $file.Length -gt 0 -and $file.LastWriteTimeUtc -ge $NotBefore) {
        return ('{0}:{1}' -f $file.LastWriteTimeUtc.Ticks, $file.Length)
    }
    return $null
}

function Read-QrLoginImage([string]$Path) {
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
    $original = $null
    try {
        $original = [System.Drawing.Image]::FromStream($stream)
        return [System.Drawing.Bitmap]::new($original)
    } finally {
        if ($original) { $original.Dispose() }
        $stream.Dispose()
    }
}

function Write-QrLoginLog($State, [string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f [datetime]::Now, $Message
    Add-Content -LiteralPath $State.LogPath -Value $line -Encoding UTF8
}

function Close-QrLoginWindow($State) {
    if ($State.Form) {
        $form = $State.Form
        $State.Form = $null
        $State.Picture = $null
        $image = $form.Tag
        $form.Tag = $null
        $form.Close()
        $form.Dispose()
        if ($image) { $image.Dispose() }
        Write-QrLoginLog $State 'QR window closed; login service is ready or watcher is stopping.'
    }
}

function Update-QrLoginWindow($State) {
    if (Test-QrLoginReady $State.Port) {
        Close-QrLoginWindow $State
        $State.NotBefore = [datetime]::UtcNow
        return
    }
    $revision = Get-QrLoginRevision $State.ImagePath $State.NotBefore
    if (-not $revision -or $revision -eq $State.Revision) { return }
    # NapCat may still be writing the PNG; retry it on the next timer tick.
    try { $image = Read-QrLoginImage $State.ImagePath }
    catch {
        if ($State.FailedRevision -ne $revision) {
            Write-QrLoginLog $State ($_.Exception.ToString() + "`n" + $_.ScriptStackTrace)
            $State.FailedRevision = $revision
        }
        return
    }
    if (-not $State.Form -or $State.Form.IsDisposed) {
        $form = New-Object System.Windows.Forms.Form
        $form.Text = 'QQ 登录：请扫码并在手机上确认'
        $form.ClientSize = New-Object System.Drawing.Size(480, 540)
        $form.MinimumSize = New-Object System.Drawing.Size(360, 420)
        $form.StartPosition = 'CenterScreen'
        $form.Add_FormClosed({
            param($closedForm, $closedEvent)
            if ($closedForm.Tag) {
                $heldImage = $closedForm.Tag
                $closedForm.Tag = $null
                $heldImage.Dispose()
            }
        })
        $label = New-Object System.Windows.Forms.Label
        $label.Text = "请使用 QQ 扫描下方二维码，并在手机上确认登录。`n登录完成后本窗口自动关闭；二维码更新时会自动刷新。"
        $label.Dock = 'Top'
        $label.Height = 60
        $label.TextAlign = 'MiddleCenter'
        $picture = New-Object System.Windows.Forms.PictureBox
        $picture.Dock = 'Fill'
        $picture.SizeMode = 'Zoom'
        $picture.BackColor = [System.Drawing.Color]::White
        $form.Controls.Add($picture)
        $form.Controls.Add($label)
        if ($State.Hidden) { $form.Opacity = 0; $form.ShowInTaskbar = $false }
        $State.Form = $form
        $State.Picture = $picture
        $form.Tag = $image
        $picture.Image = $image
        $form.Show()
        if (-not $State.Hidden) { $form.Activate() }
        Write-QrLoginLog $State 'QR window opened; waiting for phone confirmation.'
    } else {
        $previous = $State.Form.Tag
        $State.Form.Tag = $image
        $State.Picture.Image = $image
        if ($previous) { $previous.Dispose() }
        Write-QrLoginLog $State 'QR image refreshed.'
    }
    $State.Revision = $revision
    $State.FailedRevision = $null
}

function Invoke-QrLoginWatcher([string]$Directory, [int]$Port, [int]$Owner) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $directoryPath = [System.IO.Path]::GetFullPath($Directory).TrimEnd('\').ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $identity = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($directoryPath))).Replace('-', '') }
    finally { $sha.Dispose() }
    $mutex = New-Object System.Threading.Mutex($false, ('Local\qq-deepseek-qr-' + $identity))
    $owned = $false
    $timer = $null
    $context = $null
    $script:qrLoginState = $null
    try {
        try { $owned = $mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { return }
        $logDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'data\logs'
        [System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
        $ownerStarted = if ($Owner) { (Get-Process -Id $Owner -ErrorAction Stop).StartTime } else { $null }
        $script:qrLoginState = [pscustomobject]@{
            ImagePath = (Join-Path $directoryPath 'cache\qrcode.png'); Port = $Port
            NotBefore = [datetime]::UtcNow.AddSeconds(-120); Revision = $null; FailedRevision = $null
            Form = $null; Picture = $null; Hidden = $false
            LogPath = (Join-Path $logDirectory 'napcat-login.log')
        }
        $context = New-Object System.Windows.Forms.ApplicationContext
        $script:qrLoginContext = $context
        $script:qrLoginOwner = $Owner
        $script:qrLoginOwnerStarted = $ownerStarted
        $timer = New-Object System.Windows.Forms.Timer
        $timer.Interval = 3000
        $timer.Add_Tick({
            try {
                if ($script:qrLoginOwner) {
                    $ownerProcess = Get-Process -Id $script:qrLoginOwner -ErrorAction SilentlyContinue
                    if (-not $ownerProcess -or $ownerProcess.StartTime -ne $script:qrLoginOwnerStarted) {
                        $script:qrLoginContext.ExitThread()
                        return
                    }
                }
                Update-QrLoginWindow $script:qrLoginState
            } catch {
                Write-QrLoginLog $script:qrLoginState ($_.Exception.ToString() + "`n" + $_.ScriptStackTrace)
            }
        })
        $timer.Start()
        Update-QrLoginWindow $script:qrLoginState
        [System.Windows.Forms.Application]::Run($context)
    } finally {
        if ($timer) { $timer.Stop(); $timer.Dispose() }
        if ($script:qrLoginState) { Close-QrLoginWindow $script:qrLoginState }
        if ($context) { $context.Dispose() }
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if (-not $Library) { Invoke-QrLoginWatcher $NapCatDirectory $ReadyPort $OwnerProcessId }
