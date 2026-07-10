@echo off
cd /d "%~dp0"
python scripts\fetch_timeline.py --watch --interval 60
