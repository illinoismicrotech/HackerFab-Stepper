# Builds dist\HackerfabStepper.exe. Run from the stepper folder:  .\packaging\build_exe.ps1
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
Invoke-Checked .venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\HackerfabStepper.spec

Write-Host "`nSmoke test (builds the UI with no hardware, checks camera worker processes)..." -ForegroundColor Yellow
$p = Start-Process -FilePath "dist\HackerfabStepper.exe" -ArgumentList "--smoke-test" -Wait -PassThru
Get-Content "dist\HackerfabStepper.log" -Tail 8 -ErrorAction SilentlyContinue
if ($p.ExitCode -ne 0) { throw "Smoke test failed (exit code $($p.ExitCode))" }
# Remove files the smoke test created so dist\ holds only the exe
Remove-Item "dist\HackerfabStepper.log", "dist\default.toml", "dist\smoke-test-config.toml", "dist\HOW-TO-GUIDE.html" -ErrorAction SilentlyContinue
Remove-Item "dist\src", "dist\stepper_captures" -Recurse -ErrorAction SilentlyContinue
Write-Host "`nBuilt dist\HackerfabStepper.exe" -ForegroundColor Green
