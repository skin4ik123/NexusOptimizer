"""Safe media processing and lossless placement of oriented scan images."""
from __future__ import annotations

import base64
import io
import math
import os
import time
from pathlib import Path

from PIL import Image, ImageOps, ImageStat
from nexus_support import AtomicOutput, ProcessingCancelled, paths_alias, available_memory


def _image_ok(path, expected_size):
    try:
        with Image.open(path) as image:
            image.load()
            return image.size == expected_size
    except (OSError, ValueError):
        return False


def compress_image(ctx, inp, out, quality, max_side, fmt, smart, effort="balanced", target_bytes=0):
    orig = 0
    try:
        if not os.path.isfile(inp):
            return inp, False, "err_not_found", 0, 0
        orig = os.path.getsize(inp)
        fmt = fmt.upper()
        fmt = "JPEG" if fmt in ("JPEG", "JPG") else "WEBP" if fmt == "WEBP" else "PNG"
        suffixes = {"JPEG": (".jpg", ".jpeg"), "WEBP": (".webp",), "PNG": (".png",)}[fmt]
        if not out.lower().endswith(suffixes):
            out = os.path.splitext(out)[0] + suffixes[0]
        if paths_alias(inp, out):
            return inp, False, "err_same_file", orig, orig
        if ctx.stop_flag.is_set():
            return inp, False, "stopped", orig, 0
        if not ctx.ensure_disk_space(out, max(orig, 10 << 20)):
            return inp, False, "err_no_space", orig, 0
        maximum = ctx.safe_int(max_side, 0, 0, 20000)
        with Image.open(inp) as original:
            if getattr(original, "n_frames", 1) > 1:
                return inp, False, "err_animated", orig, orig
            original_mode = original.mode
            icc = original.info.get("icc_profile")
            dpi = original.info.get("dpi")
            # JPEG decoder reduction happens before a full raster is allocated.
            if maximum and original.format == "JPEG":
                original.draft(original.mode, (maximum, maximum))
            if original.width * original.height * 12 > available_memory() * 0.65:
                return inp, False, "err_image_memory", orig, orig
            image = ImageOps.exif_transpose(original)
            image.load()
        original.close()
        angle = ctx.ScanSessionStore.rotation(inp)
        operation = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
                     270: Image.Transpose.ROTATE_270}.get(angle)
        if operation:
            image = image.transpose(operation)
        if original_mode == "CMYK":
            try:
                from PIL import ImageCms
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc)) if icc else None
                srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
                image = ImageCms.profileToProfile(image, profile, srgb, outputMode="RGB") if profile else image.convert("RGB")
                icc = srgb.tobytes() if profile else None
            except Exception:
                image = image.convert("RGB")
                icc = None  # A CMYK profile must never be attached to RGB pixels.
        if fmt == "JPEG" and image.mode in ("RGBA", "LA", "P", "PA"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        elif image.mode in ("P", "PA", "LA"):
            if fmt != "PNG" or image.mode == "PA":
                image = image.convert("RGBA" if image.mode in ("PA", "LA") or "transparency" in image.info else "RGB")
        elif image.mode in ("I", "I;16", "I;16L", "I;16B", "F"):
            if fmt != "PNG" or image.mode == "F" or not (0 <= image.getextrema()[0] <= image.getextrema()[1] <= 65535):
                return inp, False, "err_bit_depth", orig, orig
            image = image.convert("I;16")
        elif image.mode not in ("RGB", "L", "RGBA"):
            image = image.convert("RGB")
        if maximum and max(image.size) > maximum:
            if image.mode == "I;16":
                image = image.convert("I")
                image.thumbnail((maximum, maximum), Image.Resampling.BILINEAR)
                image = image.convert("I;16")
            else:
                image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS, reducing_gap=2.0)
        small = image.convert("RGBA").convert("RGB").resize((48, 48), Image.Resampling.BILINEAR) if smart else None
        photo_like = not smart or sum(ImageStat.Stat(small).stddev) > 42 or (small.getcolors(600) is None)
        q = max(15, min(95, ctx.safe_int(quality, 75)))
        if smart and not photo_like and fmt == "JPEG":
            q = min(90, q + 8)
        effort = effort if effort in ("fast", "balanced", "smallest") else "balanced"
        if fmt == "PNG" and smart and not photo_like and image.mode in ("RGB", "RGBA"):
            # Palette conversion is lossless only when the image actually fits.
            colors = image.getcolors(256)
            if colors is not None:
                palette = image.quantize(256, method=Image.Quantize.FASTOCTREE)
                from PIL import ImageChops
                difference = ImageChops.difference(image.convert("RGBA"), palette.convert("RGBA"))
                if all(channel.getbbox() is None for channel in difference.split()):
                    image = palette

        def encode(level):
            if ctx.stop_flag.is_set():
                raise ProcessingCancelled()
            options = dict(format=fmt)
            if fmt == "JPEG":
                options.update(quality=level, optimize=effort != "fast", progressive=effort != "fast",
                               subsampling=0 if level >= 90 else 2)
            elif fmt == "WEBP":
                options.update(quality=level, method={"fast": 2, "balanced": 4, "smallest": 6}[effort])
            else:
                options.update(compress_level={"fast": 2, "balanced": 6, "smallest": 9}[effort],
                               optimize=effort == "smallest")
            if icc:
                options["icc_profile"] = icc
            if dpi and fmt in ("JPEG", "PNG"):
                options["dpi"] = dpi
            data = io.BytesIO()
            image.save(data, **options)
            return data.getvalue()

        encoded = encode(q)
        if target_bytes and len(encoded) > target_bytes and fmt in ("JPEG", "WEBP"):
            low, high = min(50, q), q
            smallest = encode(low)
            encoded = smallest
            if len(smallest) <= target_bytes:
                for _ in range(5):
                    if high - low <= 1:
                        break
                    middle = (high + low) // 2
                    candidate = encode(middle)
                    if len(candidate) <= target_bytes:
                        low, encoded = middle, candidate
                    else:
                        high = middle
        if ctx.stop_flag.is_set():
            raise ProcessingCancelled()
        if len(encoded) >= orig * 0.98 and ctx._same_format_family(inp, fmt):
            status = "err_target_unreachable" if target_bytes and orig > target_bytes else "err_no_gain"
            return inp, False, status, orig, orig
        with AtomicOutput(out, ctx.TEMP_DIR, ctx.stop_flag) as stage:
            Path(stage.path).write_bytes(encoded)
            if not _image_ok(stage.path, image.size):
                return inp, False, "err_bad_result", orig, 0
            stage.commit()
        status = "OK_TARGET_MISSED" if target_bytes and len(encoded) > target_bytes else "OK"
        return inp, True, status, orig, len(encoded)
    except ProcessingCancelled:
        return inp, False, "stopped", orig, 0
    except PermissionError:
        return inp, False, "err_no_access", orig, 0
    except Exception as error:
        ctx._log_exc("process image", error)
        return inp, False, str(error)[:120], orig, 0


