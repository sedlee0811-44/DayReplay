# DayReplay Installer (PowerShell version)
# Run: right-click → "Run with PowerShell"  OR  .\install.ps1

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "DayReplay Installer"
$ErrorActionPreference = "SilentlyContinue"

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "    DayReplay  -  A diary that writes itself" -ForegroundColor White
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""

# --- Find installation directory ---
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $InstallDir
Write-Host "  Install location: $InstallDir"

# --- Check Python ---
if (-not (Test-Path "$InstallDir\python\python.exe")) {
    Write-Host "  [ERROR] Python runtime not found!" -ForegroundColor Red
    Write-Host "          Please re-extract the full ZIP file." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

# --- Create desktop shortcuts ---
Write-Host ""
Write-Host "  [1/4] Creating desktop shortcuts..." -ForegroundColor Yellow

$WshShell = New-Object -ComObject WScript.Shell

$StartShortcut = "$([Environment]::GetFolderPath('Desktop'))\DayReplay.lnk"
$Sc = $WshShell.CreateShortcut($StartShortcut)
$Sc.TargetPath = "$InstallDir\start_voice.vbs"
$Sc.WorkingDirectory = $InstallDir
$Sc.Description = "DayReplay - Start Recording"
$Sc.Save()
Write-Host "        OK: Start shortcut created" -ForegroundColor Green

$StopShortcut = "$([Environment]::GetFolderPath('Desktop'))\Stop DayReplay.lnk"
$Sc2 = $WshShell.CreateShortcut($StopShortcut)
$Sc2.TargetPath = "$InstallDir\stop_voice.vbs"
$Sc2.WorkingDirectory = $InstallDir
$Sc2.Description = "DayReplay - Stop Recording"
$Sc2.Save()
Write-Host "        OK: Stop shortcut created" -ForegroundColor Green

# --- Check models ---
Write-Host ""
Write-Host "  [2/4] Checking AI models..." -ForegroundColor Yellow

$HasModels = (Get-ChildItem "$InstallDir\models" -Include *.pt,*.onnx -ErrorAction SilentlyContinue).Count -gt 0

if (-not $HasModels) {
    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor Red
    Write-Host "    AI MODELS NOT FOUND" -ForegroundColor Red
    Write-Host "  ============================================" -ForegroundColor Red
    Write-Host "  DayReplay needs AI models (~1.3 GB, one-time)" -ForegroundColor White
    Write-Host ""
    $Download = Read-Host "  Download models now? (Y/n)"
    
    if ($Download -ne 'n') {
        Write-Host ""
        Write-Host "  Starting model download... (this takes a while)" -ForegroundColor Yellow
        & "$InstallDir\python\python.exe" "$InstallDir\download_models.py"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [WARN] Download had issues. Retry later with:" -ForegroundColor Yellow
            Write-Host "         python\python.exe download_models.py" -ForegroundColor Gray
        }
    } else {
        Write-Host "  [OK] Skipped. Run download_models.py later." -ForegroundColor Green
    }
} else {
    Write-Host "        OK: Models found, ready to go!" -ForegroundColor Green
}

# --- Self-test ---
Write-Host ""
Write-Host "  [3/4] Running quick self-test..." -ForegroundColor Yellow
& "$InstallDir\python\python.exe" "$InstallDir\selftest.py" --quick
if ($LASTEXITCODE -ne 0) {
    Write-Host "        [WARN] Self-test reported issues." -ForegroundColor Yellow
} else {
    Write-Host "        OK: Self-test passed!" -ForegroundColor Green
}

# --- Done ---
Write-Host ""
Write-Host "  [4/4] Installation complete!" -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  HOW TO USE:" -ForegroundColor White
Write-Host ""
Write-Host "  Desktop shortcuts created:" -ForegroundColor Gray
Write-Host "    DayReplay      → START recording" -ForegroundColor White
Write-Host "    Stop DayReplay → STOP recording" -ForegroundColor White
Write-Host ""
Write-Host "  Your data folder: $InstallDir\data\" -ForegroundColor Gray
Write-Host "  Log files:        $InstallDir\logs\" -ForegroundColor Gray
Write-Host ""
Write-Host "  First timeline appears within minutes of starting." -ForegroundColor Green
Write-Host ""
Read-Host "  Press Enter to exit"

# Cleanup COM object
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($WshShell) | Out-Null
