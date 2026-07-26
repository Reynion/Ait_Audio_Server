@echo off
echo Demucs 서버를 종료합니다...
taskkill /FI "WINDOWTITLE eq Demucs Server*" /T /F >nul 2>&1
taskkill /IM cloudflared.exe /F >nul 2>&1
echo 완료.
pause