# EXIF transformation = rotation followed by a possible reflection.
_ORIENTATIONS = {1: (0, None), 2: (0, "h"), 3: (180, None), 4: (0, "v"),
                 5: (90, "v"), 6: (270, None), 7: (270, "v"), 8: (90, None)}


def images_to_pdf(ctx, paths, output_pdf, progress_cb=None):
    if not ctx.fitz:
        return False, "err_pymupdf", 0, len(paths)
    if any(paths_alias(path, output_pdf) for path in paths):
        return False, "err_same_file", 0, len(paths)
    added = skipped = 0
    doc = ctx.fitz.open()
    try:
        for index, path in enumerate(paths, 1):
            start = doc.page_count
            try:
                if ctx.stop_flag.is_set():
                    raise ProcessingCancelled()
                with Image.open(path) as original:
                    frames = getattr(original, "n_frames", 1)
                    for frame_index in range(frames):
                        if ctx.stop_flag.is_set():
                            raise ProcessingCancelled()
                        original.seek(frame_index)
                        width, height = original.size
                        orientation = int(original.getexif().get(274, 1))
                        angle, reflection = _ORIENTATIONS.get(orientation, (0, None))
                        user_rotation = ctx.ScanSessionStore.rotation(path)
                        angle = (angle + user_rotation) % 360
                        if reflection and user_rotation % 180:
                            reflection = "v" if reflection == "h" else "h"
                        if angle % 180:
                            width, height = height, width
                        dpi = original.info.get("dpi")
                        density = float(dpi[0]) if dpi and float(dpi[0]) >= 50 else 0
                        if density:
                            page_width, page_height = width * 72 / density, height * 72 / density
                        else:
                            page_width, page_height = (842, 595) if width > height else (595, 842)
                        page = doc.new_page(width=page_width, height=page_height)
                        if frames == 1 and original.format in ("JPEG", "PNG", "JPEG2000"):
                            page.insert_image(page.rect, filename=path, rotate=angle, keep_proportion=True)
                        else:
                            # The generic path also handles TIFF/BMP/HEIF safely.
                            raster = original.copy()
                            if raster.mode not in ("1", "L", "RGB", "RGBA"):
                                raster = raster.convert("RGB")
                            data = io.BytesIO()
                            raster.save(data, "PNG", compress_level=2)
                            page.insert_image(page.rect, stream=data.getvalue(), rotate=angle, keep_proportion=True)
                        if reflection:
                            matrix = f"-1 0 0 1 {page_width} 0" if reflection == "h" else f"1 0 0 -1 0 {page_height}"
                            xref = page.get_contents()[0]
                            doc.update_stream(xref, f"q {matrix} cm\n".encode() + doc.xref_stream(xref) + b"\nQ")
                added += doc.page_count - start
            except ProcessingCancelled:
                raise
            except Exception as error:
                # A failed insertion must not leave an uncounted empty page.
                while doc.page_count > start:
                    doc.delete_page(doc.page_count - 1)
                skipped += 1
                ctx._log_exc(f"images to PDF {Path(path).name}", error)
            finally:
                if progress_cb:
                    progress_cb(index, max(1, len(paths)))
        if not added:
            return False, "err_add_pages", 0, skipped
        with AtomicOutput(output_pdf, ctx.TEMP_DIR, ctx.stop_flag) as stage:
            doc.save(stage.path, garbage=4, deflate=True)
            if not ctx._pdf_output_ok(stage.path, added):
                return False, "err_bad_result", 0, len(paths)
            stage.commit()
        return True, "OK", added, skipped
    except ProcessingCancelled:
        return False, "stopped", 0, len(paths)
    except Exception as error:
        return False, str(error)[:120], 0, len(paths)
    finally:
        doc.close()


