param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $Root 'tools\napcat-login.ps1') -Library

function Assert-QrProbe([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Save-QrProbe([string]$Path, [System.Drawing.Color]$Color) {
    $bitmap = New-Object System.Drawing.Bitmap(20, 20)
    try {
        $bitmap.SetPixel(0, 0, $Color)
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $bitmap.Dispose() }
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('qr-login-probe-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
$listener = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
$imageCopy = $null
$state = $null
try {
    $imagePath = Join-Path $temporary 'qrcode.png'
    Save-QrProbe $imagePath ([Drawing.Color]::Red)
    [IO.File]::SetLastWriteTimeUtc($imagePath, [datetime]::UtcNow.AddMinutes(-5))
    Assert-QrProbe (-not (Get-QrLoginRevision $imagePath ([datetime]::UtcNow.AddMinutes(-2)))) 'Old QR was accepted.'
    $imageCopy = Read-QrLoginImage $imagePath
    $exclusive = [IO.File]::Open($imagePath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $exclusive.Dispose()
    [IO.File]::Move($imagePath, ($imagePath + '.moved'))
    [IO.File]::Move(($imagePath + '.moved'), $imagePath)
    Assert-QrProbe ($imageCopy.GetPixel(0, 0).ToArgb() -eq [Drawing.Color]::Red.ToArgb()) 'Image clone was not valid after the source closed.'
    [IO.File]::SetLastWriteTimeUtc($imagePath, [datetime]::UtcNow.AddSeconds(-5))
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    $state = [pscustomobject]@{
        ImagePath = $imagePath; Port = $port; NotBefore = [datetime]::UtcNow.AddMinutes(-2)
        Revision = $null; FailedRevision = $null; Form = $null; Picture = $null; Hidden = $true
        LogPath = (Join-Path $temporary 'watcher.log')
    }
    Update-QrLoginWindow $state
    [Windows.Forms.Application]::DoEvents()
    Assert-QrProbe ($state.Form -and $state.Form.Visible) 'QR window was not opened.'
    $firstForm = $state.Form
    Update-QrLoginWindow $state
    Assert-QrProbe ([object]::ReferenceEquals($firstForm, $state.Form)) 'Duplicate window for the same QR.'
    Save-QrProbe $imagePath ([Drawing.Color]::Green)
    [IO.File]::SetLastWriteTimeUtc($imagePath, [datetime]::UtcNow.AddSeconds(-1))
    Update-QrLoginWindow $state
    Assert-QrProbe ([object]::ReferenceEquals($firstForm, $state.Form)) 'QR refresh replaced the whole window.'
    Assert-QrProbe ($state.Picture.Image.GetPixel(0, 0).ToArgb() -eq [Drawing.Color]::Green.ToArgb()) 'QR did not refresh.'
    # Use a private fixture listener; no real QQ or NapCat API is called.
    $listener = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $port)
    $listener.Start()
    Update-QrLoginWindow $state
    Assert-QrProbe (-not $state.Form -and $firstForm.IsDisposed) 'Successful login did not close the owned window.'
    $listener.Stop()
    Update-QrLoginWindow $state
    Assert-QrProbe (-not $state.Form) 'An old QR reopened after reconnect.'
    Save-QrProbe $imagePath ([Drawing.Color]::Blue)
    Update-QrLoginWindow $state
    Assert-QrProbe ($state.Form -and $state.Form.Visible) 'A new QR did not open after the next disconnect.'
    $state.Form.Close()
    Update-QrLoginWindow $state
    Assert-QrProbe ($state.Form.IsDisposed) 'Manual close immediately reopened the same QR.'
    'QR probe passed: unlocked image, refresh, automatic close, reconnect, manual close.'
} finally {
    if ($state) { Close-QrLoginWindow $state }
    if ($imageCopy) { $imageCopy.Dispose() }
    $listener.Stop()
    # The path is an exact test-owned directory created above, not a computed workspace target.
    [IO.Directory]::Delete($temporary, $true)
}
