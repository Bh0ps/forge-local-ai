param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$target = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) { throw 'Choose a new compiler folder; existing tools will not be replaced.' }
New-Item -ItemType Directory -Path $target | Out-Null
$package = Join-Path $target 'innosetup-7.1.0-x64.exe'
Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe' -OutFile $package
$digest = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
if ($digest -ne '0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f') { throw 'Pinned Inno Setup package hash differs. Nothing was executed.' }
$signature = Get-AuthenticodeSignature -LiteralPath $package
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -ne 'CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL') { throw 'Compiler installer publisher verification failed.' }
$compiler = Join-Path $target 'compiler'
$process = Start-Process -FilePath $package -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$compiler`"") -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $compiler 'ISCC.exe'))) { throw 'Pinned compiler installation failed.' }
Write-Host 'Verified Inno Setup 7.1.0 compiler installed in the selected build folder.'
