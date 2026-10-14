@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Siphon

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"

rem Reuse the private environment unless the Python it was built from is gone.
if not exist "%VPY%" goto :create
"%VPY%" -c "import sys" >nul 2>&1
if not errorlevel 1 goto :setup
echo  Python changed, rebuilding Siphon's environment...
rmdir /s /q "%VENV%"

:create
call :find_python
if not defined PY goto :no_python
echo.
echo  Siphon - first launch setup
echo  Creating a private Python environment...
%PY% -m venv "%VENV%"
if errorlevel 1 goto :fail

:setup
"%VPY%" "%~dp0siphon\setup_env.py"
if errorlevel 1 goto :fail
start "" "%VPYW%" "%~dp0siphon\app.py"
exit /b 0

:find_python
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :try_python
set "PY=py -3"
exit /b 0
:try_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=python"
exit /b 0

:no_python
echo.
echo  Siphon needs Python 3.10 or newer.
echo  Install it from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" during the install.
echo  Then launch Siphon again.
echo.
pause
exit /b 1

:fail
echo.
echo  Setup failed. Scroll up for details, check your connection, then try again.
echo.
pause
exit /b 1
