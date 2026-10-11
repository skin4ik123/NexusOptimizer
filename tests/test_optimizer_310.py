"""Regression checks for session safety, orientation, media and native workers."""
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEXUS_MULTI", "1")
import NexusPDFOptimizer as nx
from PIL import Image, ImageChops, ImageDraw, ImageOps
from nexus_support import (AtomicOutput, copy_atomic, cleanup_stage_journal, paths_alias,
                           parse_target_bytes, remove_owned_tree, resource_workers)


class StoreFixture:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nexus-store-test-")
        self.root = Path(self.temp.name)
        self.saved = nx.ScanSessionStore.DIR, nx.ScanSessionStore.FILE, nx.ScanSessionStore.BASE
        nx.ScanSessionStore.DIR = self.root / "session"
        nx.ScanSessionStore.FILE = nx.ScanSessionStore.DIR / "pages.json"
        nx.ScanSessionStore.BASE = self.root / "sessions"
        nx.ScanSessionStore.DIR.mkdir()
        nx.stop_flag.clear()
        return self.root

    def __exit__(self, *args):
        nx.ScanSessionStore.DIR, nx.ScanSessionStore.FILE, nx.ScanSessionStore.BASE = self.saved
        self.temp.cleanup()
        nx.stop_flag.clear()


def photo(size=(800, 1100)):
    image = Image.effect_noise(size, 50).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((size[0] // 16, size[1] // 16, size[0] // 2, size[1] // 3), fill=(30, 140, 210))
    return image


def make_pdf(path, size=(1000, 1400), quality=96):
    data = io.BytesIO()
    photo(size).save(data, "JPEG", quality=quality)
    with nx.fitz.open() as document:
        page = document.new_page(width=500, height=700)
        page.insert_image(page.rect, stream=data.getvalue())
        page.insert_text((20, 30), "Searchable text stays")
        document.set_toc([[1, "Bookmark", 1]])
        document.save(path)


def test_faint_ink_is_not_blank_or_erased_by_bw():
    image = Image.new("L", (800, 1100), 245)
    draw = ImageDraw.Draw(image)
    draw.line((100, 500, 680, 540), fill=220, width=3)
    assert not nx.is_blank_page(image)
    assert nx.to_black_and_white(image).convert("L").getpixel((390, 520)) == 0
    dark = Image.new("L", (800, 1100), 30)
    ImageDraw.Draw(dark).line((100, 500, 680, 500), fill=200, width=3)
    assert not nx.is_blank_page(dark), "Dark paper is not an empty white sheet"


def test_save_failure_retains_received_scan():
    with StoreFixture() as root:
        source = root / "received.bmp"
        photo((100, 140)).save(source)
        original = source.read_bytes()
        invalid = root / "not-a-directory"
        invalid.write_bytes(b"fixture")
        try:
            nx.finalize_scan_page(str(source), invalid, "gray", False, False)
        except OSError:
            pass
        else:
            raise AssertionError("Invalid destination unexpectedly worked")
        assert source.read_bytes() == original
        saved = nx.save_page_image
        try:
            nx.save_page_image = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
            result = nx.finalize_scan_page(str(source), nx.ScanSessionStore.DIR, "gray", False, False)
        finally:
            nx.save_page_image = saved
        assert result.get("error")
        assert Path(result["recovery_path"]).read_bytes() == original
        nx.ScanSessionStore.record_result(result)
        assert nx.ScanSessionStore.recovery()[0]["path"] == result["recovery_path"]


def test_suspected_blank_is_recoverable_and_lossless():
    with StoreFixture() as root:
        source = root / "blank.bmp"
        image = Image.new("RGB", (180, 240), (245, 244, 242))
        image.save(source, dpi=(300, 300))
        result = nx.finalize_scan_page(str(source), nx.ScanSessionStore.DIR, "color", False, True)
        assert result["blank"] and not source.exists()
        with Image.open(result["recovery_path"]) as recovered:
            assert ImageChops.difference(image, recovered.convert("RGB")).getbbox() is None
        nx.ScanSessionStore.record_result(result)
        restored = nx.ScanSessionStore.restore([result["recovery_path"]])
        assert restored == [result["recovery_path"]]
        nx.ScanSessionStore.save(restored)
        assert nx.ScanSessionStore.load() == restored
        assert nx.ScanSessionStore.recovery() == []


def test_scan_rotation_never_reencodes_jpeg():
    with StoreFixture() as root:
        source = root / "phone.jpg"
        photo((180, 240)).save(source, quality=90)
        original = source.read_bytes()
        rotated = nx.rotate_page_file(str(source), 90, nx.ScanSessionStore.DIR)
        assert Path(rotated).read_bytes() == original and source.read_bytes() == original
        assert nx.ScanSessionStore.open_image(rotated).size == (240, 180)
        for _ in range(3):
            assert nx.rotate_page_file(rotated, 90, nx.ScanSessionStore.DIR) == rotated
        assert Path(rotated).read_bytes() == original
        assert nx.ScanSessionStore.open_image(rotated).size == (180, 240)


def test_exif_all_orientations_render_correctly_without_jpeg_reencoding():
    with StoreFixture() as root:
        image = Image.new("RGB", (100, 80))
        draw = ImageDraw.Draw(image)
        for box, color in [((0, 0, 49, 39), "red"), ((50, 0, 99, 39), "green"),
                           ((0, 40, 49, 79), "blue"), ((50, 40, 99, 79), "yellow")]:
            draw.rectangle(box, fill=color)
        for orientation in range(1, 9):
            exif = Image.Exif()
            exif[274] = orientation
            source, output = root / f"phone{orientation}.jpg", root / f"out{orientation}.pdf"
            image.save(source, quality=94, exif=exif, dpi=(100, 100))
            assert nx.images_to_pdf([str(source)], str(output))[0]
            with Image.open(source) as opened:
                reference = ImageOps.exif_transpose(opened).convert("RGB")
            with nx.fitz.open(output) as document:
                assert document.xref_stream_raw(document[0].get_images()[0][0]) == source.read_bytes()
                pixels = document[0].get_pixmap(dpi=100, alpha=False)
                rendered = Image.frombytes("RGB", (pixels.width, pixels.height), pixels.samples)
            for x, y in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)):
                a = reference.getpixel((int(reference.width * x), int(reference.height * y)))
                b = rendered.getpixel((int(rendered.width * x), int(rendered.height * y)))
                assert max(abs(v - w) for v, w in zip(a, b)) <= 5, (orientation, x, y, a, b)


