param(
    [string]$InstallDir = "$env:ProgramData\KidTime",
    [switch]$KeepData
)

$ErrorActionPreference = "Stop"

$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $IsAdmin) {
    throw "Please run this uninstaller from an elevated PowerShell window."
}

$TaskNames = @("KidTimeMonitor", "KidTimeMonitorWatchdog")
foreach ($TaskName in $TaskNames) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*kidtimeCli.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

$EnvNames = @(
    "KIDTIME_SERVER_URL",
    "KIDTIME_CLIENT_ID",
    "KIDTIME_SHARED_KEY_HEX",
    "KIDTIME_BASE_DIR"
)
foreach ($EnvName in $EnvNames) {
    [Environment]::SetEnvironmentVariable($EnvName, $null, "Machine")
}

if (-not $KeepData -and (Test-Path -LiteralPath $InstallDir)) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
}

Write-Host "KidTime client scheduled tasks removed."
Write-Host "KidTime client processes stopped if any were running."
Write-Host "KidTime machine environment variables removed."
if ($KeepData) {
    Write-Host "Kept install directory: $InstallDir"
} else {
    Write-Host "Removed install directory: $InstallDir"
}
