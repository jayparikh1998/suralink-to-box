$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Users\jaypa\AppData\Local\suralink-to-box-venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "JWT virtual environment not found at: $python"
}

Set-Location $repoRoot
$env:PYTHONPATH = "src"

& $python -m streamlit run app.py