def compress_pdf(ctx, inp, out, preset, dpi, quality, gray, resolved=None,
                 progress_cb=None, threads=1, target_bytes=0):
    if not target_bytes:
        return ctx._process_pdf_once(inp, out, preset, dpi, quality, gray, resolved, progress_cb, threads)
    orig = os.path.getsize(inp) if os.path.isfile(inp) else 0
    if paths_alias(inp, out):
        return inp, False, "err_same_file", orig, orig
    if not orig:
        return inp, False, "err_not_found", 0, 0
    if orig <= target_bytes:
        return inp, False, "err_no_gain", orig, orig
    info = ctx.analyze_pdf(inp)
    rdpi, rq, structure = ctx.resolve_pdf_params(preset, dpi, quality, info)
    if resolved:
        rdpi, rq, structure = resolved["dpi"], resolved["quality"], resolved["structure_only"]
    # Target mode has a readability floor, and always retries the original.
    # It never accumulates JPEG generations or reduces 1-bit text scans.
    rdpi = max(130, rdpi)
    qualities = list(dict.fromkeys((max(50, rq), max(50, min(rq, 60)), 50)))
    best_size = orig
    best = None
    stages = []
    try:
        for index, level in enumerate(qualities):
            stage = AtomicOutput(out, ctx.TEMP_DIR, ctx.stop_flag)
            stage.__enter__()
            stages.append(stage)
            parameters = {"dpi": rdpi, "quality": level, "structure_only": structure,
                          "use_gray": bool(gray and info.get("is_scanned_like"))}
            result = ctx._process_pdf_once(
                inp, stage.path, preset, rdpi, level, gray, parameters,
                (lambda fraction, i=index: progress_cb((i + fraction) / len(qualities))) if progress_cb else None,
                threads)
            if result[2] == "stopped":
                return result
            if result[1] and result[4] < best_size:
                best, best_size = stage, result[4]
            elif not result[1] and result[2] != "err_no_gain":
                return result
            if best_size <= target_bytes or structure:
                break
        if best is None:
            return inp, False, "err_target_unreachable", orig, orig
        best.commit()
        if progress_cb:
            progress_cb(1.0)
        return inp, True, "OK" if best_size <= target_bytes else "OK_TARGET_MISSED", orig, best_size
    except ProcessingCancelled:
        return inp, False, "stopped", orig, 0
    except Exception as error:
        ctx._log_exc("target PDF", error)
        return inp, False, str(error)[:120], orig, 0
    finally:
        for stage in stages:
            stage.__exit__(None, None, None)


