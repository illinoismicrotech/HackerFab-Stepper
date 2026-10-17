# Install or update the HackerFab Stepper app on Windows.
#
#   irm https://raw.githubusercontent.com/illinoismicrotech/HackerFab-Stepper/main/install.ps1 | iex
#
# Downloads HackerfabStepper-Windows.exe from the GitHub releases, checks its SHA-256 and installs
# it to %LOCALAPPDATA%\Programs\HackerfabStepper with Start menu and desktop shortcuts. Running it
# again updates the app; settings, logs and snapshots in that folder are kept.
#
# Options (when saved as a file: .\install.ps1 -Release -Dir D:\Stepper):
#   -Release   newest numbered release instead of the Latest build of main
#   -Dir       install somewhere else
param(
    [switch]$Release,
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "Programs\HackerfabStepper")
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is many times slower with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "illinoismicrotech/HackerFab-Stepper"
$Asset = "HackerfabStepper-Windows.exe"
$Exe = Join-Path $Dir "HackerfabStepper.exe"

function Fail($message) {
    # throw, not exit: run through "irm ... | iex", exit would close the user's PowerShell window
    throw "HackerFab Stepper installer: $message"
}

Write-Host ""
Write-Host "  HackerFab Stepper installer"
Write-Host "  ---------------------------"

# -- Find the download -------------------------------------------------------
$api = "https://api.github.com/repos/$Repo/releases/tags/latest"
if ($Release) { $api = "https://api.github.com/repos/$Repo/releases/latest" }
try {
    $headers = @{ "User-Agent" = "HackerfabStepper-installer" }
    if ($env:GITHUB_TOKEN) { $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
    $info = Invoke-RestMethod -Uri $api -Headers $headers
} catch {
    Fail "could not get the release from GitHub ($api). Check the internet connection; with -Release, there may be no numbered release yet."
}
$file = $info.assets | Where-Object { $_.name -eq $Asset } | Select-Object -First 1
if (-not $file) { Fail "this release has no $Asset (it may still be building; try again in a few minutes)." }
if (-not ($file.digest -like "sha256:*")) { Fail "GitHub published no checksum for $Asset, so it cannot be verified." }
$sha = $file.digest.Substring(7)

Write-Host "  Release:  $($info.tag_name)"
Write-Host "  File:     $Asset ($([math]::Round($file.size / 1MB)) MB)"
Write-Host "  Into:     $Dir"
Write-Host ""

# -- Download and verify -----------------------------------------------------
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$download = Join-Path $Dir ".HackerfabStepper-update-$Asset"
Write-Host "  Downloading..."
try {
    Invoke-WebRequest -Uri $file.browser_download_url -OutFile $download -UseBasicParsing
} catch {
    Fail "the download failed: $($_.Exception.Message)"
}
$got = (Get-FileHash -Algorithm SHA256 -Path $download).Hash.ToLower()
if ($got -ne $sha.ToLower()) {
    Remove-Item -Force $download
    Fail "the download is damaged (checksum mismatch). Nothing was changed."
}
Write-Host "  Checksum OK"

# -- Install: a running copy can be renamed but not overwritten --------------
$old = Join-Path $Dir "HackerfabStepper.old.exe"
Remove-Item -Force -ErrorAction SilentlyContinue $old
if (Test-Path $Exe) {
    try { Move-Item -Force $Exe $old } catch { Remove-Item -Force $download; Fail "close HackerFab Stepper and run this again." }
}
Move-Item -Force $download $Exe
Remove-Item -Force -ErrorAction SilentlyContinue $old   # still running: the app deletes it on its next start

# -- Shortcuts -----------------------------------------------------------------
$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))
foreach ($place in $places) {
    $link = $shell.CreateShortcut((Join-Path $place "HackerFab Stepper.lnk"))
    $link.TargetPath = $Exe
    $link.WorkingDirectory = $Dir
    $link.Save()
}

Write-Host "  Installed $Exe"
Write-Host "  Start it from the Start menu or the desktop shortcut (HackerFab Stepper)."
Write-Host ""
Write-Host "  Updates: the app checks GitHub once a day (Settings -> Updates), or run this again."
Write-Host ""
