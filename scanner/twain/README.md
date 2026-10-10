# TWAINDSM.dll — where it came from

These binaries are not our build, so their provenance is recorded in full:
without it the files can be neither verified nor updated.

| Item | x64\TWAINDSM.dll | x86\TWAINDSM.dll |
| --- | --- | --- |
| Size | 206,240 bytes | 176,544 bytes |
| Version | 2.5.1.0 | 2.5.1.0 |
| Path in the archive | `dsm_020501/windows/64/TWAINDSM.dll` | `dsm_020501/windows/32/TWAINDSM.dll` |
| SHA-256 | `25869F88A2BC76DED62A13AAEA9F2084F0165BFDBBB7F46B9C3A43298938D467` | `CEE9A6FF8026DDE09EDA9F892C57CC928D42E4A4D22BB740FE38EE6E91797A28` |
| Signature | Valid: `CN=TWAIN Working Group, O=TWAIN Working Group, L=Raeford, S=North Carolina, C=US` | same |
| Licence | LGPL-2.1-or-later (source in the same repository) | same |

Archive: <https://github.com/twain/twain-dsm/releases/tag/v2.5.1>,
`dsm_020501.zip`, SHA-256
`6D5B520E0D4D524A195AA6ED749EDB09D7118EED96C7DEFB4952BFCDB1590CEE`.

The files are shipped unmodified.

## Why both are bundled

TWAIN does not work without a Data Source Manager. A 64-bit one is not part of
Windows, and a 32-bit `TWAINDSM.dll` is installed only by some scanner drivers.

A process can load only TWAIN drivers of its own bitness. Most office MFPs
still install 32-bit TWAIN drivers only, which a 64-bit program cannot see at
all — that was why earlier versions "saw no TWAIN scanners". The scanner
worker is therefore built twice, and each build loads the manager from its own
folder by full path (`x64\` for NexusScan64.exe, `x86\` for NexusScan32.exe),
so the bitness always matches and a copy installed by a driver cannot get in
the way. Windows' old `twain_32.dll` is the last resort for the 32-bit worker.

## Updating

1. Download the new release from `twain/twain-dsm`.
2. Take `windows/64/TWAINDSM.dll` and `windows/32/TWAINDSM.dll`.
3. Check that both signatures are valid and the machine types are x64 and x86.
4. Replace the files and update the tables here and in
   [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md).
