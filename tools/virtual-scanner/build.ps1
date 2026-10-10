<#
.SYNOPSIS
    Builds the Nexus Virtual Scanner (a test TWAIN data source) for x86 and x64.

.DESCRIPTION
    Needs Visual Studio with the C++ tools; found through vswhere. The result
    is out\x86\NexusVirtualScanner.ds and out\x64\NexusVirtualScanner.ds.
    install.ps1 copies them to where the TWAIN manager looks for sources.
    Test equipment only: never shipped with the program.
#>
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere.exe not found: Visual Studio is not installed" }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw "Visual Studio C++ tools (VC.Tools.x86.x64) not found" }
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvarsall.bat"

foreach ($arch in "x86", "x64") {
    $out = Join-Path $PSScriptRoot "out\$arch"
    New-Item -ItemType Directory -Force $out | Out-Null
    $obj = Join-Path $out "obj"
    New-Item -ItemType Directory -Force $obj | Out-Null
    $cmd = "`"$vcvars`" $arch >nul && cl /nologo /LD /O2 /W4 /WX /utf-8 /MT /D_CRT_SECURE_NO_WARNINGS " +
           "/Fo`"$obj\\`" nexusvscan.c /link /DEF:nexusvscan.def /OUT:`"$out\NexusVirtualScanner.ds`" " +
           "/IMPLIB:`"$obj\NexusVirtualScanner.lib`" user32.lib gdi32.lib kernel32.lib"
    cmd /c $cmd
    if ($LASTEXITCODE -ne 0) { throw "build failed for $arch" }
    Write-Host "built $out\NexusVirtualScanner.ds"
}
