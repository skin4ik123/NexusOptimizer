# twaindsm.dll — where it came from

This binary is not our build, so its provenance is recorded in full: without it
the file can be neither verified nor updated.

| Item | Value |
| --- | --- |
| File | `twaindsm.dll`, 206,240 bytes |
| Version | 2.5.1.0, x64 |
| Source | <https://github.com/twain/twain-dsm/releases/tag/v2.5.1>, archive `dsm_020501.zip`, path `windows/64/TWAINDSM.dll` |
| SHA-256 of the archive | `6D5B520E0D4D524A195AA6ED749EDB09D7118EED96C7DEFB4952BFCDB1590CEE` |
| SHA-256 of the file | `25869F88A2BC76DED62A13AAEA9F2084F0165BFDBBB7F46B9C3A43298938D467` |
| Signature | Valid: `CN=TWAIN Working Group, O=TWAIN Working Group, L=Raeford, S=North Carolina, C=US` |
| Licence | LGPL-2.1-or-later (source in the same repository) |

The file is shipped unmodified.

## Why it is bundled

TWAIN does not work without a Data Source Manager, and a 64-bit `twaindsm.dll`
is not part of Windows and is not installed by every scanner driver. On the
development machine, for example, only the 32-bit copy in `SysWOW64` exists, and
a 64-bit application cannot load it — which is exactly why TWAIN found no
devices in earlier versions.

The program looks for the manager next to itself first, then through the normal
Windows search, so a system copy can be overridden by a local one.

## Updating it

1. Download the new release from `twain/twain-dsm`.
2. Take `windows/64/TWAINDSM.dll`.
3. Check that the signature is valid and the file is x64.
4. Replace the file and update the table above together with
   [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
