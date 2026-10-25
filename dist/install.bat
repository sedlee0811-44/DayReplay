@echo off
chcp 65001 >nul 2>&1
title DayReplay Installer
echo.
echo  ============================================
echo   DayReplay  -  A diary that writes itself
echo  ============================================
echo.

:: --- Check admin (we don't need it, but warn if running as admin) ---
net session >nul 2>&1
if %errorLevel% equ 0 (
    echo  [NOTE] Running as administrator is NOT required.
    echo         DayReplay runs entirely from your user folder.
    echo.
)

:: --- Find the code directory (where this bat lives) ---
set "INSTALL_DIR=%~dp0"
cd /d "%INSTALL_DIR%"

:: --- Check Python ---
if not exist "python\python.exe" (
    echo  [ERROR] Python runtime not found!
    echo          This installer should be inside the DayReplay package folder.
    echo          Please re-download and extract the full ZIP first.
    echo.
    pause
    exit /b 1
)

:: --- Create desktop shortcut ---
echo  [1/4] Creating desktop shortcut...
set "SHORTCUT=%USERPROFILE%\Desktop\DayReplay.lnk"
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%SHORTCUT%'); $sc.TargetPath = '%INSTALL_DIR%start_voice.vbs'; $sc.WorkingDirectory = '%INSTALL_DIR%'; $sc.Description = 'DayReplay - Start Recording'; $sc.Save()"
if exist "%SHORTCUT%" (
    echo        OK: Desktop shortcut created
) else (
    echo        [WARN] Could not create shortcut (you can manually run start_voice.vbs)
)

:: --- Stop shortcut ---
set "STOP_SHORTCUT=%USERPROFILE%\Desktop\Stop DayReplay.lnk"
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%STOP_SHORTCUT%'); $sc.TargetPath = '%INSTALL_DIR%stop_voice.vbs'; $sc.WorkingDirectory = '%INSTALL_DIR%'; $sc.Description = 'DayReplay - Stop Recording'; $sc.Save()"

:: --- Check models ---
echo.
echo  [2/4] Checking AI models...
if not exist "models\*.pt" if not exist "models\*.onnx" (
    echo.
    echo  ============================================
    echo   AI MODELS NOT FOUND
    echo  ============================================
    echo  DayReplay needs AI models to understand speech
    echo  and sounds (~1.3 GB, one-time download).
    echo.
    set /p DOWNLOAD="  Download models now? (Y/n): "
    if /i not "%DOWNLOAD%"=="n" (
        echo.
        echo  Starting model download... (this may take a while)
        call "python\python.exe" download_models.py
        if errorlevel 1 (
            echo.
            echo  [WARN] Model download had issues. You can retry later by running:
            echo         python\python.exe download_models.py
        )
    ) else (
        echo  [OK] Skipped. Run download_models.py later when ready.
    )
) else (
    echo        OK: Models found, ready to go!
)

:: --- Self-test ---
echo.
echo  [3/4] Running quick self-test...
call "python\python.exe" selftest.py --quick
if errorlevel 1 (
    echo        [WARN] Self-test reported issues. Check logs/ for details.
) else (
    echo        OK: Self-test passed!
)

:: --- Done ---
echo.
echo  [4/4] Installation complete!
echo  ============================================
echo.
echo  HOW TO USE:
echo.
echo  1. Double-click "DayReplay" on your Desktop to START recording.
echo  2. Double-click "Stop DayReplay" on your Desktop to STOP.
echo  3. Open today's timeline: double-click open_ledger.py
     (or run: python\python.exe open_ledger.py)
echo.
echo  Your data is stored in: %INSTALL_DIR%data\
echo  Logs are in:            %INSTALL_DIR%logs\
echo.
echo  First timeline will appear within minutes of starting.
echo.
pause
