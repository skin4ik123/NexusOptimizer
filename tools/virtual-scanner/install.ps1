<#
.SYNOPSIS
    Installs (or with -Uninstall removes) the Nexus Virtual Scanner test device.

.DESCRIPTION
    The TWAIN manager looks for data sources only in %WINDIR%\twain_32 and
    %WINDIR%\twain_64, so this needs administrator rights. Two files are
    copied, nothing else on the system changes; -Uninstall deletes exactly them.
    Writes the outcome to %TEMP%\nexus_vscan_install.txt for scripts that start
    it elevated and cannot see its output.
#>
param([switch]$Uninstall)

$marker = Join-Path $env:TEMP "nexus_vscan_install.txt"
try {
    $targets = @{
        "x86" = Join-Path $env:WINDIR "twain_32\NexusVirtualScanner"
        "x64" = Join-Path $env:WINDIR "twain_64\NexusVirtualScanner"
    }
    foreach ($arch in $targets.Keys) {
        $dir = $targets[$arch]
        if ($Uninstall) {
            if (Test-Path $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
            # twain_64 did not exist before: remove it again if we left it empty
            $parent = Split-Path $dir -Parent
            if ((Test-Path $parent) -and -not (Get-ChildItem $parent -Force) -and $parent -like "*twain_64") {
                Remove-Item -LiteralPath $parent -Force
            }
        } else {
            $src = Join-Path $PSScriptRoot "out\$arch\NexusVirtualScanner.ds"
            if (-not (Test-Path $src)) { throw "missing $src - run build.ps1 first" }
            New-Item -ItemType Directory -Force $dir | Out-Null
            Copy-Item $src (Join-Path $dir "NexusVirtualScanner.ds") -Force
        }
    }
    $state = if ($Uninstall) { "UNINSTALLED" } else { "INSTALLED" }
    Set-Content $marker "OK $state" -Encoding ascii
} catch {
    Set-Content $marker ("FAIL " + $_.Exception.Message) -Encoding ascii
    exit 1
}
