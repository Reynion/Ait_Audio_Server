@echo off
cd /d "%~dp0"
call demucs-env\Scripts\activate.bat
python run.py
