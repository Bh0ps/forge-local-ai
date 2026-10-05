param([string]$Python = 'python', [string]$Iscc = 'iscc', [switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $SkipInstall) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed' }
    $Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    & $Python -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
}
Push-Location frontend
try {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
} finally { Pop-Location }
& $Python scripts/generate_assets.py
& $Python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Validation failed; no release was produced' }
& $Python -m PyInstaller --noconfirm --clean --onedir --noupx --windowed --name Forge --add-data 'frontend/dist;frontend/dist' --add-data 'assets;assets' --add-data 'browser-extension;browser-extension' --add-data 'LICENSE;.' --add-data 'THIRD_PARTY_NOTICES.md;.' --icon assets/forge.ico --collect-all ddgs --collect-all tzdata --collect-all faster_whisper --collect-all av --collect-all huggingface_hub --collect-all hf_xet --collect-all gguf --copy-metadata huggingface-hub --collect-all sounddevice --collect-all pystray --collect-all webview --collect-submodules mcp.client --collect-submodules mcp.shared --copy-metadata mcp --collect-all playwright --hidden-import webview.platforms.edgechromium --hidden-import pywinauto --hidden-import forge_service desktop.py
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed' }
& $Python -m PyInstaller --noconfirm --clean --onefile --noupx --console --name ForgeBrowserHost --exclude-module playwright browser_tools.py
if ($LASTEXITCODE -ne 0) { throw 'Browser native host build failed' }
Copy-Item -LiteralPath 'dist\ForgeBrowserHost.exe' -Destination 'dist\Forge\ForgeBrowserHost.exe'
Copy-Item -LiteralPath 'LICENSE','THIRD_PARTY_NOTICES.md' -Destination 'dist\Forge'
& $Python scripts/collect_licenses.py --output dist/Forge/third-party-licenses
if ($LASTEXITCODE -ne 0) { throw 'Dependency license collection failed' }
& $Python scripts/prune_license_cache.py --app dist/Forge
if ($LASTEXITCODE -ne 0) { throw 'Dependency notice sanitization failed' }
New-Item -ItemType Directory -Force -Path release | Out-Null
Compress-Archive -Path 'dist\Forge\*' -DestinationPath 'release\Forge-4.2.2-Portable.zip' -Force
if (Get-Command $Iscc -ErrorAction SilentlyContinue) {
    & $Iscc installer.iss
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
} else { Write-Host 'Portable app built. Install Inno Setup 7 and pass -Iscc to also build the installer.' }
Get-ChildItem release -File | Where-Object Extension -In '.exe','.zip' | Get-FileHash -Algorithm SHA256 | ForEach-Object { "$($_.Hash.ToLower())  $([IO.Path]::GetFileName($_.Path))" } | Set-Content -Encoding utf8 release/SHA256SUMS.txt
Write-Host 'Forge packages created in release/.'
