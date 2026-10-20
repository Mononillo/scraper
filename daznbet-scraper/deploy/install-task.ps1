# Registra el scraper como tarea programada de Windows (arranque automatico).
# Ejecutar en una consola PowerShell COMO ADMINISTRADOR.
$ErrorActionPreference = 'Stop'
$proj = (Resolve-Path "$PSScriptRoot\..").Path
$py   = Join-Path $proj '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "Crea el venv primero: py -3 -m venv .venv" }

$action  = New-ScheduledTaskAction -Execute $py -Argument '-u daznbet_odds.py' -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                                          -ExecutionTimeLimit (New-TimeSpan -Days 0) -StartWhenAvailable
Register-ScheduledTask -TaskName 'DAZN Bet Scraper' -Action $action -Trigger $trigger `
                       -Settings $settings -RunLevel Highest -Force

Write-Host "Tarea registrada."
Write-Host "Arrancar:  Start-ScheduledTask -TaskName 'DAZN Bet Scraper'"
Write-Host "Logs:      Get-ScheduledTaskInfo -TaskName 'DAZN Bet Scraper'"
Write-Host "Borrar:    Unregister-ScheduledTask -TaskName 'DAZN Bet Scraper' -Confirm:`$false"