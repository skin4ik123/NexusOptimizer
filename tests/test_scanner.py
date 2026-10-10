# -*- coding: utf-8 -*-
"""
Scanner tests.

Three layers:
  1. page processing and bookkeeping (pure functions, always run);
  2. the scanner worker through the program's own ScanJob, against the fake
     WIA device (needs a worker built with -p:TestHooks=true) and against the
     Nexus Virtual Scanner TWAIN source in both bitnesses;
  3. the Scanner section of the real window, driven like a user: pick the
     device, Scan, pages appear, rotate, save the PDF.

Layers 2 and 3 need equipment that a plain checkout does not have. When it is
missing the test says SKIP with the reason; it never passes silently.

The worker is found through NEXUS_SCAN_HELPERS (build_installer.ps1 points it
at the test build), otherwise in scanner/out.

Run:  python tests\\test_scanner.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEXUS_MULTI", "1")

import NexusPDFOptimizer as nexus  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

VIRTUAL = "Nexus Virtual Scanner"
FAKE_WIA = "Nexus Fake WIA Scanner"


class Skip(Exception):
    """The equipment a test needs is not present."""


# ---------------------------------------------------------------- fixtures

def text_page(w=1240, h=1754, mode="L", lines=True, paper=238):
    """A page-like picture: paper, rows of dark 'words'."""
    img = Image.new("L", (w, h), paper)
    if lines:
        d = ImageDraw.Draw(img)
        y = int(h * 0.12)
        row = 0
        while y < h * 0.88:
            x = int(w * 0.1)
            k = 0
            while x < w * 0.88:
                ww = 40 + (row * 37 + k * 53) % 90
                d.rectangle([x, y, min(x + ww, int(w * 0.88)), y + 12], fill=35)
                x += ww + 16
                k += 1
            y += 34 if row % 7 != 6 else 68
            row += 1
    noise = Image.effect_noise((w, h), 9)
    img = Image.blend(img, noise, 0.04)
    if mode == "RGB":
        img = img.convert("RGB")
    elif mode == "1":
        img = img.point(lambda v: 255 if v > 128 else 0, mode="1")
    return img


def blank_page(w=1240, h=1754, dust=True, bleed=False):
    img = Image.blend(Image.new("L", (w, h), 241), Image.effect_noise((w, h), 10), 0.05)
    d = ImageDraw.Draw(img)
    if dust:
        for x, y in ((300, 400), (900, 1300), (620, 880)):
            d.ellipse([x, y, x + 3, y + 3], fill=70)
    if bleed:  # the other side shining through: only a little darker than paper
        for y in range(200, 1500, 34):
            d.rectangle([150, y, 1050, y + 12], fill=218)
    return img


def helpers_dir():
    h = nexus.scan_helper_dir()
    if h is None:
        raise Skip("scanner worker not built (scanner/out or NEXUS_SCAN_HELPERS)")
    return h


def worker_hello(arch=64):
    exe = nexus._helper_exe(helpers_dir(), arch)
    out = subprocess.run([str(exe), "layout"], capture_output=True, timeout=60)
    events = [json.loads(x) for x in out.stdout.splitlines() if x.strip()]
    return events, out.returncode


def needs_test_hooks():
    events, _rc = worker_hello(64)
    hello = next((e for e in events if e.get("event") == "hello"), {})
    if not hello.get("test_hooks"):
        raise Skip("worker built without -p:TestHooks=true (no fake WIA device)")


def needs_virtual_scanner():
    import os.path as p
    win = os.environ.get("WINDIR", r"C:\Windows")
    if not (p.isfile(p.join(win, "twain_64", "NexusVirtualScanner", "NexusVirtualScanner.ds")) and
            p.isfile(p.join(win, "twain_32", "NexusVirtualScanner", "NexusVirtualScanner.ds"))):
        raise Skip("Nexus Virtual Scanner not installed (tools/virtual-scanner/install.ps1)")


class Env:
    """Temporarily sets environment variables (the worker and the virtual
    scanner inherit them)."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_job(device, opts, cancel_after=None, timeout=90):
    """Runs a ScanJob to the end; returns the events in order."""
    out = tempfile.mkdtemp(prefix="nexus_scanjob_")
    events = []
    finished = threading.Event()
    job_box = {}

    def on_event(ev):
        events.append(ev)
        if (cancel_after is not None and ev.get("event") == "page"
                and ev.get("index") == cancel_after):
            job_box["job"].cancel()
        if ev.get("event") == "done":
            finished.set()

    job = nexus.ScanJob(device, opts, out, 0, on_event)
    job_box["job"] = job
    job.start()
    if not finished.wait(timeout):
        job._kill("test timeout")
        raise AssertionError(f"scan did not finish in {timeout}s: {events[-3:]}")
    for ev in events:
        if ev.get("event") == "page":
            assert os.path.isfile(ev["path"]), ev
    return events, out


