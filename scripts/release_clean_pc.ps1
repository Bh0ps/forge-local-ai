param([Parameter(Mandatory=$true)][string]$Installer, [Parameter(Mandatory=$true)][string]$Sha256,
      [Parameter(Mandatory=$true)][string]$Output, [switch]$AllowUnsignedPreview)
$ErrorActionPreference = 'Stop'
$package = (Resolve-Path -LiteralPath $Installer).Path
if ($Sha256 -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash -ne $Sha256) { throw 'Installer checksum differs; nothing was installed.' }
$signature = Get-AuthenticodeSignature -LiteralPath $package
if ($signature.Status -ne 'Valid' -and -not $AllowUnsignedPreview) { throw 'A trusted signed installer is required, or explicitly select the unsigned development preview on a disposable VM.' }
$installation = Join-Path $env:LOCALAPPDATA 'Programs/Forge4'
if (Test-Path -LiteralPath $installation) { throw 'This clean-PC test requires a disposable account with no Forge installation.' }
if (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.forge')) { throw 'This test requires a disposable account with no Forge data.' }
$process = Start-Process -FilePath $package -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/TASKS=desktopicon') -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -notin @(0,3010)) { throw 'Clean installation failed.' }
& "$PSScriptRoot/release_smoke.ps1" -Executable (Join-Path $installation 'Forge.exe') -Output $Output
Write-Host 'Disposable clean-account install and native fixture passed. Check DPI, permissions, microphone and uninstall manually before recording clean-PC acceptance.'
