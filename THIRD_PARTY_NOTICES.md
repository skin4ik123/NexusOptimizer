# Third-party components

Nexus Optimizer bundles the components below. Their licence terms are listed
with the exact versions that ship in the installer. Nexus Optimizer itself is
licensed under the **GNU Affero General Public License v3.0** — see
[LICENSE](LICENSE).

## Why the whole program is AGPL-3.0

Two bundled components decide the licence of the combined work:

| Component | Licence | Effect |
| --- | --- | --- |
| PyMuPDF (MuPDF) | AGPL-3.0 or Artifex commercial | Forces AGPL-3.0 on the combined work unless a commercial licence is bought |
| FFmpeg (`--enable-gpl --enable-version3`) | GPL-3.0 | Compatible with AGPL-3.0 |

Everything else is permissive and imposes no further conditions.

## Components

| Component | Version | Purpose | Licence |
| --- | --- | --- | --- |
| PyMuPDF / MuPDF | 1.26.7 | PDF page and image compression | AGPL-3.0 or commercial (Artifex) |
| pikepdf / QPDF | 9.11.0 | PDF structure, linearization, metadata | MPL-2.0 |
| FFmpeg | 7.1 (gyan.dev essentials) | video transcoding | GPL-3.0 |
| imageio-ffmpeg | 0.6.0 | locating and running FFmpeg | BSD-2-Clause |
| Pillow | 11.3.0 | image decoding, scaling, saving | MIT-CMU |
| pillow-heif / libheif | 1.5.0 | HEIC/HEIF/AVIF photos | BSD-3-Clause (libheif: LGPL-3.0) |
| CustomTkinter | 5.2.2 | interface widgets | CC0-1.0 |
| tkinterdnd2 / tkdnd | 0.4.4.1 | drag and drop | MIT |
| comtypes | 1.4.16 | Explorer shortcuts | MIT |
| darkdetect | 0.8.0 | following the Windows theme | BSD-3-Clause |
| lxml | 6.1.1 | XMP metadata (via pikepdf) | BSD-3-Clause |
| Python | 3.13 | runtime | PSF-2.0 |

## Scanning: WIA and TWAIN

Scanning is done by the program's own worker, `NexusScan64.exe` and
`NexusScan32.exe` (`scanner/NexusScan`, C# on .NET Framework 4.6.2, which is
part of Windows). Its TWAIN code is written against the public TWAIN
specification and its WIA code uses the WIA automation library that ships with
Windows. It carries no third-party code and therefore no third-party
conditions.

Earlier versions used the `pytwain` package, which is distributed strictly
under **GPL-2.0** with no "or later" clause. GPL-2.0-only cannot be combined
with AGPL-3.0 (PyMuPDF) or GPL-3.0 (FFmpeg), so shipping it alongside them was
not permitted. Replacing it removed the conflict and kept the feature.

`TWAINDSM.dll` — the TWAIN Data Source Manager — is shipped in both bitnesses
next to the worker (`scan\x64`, `scan\x86`) and loaded dynamically. Neither
file is modified in any way.

| Item | x64 | x86 |
| --- | --- | --- |
| Version | 2.5.1.0 | 2.5.1.0 |
| SHA-256 | `25869F88A2BC76DED62A13AAEA9F2084F0165BFDBBB7F46B9C3A43298938D467` | `CEE9A6FF8026DDE09EDA9F892C57CC928D42E4A4D22BB740FE38EE6E91797A28` |
| Licence | LGPL-2.1-or-later | LGPL-2.1-or-later |
| Publisher | TWAIN Working Group (Authenticode signature verified) | same |
| Source | <https://github.com/twain/twain-dsm> | same |
| Release | <https://github.com/twain/twain-dsm/releases/tag/v2.5.1>, `dsm_020501.zip` | same archive |

They are bundled because a 64-bit manager is not part of Windows, and a 32-bit
one is installed only by some scanner drivers. LGPL-2.1-or-later permits the
upgrade to LGPL-3.0, which is compatible with AGPL-3.0, and the library is used
through dynamic loading, so it may be replaced by the user with any other build
of the same manager. Provenance and the update procedure:
[scanner/twain/README.md](scanner/twain/README.md).

## Source code for the GPL components

FFmpeg is shipped unmodified. Its corresponding source, together with the exact
build configuration, is published by the build's author at
<https://www.gyan.dev/ffmpeg/builds/> and by the FFmpeg project at
<https://ffmpeg.org/download.html>.

MuPDF source is published by Artifex Software at <https://mupdf.com/releases>.
PyMuPDF source is at <https://github.com/pymupdf/PyMuPDF>.

Nexus Optimizer's own source is at
<https://github.com/skin4ik123/NexusOptimizer>.
