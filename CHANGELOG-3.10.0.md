# Nexus Optimizer 3.10.0

- Normal close cancels and waits for writers, then clears file lists, scan-session storage, session history and temporary candidates on all destination volumes. Imported and exported files are preserved. Abandoned runs are cleaned on startup after recovering received scans that had not yet been processed; interrupted scan sessions can be recovered after a crash.
- Scan preprocessing retains a lossless original. Suspected blank, failed and removed pages can be restored or saved separately. Faint pencil strokes survive blank-page detection and black-and-white conversion. Manual rotations use metadata, avoiding repeated JPEG encoding. Restore Original undoes preprocessing and rotation.
- JPEG resizing reduces pixels inside the decoder. Lossless PNG scans replace large intermediate BMPs; unchanged colour scans use a single stored raster. Palette/greyscale PNG transparency and 16-bit PNG values are preserved. All eight EXIF orientations, including mirrored ones, are applied in PDFs while retaining the original encoded JPEG stream.
- Native PDF batches use persistent isolated workers in both source and windowed installed builds. Worker count and raster queues respect available RAM. Candidate PDFs keep text, links, bookmarks and image streams and are checked before publishing.
- PDF/photo target sizes have a quality floor and explicit unreachable-target results. Photos have Fast, Balanced and Maximum modes, with lossless PNG palette conversion only when pixel-identical. Animated files keep their original frames.
- Before/after comparison runs asynchronously. Sections keep their lists when switching during a session. Settings writes use unique candidates; scan presets use language-independent keys and device-specific profiles. Local scanner diagnostics can be saved on request.
- Video never upscales a smaller source, preserves alternate audio tracks, checks duration and decoded frames, keeps existing output on failure, and offers optional metadata removal.
- Verification includes PDF repeated-pass/data-preservation regressions, real 32/64-bit virtual TWAIN tests, GUI close/reopen and close-during-write tests, and the windowed executable's PDF workers. Physical Epson hardware remains a separate driver check.

JPEG resizing measurement: [BENCHMARKS-3.10.0.md](BENCHMARKS-3.10.0.md).
