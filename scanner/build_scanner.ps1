<#
.SYNOPSIS
    Builds the scanner worker (NexusScan64.exe / NexusScan32.exe) next to its
    TWAIN managers, in the layout the program expects.

.DESCRIPTION
    scanner\out\       release build, shipped with the program (scan\ inside it)
    scanner\out-test\  -TestHooks: adds the fake WIA device, used by the tests only

    Layout of either folder:
      NexusScan64.exe, NexusScan32.exe (+ .exe.config)
      x64\TWAINDSM.dll, x86\TWAINDSM.dll

    The 32-bit worker is marked large-address-aware: on 64-bit Windows it then
    has 4 GB of address space, and a 600 dpi colour page (about 100 MB as one
    image) fits even after a long scanning session.
#>
[CmdletBinding()]
param(
    [switch]$TestHooks,
    [string]$Out
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not $Out) { $Out = Join-Path $root ($(if ($TestHooks) { "out-test" } else { "out" })) }

$dotnetRoot = Join-Path $env:USERPROFILE ".dotnet"
if (Test-Path (Join-Path $dotnetRoot "dotnet.exe")) {
    $env:DOTNET_ROOT = $dotnetRoot
    $env:PATH = "$dotnetRoot;$env:PATH"
}

function Set-LargeAddressAware([string]$path) {
    $bytes = [IO.File]::ReadAllBytes($path)
    $pe = [BitConverter]::ToInt32($bytes, 0x3C)
    if ([BitConverter]::ToUInt32($bytes, $pe) -ne 0x00004550) { throw "not a PE file: $path" }
    $at = $pe + 22  # IMAGE_FILE_HEADER.Characteristics
    $flags = [BitConverter]::ToUInt16($bytes, $at) -bor 0x0020
    [BitConverter]::GetBytes([uint16]$flags).CopyTo($bytes, $at)
    [IO.File]::WriteAllBytes($path, $bytes)
}

$project = Join-Path $root "NexusScan\NexusScan.csproj"
$work = Join-Path $env:TEMP ("nexusscan-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $Out, (Join-Path $Out "x64"), (Join-Path $Out "x86") | Out-Null
$hooks = if ($TestHooks) { "true" } else { "false" }
try {
    foreach ($pair in @(@("x64", "NexusScan64"), @("x86", "NexusScan32"))) {
        $arch, $name = $pair
        $log = & dotnet build $project -c Release "-p:Platform=$arch" "-p:AssemblyName=$name" `
            "-p:TestHooks=$hooks" -o (Join-Path $work $arch) -nologo 2>&1
        if ($LASTEXITCODE -ne 0) {
            $log | Select-String ": error " | Select-Object -First 15 | ForEach-Object { Write-Host $_.Line }
            throw "scanner worker build failed: $name"
        }
        Copy-Item (Join-Path $work "$arch\$name.exe") $Out -Force
        Copy-Item (Join-Path $work "$arch\$name.exe.config") $Out -Force
    }
    Set-LargeAddressAware (Join-Path $Out "NexusScan32.exe")
    Copy-Item (Join-Path $root "twain\x64\TWAINDSM.dll") (Join-Path $Out "x64") -Force
    Copy-Item (Join-Path $root "twain\x86\TWAINDSM.dll") (Join-Path $Out "x86") -Force
} finally {
    if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}

# both workers must start and pass their own structure-layout check
foreach ($exe in "NexusScan64.exe", "NexusScan32.exe") {
    # The worker is a windowed program (no console flashes up when the program
    # starts it); PowerShell waits for such a program and captures its output
    # only inside a pipeline, hence Out-String.
    $lines = & (Join-Path $Out $exe) layout | Out-String
    if ($LASTEXITCODE -ne 0 -or -not ($lines -match '"layout","ok":true')) {
        throw "$exe failed its self-check: $lines"
    }
}
Write-Host "    scanner worker: $Out$(if ($TestHooks) { ' (test hooks)' })"