def device(backend, arch, name):
    return nexus.ScanDevice({"backend": backend, "arch": arch, "name": name})


def pages_of(events):
    return [e for e in events if e.get("event") == "page"]


def errors_of(events):
    return [e.get("code") for e in events if e.get("event") == "error"]


# ------------------------------------------------- layer 1: page processing

def test_black_and_white_threshold_from_the_page():
    page = text_page(mode="L", paper=226)  # greyish paper: a fixed 128 would still work,
    bw = nexus.to_black_and_white(page)    # but the threshold must sit between ink and paper
    assert bw.mode == "1"
    gray = bw.convert("L")
    assert gray.getpixel((5, 5)) == 255, "paper must turn white"
    assert gray.getpixel((int(1240 * 0.1) + 5, int(1754 * 0.12) + 6)) == 0, "ink must stay black"
    t_ = nexus._otsu_threshold(page.histogram())
    assert 60 < t_ < 210, t_


def test_blank_page_detection():
    assert nexus.is_blank_page(blank_page())
    assert nexus.is_blank_page(blank_page(bleed=True)), "bleed-through is not content"
    assert nexus.is_blank_page(blank_page().point(lambda v: 255 if v > 128 else 0, mode="1"))
    assert not nexus.is_blank_page(text_page())
    # a lone signature (the page people care about most) is not blank
    sig = blank_page()
    d = ImageDraw.Draw(sig)
    d.line([(400, 1200), (700, 1180), (820, 1215)], fill=40, width=4)
    assert not nexus.is_blank_page(sig), "a signature-only page must be kept"


def test_skew_is_detected_and_corrected():
    page = text_page()
    assert nexus.detect_skew_angle(page) == 0.0, "a straight page needs no rotation"
    for tilt in (1.6, -2.3, 0.9):
        rotated = page.rotate(tilt, resample=Image.Resampling.BICUBIC, fillcolor=238)
        found = nexus.detect_skew_angle(rotated)
        assert abs(found + tilt) <= 0.2, f"tilt {tilt}: found {found}"
        fixed, angle = nexus.deskew_image(rotated)
        assert abs(nexus.detect_skew_angle(fixed)) <= 0.2, (tilt, angle)
        assert fixed.size == rotated.size
    # no text lines (a photo-like page): nothing to straighten
    photo = Image.radial_gradient("L").resize((1240, 1754))
    assert nexus.detect_skew_angle(photo) == 0.0


