@echo off
REM Run the gateway in the foreground on Windows. Arguments are passed to main.py, e.g.
REM   deploy\run_gateway.bat --validate-only
setlocal
set ROOT=%~dp0..
cd /d "%ROOT%"
if not exist ".venv\Scripts\python.exe" (
  echo virtualenv missing: run deploy\install_windows.ps1 first
  exit /b 1
)
".venv\Scripts\python.exe" main.py %*
exit /b %ERRORLEVEL%
