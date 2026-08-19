@echo off
cd /d "%~dp0"
call demucs-env-gpu\Scripts\activate.bat
python run.py