def test_multipage_tiff_and_failed_import_do_not_add_blank_pages():
    with StoreFixture() as root:
        source = root / "pages.tiff"
        Image.new("RGB", (100, 140), "red").save(source, save_all=True,
                                                append_images=[Image.new("RGB", (100, 140), "blue")])
        broken = root / "broken.jpg"
        broken.write_bytes(b"not an image")
        output = root / "pages.pdf"
        result = nx.images_to_pdf([str(source), str(broken)], str(output))
        assert result[0] and result[2:] == (2, 1), result
        with nx.fitz.open(output) as document:
            assert document.page_count == 2
            assert all(page.get_images() for page in document)


def test_photo_no_gain_alias_cancel_and_decode_error_preserve_files():
    with StoreFixture() as root:
        source, output = root / "small.jpg", root / "existing.jpg"
        Image.new("RGB", (80, 100), "white").save(source, quality=30, optimize=True)
        original = source.read_bytes()
        output.write_bytes(b"existing result")
        before = output.read_bytes()
        assert nx.process_image(str(source), str(source), 30, 0, "JPEG", False)[2] == "err_same_file"
        assert source.read_bytes() == original
        result = nx.process_image(str(source), str(output), 30, 0, "JPEG", False)
        assert result[2] == "err_no_gain" and output.read_bytes() == before
        nx.stop_flag.set()
        assert nx.process_image(str(source), str(output), 60, 0, "JPEG", True)[2] == "stopped"
        nx.stop_flag.clear()
        broken = root / "bad.png"
        broken.write_bytes(b"bad data")
        assert not nx.process_image(str(broken), str(output), 60, 0, "JPEG", True)[1]
        assert output.read_bytes() == before
        assert not list(root.glob(".nexus-*"))


