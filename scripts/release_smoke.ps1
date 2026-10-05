param([Parameter(Mandatory=$true)][string]$Executable, [Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference = 'Stop'
$target = (Resolve-Path -LiteralPath $Executable).Path
$folder = [System.IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $folder) { throw 'Smoke output must be a new disposable folder.' }
New-Item -ItemType Directory -Path $folder | Out-Null
$report = Join-Path $folder 'native-smoke.json'
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $target
$info.Arguments = '--smoke-test --native-browser-smoke'
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.EnvironmentVariables['FORGE_DATA_DIR'] = Join-Path $folder 'state'
$info.EnvironmentVariables['LOCAL_AI_SMOKE_PATH'] = $report
$process = [System.Diagnostics.Process]::Start($info)
if (-not $process.WaitForExit(120000)) { throw 'Native smoke timed out. Inspect the disposable session; no production process was stopped.' }
if (-not (Test-Path -LiteralPath $report)) { throw 'Native smoke did not produce a report. Check WebView2 and Windows runtime prerequisites.' }
$result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
if ($result.error -or $result.title -ne 'Forge' -or -not $result.close_hides -or -not $result.native_browser.fixture_updated -or $result.native_browser.bridge_exposed -ne 'undefined') { throw 'Native lifecycle/browser smoke failed. See the disposable report.' }
if (-not $result.library.rendered -or $result.library.starter_skills -ne 10 -or $result.library.discover_cards -lt 12) { throw 'Packaged library smoke failed. See the disposable report.' }
Write-Host 'Native workspace, library, HUD, background close and isolated WebView2 fixture passed.'
