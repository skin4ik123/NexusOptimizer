"""Exercise real Tk windows, asynchronous previews and normal-close cleanup."""
import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def scenario(name, root):
    # Every scenario runs in a new process, as a real reopening does.
    os.environ.update(APPDATA=str(root / "roaming"), LOCALAPPDATA=str(root / "local"), NEXUS_MULTI="1")
    (root / "temp").mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(root / "temp")
    sys.argv = [sys.argv[0]]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import NexusPDFOptimizer as nx
    from PIL import Image, ImageDraw, ImageGrab
    from nexus_support import AtomicOutput
    from nexus_preview import ComparisonWindow
    import nexus_media

    def pump(app, condition, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.update()
            if condition():
                return True
            time.sleep(0.01)
        return False

    def close(app):
        closed = threading.Event()
        destroy = app.destroy
        def finish():
            destroy()
            closed.set()
        app.destroy = finish
        app._on_close()
        assert pump(app, closed.is_set), "Normal close did not finish"
        assert not nx.ScanSessionStore.DIR.exists(), "Scan session remained"
        assert not nx.TEMP_DIR.exists(), "Temporary output remained"
        assert not nx.HISTORY_FILE.exists(), "Session history remained"

    def screenshot(app, label):
        folder = os.environ.get("NEXUS_VERIFY_SCREENSHOTS")
        if folder and os.name == "nt":
            assert pump(app, lambda: app.winfo_ismapped() and app.winfo_width() >= 400), "Screenshot window is not visible"
            app.lift()
            app.update()
            app.update_idletasks()
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=(app.winfo_rootx(), app.winfo_rooty(),
                                app.winfo_rootx() + app.winfo_width(),
                                app.winfo_rooty() + app.winfo_height())).save(target / (label + ".png"))

    nx.ScanSessionStore.prepare()
    if name == "reopen":
        assert nx.ScanSessionStore.load() == []
        app = nx.App()
        app.show("pdf")
        assert app.current.files == []
        app.show("img")
        assert app.current.files == []
        app.show("scan")
        assert app.current.pages == []
        close(app)
        return

    image = Image.effect_noise((800, 1100), 45).convert("RGB")
    ImageDraw.Draw(image).rectangle((30, 50, 400, 230), fill=(20, 100, 190))
    source = root / "imported.jpg"
    image.save(source, quality=96)
    source_bytes = source.read_bytes()
    scan = nx.ScanSessionStore.DIR / "page_fixture.png"
    image.save(scan, compress_level=2)
    nx.ScanSessionStore.record_result({"path": str(scan), "raw": str(scan)})
    nx.ScanSessionStore.save([str(scan)])
    if name == "seed":
        image.save(root / "exported.png")
        settings = nx.load_settings()
        settings.update(last_section="scan", sound=False)
        nx.save_settings(settings)
        nx.add_history({"mode": "Scan", "count": 1, "saved_mb": 1, "folder": str(root)})
        print("Seeded interrupted scan session and external export", flush=True)
        return
    app = nx.App()
    app.show("img")
    section = app.current
    section.add_paths([str(source)])

    if name == "busy-close":
        output = root / "existing.jpg"
        output.write_bytes(b"existing result must survive cancellation")
        entered = threading.Event()
        original = nexus_media._image_ok
        results = []
        def delayed_validation(path, size):
            entered.set()
            assert nx.stop_flag.wait(10), "Close never cancelled the worker"
            assert nx.TEMP_DIR.exists(), "Cleanup ran before the active writer finished"
            return original(path, size)
        nexus_media._image_ok = delayed_validation
        section._start_task(lambda: results.append(nx.process_image(str(source), str(output), 65, 0, "JPEG", False)))
        assert pump(app, entered.is_set), "Real image processing did not reach validation"
        close(app)
        assert results and results[0][2] == "stopped", results
        assert output.read_bytes() == b"existing result must survive cancellation"
        assert source.read_bytes() == source_bytes
        assert not list(root.glob(".nexus-*"))
        return

    pdf = root / "exported.pdf"
    assert nx.images_to_pdf([str(source), str(source)], str(pdf))[0]
    pdf_bytes = pdf.read_bytes()
    if name == "comparison":
        app.show("pdf")
        section = app.current
        window = ComparisonWindow(section, nx, "pdf", {
            "input": str(pdf), "preset": "custom", "dpi": 130,
            "quality": 55, "gray": False, "target_bytes": len(pdf_bytes) // 2})
        assert pump(app, lambda: window.images is not None, 60), window.status.cget("text")
        assert len(window.images) == 2 and window.page_menu.cget("values") == ["1", "2"]
        window.images = None
        window._load(1)
        assert pump(app, lambda: window.images is not None, 60), window.status.cget("text")
        assert window.page.get() == "2"
        screenshot(window, "comparison")
        window._close()
        app.show("scan")
        section = app.current
        section._page_sel = {str(scan)}
        assert pump(app, lambda: str(scan) in section._thumb_cache)
        scan_bytes = scan.read_bytes()
        for angle in (90, 180, 270, 0):
            section._rotate_selected(90)
            assert pump(app, lambda: not section._edit_busy and str(scan) in section._thumb_cache)
            assert nx.ScanSessionStore.rotation(str(scan)) == angle
            label = section._page_cards[str(scan)]["img"]
            assert str(label._label.cget("image")) in app.tk.call("image", "names"), "Rotation left a deleted Tk image"
        assert scan.read_bytes() == scan_bytes
        section._remove_pages()
        assert nx.ScanSessionStore.recovery()[0]["path"] == str(scan)
        section._show_recovery()
        assert pump(app, lambda: section._recovery_window is not None)
        screenshot(section._recovery_window, "recovery")
        restored = nx.ScanSessionStore.restore([str(scan)])
        assert restored == [str(scan)] and not nx.ScanSessionStore.recovery()
        close(app)
        assert source.read_bytes() == source_bytes and pdf.read_bytes() == pdf_bytes
        return

    section.effort_var.set(nx.t("effort_fast"))
    section.target_var.set("0,5")
    app.show("pdf")
    app.current.add_paths([str(pdf)])
    app.current.target_var.set("5")
    screenshot(app, "pdf")
    app.show("img")
    assert app.current is section and app.current.files == [str(source)]
    screenshot(app, "photos")
    app.show("vid")
    app.current.metadata_var.set(False)
    screenshot(app, "video")
    app.show("scan")
    assert app.current.pages == [str(scan)]
    screenshot(app, "scanner")
    nx.add_history({"mode": "Image", "count": 1, "saved_mb": 1, "folder": str(root)})
    nx.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    scratch = nx.TEMP_DIR / "scratch.png"
    image.save(scratch)
    scratch.chmod(0o400)
    candidate = AtomicOutput(root / "unfinished.pdf", nx.TEMP_DIR)
    candidate.__enter__()
    Path(candidate.path).write_bytes(b"incomplete")
    close(app)
    assert not Path(candidate.path).exists(), "A candidate on the destination volume remained"
    assert not (root / "unfinished.pdf").exists()
    assert source.read_bytes() == source_bytes and pdf.read_bytes() == pdf_bytes
    settings = nx.load_settings()
    assert settings["img_effort"] == "fast" and settings["pdf_target_mb"] == "5"
    assert settings["vid_strip_metadata"] is False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--root")
    args = parser.parse_args()
    if args.scenario:
        scenario(args.scenario, Path(args.root))
    else:
        with tempfile.TemporaryDirectory(prefix="nexus-lifecycle-") as directory:
            root = Path(directory)
            for name in ("normal-close", "reopen", "busy-close", "comparison"):
                case = root / ("normal-close" if name == "reopen" else name)
                case.mkdir(exist_ok=True)
                subprocess.run([sys.executable, __file__, "--scenario", name, "--root", str(case)],
                               check=True, timeout=150)
                print("PASS test_gui_" + name.replace("-", "_"), flush=True)
        print("ALL OK", flush=True)
