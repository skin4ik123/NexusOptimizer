<div align="center">

<img src="docs/images/logo.png" width="112" alt="Nexus Optimizer">

# Nexus Optimizer

### Smaller files, same look — on your own computer.

Compress PDFs, photos and video, and scan straight to PDF. Everything runs on
your own processor: no account, no upload, no subscription. Originals are kept
until you say otherwise.

[![Version](https://img.shields.io/github/v/tag/skin4ik123/NexusOptimizer?label=version&sort=semver&color=0e7c6b&style=flat-square)](https://nexus.internetdeco.com) [![Windows](https://img.shields.io/badge/Windows%2010%20%C2%B7%2011-64--bit-0ea5e9?style=flat-square&logo=windows&logoColor=white)](https://nexus.internetdeco.com) [![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/) [![Licence](https://img.shields.io/badge/licence-AGPL--3.0-7c3aed?style=flat-square)](LICENSE) [![Telemetry](https://img.shields.io/badge/telemetry-none-10b981?style=flat-square)](#privacy)

**[Download](https://nexus.internetdeco.com)** · [Website](https://nexus.internetdeco.com) · [Support the work](#support-the-work)

<img src="docs/images/main.png" alt="Nexus Optimizer main window">

</div>

## Support the work

Nexus Optimizer is free and stays free — no subscription, no paid tier, no ads.
It is written by one person in his own time. If it saved you an hour, a coffee is
a fair trade.

<div align="center">

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/clfankitm8) [![Binance Pay](https://img.shields.io/badge/Binance%20Pay-90127003-F0B90B?style=for-the-badge&logo=binance&logoColor=black)](https://nexus.internetdeco.com/#support) [![USDT TRC20](https://img.shields.io/badge/USDT-TRC20-26A17B?style=for-the-badge&logo=tether&logoColor=white)](https://nexus.internetdeco.com/#support)

</div>

| Method | Details |
| --- | --- |
| **Buy Me a Coffee** | [buymeacoffee.com/clfankitm8](https://buymeacoffee.com/clfankitm8) — card, one-off or monthly |
| **Binance Pay** | ID `90127003` |
| **USDT · TRC20** | wallet address below |

```text
TLbuKujXxfZgAobVsQZ9igTFiGCB12x6Tq
```

All methods, with QR codes, are on the
[website](https://nexus.internetdeco.com/#support). Stars and bug reports help
just as much as money.

## What it does

| Feature | Details |
| --- | --- |
| **PDF compression** | A "smart" profile reads the document and picks its own settings. Every image is decided on its own: scans are brought down to the target density by the real placement on the page and encoded at a decent quality, JPEGs already at the target are left alone, 1-bit and transparent images are never touched. The OCR text layer, bookmarks, links and forms survive. |
| **Photos** | Batch resize and recompress, JPEG/PNG/WebP, HEIC and AVIF from phones, ICC profiles kept, EXIF orientation applied. |
| **Video** | H.264, H.265, AV1 and VP9 with GPU encoding — Intel QSV, NVENC, AMF or Media Foundation, detected automatically and falling back to the processor. |
| **Scanning** | WIA and TWAIN, including the 32-bit TWAIN drivers most office MFPs install. Flatbed, document feeder and two-sided scanning with the program's own settings or in the driver's window. Pages appear as they are scanned; skewed pages are straightened, blank backs skipped; pages can be rotated, reordered and survive closing the program. A crashing driver cannot take the program down. |
| **Explorer menu** | "Compress with Nexus Optimizer" on files and folders, plus a "Send to" shortcut that keeps working on Windows 11 24H2. |
| **Originals preserved** | No code path deletes a source file before the result is confirmed written. |
| **Folders, not just files** | Drop a folder, optionally recurse, choose where results go and how they are named. |
| **History** | Every run recorded: what was processed, how much was saved, where the files went. |

Interface in Russian and English, light and dark themes following Windows.

<table>
<tr>
<td width="50%"><img src="docs/images/photos.png" alt="Photo compression"></td>
<td width="50%"><img src="docs/images/video.png" alt="Video compression"></td>
</tr>
<tr>
<td align="center"><b>Photos</b><br>batch resize and recompress, HEIC included</td>
<td align="center"><b>Video</b><br>GPU encoding, detected automatically</td>
</tr>
</table>

## Privacy

Files are processed locally and never uploaded. There is no account, no licence
server and no telemetry. The log stays in `%APPDATA%\NexusOptimizer` and never
contains file contents.

## System requirements

- Windows 10 version 21H2 or Windows 11, 64-bit
- About 700 MB of free disk space
- 4 GB of memory; 8 GB is more comfortable for video
- Nothing to download first: the program carries its own Python and Visual C++
  runtime, and the installer runs on the .NET Framework that is part of Windows
- No internet connection required, during install or afterwards

## Installing

Download `NexusOptimizer-Setup-<version>.exe` from the
[website](https://nexus.internetdeco.com) and run it. It installs for the current
user by default and needs no administrator rights.

Silent install:

```powershell
NexusOptimizer-Setup-3.9.0.exe /S
NexusOptimizer-Setup-3.9.0.exe /S /allusers /nodesktop
NexusOptimizer-Setup-3.9.0.exe /S /dir="C:\Apps\Nexus Optimizer" /nomenu
```

## Building from source

Requires Windows, Python 3.11+, the [.NET 10 SDK](https://dotnet.microsoft.com/download)
(it also builds the .NET Framework 4.6.2 installer and scanner worker) and
[WiX 5](https://wixtoolset.org/) (`dotnet tool install --global wix`).

```powershell
./build_installer.ps1
```

This creates the virtual environment, builds the scanner worker and the
application with PyInstaller,
runs the tests, packages the MSI and produces the branded installer in
`artifacts/`. The version comes from `APP_VERSION` in `NexusPDFOptimizer.py` and
from nowhere else.

To run from source without building:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python NexusPDFOptimizer.py
```

## Licence

Nexus Optimizer is licensed under the **GNU Affero General Public License v3.0** —
see [LICENSE](LICENSE).

The reason is MuPDF: PDF compression uses PyMuPDF by Artifex Software, which is
distributed under the AGPL-3.0. Bundling it means the combined work carries the
same licence. FFmpeg, also bundled, is a GPL-3.0 build and is compatible with it.

In practice: you may use, study, modify and redistribute Nexus Optimizer, and
anyone who distributes a modified version must publish their source under the
same terms.

Third-party components and their licences:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Scanning

Scanning runs in a separate worker, `NexusScan64.exe` and `NexusScan32.exe`
(`scanner/NexusScan`, C# on .NET Framework 4.6.2, which is part of Windows):

- a process can load only TWAIN drivers of its own bitness, and most office
  MFPs still install 32-bit ones — the 32-bit worker reaches them;
- WIA runs on the worker's single-threaded COM apartment, as it must;
- a driver that crashes or hangs takes the worker down, never the program or
  the pages already scanned.

TWAIN and WIA are first-party code written against the public specifications.
Earlier versions used the `pytwain` package, which is GPL-2.0-only and
therefore incompatible with both AGPL-3.0 (PyMuPDF) and GPL-3.0 (FFmpeg).

The TWAIN Data Source Manager is bundled in both bitnesses; provenance and
checksums are in [scanner/twain/README.md](scanner/twain/README.md). For
testing without a scanner there is a virtual one:
[tools/virtual-scanner](tools/virtual-scanner/README.md).

## Known limitations

- The builds are not code-signed, so SmartScreen warns on download. A SHA-256
  checksum is published with every release.
- Video encoding on a machine without a supported GPU falls back to the
  processor and is correspondingly slower.
- Scanning is tested end to end against the virtual scanner (TWAIN, both
  bitnesses) and a simulated WIA device. Real drivers differ in what they
  support; the program falls back to the nearest setting and says so.

---

<div align="center">

Crafted by Artur Yurchuk

</div>