def pdf_preview(ctx, payload):
    """Compare actual first-page compression in the native worker process."""
    ctx.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    original = str(ctx.TEMP_DIR / f"preview_{time.time_ns()}.pdf")
    output = str(ctx.TEMP_DIR / f"preview_result_{time.time_ns()}.pdf")
    try:
        with ctx.fitz.open(payload["input"]) as source:
            page_index = max(0, min(int(payload.get("page", 0)), source.page_count - 1))
            with ctx.fitz.open() as sample:
                sample.insert_pdf(source, from_page=page_index, to_page=page_index)
                sample.save(original)
            pages = source.page_count
        target = int(payload.get("target_bytes", 0))
        sample_target = max(1, int(target * os.path.getsize(original) / os.path.getsize(payload["input"]))) if target else 0
        result = ctx.process_pdf(original, output, payload["preset"], payload["dpi"],
                                 payload["quality"], payload.get("gray", False), target_bytes=sample_target)
        if not result[1] and result[2] not in ("err_no_gain", "err_target_unreachable"):
            raise ValueError(result[2])
        images = []
        for path in (original, output if result[1] else original):
            with ctx.fitz.open(path) as document:
                page = document[0]
                # Two worst-case RGB PNGs plus base64 fit the 32 MiB IPC budget.
                zoom = min(2.0, 1800 / max(page.rect.width, page.rect.height))
                pixels = page.get_pixmap(matrix=ctx.fitz.Matrix(zoom, zoom), alpha=False)
                images.append(base64.b64encode(pixels.tobytes("png")).decode("ascii"))
        return {"images": images, "original_bytes": result[3], "new_bytes": result[4],
                "pages": pages, "page": page_index, "sample": True, "status": result[2]}
    finally:
        Path(original).unlink(missing_ok=True)
        Path(output).unlink(missing_ok=True)


