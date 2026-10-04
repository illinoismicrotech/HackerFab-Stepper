@echo off
:: HackerFab Stepper – Windows launcher
:: Double-click this file to start the application.
title HackerFab Stepper

:: Run the PowerShell launcher (creates a Python environment and installs dependencies).
:: Arguments are forwarded, e.g. run.bat -SetupOnly for an install-only check.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

:: Keep the window open if something went wrong
if not "%EXIT_CODE%"=="0" (
    echo.
    echo  Something went wrong. See the error above.
    pause
)

exit /b %EXIT_CODE%
