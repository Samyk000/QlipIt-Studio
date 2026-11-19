# Make the Windows app, and the installer that puts it on a computer.
#
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1                 app folder + installer
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -SkipInstaller  app folder only
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -BundleModel    pre-bundle speech model (offline installer)
#
# Ends with: desktop\dist\PeakCut\ (the app itself) and desktop\dist\installer\PeakCut-Setup-1.0.0.exe
param([switch]$SkipInstaller, [switch]$BundleModel)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = Join-Path $root "venv\Scripts\python.exe" }
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }

# ffmpeg: the two programs PeakCut runs, put inside the app so nothing has to be installed beforehand.
$ffmpeg = Join-Path $PSScriptRoot "ffmpeg"
if (-not (Test-Path (Join-Path $ffmpeg "ffmpeg.exe"))) {
    Write-Host "Getting ffmpeg (once, about 90 MB)..."
    $zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
    if (-not (Test-Path $zip)) {
        Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip -UseBasicParsing
    }
    $tmp = Join-Path $env:TEMP "ffmpeg-unpacked"
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive $zip -DestinationPath $tmp
    New-Item -ItemType Directory -Force $ffmpeg | Out-Null
    Get-ChildItem $tmp -Recurse -Include ffmpeg.exe, ffprobe.exe | ForEach-Object { Copy-Item $_.FullName $ffmpeg -Force }
    Remove-Item $tmp -Recurse -Force
}

$modelsDir = Join-Path $PSScriptRoot "models"
if ($BundleModel) {
    Write-Host "Ensuring speech model is available for pre-bundling..."
    & $python -m studio.fetch_model
    $defaultModel = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
    $srcModel = Join-Path $root "data\models\$defaultModel"
    if (Test-Path $srcModel) {
        $destModel = Join-Path $modelsDir $defaultModel
        New-Item -ItemType Directory -Force $destModel | Out-Null
        Copy-Item "$srcModel\*" $destModel -Recurse -Force
    }
}

Write-Host "Building the app folder..."
& $python -m PyInstaller (Join-Path $PSScriptRoot "peakcut.spec") --noconfirm `
    --distpath (Join-Path $PSScriptRoot "dist") --workpath (Join-Path $PSScriptRoot "build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

if ($SkipInstaller) { Write-Host "Done: desktop\dist\QlipIt\"; return }

$iscc = @("$env:ProgramFiles\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Host "Inno Setup is not installed, so the installer was not made (the app folder is ready)."
    Write-Host "Install it once with:  winget install JRSoftware.InnoSetup"
    return
}

Write-Host "Building the installer..."
& $iscc (Join-Path $PSScriptRoot "peakcut.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
Write-Host "Done: desktop\dist\installer\"
