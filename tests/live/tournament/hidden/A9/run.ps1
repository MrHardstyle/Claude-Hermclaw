# Hidden checks of task A9 (Get-ServiceReport.ps1). Prints TAP like `node --test` so the grader can count it:
#   pwsh -NoProfile -File _hidden/run.ps1
$ErrorActionPreference = 'Continue'
$root = (Get-Location).Path
$here = $PSScriptRoot
$script = Join-Path $root 'Get-ServiceReport.ps1'
$results = [System.Collections.Generic.List[object]]::new()

function Invoke-Report([string]$list, [string]$arguments) {
    $env:FAKE_SERVICES = Join-Path $here $list
    $cmd = ". '$here/fake.ps1'; & '$script' $arguments"
    $errFile = [System.IO.Path]::GetTempFileName()
    $out = & pwsh -NoProfile -NonInteractive -Command $cmd 2> $errFile
    $code = $LASTEXITCODE
    [pscustomobject]@{ Out = (($out | Out-String).Trim()); Code = $code; Err = (Get-Content -Raw $errFile) }
    Remove-Item $errFile -ErrorAction SilentlyContinue
}

function Read-Array([string]$text) {
    if (-not $text.StartsWith('[')) { throw "output is not a JSON array: $($text.Substring(0, [Math]::Min(200, $text.Length)))" }
    $parsed = $text | ConvertFrom-Json -NoEnumerate
    , [object[]]$parsed
}

function Check([string]$name, [scriptblock]$body) {
    try {
        & $body
        $results.Add([pscustomobject]@{ Name = $name; Ok = $true; Why = '' })
    } catch {
        $results.Add([pscustomobject]@{ Name = $name; Ok = $false; Why = "$_" })
    }
}

function Assert([bool]$cond, [string]$why) { if (-not $cond) { throw $why } }

Check 'the script exists' { Assert (Test-Path $script) 'Get-ServiceReport.ps1 missing in the repository root' }

Check 'all services as a JSON array with the four fields' {
    $r = Invoke-Report 'services.json' ''
    Assert ($r.Code -eq 0) "exit code $($r.Code): $($r.Err)"
    $rows = Read-Array $r.Out
    Assert ($rows.Count -eq 3) "expected 3 services, got $($rows.Count)"
    foreach ($field in 'Name', 'DisplayName', 'Status', 'StartType') {
        Assert ($null -ne $rows[0].PSObject.Properties[$field]) "field $field missing"
    }
    $beta = $rows | Where-Object Name -eq 'Beta'
    Assert ($beta.DisplayName -eq 'Dienst für Überwachung') "DisplayName wrong: $($beta.DisplayName)"
}

Check 'status and start type are text, not numbers' {
    $rows = Read-Array (Invoke-Report 'services.json' '').Out
    $alpha = $rows | Where-Object Name -eq 'Alpha'
    Assert ($alpha.Status -is [string] -and $alpha.Status -eq 'Running') "Status of Alpha: $($alpha.Status)"
    Assert ($alpha.StartType -is [string] -and $alpha.StartType -eq 'Automatic') "StartType of Alpha: $($alpha.StartType)"
    $beta = $rows | Where-Object Name -eq 'Beta'
    Assert ($beta.Status -eq 'Stopped' -and $beta.StartType -eq 'Manual') "Beta: $($beta.Status) $($beta.StartType)"
}

Check '-Status Running returns only running services' {
    $r = Invoke-Report 'services.json' '-Status Running'
    Assert ($r.Code -eq 0) "exit code $($r.Code): $($r.Err)"
    $rows = Read-Array $r.Out
    $names = ($rows | ForEach-Object Name | Sort-Object) -join ','
    Assert ($names -eq 'Alpha,Gamma') "got $names"
}

Check 'a single service is still a JSON array' {
    $r = Invoke-Report 'services.json' '-Status Stopped'
    $rows = Read-Array $r.Out
    Assert ($rows.Count -eq 1 -and $rows[0].Name -eq 'Beta') "got $($r.Out)"
}

Check 'no matching service gives an empty JSON array' {
    $r = Invoke-Report 'all-running.json' '-Status Stopped'
    Assert ($r.Code -eq 0) "exit code $($r.Code): $($r.Err)"
    Assert (($r.Out -replace '\s', '') -eq '[]') "expected [], got '$($r.Out)'"
}

Check 'an unknown -Status value is rejected' {
    $r = Invoke-Report 'services.json' '-Status Paused'
    Assert ($r.Code -ne 0) 'exit code 0 for -Status Paused'
    Assert (-not $r.Out.StartsWith('[')) 'printed a report for -Status Paused'
}

Check '-OutFile writes the JSON as UTF-8 instead of printing it' {
    $file = Join-Path ([System.IO.Path]::GetTempPath()) "report-$([guid]::NewGuid()).json"
    $r = Invoke-Report 'services.json' "-OutFile '$file' -Status Stopped"
    Assert ($r.Code -eq 0) "exit code $($r.Code): $($r.Err)"
    Assert (Test-Path $file) 'no file written'
    $bytes = [System.IO.File]::ReadAllBytes($file)
    $text = [System.Text.UTF8Encoding]::new($false, $true).GetString($bytes).TrimStart([char]0xFEFF).Trim()
    $rows = Read-Array $text
    Assert ($rows.Count -eq 1 -and $rows[0].DisplayName -eq 'Dienst für Überwachung') "file content: $text"
    Assert ($r.Out -eq '') "also printed: $($r.Out)"
    Remove-Item $file
}

Check 'the README describes the call and both parameters' {
    $readme = Get-Content -Raw (Join-Path $root 'README.md')
    foreach ($needle in 'Get-ServiceReport.ps1', '-Status', '-OutFile') { Assert ($readme.Contains($needle)) "README lacks $needle" }
}

Check 'own Pester tests exist and pass' {
    Assert (Test-Path (Join-Path $root 'tests')) 'no tests directory'
    $cmd = "`$r = Invoke-Pester -Path '$root/tests' -PassThru -Output None; Write-Output ('{0} {1}' -f `$r.PassedCount, `$r.FailedCount)"
    $line = (& pwsh -NoProfile -NonInteractive -Command $cmd | Select-Object -Last 1)
    $passed, $failed = "$line".Split(' ')
    Assert ([int]$failed -eq 0) "$failed own tests fail"
    Assert ([int]$passed -ge 3) "only $passed own tests pass"
}

$i = 0
foreach ($r in $results) {
    $i++
    if ($r.Ok) { Write-Output "ok $i - $($r.Name)" } else { Write-Output "not ok $i - $($r.Name)"; Write-Output "  # $($r.Why -replace "`n", ' ')" }
}
$fail = @($results | Where-Object { -not $_.Ok }).Count
Write-Output "1..$i"
Write-Output "# tests $i"
Write-Output "# pass $($i - $fail)"
Write-Output "# fail $fail"
exit ([int]($fail -gt 0))
