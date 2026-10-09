@echo off
cd /d "C:\Users\peter\Dropbox\Documents\Arbejde\Regenda\Grafik\websolution\frontend"
for /r %%f in (*.html *.css *.js) do (
    if not exist "..\dump" mkdir "..\dump"
    copy "%%f" "..\dump\%%~nf%%~xf.txt" >nul
)
echo Færdig. Filerne ligger i ..\dump
pause
