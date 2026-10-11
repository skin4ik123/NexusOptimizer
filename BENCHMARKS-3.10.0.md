# JPEG resizing measurement

Local Linux/Python 3.12/Pillow 11.3 measurement, using separate processes for
each pipeline and the median of three runs per round, in two rounds. The 3.9.1 `process_image` function
is compared with 3.10.0; both use JPEG quality 75, progressive/optimized JPEG,
smart classification disabled and a maximum side of 1800 pixels.

The generated 6000 × 4000 JPEG contains noise, coloured shapes and a thin line.
Both results are 1800 × 1200 pixels. Timings include output writing; the new
pipeline additionally decodes and validates its candidate before publishing it.
Peak RSS includes imported application modules and buffers retained by Python.

| Pipeline | Round 1 median | Round 2 median | Peak RSS range | Result bytes |
| --- | ---: | ---: | ---: | ---: |
| 3.9.1 | 0.635 s | 0.670 s | 248.1–249.3 MiB | 292,577 |
| 3.10.0 | 0.355 s | 0.424 s | 129.7–129.8 MiB | 292,525 |

This example is 1.58–1.79 times faster and uses about 48% less peak memory. Outputs
are not pixel-identical because decoder reduction replaces a full-resolution
resize; the output-to-output PSNR is 33.43 dB. These numbers describe this
fixture and machine, not a guaranteed improvement for every photo or computer.
Lossless PNG handling and original JPEG placement in PDFs are checked separately
by regression tests and do not use this lossy resizing path.
