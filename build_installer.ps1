<#
.SYNOPSIS
    Builds Nexus Optimizer: application (PyInstaller) -> MSI (WiX) ->
    NexusOptimizerSetup.exe with the MSI embedded.

.DESCRIPTION
    The version comes from a single place: APP_VERSION in NexusPDFOptimizer.py.
    Everything else — file name, MSI version, installer assembly version — is
    derived from it, so the mismatch that makes Windows stop recognising an
    upgrade cannot happen.

.EXAMPLE
    ./build_installer.ps1
    ./build_installer.ps1 -SkipApp     # rebuild the installer only
#>
[CmdletBinding()]
param(
    [switch]$SkipApp,       # do not rebuild the PyInstaller application
    [switch]$KeepArtifacts  # do not clear artifacts before building
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# .NET and its global tools (wix among them) are needed from the MSI step on.
# A user-local .NET installation is not on PATH by default, so add it when it
# exists; a machine-wide install is found without this.
$dotnetRoot = Join-Path $env:USERPROFILE ".dotnet"
if (Test-Path (Join-Path $dotnetRoot "dotnet.exe")) {
    $env:DOTNET_ROOT = $dotnetRoot
    $env:PATH = "$dotnetRoot;$dotnetRoot\tools;$env:PATH"
}

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

# ---- version from the application source ----
$src = Get-Content "NexusPDFOptimizer.py" -Raw -Encoding UTF8
if ($src -notmatch 'APP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"') {
    Fail "APP_VERSION not found in NexusPDFOptimizer.py"
}
$version = $Matches[1]
Write-Host "Nexus Optimizer $version" -ForegroundColor Green

$artifacts = Join-Path $PSScriptRoot "artifacts"
if (-not $KeepArtifacts -and (Test-Path $artifacts)) {
    Remove-Item $artifacts -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $artifacts | Out-Null

# ---- 1. application ----
if (-not $SkipApp) {
    Step 1 "Application (PyInstaller)"
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { Fail "could not create .venv" }
    }
    $py = ".\.venv\Scripts\python.exe"
    & $py -m pip install --upgrade pip wheel --quiet
    & $py -m pip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) { Fail "pip install" }

    & $py make_icon.py
    Get-Process NexusOptimizer, NexusScan64, NexusScan32 -ErrorAction SilentlyContinue | Stop-Process -Force
    if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
    if (Test-Path "dist\NexusOptimizer") { Remove-Item "dist\NexusOptimizer" -Recurse -Force }

    # The scanner worker goes into the bundle as scan\ (see NexusOptimizer.spec);
    # the test build with the fake WIA device is used by the tests only.
    & powershell -NoProfile -ExecutionPolicy Bypass -File "scanner\build_scanner.ps1"
    if ($LASTEXITCODE -ne 0) { Fail "scanner worker" }
    & powershell -NoProfile -ExecutionPolicy Bypass -File "scanner\build_scanner.ps1" -TestHooks
    if ($LASTEXITCODE -ne 0) { Fail "scanner worker (test build)" }

    & $py -m PyInstaller --noconfirm --clean NexusOptimizer.spec
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller" }
} else {
    Step 1 "Application - skipped (-SkipApp)"
}

$payload = Join-Path $PSScriptRoot "dist\NexusOptimizer"
if (-not (Test-Path (Join-Path $payload "NexusOptimizer.exe"))) {
    Fail "no build at dist\NexusOptimizer\NexusOptimizer.exe"
}