def test_finalize_page_formats_and_cleanup():
    d = Path(tempfile.mkdtemp(prefix="nexus_final_"))
    try:
        src = d / "in.bmp"
        text_page(mode="RGB").save(src, dpi=(300, 300))
        res = nexus.finalize_scan_page(str(src), d / "out", "gray", False, False)
        assert not src.exists(), "the worker's BMP is removed"
        with Image.open(res["path"]) as im:
            assert res["path"].endswith(".jpg") and im.mode == "L"
            assert round(im.info["dpi"][0]) == 300, "the page keeps its real size"

        src = d / "in2.bmp"
        text_page(mode="L").save(src, dpi=(200, 200))
        res = nexus.finalize_scan_page(str(src), d / "out", "bw", False, False)
        with Image.open(res["path"]) as im:
            assert res["path"].endswith(".png") and im.mode == "1"

        src = d / "in3.bmp"
        blank_page().save(src)
        assert nexus.finalize_scan_page(str(src), d / "out", "color", False, True)["blank"]
        src = d / "in4.bmp"
        blank_page().save(src)
        kept = nexus.finalize_scan_page(str(src), d / "out", "color", False, False)
        assert not kept["blank"] and os.path.isfile(kept["path"]), "skip only when asked"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rotate_never_touches_a_dragged_in_original():
    d = Path(tempfile.mkdtemp(prefix="nexus_rot_"))
    old_dir = nexus.ScanSessionStore.DIR
    try:
        nexus.ScanSessionStore.DIR = d / "session"
        nexus.ScanSessionStore.DIR.mkdir()
        external = d / "photo.jpg"
        text_page(800, 1100, "RGB").save(external, quality=90)
        before = external.read_bytes()
        new = nexus.rotate_page_file(str(external), 90, nexus.ScanSessionStore.DIR)
        assert external.read_bytes() == before, "the user's own file is not modified"
        with Image.open(new) as im:
            assert im.size == (1100, 800)
        owned = nexus.rotate_page_file(new, 270, nexus.ScanSessionStore.DIR)
        assert not os.path.exists(new), "a session page is replaced, not duplicated"
        with Image.open(owned) as im:
            assert im.size == (800, 1100)
    finally:
        nexus.ScanSessionStore.DIR = old_dir
        shutil.rmtree(d, ignore_errors=True)


def test_session_store_survives_restart_and_sweeps_orphans():
    d = Path(tempfile.mkdtemp(prefix="nexus_sess_"))
    saved = nexus.ScanSessionStore.DIR, nexus.ScanSessionStore.FILE
    try:
        nexus.ScanSessionStore.DIR = d / "s"
        nexus.ScanSessionStore.FILE = d / "s" / "pages.json"
        nexus.ScanSessionStore.DIR.mkdir()
        a = nexus.ScanSessionStore.DIR / "page_1.jpg"
        b = nexus.ScanSessionStore.DIR / "page_2.png"
        orphan = nexus.ScanSessionStore.DIR / "page_3.jpg"
        external = d / "ext.jpg"
        for p in (a, b, orphan, external):
            text_page(100, 140).save(p)
        nexus.ScanSessionStore.save([str(b), str(external), str(a)])
        loaded = nexus.ScanSessionStore.load()
        assert loaded == [str(b), str(external), str(a)], "order is kept"
        os.unlink(b)
        assert nexus.ScanSessionStore.load() == [str(external), str(a)], "missing files dropped"
        nexus.ScanSessionStore.sweep([str(a)])
        assert not orphan.exists() and a.exists() and external.exists()
    finally:
        nexus.ScanSessionStore.DIR, nexus.ScanSessionStore.FILE = saved
        shutil.rmtree(d, ignore_errors=True)


def test_device_list_drops_windows_duplicates():
    results = {
        64: [{"event": "hello"},
             {"event": "device", "backend": "wia", "arch": 64, "id": "{X}\\0001",
              "name": "HP LaserJet MFP"},
             {"event": "device", "backend": "twain", "arch": 64, "name": "HP LaserJet TWAIN"},
             {"event": "done"}],
        32: [{"event": "device", "backend": "twain", "arch": 32, "name": "HP LaserJet TWAIN"},
             {"event": "device", "backend": "twain", "arch": 32, "name": "Canon LiDE 110"},
             {"event": "device", "backend": "twain", "arch": 32, "name": "WIA-HP LaserJet MFP",
              "bridge": True},
             {"event": "note", "backend": "twain", "code": "dsm_missing"}],
    }
    devices, notes = nexus.merge_scanner_events(results)
    assert [d.key for d in devices] == [
        "wia64:{X}\\0001", "twain64:HP LaserJet TWAIN", "twain32:Canon LiDE 110"], devices
    assert devices[0].device == "{X}\\0001", "WIA is found again by its device id"
    assert "TWAIN" in devices[1].label and notes[0]["code"] == "dsm_missing"


