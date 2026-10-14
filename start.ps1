$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& "$PSScriptRoot\start-ngrok.ps1"
if (-not (Test-Path .venv)) {
  python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe app.py
