@echo off
cd /d "%~dp0"
start "Demucs Server" cmd /k "demucs-env\Scripts\activate.bat && python run.py"
echo Demucs 서버를 새 창에서 시작했습니다. 그 창을 닫거나 stop-server.bat을 실행하면 꺼집니다.
