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
| comtypes | 1.4.16 | WIA scanning, shortcuts | MIT |
| darkdetect | 0.8.0 | following the Windows theme | BSD-3-Clause |
| lxml | 6.1.1 | XMP metadata (via pikepdf) | BSD-3-Clause |
| Python | 3.13 | runtime | PSF-2.0 |

## TWAIN

TWAIN support is a first-party implementation in `NexusPDFOptimizer.py`: a
direct `ctypes` binding to `twaindsm.dll`, written against the public TWAIN
specification. It carries no third-party code and therefore no third-party
conditions.

Earlier versions used the `pytwain` package, which is distributed strictly
under **GPL-2.0** with no "or later" clause. GPL-2.0-only cannot be combined
with AGPL-3.0 (PyMuPDF) or GPL-3.0 (FFmpeg), so shipping it alongside them was
not permitted. Replacing it removed the conflict and kept the feature.

`twaindsm.dll` — the TWAIN Data Source Manager — is shipped alongside the
program and loaded dynamically. It is **not** modified in any way.

| Item | Value |
| --- | --- |
| Version | 2.5.1.0, x64 |
| Licence | LGPL-2.1-or-later |
| Publisher | TWAIN Working Group (Authenticode signature verified) |
| Source | <https://github.com/twain/twain-dsm> |
| Release | <https://github.com/twain/twain-dsm/releases/tag/v2.5.1>, `dsm_020501.zip` |
| SHA-256 | `25869F88A2BC76DED62A13AAEA9F2084F0165BFDBBB7F46B9C3A43298938D467` |

It is bundled because a 64-bit DSM is not present on every Windows machine —
without it TWAIN finds no scanners at all. LGPL-2.1-or-later permits the
upgrade to LGPL-3.0, which is compatible with AGPL-3.0, and the library is used
through dynamic loading, so it may be replaced by the user with any other build
of the same manager.

## Source code for the GPL components

FFmpeg is shipped unmodified. Its corresponding source, together with the exact
build configuration, is published by the build's author at
<https://www.gyan.dev/ffmpeg/builds/> and by the FFmpeg project at
<https://ffmpeg.org/download.html>.

MuPDF source is published by Artifex Software at <https://mupdf.com/releases>.
PyMuPDF source is at <https://github.com/pymupdf/PyMuPDF>.

Nexus Optimizer's own source is at
<https://github.com/skin4ik123/NexusOptimizer>.
