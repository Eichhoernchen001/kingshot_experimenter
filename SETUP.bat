@echo off
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1

echo.
echo Kingshot setup
echo ==============
echo This setup does not require administrator rights.
echo It creates a private Python environment inside this Kingshot folder.
echo.

set "BASE_PY="
set "BASE_ARGS="

rem 1) If launched from Anaconda Prompt, prefer the active Conda Python.
if defined CONDA_PREFIX (
    call :check_python "%CONDA_PREFIX%\python.exe"
    if not errorlevel 1 goto :python_found
)

rem 2) Standard Windows Python launcher.
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "BASE_PY=py"
        set "BASE_ARGS=-3"
        goto :python_found
    )
)

rem 3) Python already available on PATH.
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "BASE_PY=python"
        set "BASE_ARGS="
        goto :python_found
    )
)

rem 4) Common per-user Anaconda / Miniconda locations.
for %%P in (
    "%USERPROFILE%\anaconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "%LOCALAPPDATA%\anaconda3\python.exe"
    "%LOCALAPPDATA%\miniconda3\python.exe"
    "%PROGRAMDATA%\anaconda3\python.exe"
    "%PROGRAMDATA%\miniconda3\python.exe"
    "C:\Anaconda3\python.exe"
    "C:\Miniconda3\python.exe"
) do (
    call :check_python "%%~P"
    if not errorlevel 1 goto :python_found
)

echo Python 3.10 or newer could not be located from a normal Windows session.
echo.
echo If you normally use Python through Anaconda Prompt:
echo   1. Open Anaconda Prompt.
echo   2. cd /d "%~dp0"
echo   3. Run SETUP.bat
echo.
echo No administrator rights are required.
pause
exit /b 1

:python_found
echo Found Python: %BASE_PY% %BASE_ARGS%
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Creating private Kingshot Python environment...
    "%BASE_PY%" %BASE_ARGS% -m venv ".venv"
    if errorlevel 1 goto :fail
) else (
    echo Existing private Kingshot Python environment found.
)

set "PY=.venv\Scripts\python.exe"

echo Checking Tkinter support...
"%PY%" -c "import tkinter"
if errorlevel 1 (
    echo Tkinter is missing. Install Python with Tcl/Tk support from python.org.
    goto :fail
)

echo.
echo Installing/updating Python packages in the private environment...
"%PY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
"%PY%" -m pip install -r "app\requirements.txt"
if errorlevel 1 goto :fail

echo.
echo Installing the Playwright Chromium browser for this user...
"%PY%" -m playwright install chromium
if errorlevel 1 goto :fail

echo.
echo Setup complete.
echo You can now double-click START.bat.
pause
exit /b 0

:check_python
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "BASE_PY=%~1"
set "BASE_ARGS="
exit /b 0

:fail
echo.
echo Setup failed. The error above should show which step failed.
echo Nothing here requires administrator privileges.
pause
exit /b 1
