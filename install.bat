@echo off
setlocal
pushd "%~dp0"

echo [INFO] Workdir: %CD%
echo [INFO] Python:
where python
python --version
echo.

echo [1/3] Upgrade pip
python -m pip install -U pip || goto :ERR

echo [2/3] Install deps from requirements.txt
if not exist "requirements.txt" (
  echo [ERR] requirements.txt not found in %CD%
  goto :ERR
)
python -m pip install -r "requirements.txt" || goto :ERR

echo [3/3] Install Playwright Chromium
python -m playwright install chromium || goto :ERR

echo.
echo OK. Now run: run.bat
pause
exit /b 0

:ERR
echo.
echo [ERR] Install failed. Scroll up for the error message.
pause
exit /b 1
