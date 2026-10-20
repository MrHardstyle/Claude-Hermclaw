# Stand-in for Windows' Get-Service on the Linux grading sandbox: the same property types (Status and StartType are
# enums, which ConvertTo-Json writes as numbers unless the script converts them). $env:FAKE_SERVICES names the list.
enum ServiceControllerStatus { Stopped = 1; StartPending = 2; StopPending = 3; Running = 4; Paused = 7 }
enum ServiceStartMode { Boot = 0; System = 1; Automatic = 2; Manual = 3; Disabled = 4 }

$global:FakeServiceList = @(
    Get-Content -Raw -Encoding utf8 $env:FAKE_SERVICES | ConvertFrom-Json | ForEach-Object {
        [pscustomobject]@{
            Name        = $_.Name
            ServiceName = $_.Name
            DisplayName = $_.DisplayName
            Status      = [ServiceControllerStatus]$_.Status
            StartType   = [ServiceStartMode]$_.StartType
            CanStop     = $true
        }
    }
)

function global:Get-Service {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string[]]$Name = '*',
        [string[]]$DisplayName,
        [string[]]$Include,
        [string[]]$Exclude
    )
    $global:FakeServiceList | Where-Object {
        $svc = $_
        ($Name | Where-Object { $svc.Name -like $_ }) -and
        (-not $DisplayName -or ($DisplayName | Where-Object { $svc.DisplayName -like $_ }))
    }
}