def compress_video(ctx, inp, out, codec, crf, preset, max_height, audio_kbps,
                   container="MP4", use_hw=True, progress_cb=None, strip_metadata=True):
    orig = 0
    try:
        if not os.path.isfile(inp):
            return inp, False, "err_not_found", 0, 0
        orig = os.path.getsize(inp)
        out, codec = ctx._resolve_video_out(out, codec, container)
        if paths_alias(inp, out):
            return inp, False, "err_same_file", orig, orig
        if ctx.stop_flag.is_set():
            return inp, False, "stopped", orig, 0
        if not ctx.ensure_disk_space(out, int(orig * 1.3)):
            return inp, False, "err_no_space", orig, 0
        ff = ctx.find_ffmpeg()
        if not ff:
            return inp, False, "err_no_ffmpeg", orig, 0
        crf = max(14, min(40, int(crf)))
        audio_kbps = max(0, min(320, int(audio_kbps)))
        cont = Path(out).suffix.lower()
        duration = ctx._probe_duration(ff, inp)
        attempts = ctx.working_hw_encoders(codec) if use_hw else []
        attempts = list(attempts) + [None] + (["libaom"] if codec == "AV1" else [])
        with AtomicOutput(out, ctx.TEMP_DIR, ctx.stop_flag) as stage:
            code, error = -1, b""
            for attempt, encoder in enumerate(attempts):
                stage.check_cancelled()
                command = [ff, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                           "-progress", "pipe:1", "-stats_period", "0.2", "-i", inp,
                           "-map", "0:v:0", "-map_metadata", "-1" if strip_metadata else "0"]
                if cont == ".mp4":
                    command += ["-movflags", "+faststart"]
                command += ctx._video_codec_args(codec, encoder, crf, preset or "medium", cont)
                if max_height and int(max_height) > 0:
                    from nexus_support import limited_video_scale
                    command += ["-vf", limited_video_scale(max(2, int(max_height)))]
                else:
                    command += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
                if audio_kbps <= 0:
                    command += ["-an"]
                else:
                    # Preserve alternate audio tracks instead of silently taking one.
                    command += ["-map", "0:a?", "-c:a", "libopus" if cont == ".webm" else "aac",
                                "-b:a", f"{audio_kbps}k"]
                if cont == ".mkv":
                    command += ["-map", "0:s?", "-c:s", "copy"]
                command.append(stage.path)
                label = f"{encoder or 'cpu'} "
                code, error = ctx._run_ff(command, label, duration, progress_cb)
                stage.check_cancelled()
                if code == 0 and os.path.getsize(stage.path) > 1000:
                    break
                ctx.log.warning("encoder %s failed: %s", encoder or "software",
                                error[-300:].decode("utf-8", "ignore"))
                if progress_cb and attempt + 1 < len(attempts):
                    progress_cb(0.01, ctx.t("hw_fallback"))
            if code or os.path.getsize(stage.path) <= 1000:
                return inp, False, "FFmpeg: " + error[-220:].decode("utf-8", "ignore"), orig, 0
            # Validate duration and decoded first/last frames before publishing.
            actual_duration = ctx._probe_duration(ff, stage.path)
            if duration and (not actual_duration or abs(actual_duration - duration) > max(0.25, duration * 0.01)):
                return inp, False, "err_bad_result", orig, 0
            for seek in ([], ["-sseof", "-0.5"]):
                command = [ff, "-nostdin", "-hide_banner", "-loglevel", "error",
                           "-progress", "pipe:1", *seek, "-i", stage.path,
                           "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"]
                code, _ = ctx._run_ff(command, "", 0, None)
                stage.check_cancelled()
                if code:
                    return inp, False, "err_bad_result", orig, 0
            new = os.path.getsize(stage.path)
            if new >= orig:
                return inp, False, "err_no_gain", orig, orig
            stage.commit()
        if progress_cb:
            progress_cb(1.0, "100%")
        return inp, True, "OK", orig, new
    except ProcessingCancelled:
        return inp, False, "stopped", orig, 0
    except PermissionError:
        return inp, False, "err_no_access", orig, 0
    except Exception as error:
        ctx._log_exc("process video", error)
        return inp, False, str(error)[:140], orig, 0
