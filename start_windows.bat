@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Construction Defects Web App

echo =========================================================
echo  Construction Defects Web App - Windows Launcher
 echo =========================================================
echo.
if not exist "app.py" goto missingfiles
if not exist "requirements.txt" goto missingfiles

set "RUNPY="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "RUNPY=py -3"
)
if not defined RUNPY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
        if not errorlevel 1 set "RUNPY=python"
    )
)
if not defined RUNPY (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
        if not errorlevel 1 set "RUNPY=python3"
    )
)
if not defined RUNPY goto nopython

echo Found Python: %RUNPY%
%RUNPY% --version
echo.
set "PYTHONUTF8=1"

if not exist ".env" (
    if exist ".env.example" copy /y ".env.example" ".env" >nul
)

echo [1/2] Checking required packages...
%RUNPY% -c "import flask, openai, PIL, xlsxwriter, dotenv" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages. Internet connection is needed the first time.
    echo Log file: setup_log.txt
    %RUNPY% -m pip install -r "requirements.txt" >"setup_log.txt" 2>&1
    if errorlevel 1 goto pipfailed
)

echo [2/2] Starting the web app...
echo.
echo Please keep this black window OPEN while using the web app.
echo Open this URL in your browser: http://127.0.0.1:5000
echo If the browser shows a connection error, refresh after the server starts.
echo.
start "" "http://127.0.0.1:5000"
%RUNPY% "app.py"
if errorlevel 1 goto appfailed

echo.
echo Web app stopped.
pause
exit /b 0

:missingfiles
echo ERROR: Missing app.py or requirements.txt in this folder.
echo You probably started this launcher INSIDE the ZIP archive.
echo Right-click the ZIP and select "Extract All" first.
goto finisherror

:nopython
echo ERROR: Python 3.10+ could not be found.
echo Please open Command Prompt and run: py --version
echo If that fails, try: python --version
echo Reinstall Python and enable "Add Python to PATH" if needed.
goto finisherror

:pipfailed
echo ERROR: Required packages could not be installed.
echo Please see the setup_log.txt file in this folder.
echo Common causes are no internet, firewall, or missing pip.
echo.
type "setup_log.txt"
goto finisherror

:appfailed
echo ERROR: Python web app stopped with an error.
echo Copy the error text above, or take a screenshot and share it.
goto finisherror

:finisherror
echo.
echo The launcher will stay open so you can read the error.
pause
exit /b 1
