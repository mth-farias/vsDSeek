@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE=%~dp0runtime\python\python.exe"
set "PYARGS="
if exist "%PYEXE%" goto :run
set "PYEXE=py"
set "PYARGS=-3"
where py >nul 2>&1 && goto :run
set "PYEXE=python"
set "PYARGS="
where python >nul 2>&1 && goto :run
echo Python runtime missing and no fallback Python on PATH.
echo Run VsDSeek-update.bat from a complete copy of the app to restore it.
pause
exit /b 1

:run
"%PYEXE%" %PYARGS% "%~dp0app.py" doctor
echo.
pause
exit /b 0
