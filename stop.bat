@echo off

setlocal
set PORT=%FORGE_PORT%
if "%PORT%"=="" set PORT=8790

echo Menghentikan ContentForge di port %PORT%...
powershell -NoProfile -Command ^
 "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue;" ^
 "if ($c) { $c.OwningProcess | Sort-Object -Unique | ForEach-Object {" ^
 "  $proc = Get-Process -Id $_ -ErrorAction SilentlyContinue;" ^
 "  Write-Host ('  stop PID ' + $_ + ' (' + $proc.ProcessName + ')');" ^
 "  Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue };" ^
 "  Write-Host '  ContentForge berhenti.' }" ^
 "else { Write-Host ('  tidak ada server listen di port ' + '%PORT%') }"

ping -n 4 127.0.0.1 > nul
