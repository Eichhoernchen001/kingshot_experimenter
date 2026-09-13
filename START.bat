@echo off
setlocal
cd /d "%~dp0" || exit /b 1

if exist ".venv\Scripts\pythonw.exe" (
    start "Kingshot" ".venv\Scripts\pythonw.exe" "app\kingshot_gui.py"
    exit /b 0
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "app\kingshot_gui.py"
    exit /b %errorlevel%
)

echo Kingshot has not been set up in this folder yet.
echo Please run SETUP.bat once first.
echo.
echo If SETUP.bat cannot see your Anaconda installation,
echo open Anaconda Prompt, cd to this folder, and run SETUP.bat there.
pause
exit /b 1
