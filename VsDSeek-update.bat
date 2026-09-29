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
echo Re-copy the app folder to restore it.
pause
exit /b 1

:run
echo Refreshing pinned versions (VS Code, Claude Code), then installing...
"%PYEXE%" %PYARGS% "%~dp0app.py" update
set "rc=%ERRORLEVEL%"
echo.
if not "%rc%"=="0" (
  echo Update failed with exit code %rc%.
) else (
  echo Done. Click VsDSeek.bat to activate DeepSeek again.
)
pause
exit /b %rc%
