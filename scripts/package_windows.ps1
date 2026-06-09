$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$iconPath = Join-Path $root "assets\icons\expense_manager_matte.ico"
if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "Missing installer icon: $iconPath"
}

$inVirtualEnv = python -c "import sys; print(str(sys.prefix != sys.base_prefix).lower())"
$pipArgs = @("install", "-r", "requirements.txt", "pyinstaller")
if ($inVirtualEnv.Trim() -ne "true") {
    $pipArgs = @("install", "--user", "-r", "requirements.txt", "pyinstaller")
}

python -m pip @pipArgs
if ($LASTEXITCODE -ne 0) {
    throw "Dependency install failed."
}

python -m PyInstaller --clean expense_manager_pyqt.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$exePath = Join-Path $root "dist\expense_manager_pyqt.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Package build did not produce: $exePath"
}

Write-Host "Packaged Expense Manager: $exePath"
Write-Host "Embedded installer icon: $iconPath"