def test_photo_target_size_and_decoder_reduction():
    with StoreFixture() as root:
        source = root / "large.jpg"
        photo((3200, 2400)).save(source, quality=98)
        output = root / "small.jpg"
        result = nx.process_image(str(source), str(output), 85, 1200, "JPEG", True, "balanced", 150000)
        assert result[1] and result[4] <= 150000, result
        with Image.open(output) as image:
            assert image.size == (1200, 900)
        result = nx.process_image(str(source), str(root / "tiny.jpg"), 85, 1200,
                                  "JPEG", True, "balanced", 100)
        assert result[1] and result[2] == "OK_TARGET_MISSED", result


def test_palette_and_alpha_are_not_silently_changed():
    with StoreFixture() as root:
        source, output = root / "alpha.png", root / "optimized.png"
        image = Image.new("RGBA", (240, 180), (100, 180, 210, 80))
        ImageDraw.Draw(image).rectangle((30, 40, 210, 140), fill=(20, 30, 40, 240))
        image.save(source, compress_level=0)
        assert nx.process_image(str(source), str(output), 60, 0, "PNG", True)[1]
        with Image.open(output) as result:
            difference = ImageChops.difference(image, result.convert("RGBA"))
            assert all(channel.getbbox() is None for channel in difference.split())


def test_animated_images_are_kept_intact():
    with StoreFixture() as root:
        source = root / "animation.gif"
        Image.new("RGB", (40, 40), "red").save(source, save_all=True,
                                               append_images=[Image.new("RGB", (40, 40), "blue")], duration=100, loop=0)
        original = source.read_bytes()
        assert nx.process_image(str(source), str(root / "out.webp"), 70, 0, "WEBP", True)[2] == "err_animated"
        assert source.read_bytes() == original and not (root / "out.webp").exists()


def test_target_pdf_preserves_search_and_bookmarks_on_every_attempt():
    with StoreFixture() as root:
        source, output = root / "in.pdf", root / "out.pdf"
        make_pdf(source)
        result = nx.process_pdf(str(source), str(output), "smart", 130, 68, False, target_bytes=120000)
        assert result[1], result
        with nx.fitz.open(source) as original, nx.fitz.open(output) as processed:
            assert original[0].get_text() == processed[0].get_text()
            assert original.get_toc() == processed.get_toc()
            assert processed[0].get_images()
        assert not list(root.glob(".nexus-*"))


def test_isolated_pdf_workers_reuse_processes_and_clean_up():
    with StoreFixture() as root:
        source = root / "in.pdf"
        make_pdf(source, (500, 700))
        clients = []
        with nx.get_executor(2) as executor:
            def job(index):
                result = nx.nexus_workers.run_pdf(str(source), str(root / f"out{index}.pdf"), "custom", 100, 40, False)
                assert result[1], result
                return nx.nexus_workers._local.client.proc.pid
            first = [executor.submit(job, index) for index in range(2)]
            pids = {future.result(timeout=60) for future in first}
            assert os.getpid() not in pids and len(pids) == 2
            second = [executor.submit(job, index) for index in range(2, 4)]
            assert {future.result(timeout=60) for future in second} <= pids
            clients = [client.proc for client in executor.clients]
        assert all(process.poll() is not None for process in clients)


