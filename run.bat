@echo off
setlocal
chcp 65001 >nul 2>nul
pushd "%~dp0"
echo [INFO] Workdir: %CD%
echo [INFO] Python:

REM ========== 優先用內嵌 Python 3.12.7 解 stdlib ssl bug,沒有就 fallback 系統 Python ==========
REM v6.1.22: 首次運行如果沒 python_3.12.7\ 就從 Worker R2 自動下載 約107MB 首次 1-3 分鐘
REM 注意: 此區塊內 echo 訊息禁止用圓括號 cmd 會誤判 if 區塊結束
if not exist "%~dp0python_3.12.7\python.exe" call :BOOTSTRAP_PYTHON

if exist "%~dp0python_3.12.7\python.exe" (
    set "PYTHON=%~dp0python_3.12.7\python.exe"
    echo [INFO] 使用內嵌 Python 3.12.7
) else (
    set "PYTHON=python"
    where python 2>nul || echo [ERROR] python 不在 PATH 中！请先运行 install.bat
)
"%PYTHON%" --version
goto :AFTER_PYTHON_SETUP

:BOOTSTRAP_PYTHON
echo [INFO] python_3.12.7 不存在 首次自動下載中 約107MB 需 1-3 分鐘...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest 'https://square-river-tg.<PHONE_REDACTED>.workers.dev/runtime/python312.zip?key=<WORKER_API_KEY_REDACTED>' -OutFile '%~dp0python312_dl.zip' -UseBasicParsing -TimeoutSec 600 } catch { Write-Host '[ERR] download failed:' $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo [WARN] python_3.12.7 下載失敗 fallback 用系統 Python 注意 3.12.3 有 SSL crash bug
    exit /b 0
)
echo [INFO] 解壓 python_3.12.7 中...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%~dp0python312_dl.zip' -DestinationPath '%~dp0' -Force"
if errorlevel 1 (
    echo [WARN] python_3.12.7 解壓失敗 fallback 用系統 Python
    del "%~dp0python312_dl.zip" >nul 2>nul
    exit /b 0
)
del "%~dp0python312_dl.zip" >nul 2>nul
if exist "%~dp0python_3.12.7\python.exe" (
    echo [OK] python_3.12.7 部署完成
) else (
    echo [WARN] 解壓後找不到 python.exe fallback 系統 Python
)
exit /b 0

:AFTER_PYTHON_SETUP

REM 启动前检查关键依赖（缺失则自动安装,內嵌 Python 已預裝,跳過會很快）
"%PYTHON%" -c "import boto3" 2>nul || (
    echo [INFO] boto3 未安装，正在自动安装...
    "%PYTHON%" -m pip install boto3 -q
)
"%PYTHON%" -c "import curl_cffi" 2>nul || (
    echo [INFO] curl_cffi 未安装，正在自动安装...
    "%PYTHON%" -m pip install curl_cffi -q
)
"%PYTHON%" -c "import customtkinter" 2>nul || (
    echo [INFO] customtkinter 未安装，正在自动安装...
    "%PYTHON%" -m pip install customtkinter -q
)
"%PYTHON%" -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM" 2>nul || (
    echo [INFO] cryptography 未安装，正在自动安装...
    "%PYTHON%" -m pip install cryptography -q
)
"%PYTHON%" -c "from Crypto.Cipher import AES" 2>nul || (
    echo [INFO] pycryptodome 未安装 - v6.1 BOSH JWT 解密必需 - 正在自动安装...
    "%PYTHON%" -m pip install pycryptodome -q
)
"%PYTHON%" -c "import ddddocr" 2>nul || (
    echo [INFO] ddddocr 未安装 - v6.1.52 SYB 验证码本地识别 - 正在自动安装...
    "%PYTHON%" -m pip install ddddocr -q
)
"%PYTHON%" -c "import imageio_ffmpeg" 2>nul || (
    echo [INFO] imageio_ffmpeg 未安装 - v6.1.53 TG 视频 30s 自动切分 - 正在自动安装...
    "%PYTHON%" -m pip install imageio-ffmpeg -q
)

REM 检查 Noto Sans TC 字体是否已安装，未安装则自动安装
if exist "fonts\NotoSansTC-Regular.ttf" (
    "%PYTHON%" -c "import tkinter as tk, tkinter.font as f; r=tk.Tk(); r.withdraw(); ok='Noto Sans TC' in f.families(); r.destroy(); exit(0 if ok else 1)" 2>nul
    if errorlevel 1 (
        echo [INFO] 正在安装 Noto Sans TC 字体...
        copy /Y "fonts\NotoSansTC-Regular.ttf" "%LOCALAPPDATA%\Microsoft\Windows\Fonts\" >nul 2>nul
        reg add "HKCU\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts" /v "Noto Sans TC (TrueType)" /t REG_SZ /d "%LOCALAPPDATA%\Microsoft\Windows\Fonts\NotoSansTC-Regular.ttf" /f >nul 2>nul
        echo [INFO] 字体安装完成，首次需重启程序生效
    )
)

set CRASH_COUNT=0
set MAX_CRASHES=5

:loop
echo [INFO] Starting app...
REM 把 stderr 同时输出到屏幕和 crash_stderr.log，方便诊断 Python 崩溃
"%PYTHON%" app.py 2>>crash_stderr.log
set EXIT_CODE=%ERRORLEVEL%
echo.
echo [INFO] App exited with code %EXIT_CODE%

REM 正常退出(0)或用户Ctrl+C → 不重启
if "%EXIT_CODE%"=="0" goto done

REM 非正常退出 → 递增崩溃计数
set /a CRASH_COUNT+=1
if %CRASH_COUNT% GEQ %MAX_CRASHES% (
    echo [ERROR] Crashed %CRASH_COUNT% times, auto-restart stopped.
    echo [ERROR] Please check Python environment and run run.bat manually.
    goto done
)

echo [WARN] 非正常退出 (%CRASH_COUNT%/%MAX_CRASHES%)，5秒后自动重启...
REM timeout 在某些精简版 Windows 上不存在，用 ping 作备用
timeout /t 5 /nobreak >nul 2>nul || ping -n 6 127.0.0.1 >nul 2>nul
goto loop

:done
echo.
echo [INFO] 已退出。按任意键关闭窗口...
pause
