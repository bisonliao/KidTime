param(
    [string]$InstallDir = "$env:ProgramData\KidTime",
    [string]$ServerUrl = "http://8.148.226.47:8001",
    [string]$ClientId = $env:COMPUTERNAME,
    [Parameter(Mandatory = $true)]
    [string]$SharedKeyHex
)

$ErrorActionPreference = "Stop"

if ($SharedKeyHex.Length -ne 64 -or $SharedKeyHex -notmatch '^[0-9a-fA-F]+$') {
    throw "SharedKeyHex must be a 64-character hex string."
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force -Path ".\kidtimeCli.py" -Destination (Join-Path $InstallDir "kidtimeCli.py")
Copy-Item -Force -Path ".\requirements-cli.txt" -Destination (Join-Path $InstallDir "requirements-cli.txt")

python -m pip install --upgrade pip
python -m pip install -r (Join-Path $InstallDir "requirements-cli.txt")

[Environment]::SetEnvironmentVariable("KIDTIME_SERVER_URL", $ServerUrl, "Machine")
[Environment]::SetEnvironmentVariable("KIDTIME_CLIENT_ID", $ClientId, "Machine")
[Environment]::SetEnvironmentVariable("KIDTIME_SHARED_KEY_HEX", $SharedKeyHex.ToLower(), "Machine")
[Environment]::SetEnvironmentVariable("KIDTIME_BASE_DIR", $InstallDir, "Machine")

$env:KIDTIME_SERVER_URL = $ServerUrl
$env:KIDTIME_CLIENT_ID = $ClientId
$env:KIDTIME_SHARED_KEY_HEX = $SharedKeyHex.ToLower()
$env:KIDTIME_BASE_DIR = $InstallDir

python (Join-Path $InstallDir "kidtimeCli.py") --install-startup --base-dir $InstallDir
python (Join-Path $InstallDir "kidtimeCli.py") --ensure-running --base-dir $InstallDir

Write-Host "KidTime client installed in $InstallDir"