# ---------------------------------------------- layer 2: the scanner worker

def test_worker_structure_layout_both_bitnesses():
    for arch in (64, 32):
        events, rc = worker_hello(arch)
        assert rc == 0, (arch, events)
        hello = events[0]
        assert hello["event"] == "hello" and hello["arch"] == arch, events
        assert events[1] == {"event": "layout", "ok": True}, events


def test_fake_wia_feeder_through_scanjob():
    needs_test_hooks()
    with Env(NEXUS_SCAN_FAKE_WIA=1, NEXUS_FAKEWIA_PAGES=3):
        devices, _notes = nexus.list_scanners()
        dev = next((d for d in devices if d.name == FAKE_WIA), None)
        assert dev is not None and dev.backend == "wia", devices
        events, _ = run_job(dev, {"dpi": 200, "color": "gray", "source": "feeder", "paper": "a4"})
    assert len(pages_of(events)) == 3 and not errors_of(events), events
    done = events[-1]
    assert done == {"event": "done", "pages": 3, "cancelled": False}, done


def test_fake_wia_errors_reach_the_program():
    needs_test_hooks()
    dev = None
    with Env(NEXUS_SCAN_FAKE_WIA=1):
        devices, _ = nexus.list_scanners()
        dev = next(d for d in devices if d.name == FAKE_WIA)
    with Env(NEXUS_SCAN_FAKE_WIA=1, NEXUS_FAKEWIA_JAM_AT=2):
        events, _ = run_job(dev, {"source": "feeder", "dpi": 100})
        assert len(pages_of(events)) == 1 and errors_of(events) == ["paper_jam"], events
    with Env(NEXUS_SCAN_FAKE_WIA=1, NEXUS_FAKEWIA_BUSY=1):
        events, _ = run_job(dev, {"dpi": 100})
        assert errors_of(events) == ["device_busy"], events


def test_virtual_twain_both_bitnesses():
    needs_virtual_scanner()
    with Env(NEXUS_VSCAN_PAGES=2, NEXUS_VSCAN_DELAY_MS=10):
        devices, _ = nexus.list_scanners()
        names = [(d.name, d.arch) for d in devices if d.backend == "twain"]
        assert (VIRTUAL, 64) in names, f"the 64-bit copy is listed: {names}"
        assert (VIRTUAL, 32) not in names, "the 32-bit duplicate is hidden"
        events, _ = run_job(device("twain", 64, VIRTUAL),
                            {"dpi": 150, "color": "gray", "source": "duplex"})
        assert len(pages_of(events)) == 4 and not errors_of(events), events
        # the 32-bit worker reaches 32-bit drivers — the reason it exists
        events, _ = run_job(device("twain", 32, VIRTUAL),
                            {"dpi": 100, "color": "color", "source": "feeder"})
        assert len(pages_of(events)) == 2 and not errors_of(events), events


def test_virtual_twain_crash_keeps_pages_and_program():
    needs_virtual_scanner()
    with Env(NEXUS_VSCAN_PAGES=4, NEXUS_VSCAN_CRASH_AT=2, NEXUS_VSCAN_DELAY_MS=10):
        events, _ = run_job(device("twain", 32, VIRTUAL), {"dpi": 100, "source": "feeder"})
    assert len(pages_of(events)) == 1, events
    assert errors_of(events) == ["driver_crashed"], events
    assert events[-1]["event"] == "done" and events[-1]["pages"] == 1


