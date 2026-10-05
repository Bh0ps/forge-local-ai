param([Parameter(Mandatory=$true)][string[]]$Files, [Parameter(Mandatory=$true)][string]$SignTool)
$ErrorActionPreference = 'Stop'
if (-not $env:FORGE_SIGNER_THUMBPRINT -or -not $env:FORGE_PUBLISHER_SUBJECT) { throw 'Set signing references privately in this shell. Never commit them.' }
if ($env:FORGE_SIGNER_THUMBPRINT -notmatch '^[0-9a-fA-F]{40}$') { throw 'Invalid certificate reference' }
foreach ($item in $Files) {
    $target = (Resolve-Path -LiteralPath $item).Path
    & $SignTool sign /sha1 $env:FORGE_SIGNER_THUMBPRINT /fd SHA256 /tr https://timestamp.digicert.com /td SHA256 $target
    if ($LASTEXITCODE -ne 0) { throw 'Signing failed; no package may be published' }
    $signature = Get-AuthenticodeSignature -LiteralPath $target
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -ne $env:FORGE_PUBLISHER_SUBJECT -or -not $signature.TimeStamperCertificate) { throw 'Signature verification failed' }
}
Write-Host 'All selected files have valid timestamped signatures from the configured publisher.'
