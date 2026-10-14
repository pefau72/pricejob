# pull-prices.ps1 — spejler backend/out fra ubserv ned i frontend/data på denne PC.
# Læser kun det push_prices.py allerede har bygget; rører ikke netsite.
$ErrorActionPreference = 'Stop'

$RemoteHost = 'ubserv'
$RemoteUser = 'peterfausboll'
$RemotePath = 'code/pricejob/backend/out'   # relativ til ~ på serveren
$LocalPath  = 'C:\Users\peter\Dropbox\Documents\Arbejde\Regenda\Grafik\Websolution\frontend\pricedata'
$LogFile    = "$env:LOCALAPPDATA\pull-prices.log"

New-Item -ItemType Directory -Force -Path $LocalPath | Out-Null

# scp med glob: hent alle .json. -p bevarer tidsstempler fra serveren,
# så vi kan afgøre hvad der faktisk er nyt.
$target = "${RemoteUser}@${RemoteHost}:${RemotePath}/*.json"

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') pull fra {$RemoteHost}:{$RemotePath}" |
    Add-Content $LogFile
scp -p -o BatchMode=yes $target $LocalPath 2>&1 | Add-Content $LogFile
"=== færdig rc=$LASTEXITCODE" | Add-Content $LogFile
