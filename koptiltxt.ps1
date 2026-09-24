$SourceFolder = "C:\Users\peter\Dropbox\Documents\Arbejde\Regenda\Grafik\Pinegrow"
$DestinationFolder = "C:\Users\peter\downloads\"
New-Item -ItemType Directory -Path $DestinationFolder -Force | Out-Null
Get-ChildItem -Path $SourceFolder -Recurse -Include *.html, *.css, *.js | ForEach-Object {
$RelativePath = $_.FullName.Substring($SourceFolder.Length)
$DestinationPath = Join-Path $DestinationFolder ($RelativePath + ".txt")
$DestinationDir = Split-Path $DestinationPath -Parent
New-Item -ItemType Directory -Path $DestinationDir -Force | Out-Null
Get-Content $_.FullName | Set-Content $DestinationPath
Write-Host "Oprettet: $DestinationPath"
}