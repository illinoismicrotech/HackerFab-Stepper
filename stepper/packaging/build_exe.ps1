# Builds dist\HackerfabStepper.exe. Run from the stepper folder:  .\packaging\build_exe.ps1
# -SlimOpenCV uses the experimental slim OpenCV (packaging\opencv-slim, ~29 MB exe instead of ~50 MB).
# It is NOT the default: it has not been confirmed on the stepper's camera yet (its one test ran with
# the camera on a USB 2 port, which gave a black picture with either OpenCV).
param([switch]$SlimOpenCV)
# Requires Python 3.10-3.13 (same as run.ps1). Uses its own .venv-build so the normal
# launcher environment (with PyTorch) is untouched.
$ErrorActionPreference = "Stop"
$StepperDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $StepperDir

if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
    # Same supported range as run.ps1 (3.10-3.13); prefer 3.12.
    $PyCmd = $null; $PyArgs = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("3.12", "3.13", "3.11", "3.10")) {
            & py "-$v" --version *> $null
            if ($LASTEXITCODE -eq 0) { $PyCmd = "py"; $PyArgs = @("-$v"); break }
        }
    }
    if (-not $PyCmd) { $PyCmd = "python" }
    & $PyCmd @PyArgs -m venv .venv-build
}
function Invoke-Checked { & $args[0] $args[1..($args.Count - 1)]; if ($LASTEXITCODE -ne 0) { throw "Failed: $args" } }
Invoke-Checked .venv-build\Scripts\python.exe -m pip install --upgrade pip
Invoke-Checked .venv-build\Scripts\python.exe -m pip install --prefer-binary -r packaging\requirements-exe.txt
# Optional slim OpenCV (15 MB instead of ~105 MB; see packaging\build_opencv_slim.sh). Python 3.12 only.
$Slim = "packaging\opencv-slim\cv2.cp312-win_amd64.pyd"
$PyVer = & .venv-build\Scripts\python.exe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
if ($SlimOpenCV -and (Test-Path $Slim) -and ($PyVer -eq "3.12")) {
    Write-Host "Using the slim OpenCV build" -ForegroundColor Yellow
    Invoke-Checked .venv-build\Scripts\python.exe -m pip uninstall -y opencv-python opencv-python-headless
    $Site = & .venv-build\Scripts\python.exe -c "import sysconfig; print(sysconfig.get_paths()['platlib'])"
    Copy-Item $Slim $Site -Force
} else {
    Write-Host "Using the regular opencv-python wheel" -ForegroundColor Yellow
}
Invoke-Checked .venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\HackerfabStepper.spec

Write-Host "`nSmoke test (builds the UI with no hardware, checks camera worker processes)..." -ForegroundColor Yellow
$p = Start-Process -FilePath "dist\HackerfabStepper.exe" -ArgumentList "--smoke-test" -Wait -PassThru
Get-Content "dist\HackerfabStepper.log" -Tail 8 -ErrorAction SilentlyContinue
if ($p.ExitCode -ne 0) { throw "Smoke test failed (exit code $($p.ExitCode))" }
# Remove files the smoke test created so dist\ holds only the exe
Remove-Item "dist\HackerfabStepper.log", "dist\default.toml", "dist\smoke-test-config.toml", "dist\HOW-TO-GUIDE.html" -ErrorAction SilentlyContinue
Remove-Item "dist\src", "dist\stepper_captures" -Recurse -ErrorAction SilentlyContinue
Write-Host "`nBuilt dist\HackerfabStepper.exe" -ForegroundColor Green
