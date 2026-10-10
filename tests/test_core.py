# -*- coding: utf-8 -*-
"""
Tests for the pure functions of Nexus Optimizer (no GUI).
Run:  python tests\test_core.py   or   python -m pytest tests
Only customtkinter and pillow are needed; the module imports them.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import NexusPDFOptimizer as nexus  # noqa: E402
from NexusPDFOptimizer import (  # noqa: E402
    safe_int, safe_filename, apply_rename, unique_out_path,
    _resolve_video_out, video_out_ext, resolve_pdf_params,
    _same_format_family, collect_files, _brace_parse, effective_workers,
    TRANSLATIONS,
)


def test_safe_int():
    assert safe_int("42") == 42
    assert safe_int("42,7") == 42
    assert safe_int("", 5) == 5
    assert safe_int("abc", 7) == 7
    assert safe_int(None, 3) == 3
    assert safe_int("999", 0, 0, 100) == 100
    assert safe_int("-5", 0, 0, 100) == 0
    assert safe_int(" 12 ") == 12


def test_safe_filename():
    assert safe_filename('a<b>:c"/d\\e|f?g*h') == "a_b__c__d_e_f_g_h"
    assert safe_filename("  name.  ") == "name"
    assert safe_filename("...") == "file"
    assert safe_filename("обычное имя") == "обычное имя"


def test_apply_rename():
    assert apply_rename("", r"C:\x\Отчёт.pdf") == "Отчёт"
    assert apply_rename("{name}_nexus", r"C:\x\doc.pdf") == "doc_nexus"
    out = apply_rename("{name}_{date}", r"C:\x\doc.pdf")
    assert out.startswith("doc_") and len(out) == len("doc_") + 8


def test_unique_out_path():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.pdf")
        assert unique_out_path(p) == p
        open(p, "w").close()
        p2 = unique_out_path(p)
        assert p2.endswith("f_2.pdf")
        open(p2, "w").close()
        assert unique_out_path(p).endswith("f_3.pdf")


def test_resolve_video_out():
    out, codec = _resolve_video_out("a.xxx", "H.264", "MP4")
    assert out == "a.mp4" and codec == "H.264"
    out, codec = _resolve_video_out("a.mp4", "VP9", "MP4")
    assert out == "a.webm" and codec == "VP9"       # VP9 does not live in MP4
    out, codec = _resolve_video_out("a.mp4", "H.265", "AVI")
    assert codec == "H.264"                          # AVI: H.264 only
    out, codec = _resolve_video_out("a.mp4", "AV1", "AVI")
    assert codec == "H.264" and out.endswith(".avi")  # AVI is always H.264
    out, codec = _resolve_video_out("a.mp4", "H264", "WebM")
    assert codec == "VP9" and out.endswith(".webm")
    assert video_out_ext("VP9", "MP4") == ".webm"
    assert video_out_ext("H.264", "MKV") == ".mkv"


def test_resolve_pdf_params():
    scan = {"is_scanned_like": True, "avg_image_dpi": 300}
    scan_low = {"is_scanned_like": True, "avg_image_dpi": 150}
    text = {"is_scanned_like": False, "avg_image_dpi": 0}
    assert resolve_pdf_params("smart", 0, 0, scan) == (130, 68, False)
    assert resolve_pdf_params("smart", 0, 0, scan_low) == (150, 70, False)
    assert resolve_pdf_params("smart", 0, 0, text) == (150, 76, False)
    assert resolve_pdf_params("aggressive", 0, 0, text) == (96, 45, False)
    assert resolve_pdf_params("structure", 0, 0, text)[2] is True
    d, q, s = resolve_pdf_params("custom", 999, 1, text)
    assert d == 300 and q == 20 and s is False       # clamping works


# ---- PDF engine: per-image pipeline on synthetic scans ----
#
# Each case below once broke a real document: MuPDF's rewrite_images turned
# grey scans into a doubled mess and CMYK scans into a black rectangle, shrank
# 1-bit scans until the letters fell apart and never brought a scan down to
# the requested density. The checks look at pixels and PDF objects, not at
# "the function returned OK".

def _fitz():
    import fitz
    return fitz


def _jpeg(img, quality=90, optimize=False):
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=optimize)
    return buf.getvalue()


def _scan_image(width, height, mode="RGB"):
    """Page-like picture: light paper, dark 'text' strokes, some noise."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (236, 232, 220))
    draw = ImageDraw.Draw(img)
    for row in range(height // 24):
        y = 20 + row * 24
        for col in range(0, width - 40, 37):
            draw.rectangle([col + 10, y, col + 10 + 22 + (row * col) % 9, y + 9],
                           fill=(30, 30, 34))
    noise = Image.effect_noise((width, height), 12).convert("RGB")
    img = Image.blend(img, noise, 0.04)
    return img.convert(mode) if mode != "RGB" else img


def _render(path, dpi=60, page=0):
    from PIL import Image
    fitz = _fitz()
    doc = fitz.open(path)
    pix = doc[page].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    doc.close()
    return img


def _psnr(a, b):
    import math
    from PIL import ImageChops
    if a.size != b.size:
        b = b.resize(a.size)
    hist = ImageChops.difference(a, b).histogram()
    mse = sum(c * i * i for i, c in enumerate(hist)) / (sum(hist) or 1)
    return 99.0 if mse < 1e-9 else 10 * math.log10(65025 / mse)


def _image_xrefs(path):
    fitz = _fitz()
    doc = fitz.open(path)
    res = {}
    for page in doc:
        for item in page.get_images(full=True):
            res[item[0]] = (doc.xref_get_key(item[0], "Width")[1],
                            doc.xref_get_key(item[0], "Filter")[1],
                            doc.xref_get_key(item[0], "ColorSpace")[1])
    doc.close()
    return res


def _compress(src, out, preset="smart", gray=False, threads=1):
    if os.path.exists(out):
        os.unlink(out)
    return nexus.process_pdf(src, out, preset, 0, 0, gray, threads=threads)


def _a4_doc():
    fitz = _fitz()
    return fitz.open(), fitz.paper_rect("a4")


def test_pdf_grey_and_cmyk_scans_survive():
    """The two scan types the old engine destroyed must stay what they were."""
    with tempfile.TemporaryDirectory() as d:
        for mode in ("L", "CMYK"):
            doc, a4 = _a4_doc()
            page = doc.new_page(width=a4.width, height=a4.height)
            # 2480 px across A4 = 300 dpi, as an office MFP scans by default
            page.insert_image(page.rect, stream=_jpeg(_scan_image(2480, 3508, mode)))
            src = os.path.join(d, f"scan_{mode}.pdf")
            doc.save(src)
            doc.close()
            out = os.path.join(d, f"out_{mode}.pdf")
            _i, ok, msg, orig, new = _compress(src, out, "balanced")
            assert ok, f"{mode}: {msg}"
            assert new < orig
            quality = _psnr(_render(src), _render(out))
            assert quality > 28, f"{mode}: picture damaged ({quality:.1f} dB)"


def test_pdf_scan_reaches_target_density_and_keeps_text_layer():
    """A 300 dpi scan comes down to the target density on both axes, the
    invisible OCR text and the bookmarks stay untouched."""
    fitz = _fitz()
    with tempfile.TemporaryDirectory() as d:
        doc, a4 = _a4_doc()
        for _ in range(2):
            page = doc.new_page(width=a4.width, height=a4.height)
            page.insert_image(page.rect, stream=_jpeg(_scan_image(2480, 3508)))
            page.insert_text((72, 100), "Searchable contract text", fontsize=11,
                             render_mode=3)
        doc.set_toc([[1, "Contract", 1], [1, "Annex", 2]])
        src = os.path.join(d, "scan300.pdf")
        doc.save(src)
        doc.close()
        out = os.path.join(d, "out.pdf")
        _i, ok, msg, orig, new = _compress(src, out, "balanced")   # 120 dpi
        assert ok, msg
        res = fitz.open(out)
        info = res[0].get_image_info()[0]
        width_pt = res[0].rect.width
        dpi = info["width"] / (width_pt / 72.0)
        assert 110 <= dpi <= 125, f"density {dpi:.0f} dpi instead of 120"
        assert "Searchable contract text" in res[0].get_text()
        assert len(res.get_toc()) == 2
        assert res.page_count == 2
        res.close()


def test_pdf_rotated_scan_density_from_matrix():
    """A scan inserted on its side: density comes from the placement matrix,
    not from the bounding box (which would overstate it 1.4x)."""
    fitz = _fitz()
    with tempfile.TemporaryDirectory() as d:
        doc, a4 = _a4_doc()
        page = doc.new_page(width=a4.width, height=a4.height)
        page.insert_image(page.rect, stream=_jpeg(_scan_image(3508, 2480)), rotate=90)
        src = os.path.join(d, "rot.pdf")
        doc.save(src)
        doc.close()
        out = os.path.join(d, "out.pdf")
        _i, ok, msg, _o, _n = _compress(src, out, "balanced")
        assert ok, msg
        res = fitz.open(out)
        info = res[0].get_image_info()[0]
        assert abs(info["width"] - round(3508 * 120 / 300)) <= 2, info["width"]
        res.close()


def test_pdf_untouchables_stay_byte_identical():
    """1-bit scans, transparency and an already compact JPEG stay unchanged."""
    from PIL import Image
    fitz = _fitz()
    with tempfile.TemporaryDirectory() as d:
        doc, a4 = _a4_doc()
        # 1-bit page: shrinking it would break the letters apart
        page = doc.new_page(width=a4.width, height=a4.height)
        bw = _scan_image(2480, 3508, "L").point(lambda v: 255 if v > 128 else 0).convert("1")
        import io
        buf = io.BytesIO()
        bw.save(buf, "PNG")
        page.insert_image(page.rect, stream=buf.getvalue())
        # transparency (soft mask)
        page = doc.new_page(width=a4.width, height=a4.height)
        rgba = _scan_image(1600, 1600).convert("RGBA")
        rgba.putalpha(Image.linear_gradient("L").resize((1600, 1600)))
        buf = io.BytesIO()
        rgba.save(buf, "PNG")
        page.insert_image(fitz.Rect(50, 50, 450, 450), stream=buf.getvalue())
        # JPEG at 140 dpi on A4 = within 1.3x of a 130 dpi target
        page = doc.new_page(width=a4.width, height=a4.height)
        page.insert_image(page.rect, stream=_jpeg(_scan_image(1157, 1637), 40, True))
        # and one ordinary 300 dpi scan, so the file does shrink and a result
        # is written (otherwise this test would pass without checking a thing)
        page = doc.new_page(width=a4.width, height=a4.height)
        page.insert_image(page.rect, stream=_jpeg(_scan_image(2480, 3508)))
        src = os.path.join(d, "mixed.pdf")
        doc.save(src)
        doc.close()

        def decoded(path):
            pdf = fitz.open(path)
            res = {}
            for x in _image_xrefs(path):
                res[(pdf.xref_get_key(x, "Width")[1],
                     pdf.xref_get_key(x, "Height")[1])] = pdf.xref_stream(x)
            pdf.close()
            return res

        before = decoded(src)
        out = os.path.join(d, "out.pdf")
        _i, ok, msg, _o, _n = nexus.process_pdf(src, out, "smart", 0, 0, False)
        assert ok, msg
        after = decoded(out)
        for size in (("2480", "3508"), ("1600", "1600"), ("1157", "1637")):
            if size == ("2480", "3508"):
                continue  # two images share this size; checked below by count
            assert after.get(size) == before[size], f"image {size} was re-encoded"
        # the 1-bit page is still there at full size, the 300 dpi scan is not
        sizes = sorted(_image_xrefs(out).values())
        widths = sorted(int(w) for w, _f, _c in sizes)
        assert 2480 in widths, f"1-bit scan was shrunk: {widths}"
        assert widths.count(2480) == 1, f"the 300 dpi scan was not shrunk: {widths}"


def test_pdf_shared_logo_decided_once_by_largest_placement():
    """One logo object on every page, once shown large: it is sized for the
    largest placement, and stays a single shared object."""
    fitz = _fitz()
    with tempfile.TemporaryDirectory() as d:
        doc, a4 = _a4_doc()
        logo = _jpeg(_scan_image(1800, 600), 92)
        first = None
        for i in range(4):
            page = doc.new_page(width=a4.width, height=a4.height)
            rect = fitz.Rect(50, 50, 545, 215) if i == 2 else fitz.Rect(50, 40, 170, 80)
            if first is None:
                first = page.insert_image(rect, stream=logo)
            else:
                page.insert_image(rect, xref=first)
        src = os.path.join(d, "logo.pdf")
        doc.save(src)
        doc.close()
        out = os.path.join(d, "out.pdf")
        _i, ok, msg, _o, _n = _compress(src, out, "balanced")    # 120 dpi
        assert ok, msg
        imgs = _image_xrefs(out)
        assert len(imgs) == 1, f"the shared logo split into {len(imgs)} objects"
        width = int(next(iter(imgs.values()))[0])
        # largest placement: 495 pt wide -> 120 dpi -> ~825 px
        assert 780 <= width <= 870, width


def test_pdf_inverted_grey_and_palette_images_render_the_same():
    """Decode [1 0] (inverted grey) and an indexed (palette) image: the new
    stream holds the visible pixels, the old decode/palette must not linger."""
    fitz = _fitz()
    with tempfile.TemporaryDirectory() as d:
        doc, a4 = _a4_doc()
        page = doc.new_page(width=a4.width, height=a4.height)
        xref = page.insert_image(page.rect, stream=_jpeg(_scan_image(2480, 3508, "L")))
        # flip the stored pixels and compensate with /Decode [1 0]
        from PIL import ImageOps
        inverted = ImageOps.invert(_scan_image(2480, 3508, "L"))
        doc.update_stream(xref, _jpeg(inverted), compress=0)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
        doc.xref_set_key(xref, "ColorSpace", "/DeviceGray")
        doc.xref_set_key(xref, "Decode", "[1 0]")
        page = doc.new_page(width=a4.width, height=a4.height)
        import io
        buf = io.BytesIO()
        _scan_image(2480, 3508).quantize(64).save(buf, "PNG")
        page.insert_image(page.rect, stream=buf.getvalue())
        src = os.path.join(d, "special.pdf")
        doc.save(src)
        doc.close()
        out = os.path.join(d, "out.pdf")
        _i, ok, msg, _o, _n = _compress(src, out, "balanced")
        assert ok, msg
        for pno in (0, 1):
            q = _psnr(_render(src, page=pno), _render(out, page=pno))
            assert q > 26, f"page {pno + 1}: {q:.1f} dB"


def test_pdf_forced_grey_keeps_text_layer():
    """'Grayscale for scans' used to re-render pages into pictures, which threw
    away the OCR text, links and bookmarks. Now only the image turns grey."""
    fitz = _fitz()
    with tempfile.TemporaryDirectory() as d:
        doc, a4 = _a4_doc()
        page = doc.new_page(width=a4.width, height=a4.height)
        page.insert_image(page.rect, stream=_jpeg(_scan_image(1240, 1754)))
        page.insert_text((72, 100), "Invoice 4471", fontsize=11, render_mode=3)
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 90, 200, 104),
                          "uri": "https://example.com"})
        src = os.path.join(d, "color.pdf")
        doc.save(src)
        doc.close()
        out = os.path.join(d, "grey.pdf")
        _i, ok, msg, _o, _n = _compress(src, out, "smart", gray=True)
        assert ok, msg
        res = fitz.open(out)
        cs = [res.xref_get_key(x[0], "ColorSpace")[1] for x in res[0].get_images(full=True)]
        assert cs == ["/DeviceGray"], cs
        assert "Invoice 4471" in res[0].get_text()
        assert len(res[0].get_links()) == 1
        res.close()