# ---- 2. tests ----
Step 2 "Tests"
& ".\.venv\Scripts\python.exe" "tests\test_core.py"
if ($LASTEXITCODE -ne 0) { Fail "tests failed" }
& ".\.venv\Scripts\python.exe" "tests\test_pdf_regressions.py"
if ($LASTEXITCODE -ne 0) { Fail "PDF regression tests failed" }
# Scanner: the worker's test build; the TWAIN tests need the Nexus Virtual
# Scanner (tools\virtual-scanner) and report SKIP without it.
$env:NEXUS_SCAN_HELPERS = Join-Path $PSScriptRoot "scanner\out-test"
& ".\.venv\Scripts\python.exe" "tests\test_scanner.py"
$scanRc = $LASTEXITCODE
$env:NEXUS_SCAN_HELPERS = $null
if ($scanRc -ne 0) { Fail "scanner tests failed" }
& ".\.venv\Scripts\python.exe" "tests\test_optimizer_310.py"
if ($LASTEXITCODE -ne 0) { Fail "media/session regression tests failed" }
& ".\.venv\Scripts\python.exe" "tests\test_lifecycle.py"
if ($LASTEXITCODE -ne 0) { Fail "GUI/cleanup lifecycle tests failed" }
& ".\.venv\Scripts\python.exe" "tests\test_packaged.py" --exe (Join-Path $payload "NexusOptimizer.exe")
if ($LASTEXITCODE -ne 0) { Fail "windowed executable PDF worker tests failed" }


# ---- 3. licence and notices shipped next to the program ----
Step 3 "Licence and notices"
Copy-Item "LICENSE" (Join-Path $payload "LICENSE.txt") -Force
Copy-Item "THIRD_PARTY_NOTICES.md" (Join-Path $payload "THIRD_PARTY_NOTICES.md") -Force
Write-Host "    LICENSE.txt + THIRD_PARTY_NOTICES.md"

# ---- 4. MSI ----
Step 4 "MSI (WiX)"
$assets = Join-Path $PSScriptRoot "installer\Assets"
$msi = Join-Path $artifacts "NexusOptimizer.msi"
& wix build "installer\Msi\NexusOptimizer.wxs" `
    -arch x64 -culture en-US `
    -ext WixToolset.UI.wixext -ext WixToolset.Util.wixext `
    -d "Version=$version" `
    -bindpath "payload=$payload" `
    -bindpath "assets=$assets" `
    -o $msi
if ($LASTEXITCODE -ne 0) { Fail "wix build" }
Write-Host ("    MSI: {0:N1} MB" -f ((Get-Item $msi).Length / 1MB))

# ---- 5. branded installer ----
Step 5 "NexusOptimizerSetup.exe"
# The installer targets .NET Framework 4.6.2, which is part of Windows 10/11:
# a single exe with the MSI embedded, nothing to download on the target PC.
$publish = Join-Path $artifacts "setup-publish"
& dotnet publish "installer\Setup\NexusOptimizerSetup.csproj" `
    -c Release `
    -p:MsiPath=$msi `
    -p:Version=$version `
    -p:InformationalVersion=$version `
    -o $publish
if ($LASTEXITCODE -ne 0) { Fail "dotnet publish" }

$setupName = "NexusOptimizer-Setup-$version.exe"
$setup = Join-Path $artifacts $setupName
Copy-Item (Join-Path $publish "NexusOptimizerSetup.exe") $setup -Force
Remove-Item $publish -Recurse -Force

# ---- 6. summary ----
Step 6 "Done"
$item = Get-Item $setup
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash
Write-Host ""
Write-Host "  $setupName" -ForegroundColor Green
Write-Host ("  size:    {0:N0} bytes ({1:N1} MB)" -f $item.Length, ($item.Length / 1MB))
Write-Host "  SHA-256: $hash"
Write-Host ""
Write-Host "  Silent install:"
Write-Host "    $setupName /S"
Write-Host "    $setupName /S /allusers /nodesktop"
Write-Host "    $setupName /S /dir=`"C:\Apps\Nexus Optimizer`" /nomenu"
Write-Host ""

$sums = Join-Path $artifacts "SHA256SUMS.txt"
"$hash  $setupName" | Set-Content $sums -Encoding ASCII
Write-Host "  checksums: artifacts\SHA256SUMS.txt"
