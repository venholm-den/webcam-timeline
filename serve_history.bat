@echo off
cd /d "%~dp0"
python .\scripts\serve_history.py --host 0.0.0.0 --port 8000
pause