def test_virtual_twain_simplex_overrides_persisted_duplex():
    needs_virtual_scanner()
    for arch in (32, 64):
        for transfer in ("native", "memory"):
            with Env(NEXUS_VSCAN_PAGES=3, NEXUS_VSCAN_DELAY_MS=1,
                     NEXUS_VSCAN_INITIAL_DUPLEX=1, NEXUS_VSCAN_FEEDER_SETS_DUPLEX=1):
                events, _ = run_job(device("twain", arch, VIRTUAL),
                                    {"dpi": 75, "source": "feeder", "transfer": transfer})
            assert not errors_of(events) and len(pages_of(events)) == 3, (arch, transfer, events)
            settings = next(e for e in events if e.get("event") == "settings")
            assert settings["feeder"] is True and settings["duplex"] is False, settings


def test_virtual_twain_refuses_to_start_if_simplex_is_ignored():
    needs_virtual_scanner()
    for arch in (32, 64):
        for fault in ("NEXUS_VSCAN_REFUSE_SIMPLEX", "NEXUS_VSCAN_IGNORE_SIMPLEX"):
            with Env(**{fault: 1, "NEXUS_VSCAN_INITIAL_DUPLEX": 1, "NEXUS_VSCAN_PAGES": 2}):
                events, _ = run_job(device("twain", arch, VIRTUAL), {"source": "feeder"})
            assert errors_of(events) == ["simplex_refused"] and not pages_of(events), events


def test_virtual_twain_duplex_and_simplex_only_fallback():
    needs_virtual_scanner()
    for arch in (32, 64):
        with Env(NEXUS_VSCAN_PAGES=2, NEXUS_VSCAN_DELAY_MS=1):
            events, _ = run_job(device("twain", arch, VIRTUAL), {"dpi": 75, "source": "duplex"})
        assert len(pages_of(events)) == 4 and not errors_of(events), events
        with Env(NEXUS_VSCAN_PAGES=2, NEXUS_VSCAN_DELAY_MS=1, NEXUS_VSCAN_NO_DUPLEX=1):
            events, _ = run_job(device("twain", arch, VIRTUAL), {"dpi": 75, "source": "duplex"})
        assert len(pages_of(events)) == 2 and not errors_of(events), events
        assert any(e.get("event") == "warning" and e.get("code") == "no_duplex" for e in events), events


def test_virtual_twain_hang_is_stopped_by_the_watchdog():
    needs_virtual_scanner()
    saved = nexus.SCAN_STALL_TIMEOUT
    nexus.SCAN_STALL_TIMEOUT = 4
    try:
        with Env(NEXUS_VSCAN_PAGES=4, NEXUS_VSCAN_HANG_AT=2, NEXUS_VSCAN_DELAY_MS=10):
            t0 = time.time()
            events, _ = run_job(device("twain", 64, VIRTUAL), {"dpi": 100, "source": "feeder"},
                                timeout=40)
            took = time.time() - t0
    finally:
        nexus.SCAN_STALL_TIMEOUT = saved
    assert len(pages_of(events)) == 1 and errors_of(events) == ["timeout"], events
    assert took < 20, took


def test_virtual_twain_cancel_and_jam():
    needs_virtual_scanner()
    with Env(NEXUS_VSCAN_PAGES=8, NEXUS_VSCAN_DELAY_MS=300):
        events, _ = run_job(device("twain", 64, VIRTUAL), {"dpi": 100, "source": "feeder"},
                            cancel_after=1)
    n = len(pages_of(events))
    assert events[-1]["cancelled"] is True and 1 <= n <= 3, events
    with Env(NEXUS_VSCAN_PAGES=5, NEXUS_VSCAN_JAM_AT=3, NEXUS_VSCAN_DELAY_MS=10):
        events, _ = run_job(device("twain", 32, VIRTUAL), {"dpi": 100, "source": "feeder"})
    assert len(pages_of(events)) == 2 and errors_of(events) == ["paper_jam"], events


# ---------------------------------------- layer 3: the section, like a user

