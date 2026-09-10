@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  start "ShopPOS" py shoppos.py
) else (
  start "ShopPOS" python shoppos.py
)
timeout /t 2 >nul
start http://127.0.0.1:8765
