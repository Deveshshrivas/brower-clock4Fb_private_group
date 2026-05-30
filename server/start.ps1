param(
    [string]$HostName = "0.0.0.0",
    [int]$Port = 8000
)

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Set-Location $ProjectRoot
& $Python -m uvicorn server.app:app --host $HostName --port $Port