def test_pdf_threads_give_the_same_document():
    """The single-file path processes images in a thread pool; the result must
    be the same as the one-thread path, page for page."""
    with tempfile.TemporaryDirectory() as d:
        doc, a4 = _a4_doc()
        for i in range(6):
            page = doc.new_page(width=a4.width, height=a4.height)
            page.insert_image(page.rect, stream=_jpeg(_scan_image(1800, 2546)))
        src = os.path.join(d, "multi.pdf")
        doc.save(src)
        doc.close()
        one = os.path.join(d, "one.pdf")
        many = os.path.join(d, "many.pdf")
        r1 = _compress(src, one, "balanced", threads=1)
        r4 = _compress(src, many, "balanced", threads=4)
        assert r1[1] and r4[1], (r1[2], r4[2])
        for pno in range(6):
            assert _psnr(_render(one, page=pno), _render(many, page=pno)) > 60


def test_same_format_family():
    assert _same_format_family("a.jpg", "JPEG")
    assert _same_format_family("a.JPEG", "jpeg")
    assert not _same_format_family("a.png", "JPEG")
    assert _same_format_family("a.png", "PNG")
    assert not _same_format_family("a.heic", "JPEG")


def test_collect_files():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.pdf").write_text("x")
        (Path(d) / "b.txt").write_text("x")
        (Path(d) / "_original_c.pdf").write_text("x")
        sub = Path(d) / "sub"
        sub.mkdir()
        (sub / "d.pdf").write_text("x")
        orig = Path(d) / "original"
        orig.mkdir()
        (orig / "e.pdf").write_text("x")

        flat = collect_files(d, {".pdf"}, recurse=False)
        assert [os.path.basename(p) for p in sorted(flat)] == ["a.pdf"]

        deep = sorted(os.path.basename(p) for p in collect_files(d, {".pdf"}, recurse=True))
        assert deep == ["a.pdf", "d.pdf"]            # original/ and _original_ are skipped

        single = collect_files(str(Path(d) / "a.pdf"), {".pdf"}, recurse=False)
        assert len(single) == 1


def test_brace_parse():
    assert _brace_parse("{C:\\a b\\f.pdf} {C:\\c.pdf}") == ["C:\\a b\\f.pdf", "C:\\c.pdf"]
    assert _brace_parse("C:\\one.pdf") == ["C:\\one.pdf"]


def test_effective_workers():
    assert effective_workers("auto", 8, 100) >= 1
    assert effective_workers("4", 8, 100) == 4
    assert effective_workers("12", 8, 100) == 8      # cap
    assert effective_workers("12", 8, 2) == 2        # never more than the number of files
    assert effective_workers("garbage", 8, 4) >= 1


def test_translations_complete():
    """The languages must match key for key: a missing one shows the reader
    the internal key name instead of a sentence."""
    ru = set(TRANSLATIONS["ru"])
    en = set(TRANSLATIONS["en"])
    assert ru == en, f"differ: {sorted(ru ^ en)[:10]}"


if __name__ == "__main__":
    fails = 0  # noqa: E303
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print("=" * 40)
    print("ALL OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
