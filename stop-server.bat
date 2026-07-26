@echo off
taskkill /FI "WINDOWTITLE eq Demucs Server*" /T /F >nul 2>&1
taskkill /IM cloudflared.exe /F >nul 2>&1

for /f "tokens=5" %%P in ('netstat -ano ^| findstr :5174 ^| findstr LISTENING') do (
    taskkill /PID %%P /F >nul 2>&1
)

echo Done.
pause
