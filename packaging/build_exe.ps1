param(
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pythonExe = Join-Path $root ".venv/Scripts/python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python venv not found at $pythonExe. Create and install dependencies first."
}

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install pyinstaller

$pyInstallerArgs = @(
    "--name", "MARIE",
    "--noconfirm",
    "--clean",
    "--windowed",
    "main.py"
)

if ($OneFile) {
    $pyInstallerArgs += "--onefile"
} else {
    $pyInstallerArgs += "--onedir"
}

& $pythonExe -m PyInstaller @pyInstallerArgs

Write-Host "Build completed. Output is available under dist/MARIE." -ForegroundColor Green
Write-Host "Note: Keep large runtime folders (models, piper, rvc_models) next to the built app." -ForegroundColor Yellow
