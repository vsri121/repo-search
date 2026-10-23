param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            uv sync
        } else {
            python -m venv .venv
            .venv/Scripts/python.exe -m pip install -r requirements.txt
        }
        if ($LASTEXITCODE -ne 0) { throw 'Dependency setup failed.' }
    }
    Write-Host "Innovation Farming: http://127.0.0.1:$Port"
    .venv/Scripts/python.exe -m uvicorn farm.app:app --host 127.0.0.1 --port $Port
} finally {
    Pop-Location
}
