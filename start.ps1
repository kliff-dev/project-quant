param([ValidateRange(1024, 65535)][int]$Port = 8501)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Virtual environment missing. Follow the setup steps in README.md.'
}
& $projectPython -m streamlit run dashboard/app.py --server.address 127.0.0.1 --server.port $Port --server.headless true --browser.gatherUsageStats false
exit $LASTEXITCODE
