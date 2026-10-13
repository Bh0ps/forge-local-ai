param([Parameter(Mandatory=$true)][string]$Executable, [Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference = 'Stop'
function Write-ForgeSmokeDiagnostics {
    param([object]$SmokeData, [string]$Gate, [object]$ExitCode)
    function Check-Status($Value, [bool]$Passed) {
        if ($null -eq $Value) { return 'missing' }
        if ($Passed) { return 'pass' }
        return 'fail'
    }
    # Only fixed labels, check statuses and exact known error messages leave the report.
    $knownErrors = @{
        'React workspace did not render its composer.' = 'workspace-render'
        'Forge did not launch with its workspace maximized.' = 'window-state'
        'The packaged starter library was not prepared correctly.' = 'library-initialization'
        'The Library navigation is missing.' = 'library-navigation'
        'The packaged Discover library did not render.' = 'library-render'
        'Expand did not restore the maximized workspace.' = 'window-restore'
        'Native form edit/click did not update the fixture.' = 'research-interaction'
        'Builder navigation is missing.' = 'builder-navigation'
        'Preview accepted another run snapshot.' = 'preview-isolation'
        'Preview click did not update the fixture.' = 'preview-interaction'
        'Preview profile, bridge isolation or diagnostics failed.' = 'preview-isolation'
        'The standalone preview pane did not open.' = 'standalone-attachment'
        'Standalone preview controls did not update the fixture.' = 'standalone-interaction'
        'Windows desktop session is unavailable. Unlock Windows before continuing browser work.' = 'desktop-session'
        'Windows WebView2 could not initialize.' = 'webview2-initialization'
        'Windows WebView2 initialization timed out.' = 'webview2-timeout'
        'Preview diagnostics initialization timed out.' = 'preview-diagnostics-timeout'
        'Browser script timed out; inspect the action outcome before retrying.' = 'browser-script-timeout'
        'Browser screenshot timed out.' = 'browser-screenshot-timeout'
        'Window update timed out.' = 'window-update-timeout'
        'Window is unavailable.' = 'window-unavailable'
        'Open the Browser panel in the full Forge workspace.' = 'panel-unavailable'
        'Browser panel falls outside the workspace viewport.' = 'panel-viewport'
        'Native workspace viewport is unavailable.' = 'native-viewport'
        'DPI or browser zoom changed. Refresh the Browser panel.' = 'panel-dpi'
    }
    $phases = @('workspace-ready','workspace-size','library','host-lifecycle','research-start','research-navigate',
        'research-load','research-input-target','research-type','research-button-target','research-click','research-verify',
        'research-screenshot','builder-start','builder-navigation','builder-input-target','builder-button-target',
        'builder:preview_inspect','builder:preview_type','builder:preview_click','builder:preview_viewport',
        'builder:preview_diagnostics','builder:preview_screenshot','standalone-start','standalone-open',
        'standalone-input-target','standalone-button-target','standalone-screenshot')
    $types = @('ValueError','RuntimeError','TimeoutError','StopIteration','KeyError','TypeError','AttributeError',
        'OSError','FileNotFoundError','PermissionError','BrowserGuard','JavascriptException','WebViewException')
    $hasError = $null -ne $SmokeData -and $null -ne $SmokeData.PSObject.Properties['error']
    $category = 'none'; $message = $null
    if ($hasError) {
        $category = 'unclassified'
        $errorText = [string]$SmokeData.error
        if ([string]::IsNullOrEmpty($errorText)) { $category = 'empty-error' }
        elseif ($knownErrors.ContainsKey($errorText)) { $category = $knownErrors[$errorText]; $message = $errorText }
    }
    $phase = 'unrecorded'; $errorType = 'unclassified'
    if ($phases -contains $SmokeData.smoke_phase) { $phase = $SmokeData.smoke_phase }
    if ($types -contains $SmokeData.error_type) { $errorType = $SmokeData.error_type }
    $summary = [ordered]@{ gate=$Gate; phase=$phase; process_exit=$ExitCode; error_present=$hasError;
        error_category=$category; error_type=$errorType; safe_error=$message; checks=[ordered]@{
            ready=(Check-Status $SmokeData.ready ($SmokeData.ready -eq 'complete'))
            title=(Check-Status $SmokeData.title ($SmokeData.title -eq 'Forge'))
            composer=(Check-Status $SmokeData.ui.composer ($SmokeData.ui.composer -eq $true))
            textarea=(Check-Status $SmokeData.ui.textarea ($SmokeData.ui.textarea -eq $true))
            maximized=(Check-Status $SmokeData.launch_maximized ($SmokeData.launch_maximized -eq $true))
            library=(Check-Status $SmokeData.library.rendered ($SmokeData.library.rendered -eq $true))
            starter_skills=(Check-Status $SmokeData.library.starter_skills ($SmokeData.library.starter_skills -eq 24))
            discover_cards=(Check-Status $SmokeData.library.discover_cards ($SmokeData.library.discover_cards -ge 24))
            restored=(Check-Status $SmokeData.full_restores_maximized ($SmokeData.full_restores_maximized -eq $true))
            close_hides=(Check-Status $SmokeData.close_hides ($SmokeData.close_hides -eq $true))
            research=(Check-Status $SmokeData.native_browser.fixture_updated ($SmokeData.native_browser.fixture_updated -eq $true))
            research_bridge=(Check-Status $SmokeData.native_browser.bridge_exposed ($SmokeData.native_browser.bridge_exposed -eq 'undefined'))
            preview=(Check-Status $SmokeData.native_preview.fixture_updated ($SmokeData.native_preview.fixture_updated -eq $true))
            preview_controller=(Check-Status $SmokeData.native_preview.separate_controller ($SmokeData.native_preview.separate_controller -eq $true))
            preview_profile=(Check-Status $SmokeData.native_preview.separate_profile ($SmokeData.native_preview.separate_profile -eq $true))
            preview_bridge=(Check-Status $SmokeData.native_preview.bridge_exposed ($SmokeData.native_preview.bridge_exposed -eq 'undefined'))
            preview_snapshot=(Check-Status $SmokeData.native_preview.snapshot_guarded ($SmokeData.native_preview.snapshot_guarded -eq $true))
            preview_viewport=(Check-Status $SmokeData.native_preview.viewport_verified ($SmokeData.native_preview.viewport_verified -eq $true))
            preview_diagnostics=(Check-Status $SmokeData.native_preview.diagnostics_captured ($SmokeData.native_preview.diagnostics_captured -eq $true))
            preview_screenshot=(Check-Status $SmokeData.native_preview.screenshot_bytes ($SmokeData.native_preview.screenshot_bytes -gt 1000))
            preview_stopped=(Check-Status $SmokeData.native_preview.stopped ($SmokeData.native_preview.stopped -eq $true))
            standalone_pane=(Check-Status $SmokeData.native_standalone_preview.pane_visible ($SmokeData.native_standalone_preview.pane_visible -eq $true))
            standalone=(Check-Status $SmokeData.native_standalone_preview.fixture_updated ($SmokeData.native_standalone_preview.fixture_updated -eq $true))
            standalone_plain=(Check-Status $SmokeData.native_standalone_preview.without_builder ($SmokeData.native_standalone_preview.without_builder -eq $true))
            standalone_bridge=(Check-Status $SmokeData.native_standalone_preview.bridge_exposed ($SmokeData.native_standalone_preview.bridge_exposed -eq 'undefined'))
            standalone_screenshot=(Check-Status $SmokeData.native_standalone_preview.screenshot_bytes ($SmokeData.native_standalone_preview.screenshot_bytes -gt 1000))
        } }
    Write-Host ('Forge native smoke diagnostics: ' + ($summary | ConvertTo-Json -Compress -Depth 4))
}
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
if (-not $process.WaitForExit(120000)) { Write-ForgeSmokeDiagnostics $null 'process-timeout' $null; throw 'Native smoke timed out. Inspect the disposable session; no production process was stopped.' }
if (-not (Test-Path -LiteralPath $report)) { Write-ForgeSmokeDiagnostics $null 'report-missing' $process.ExitCode; throw 'Native smoke did not produce a report. Check WebView2 and Windows runtime prerequisites.' }
try { $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json }
catch { Write-ForgeSmokeDiagnostics $null 'report-invalid' $process.ExitCode; throw 'Native smoke report is invalid.' }
if ($result.error -or $result.title -ne 'Forge' -or -not $result.close_hides -or -not $result.native_browser.fixture_updated -or $result.native_browser.bridge_exposed -ne 'undefined') { Write-ForgeSmokeDiagnostics $result 'lifecycle-browser' $process.ExitCode; throw 'Native lifecycle/browser smoke failed. See the disposable report.' }
if (-not $result.library.rendered -or $result.library.starter_skills -ne 24 -or $result.library.discover_cards -lt 24) { Write-ForgeSmokeDiagnostics $result 'library' $process.ExitCode; throw 'Packaged library smoke failed. See the disposable report.' }
if (-not $result.native_preview.fixture_updated -or -not $result.native_preview.separate_controller -or -not $result.native_preview.separate_profile -or $result.native_preview.bridge_exposed -ne 'undefined' -or -not $result.native_preview.snapshot_guarded -or -not $result.native_preview.viewport_verified -or -not $result.native_preview.diagnostics_captured -or $result.native_preview.screenshot_bytes -le 1000 -or -not $result.native_preview.stopped) { Write-ForgeSmokeDiagnostics $result 'builder-preview' $process.ExitCode; throw 'Isolated native Builder preview smoke failed. See the disposable report.' }
if (-not $result.native_standalone_preview.pane_visible -or -not $result.native_standalone_preview.fixture_updated -or -not $result.native_standalone_preview.without_builder -or $result.native_standalone_preview.bridge_exposed -ne 'undefined' -or $result.native_standalone_preview.screenshot_bytes -le 1000) { Write-ForgeSmokeDiagnostics $result 'standalone-preview' $process.ExitCode; throw 'Native standalone plan preview smoke failed. See the disposable report.' }
Write-Host 'Native workspace, library, HUD, background close, research browser and isolated Builder preview fixtures passed.'
