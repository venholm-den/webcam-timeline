@echo off
cd /d "%~dp0"

set "CLOUDFLARED=cloudflared"
where cloudflared >nul 2>nul
if errorlevel 1 (
    if exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" (
        set "CLOUDFLARED=C:\Program Files (x86)\cloudflared\cloudflared.exe"
    ) else if exist "C:\Program Files\cloudflared\cloudflared.exe" (
        set "CLOUDFLARED=C:\Program Files\cloudflared\cloudflared.exe"
    )
)

echo Starting public Cloudflare tunnel for http://127.0.0.1:8000
echo.
echo Keep this window open. Copy the trycloudflare.com URL it prints.
echo.
"%CLOUDFLARED%" tunnel --protocol http2 --edge-ip-version 4 --url http://127.0.0.1:8000
pause
