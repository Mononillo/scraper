$ErrorActionPreference = 'Stop'
Set-Location (Resolve-Path "$PSScriptRoot\..").Path
if (-not (Test-Path '.venv')) { py -3 -m venv .venv }
& '.\.venv\Scripts\python.exe' -m pip install -q -r requirements.txt
& '.\.venv\Scripts\python.exe' -m playwright install chromium
& '.\.venv\Scripts\python.exe' daznbet_odds.py @args