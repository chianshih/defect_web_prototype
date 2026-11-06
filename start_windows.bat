@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
py -m pip install -r requirements.txt
if errorlevel 1 (
 echo 安裝失敗，請確認有安裝 Python 3.10 以上版本。
 pause
 exit /b 1
)
start "" "http://127.0.0.1:5000"
py app.py
pause
