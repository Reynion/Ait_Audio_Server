@echo off
taskkill /FI "WINDOWTITLE eq Demucs Server*" /T /F >nul 2>&1
taskkill /IM cloudflared.exe /F >nul 2>&1
echo Done.
pause
