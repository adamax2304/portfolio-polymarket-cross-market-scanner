$Needles = @(
    "main.py run",
    "main.py cross-hft-paper",
    "main.py cross-hft-live",
    "main.py probability-run",
    "main.py telegram-dashboard"
)

$procs = Get-CimInstance Win32_Process | Where-Object {
    $cmd = $_.CommandLine
    $cmd -and ($Needles | Where-Object { $cmd -like "*$_*" })
}

if (@($procs).Count -eq 0) {
    Write-Output "No Polymarket bots are running"
    exit 0
}

$procs | Select-Object ProcessId, Name, CommandLine | Format-List
