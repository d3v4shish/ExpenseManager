$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $env:UV_CACHE_DIR) {
    $env:UV_CACHE_DIR = Join-Path $root "build\uv-cache"
}
if (-not $env:PYINSTALLER_CONFIG_DIR) {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $root "build\pyinstaller-cache"
}

$iconPath = Join-Path $root "assets\icons\expense_manager_matte.ico"
if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "Missing installer icon: $iconPath"
}

uv sync --frozen --extra dev
if ($LASTEXITCODE -ne 0) {
    throw "Locked dependency install failed. Install uv from https://docs.astral.sh/uv/ and retry."
}

uv run pyinstaller --noconfirm --clean expense_manager_pyqt.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$exePath = Join-Path $root "dist\ExpenseManager\ExpenseManager.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Package build did not produce: $exePath"
}

Write-Host "Packaged Expense Manager: $exePath"
Write-Host "Embedded installer icon: $iconPath"

$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if ($null -ne $iscc) {
    & $iscc.Source (Join-Path $root "installer\windows\ExpenseManager.iss")
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup installer build failed."
    }
    Write-Host "Built Windows installer under dist\installer."
} else {
    Write-Host "Inno Setup not found; skipped installer wrapper and kept the executable directory."
}
