param([Parameter(Mandatory=$true)][string]$Executable, [Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference = 'Stop'
function Read-ForgeSmokeReport {
    param([string]$Path)
    $value = [pscustomobject]@{ data=$null; state='missing' }
    $stream = $null
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        $current = $item
        while ($null -ne $current) {
            if (($current.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $value.state='unsafe'; return $value }
            $current = if ($current -is [System.IO.FileInfo]) { $current.Directory } else { $current.Parent }
        }
        if ($item -isnot [System.IO.FileInfo] -or $item.LinkType) { $value.state='unsafe'; return $value }
        $value.state='invalid'
        $stream = [System.IO.File]::Open($Path, 'Open', 'Read', ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        $limit = 1048576
        if ($stream.Length -gt $limit) { $value.state='too-large'; return $value }
        $buffer = New-Object byte[] ($limit + 1)
        $count = 0
        while ($count -le $limit) {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            if (-not $read) { break }
            $count += $read
        }
        if ($count -gt $limit -or $stream.Length -gt $limit) { $value.state='too-large'; return $value }
        $item.Refresh()
        if (-not $item.Exists -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $value.state='unsafe'; return $value }
        $text = (New-Object System.Text.UTF8Encoding($false, $true)).GetString($buffer, 0, $count).TrimStart([char]0xFEFF).Trim()
        if (-not $text.StartsWith('{') -or -not $text.EndsWith('}')) { return $value }
        $data = $text | ConvertFrom-Json -ErrorAction Stop
        if ($data -isnot [pscustomobject]) { return $value }
        $value.data=$data; $value.state='ready'
    } catch {
        # Raw file content, paths and parsing exceptions never leave this reader.
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    return $value
}
function Write-ForgeSmokeDiagnostics {
    param([object]$SmokeData, [string]$Gate, [object]$ExitCode,
          [string]$ReportState='unrecorded', [object]$CheckpointData=$null, [string]$CheckpointState='unrecorded')
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
        'Browser action cancelled.' = 'browser-cancelled'
        'Open Forge Browser or use browser_navigate before inspecting this page.' = 'browser-not-open'
        'Browser page is still loading. Inspect again when navigation completes.' = 'browser-loading'
        'Browser page could not be inspected.' = 'browser-inspection'
        'Browser navigated while inspecting. Inspect again.' = 'browser-navigation-changed'
        'Start a ready preview before inspecting it.' = 'preview-not-ready'
        'Preview inspection requires Forge desktop.' = 'preview-desktop-missing'
        'This Builder or preview belongs to another project.' = 'preview-project-scope'
        'The browser panel requires the local Forge workspace.' = 'panel-origin'
        'Forge panel layout did not respond. Open it again.' = 'panel-layout-timeout'
        'Forge panel layout could not be inspected.' = 'panel-layout-inspection'
        'Browser layout changed. Refresh its panel bounds.' = 'panel-layout-changed'
        'Browser panel changed. Inspect again after reopening it.' = 'panel-layout-changed'
        'Browser view has been disposed.' = 'browser-disposed'
        'Native input is paused while Forge confirms a restart.' = 'native-admission'
        'The built-in browser requires Forge desktop on Windows.' = 'browser-unavailable'
        'Unknown native browser panel.' = 'panel-identity'
        'Unknown embedded browser profile.' = 'panel-profile'
        'Browser panel bounds must be finite numbers.' = 'panel-bounds'
        'Native browser bounds exceed their owner.' = 'panel-owner'
        'Integration timed out. Inspect its outcome before retrying a side effect.' = 'integration-timeout'
        'Optional integration dependency is unavailable; install requirements-integrations.txt' = 'integration-dependency'
        'Integration failed (RuntimeError). Check the endpoint, authentication and server availability.' = 'integration-runtime'
        'Integration failed (AttributeError). Check the endpoint, authentication and server availability.' = 'integration-attribute'
        'Integration failed (TypeError). Check the endpoint, authentication and server availability.' = 'integration-type'
        'Integration failed (OSError). Check the endpoint, authentication and server availability.' = 'integration-os'
    }
    $phases = @('workspace-ready','workspace-size','library','host-lifecycle','research-start','research-navigate',
        'research-load','research-input-target','research-type','research-button-target','research-click','research-verify',
        'research-screenshot','builder-start','builder-navigation','builder-input-target','builder-button-target',
        'builder:preview_inspect','builder:preview_type','builder:preview_click','builder:preview_viewport',
        'builder:preview_diagnostics','builder:preview_screenshot','standalone-start','standalone-open',
        'standalone-input-target','standalone-button-target','standalone-screenshot')
    $checkpointPhases = $phases + @('startup-imports','startup-imports-ready','logging-start','logging-ready',
        'singleton-start','singleton-ready','service-start','service-ready','http-start','http-ready',
        'background-start','background-ready','host-hooks-start','host-hooks-ready','window-create',
        'window-created','webview-start','startup-failed','smoke-failed','smoke-complete')
    $types = @('ValueError','RuntimeError','TimeoutError','StopIteration','KeyError','TypeError','AttributeError',
        'OSError','FileNotFoundError','PermissionError','BrowserGuard','JavascriptException','WebViewException',
        'ImportError','ModuleNotFoundError')
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
    $checkpointPhase='unrecorded'; $checkpointErrorType='unclassified'; $checkpointErrorCategory='none'
    if ($checkpointPhases -contains $CheckpointData.phase) { $checkpointPhase=$CheckpointData.phase }
    if ($types -contains $CheckpointData.error_type) { $checkpointErrorType=$CheckpointData.error_type }
    if (@('startup-failure','smoke-failure') -contains $CheckpointData.error_category) { $checkpointErrorCategory=$CheckpointData.error_category }
    $states=@('missing','invalid','unsafe','too-large','ready','unrecorded')
    if ($states -notcontains $ReportState) { $ReportState='unrecorded' }
    if ($states -notcontains $CheckpointState) { $CheckpointState='unrecorded' }
    $gates=@('process-timeout','report-missing','report-invalid','lifecycle-browser','library','builder-preview','standalone-preview')
    if ($gates -notcontains $Gate) { $Gate='unclassified' }
    if ($ExitCode -isnot [int] -and $ExitCode -isnot [long]) { $ExitCode=$null }
    $toolFailure='unrecorded'
    if (@('not-executed','outcome-unknown','refused') -contains $SmokeData.tool_failure) { $toolFailure=$SmokeData.tool_failure }
    $researchViewCreated=$null
    if ($SmokeData.research_view_created -is [bool]) { $researchViewCreated=$SmokeData.research_view_created }
    $summary = [ordered]@{ gate=$Gate; phase=$phase; process_exit=$ExitCode; error_present=$hasError;
        error_category=$category; error_type=$errorType; safe_error=$message; report_state=$ReportState;
        tool_failure=$toolFailure; research_view_created=$researchViewCreated;
        checkpoint_state=$CheckpointState; checkpoint_phase=$checkpointPhase;
        checkpoint_error_type=$checkpointErrorType; checkpoint_error_category=$checkpointErrorCategory; checks=[ordered]@{
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
            preview_state=(Check-Status $SmokeData.smoke_preview_state.available ($SmokeData.smoke_preview_state.available -eq $true))
            preview_record_ready=(Check-Status $SmokeData.smoke_preview_state.record_ready ($SmokeData.smoke_preview_state.record_ready -eq $true))
            preview_running=(Check-Status $SmokeData.smoke_preview_state.running ($SmokeData.smoke_preview_state.running -eq $true))
            preview_load_complete=(Check-Status $SmokeData.smoke_preview_state.loading ($SmokeData.smoke_preview_state.loading -eq $false))
            preview_visible=(Check-Status $SmokeData.smoke_preview_state.visible ($SmokeData.smoke_preview_state.visible -eq $true))
            preview_view=(Check-Status $SmokeData.smoke_preview_state.view_present ($SmokeData.smoke_preview_state.view_present -eq $true))
            preview_builder_page=(Check-Status $SmokeData.smoke_preview_state.builder ($SmokeData.smoke_preview_state.builder -eq $true))
            preview_pane=(Check-Status $SmokeData.smoke_preview_state.pane ($SmokeData.smoke_preview_state.pane -eq $true))
            preview_pane_visible=(Check-Status $SmokeData.smoke_preview_state.pane_visible ($SmokeData.smoke_preview_state.pane_visible -eq $true))
            preview_modal_closed=(Check-Status $SmokeData.smoke_preview_state.modal ($SmokeData.smoke_preview_state.modal -eq $false))
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
    $safe = $summary | ConvertTo-Json -Compress -Depth 4
    if ($script:ForgeSmokeDiagnosticPath) {
        try { [System.IO.File]::WriteAllText($script:ForgeSmokeDiagnosticPath, $safe, (New-Object System.Text.UTF8Encoding($false))) }
        catch { } # Diagnostic persistence must not replace the native gate failure.
    }
    Write-Host ('Forge native smoke diagnostics: ' + $safe)
}
$target = (Resolve-Path -LiteralPath $Executable).Path
$folder = [System.IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $folder) { throw 'Smoke output must be a new disposable folder.' }
New-Item -ItemType Directory -Path $folder | Out-Null
$report = Join-Path $folder 'native-smoke.json'
$checkpoint = Join-Path $folder 'native-smoke-checkpoint.json'
$script:ForgeSmokeDiagnosticPath = Join-Path $folder 'native-smoke-diagnostics.json'
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $target
$info.Arguments = '--smoke-test --native-browser-smoke'
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.EnvironmentVariables['FORGE_DATA_DIR'] = Join-Path $folder 'state'
$info.EnvironmentVariables['LOCAL_AI_SMOKE_PATH'] = $report
$process = [System.Diagnostics.Process]::Start($info)
if (-not $process.WaitForExit(120000)) {
    $partial = Read-ForgeSmokeReport $report
    $progress = Read-ForgeSmokeReport $checkpoint
    Write-ForgeSmokeDiagnostics $partial.data 'process-timeout' $null $partial.state $progress.data $progress.state
    throw 'Native smoke timed out. Inspect the disposable session; no production process was stopped.'
}
$partial = Read-ForgeSmokeReport $report
$progress = Read-ForgeSmokeReport $checkpoint
if ($partial.state -eq 'missing') { Write-ForgeSmokeDiagnostics $null 'report-missing' $process.ExitCode $partial.state $progress.data $progress.state; throw 'Native smoke did not produce a report. Check WebView2 and Windows runtime prerequisites.' }
if ($partial.state -ne 'ready') { Write-ForgeSmokeDiagnostics $null 'report-invalid' $process.ExitCode $partial.state $progress.data $progress.state; throw 'Native smoke report is invalid.' }
$result = $partial.data
if ($result.error -or $result.title -ne 'Forge' -or -not $result.close_hides -or -not $result.native_browser.fixture_updated -or $result.native_browser.bridge_exposed -ne 'undefined') { Write-ForgeSmokeDiagnostics $result 'lifecycle-browser' $process.ExitCode $partial.state $progress.data $progress.state; throw 'Native lifecycle/browser smoke failed. See the disposable report.' }
if (-not $result.library.rendered -or $result.library.starter_skills -ne 24 -or $result.library.discover_cards -lt 24) { Write-ForgeSmokeDiagnostics $result 'library' $process.ExitCode $partial.state $progress.data $progress.state; throw 'Packaged library smoke failed. See the disposable report.' }
if (-not $result.native_preview.fixture_updated -or -not $result.native_preview.separate_controller -or -not $result.native_preview.separate_profile -or $result.native_preview.bridge_exposed -ne 'undefined' -or -not $result.native_preview.snapshot_guarded -or -not $result.native_preview.viewport_verified -or -not $result.native_preview.diagnostics_captured -or $result.native_preview.screenshot_bytes -le 1000 -or -not $result.native_preview.stopped) { Write-ForgeSmokeDiagnostics $result 'builder-preview' $process.ExitCode $partial.state $progress.data $progress.state; throw 'Isolated native Builder preview smoke failed. See the disposable report.' }
if (-not $result.native_standalone_preview.pane_visible -or -not $result.native_standalone_preview.fixture_updated -or -not $result.native_standalone_preview.without_builder -or $result.native_standalone_preview.bridge_exposed -ne 'undefined' -or $result.native_standalone_preview.screenshot_bytes -le 1000) { Write-ForgeSmokeDiagnostics $result 'standalone-preview' $process.ExitCode $partial.state $progress.data $progress.state; throw 'Native standalone plan preview smoke failed. See the disposable report.' }
Write-Host 'Native workspace, library, HUD, background close, research browser and isolated Builder preview fixtures passed.'
