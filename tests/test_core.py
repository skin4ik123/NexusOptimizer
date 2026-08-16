# -*- coding: utf-8 -*-
"""
Tests for the pure functions of Nexus Optimizer (no GUI).
Run:  python tests\test_core.py   or   python -m pytest tests
Only customtkinter and pillow are needed; the module imports them.
"""
import ctypes
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
    scan = {"is_scanned_like": True, "avg_image_dpi": 200}
    text = {"is_scanned_like": False, "avg_image_dpi": 0}
    assert resolve_pdf_params("smart", 0, 0, scan) == (100, 42, False)
    assert resolve_pdf_params("smart", 0, 0, text) == (135, 56, False)
    assert resolve_pdf_params("aggressive", 0, 0, text) == (85, 35, False)
    assert resolve_pdf_params("structure", 0, 0, text)[2] is True
    d, q, s = resolve_pdf_params("custom", 999, 1, text)
    assert d == 300 and q == 20 and s is False       # clamping works


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


def test_twain_struct_abi():
    """TWAIN structure sizes from the specification (Windows, 2-byte packing).

    A mistake here raises no exception: the driver simply reads shifted fields
    and "nothing works". Hence the sizes are checked as numbers.
    """
    expected = {
        "_TwFix32": 4,
        "_TwVersion": 42,
        "_TwIdentity": 156,
        "_TwUserInterface": 12,
        "_TwEvent": 10,
        "_TwCapability": 12,
        "_TwOneValue": 8,
        "_TwPendingXfers": 6,
    }
    for name, size in expected.items():
        got = ctypes.sizeof(getattr(nexus, name))
        assert got == size, f"{name}: {got} instead of {size}"
    # TWAIN string fields are exactly TW_STR32 (34 bytes), otherwise a source
    # name is truncated or spills into the next field.
    assert nexus._TwIdentity.ProductName.size == 34
    assert nexus._TwIdentity.Manufacturer.offset == 54


def test_twain_without_dsm_is_quiet():
    """Without twaindsm.dll TWAIN must return an empty list, not fail."""
    names = nexus.twain_source_names()
    assert isinstance(names, list)


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
