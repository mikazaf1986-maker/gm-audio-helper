@echo off
cd /d "%~dp0"
if not exist ".env" (
  echo Missing .env file. Copy .env.example to .env and insert your Discord bot token.
  pause
  exit /b 1
)
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)
if not exist ".venv312\Scripts\python.exe" (
  echo Creating Python environment...
  py -3.12 -m venv .venv312
  if errorlevel 1 goto error
)
set "VPY=.venv312\Scripts\python.exe"
"%VPY%" -m pip install -q -r requirements.txt
if errorlevel 1 goto error
"%VPY%" bot.py
if errorlevel 1 goto error
pause
exit /b 0
:error
echo.
echo Setup or bot failed. Check the error above.
pause
exit /b 1
