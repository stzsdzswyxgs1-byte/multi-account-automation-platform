@echo off
chcp 65001 >nul 2>&1
echo [INFO] Starting updater...
python "%~dp0updater.pyw"
pause