def test_atomic_cleanup_and_session_clear_preserve_external_documents():
    with StoreFixture() as root:
        external, exported = root / "input.jpg", root / "exported.pdf"
        photo((40, 60)).save(external)
        exported.write_bytes(b"saved document")
        source_before = external.read_bytes()
        owned = nx.ScanSessionStore.DIR / "page_1.jpg"
        copy_atomic(external, owned, nx.TEMP_DIR)
        owned.chmod(0o400)
        nx.ScanSessionStore.save([str(owned), str(external)])
        journal = root / "journal"
        destination = root / "existing.jpg"
        destination.write_bytes(b"existing")
        stage = AtomicOutput(destination, journal)
        stage.__enter__()
        Path(stage.path).write_bytes(b"incomplete")
        cleanup_stage_journal(journal)
        assert destination.read_bytes() == b"existing" and not Path(stage.path).exists()
        nx.ScanSessionStore.clear()
        assert not nx.ScanSessionStore.DIR.exists()
        assert external.read_bytes() == source_before and exported.read_bytes() == b"saved document"


def test_interrupted_session_recovery_does_not_touch_live_instances():
    with StoreFixture() as root:
        nx.ScanSessionStore.BASE.mkdir()
        dead = nx.ScanSessionStore.BASE / "session_99999999_old"
        dead.mkdir()
        (dead / "owner.json").write_text('{"pid":99999999}')
        page = dead / "raw_interrupted.png"
        photo((40, 60)).save(page)
        live = nx.ScanSessionStore.BASE / "session_live"
        live.mkdir()
        (live / "owner.json").write_text('{"pid":' + str(os.getpid()) + '}')
        nx.ScanSessionStore.prepare()
        assert live.exists(), "A living session must not be adopted or erased"
        assert len(nx.ScanSessionStore.recovery()) == 1
        nx.ScanSessionStore.clear()
        assert live.exists()


def test_goal_parser_and_memory_limits():
    assert parse_target_bytes("5,5") == int(5.5 * (1 << 20))
    assert parse_target_bytes("nan") == 0
    assert parse_target_bytes("-1") == 0
    import nexus_support
    before = nexus_support.available_memory
    try:
        nexus_support.available_memory = lambda: 700 << 20
        assert resource_workers(12, [], "pdf") == 1
        assert resource_workers(12, [], "video") == 1
    finally:
        nexus_support.available_memory = before


def test_video_never_upscales_and_preserves_existing_destination_on_failure():
    with StoreFixture() as root:
        ff = nx.find_ffmpeg()
        assert ff, "Bundled FFmpeg must be available"
        source, output = root / "small.mp4", root / "compressed.mp4"
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=12", "-t", "1", "-c:v", "libx264", "-crf", "0",
                        str(source)], check=True, timeout=30)
        original = source.read_bytes()
        result = nx.process_video(str(source), str(output), "H.264", 40, "ultrafast", 720, 0, use_hw=False)
        assert result[1], result
        probe = subprocess.run([ff, "-i", str(output)], capture_output=True, timeout=10)
        assert b"640x360" in probe.stderr and b"1280x720" not in probe.stderr
        assert source.read_bytes() == original
        output.write_bytes(b"existing output")
        broken = root / "broken.mp4"
        broken.write_bytes(b"not a video")
        assert not nx.process_video(str(broken), str(output), "H.264", 30, "fast", 720, 0, use_hw=False)[1]
        assert output.read_bytes() == b"existing output"
        assert nx.process_video(str(source), str(source), "H.264", 30, "fast", 0, 0, use_hw=False)[2] == "err_same_file"


if __name__ == "__main__":
    failed = []
    for name, value in sorted(list(globals().items())):
        if name.startswith("test_") and callable(value):
            try:
                value()
                print("PASS", name, flush=True)
            except Exception as error:
                import traceback
                traceback.print_exc()
                print("FAIL", name, str(error), flush=True)
                failed.append(name)
    print("ALL OK" if not failed else str(len(failed)) + " FAILED", flush=True)
    sys.exit(bool(failed))
