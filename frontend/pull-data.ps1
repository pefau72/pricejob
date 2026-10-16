# pull-prices.ps1 — spejler backend/out fra ubserv ned i frontend/data på denne PC.
# Læser kun det push_prices.py allerede har bygget; rører ikke netsite.
$ErrorActionPreference = 'Stop'

$RemoteHost = 'ubserv'
$RemoteUser = 'peterfausboll'
$RemotePath = 'code/datajob/backend/out'   # relativ til ~ på serveren
$LocalPath  = 'C:\Users\peter\Dropbox\Documents\Arbejde\Regenda\Grafik\Websolution\frontend'
$LogFile    = "$env:LOCALAPPDATA\pull-data.log"

New-Item -ItemType Directory -Force -Path $LocalPath | Out-Null

# scp med glob: hent alle .json. -p bevarer tidsstempler fra serveren,
# så vi kan afgøre hvad der faktisk er nyt.
$target = "${RemoteUser}@${RemoteHost}:${RemotePath}/*.json"

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') pull fra {$RemoteHost}:{$RemotePath}" | Add-Content $LogFile

scp -p -o BatchMode=yes    "${RemoteUser}@${RemoteHost}:${RemotePath}/pricedata/*.json"       "$LocalPath\pricedata"           | Add-Content $LogFile
scp -p -o BatchMode=yes    "${RemoteUser}@${RemoteHost}:${RemotePath}/weather/*.json"         "$LocalPath\weatherdata"         | Add-Content $LogFile 
scp -p -o BatchMode=yes -r "${RemoteUser}@${RemoteHost}:${RemotePath}/weather/normals/*.json" "$LocalPath\weatherdata\normals" | Add-Content $LogFile

"=== færdig rc=$LASTEXITCODE" | Add-Content $LogFile



