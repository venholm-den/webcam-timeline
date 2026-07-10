@echo off
cd /d "%~dp0"
python scripts\fetch_timeline.py --once
pause
