$ErrorActionPreference = 'Stop'
$proj = (Resolve-Path "$PSScriptRoot\..").Path
$out  = Join-Path (Split-Path $proj -Parent) 'daznbet-scraper.zip'
if (Test-Path $out) { Remove-Item $out -Force }
Compress-Archive -Path $proj -DestinationPath $out -CompressionLevel Optimal
Write-Host "creado $out"