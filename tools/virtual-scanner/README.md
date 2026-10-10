# Nexus Virtual Scanner

A TWAIN 2.4 data source that behaves like an office MFP, for testing Nexus
Optimizer on a computer without a scanner. **Test equipment only: it is never
shipped with the program.**

It offers what a real device offers: a flatbed and a document feeder with
duplex, colour / greyscale / black-and-white, 75-600 dpi, A4 / Letter / Legal,
native and memory transfers, its own window or none. Every page is drawn on the
fly — a header with the page number and side, lines of "text", a table with a
blue signature and a red stamp, paper tone and noise — so the result can be
checked by eye as well as by size.

## Build and install

Needs Visual Studio with the C++ tools.

```powershell
./build.ps1        # out\x86 and out\x64 NexusVirtualScanner.ds
./install.ps1      # as administrator: copies them to %WINDIR%\twain_32 and twain_64
./install.ps1 -Uninstall
```

The TWAIN manager looks for sources only in `%WINDIR%\twain_32` and
`%WINDIR%\twain_64`, hence the administrator rights. Exactly two files are
copied, and `-Uninstall` removes exactly them.

## Test behaviour

Set through environment variables, which the scanner inherits from the
program that loads it:

| Variable | Effect |
| --- | --- |
| `NEXUS_VSCAN_PAGES=n` | sheets in the feeder (default 3) |
| `NEXUS_VSCAN_EMPTY=1` | the feeder is empty |
| `NEXUS_VSCAN_JAM_AT=n` | paper jam on transfer n |
| `NEXUS_VSCAN_CRASH_AT=n` | access violation on transfer n |
| `NEXUS_VSCAN_HANG_AT=n` | hangs forever on transfer n |
| `NEXUS_VSCAN_SKEW=deg` | pages come out rotated |
| `NEXUS_VSCAN_BLANK_BACKS=1` | back sides are blank (dust and noise only) |
| `NEXUS_VSCAN_UI_AUTO=1` | its own window presses Scan by itself |
| `NEXUS_VSCAN_UI_REQUIRED=1` | refuses to scan without its window |
| `NEXUS_VSCAN_OFFLINE=1` | reports the device offline |
| `NEXUS_VSCAN_LEGACY=1` | TWAIN 1.9 behaviour: no callbacks, DAT_EVENT only |
| `NEXUS_VSCAN_DELAY_MS=n` | time to "scan" one page (default 150) |
| `NEXUS_VSCAN_LOG=path` | protocol log |

`tests/test_scanner.py` uses it in both bitnesses; without it those tests
report SKIP.
