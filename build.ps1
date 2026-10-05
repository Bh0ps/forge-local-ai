$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& ./.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean --onedir --noupx --windowed --name Sidekick --add-data 'frontend/index.html;frontend' --add-data 'assets;assets' --icon assets/sidekick.ico --collect-all ddgs desktop.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Host 'App created: dist/Sidekick/Sidekick.exe. Keep its _internal folder beside it.'