def _pump(app, until, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.update()
        if until():
            return True
        time.sleep(0.03)
    return False


def test_scan_section_end_to_end():
    """Open the Scanner section, scan a skewed two-sided batch with blank
    backs, check what the user would see, rotate a page, save the PDF."""
    needs_virtual_scanner()
    import fitz
    d = Path(tempfile.mkdtemp(prefix="nexus_e2e_"))
    saved_store = nexus.ScanSessionStore.DIR, nexus.ScanSessionStore.FILE
    saved_settings = nexus.load_settings()
    saved_log_exc = nexus._log_exc
    swallowed = []

    def record(where, e=None):
        # every exception the program catches and logs counts as a failure
        # here: "it worked, but something broke quietly" is not a pass
        swallowed.append(f"{where}: {e!r}")
        saved_log_exc(where, e)

    nexus._log_exc = record
    app = None
    try:
        nexus.ScanSessionStore.DIR = d / "session"
        nexus.ScanSessionStore.FILE = d / "session" / "pages.json"
        s = nexus.load_settings()
        s.update({"scan_device": f"twain64:{VIRTUAL}", "scan_use_ui": False,
                  "scan_source": "duplex", "scan_color": "gray", "scan_dpi": 150,
                  "scan_paper": "a4", "scan_deskew": True, "scan_skip_blank": True,
                  "scan_compress": True, "scan_folder": str(d / "out"),
                  "scan_open_folder": False, "sound": False})
        nexus.save_settings(s)
        with Env(NEXUS_VSCAN_PAGES=3, NEXUS_VSCAN_BLANK_BACKS=1, NEXUS_VSCAN_SKEW=1.8,
                 NEXUS_VSCAN_DELAY_MS=20):
            app = nexus.App()
            app.show("scan")
            sec = app.current
            assert _pump(app, lambda: sec.devices, 40), "devices were not listed"
            assert sec._selected_device().key == f"twain64:{VIRTUAL}", sec.devices
            sec._scan()
            assert sec.scan_btn.cget("text") == nexus.t("scan_cancel"), "Scan turns into Stop"
            assert _pump(app, lambda: sec._job is None and not sec.is_running, 90), "no finish"
        info = sec._job_info
        assert len(sec.pages) == 3, f"3 fronts kept, 3 blank backs skipped: {sec.pages}"
        assert info["blank"] == 3 and info["straight"] == 3, info
        assert sec.scan_btn.cget("text") == nexus.t("scan_btn")
        assert len(sec._page_cards) == 3, "a card for every page"
        assert nexus.ScanSessionStore.load() == sec.pages, "the session is on disk"
        for p in sec.pages:
            assert abs(nexus.detect_skew_angle(Image.open(p))) <= 0.25, p

        first = sec.pages[0]
        sec._page_sel = {first}
        sec._rotate_selected(270)
        assert _pump(app, lambda: sec.pages[0] != first, 20), "rotation did not apply"
        with Image.open(sec.pages[0]) as im:
            assert im.width > im.height, "the first page is now landscape"

        sec._save_one_pdf()
        out_dir = d / "out"
        assert _pump(app, lambda: not sec.is_running and list(out_dir.glob("*.pdf")), 90)
        pdf = next(out_dir.glob("*.pdf"))
        doc = fitz.open(pdf)
        try:
            assert doc.page_count == 3
            w, h = doc[1].rect.width, doc[1].rect.height
            assert abs(w - 595) < 6 and abs(h - 842) < 6, f"A4 page size kept: {w}x{h}"
            assert doc[0].rect.width > doc[0].rect.height
        finally:
            doc.close()
        _pump(app, lambda: False, 0.5)
        assert not swallowed, f"errors caught and logged during the scenario: {swallowed}"
    finally:
        nexus._log_exc = saved_log_exc
        if app is not None:
            try:
                app.current.on_leave()
                app.destroy()
            except Exception:
                pass
        nexus.save_settings(saved_settings)
        nexus.ScanSessionStore.DIR, nexus.ScanSessionStore.FILE = saved_store
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    fails = skips = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t0 = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t0:.1f}s)")
            except Skip as e:
                skips += 1
                print(f"SKIP {name}: {e}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("=" * 40)
    print(("ALL OK" if not fails else f"{fails} FAILED") + (f", {skips} SKIPPED" if skips else ""))
    sys.exit(1 if fails else 0)
