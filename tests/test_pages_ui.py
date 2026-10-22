"""Scanner page grid and file-list follow-ups, driven through real Tk windows.

  - every thumbnail stays reachable when the pages outgrow the window;
  - selection (click, Ctrl, Shift, the check mark) and rotation of one page,
    of a selection and of a whole upside-down stack;
  - reordering, also of a selection and onto the last place;
  - dragging pages out of the window exports them, dropping them back inside
    reorders instead of importing copies;
  - compressed photos can still be merged into one PDF afterwards.

Run:  python tests/test_pages_ui.py
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def scenario(name, root):
    # Every scenario runs in a new process with its own settings and temp.
    os.environ.update(APPDATA=str(root / "roaming"), LOCALAPPDATA=str(root / "local"), NEXUS_MULTI="1")
    (root / "temp").mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(root / "temp")
    sys.argv = [sys.argv[0]]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import NexusPDFOptimizer as nx
    from PIL import Image, ImageDraw

    def pump(app, condition, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.update()
            if condition():
                return True
            time.sleep(0.01)
        return False

    def settle(app, seconds=0.3):
        pump(app, lambda: False, seconds)

    def page_image(path, number, size=(620, 877), quality=90):
        image = Image.new("RGB", size, (246, 246, 240))
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 40, size[0] - 40, 140), fill=(30, 60, 160))
        draw.text((60, 70), f"PAGE {number}", fill=(255, 255, 255))
        image.save(path, quality=quality, dpi=(150, 150))
        return str(path)

    swallowed = []
    original_log_exc = nx._log_exc

    def record(where, error=None):
        # tkdnd 2.9.5 is built for Tcl 8.6; a Python on Tcl 9 runs without
        # drag and drop, which the program reports and tolerates.
        if where != "tkdnd init":
            swallowed.append(f"{where}: {error!r}")
        original_log_exc(where, error)

    nx._log_exc = record
    nx.ScanSessionStore.prepare()
    settings = nx.load_settings()
    settings.update(sound=False, scan_open_folder=False, last_section="pdf")
    nx.save_settings(settings)
    source = root / "in"
    source.mkdir()
    app = nx.App()
    app.geometry("1000x700+0+0")  # fits the 1024x768 CI desktop
    try:
        if name == "scroll":
            app.show("scan")
            sec = app.current
            pages = [page_image(source / f"p{i:02d}.jpg", i) for i in range(1, 41)]
            sec.add_paths(pages)
            assert pump(app, sec._cards_ready), "page cards were not created"
            assert sec.pages == pages, "dropped pictures become pages in order"
            canvas = sec.pages_area._parent_canvas

            def region_bottom():
                region = str(canvas.cget("scrollregion")).split()
                return float(region[3]) if len(region) == 4 else 0.0

            last = sec._page_cards[pages[-1]]["card"]
            assert pump(app, lambda: last.winfo_height() > 1 and
                        region_bottom() >= last.winfo_y() + last.winfo_height()), \
                f"scroll region {canvas.cget('scrollregion')} does not reach the last card " \
                f"(y={last.winfo_y()}, h={last.winfo_height()})"
            assert region_bottom() > canvas.winfo_height(), "40 pages do not fit, the grid must scroll"
            canvas.yview_moveto(1.0)
            settle(app)
            top, bottom = canvas.yview()
            assert top > 0 and bottom == 1.0, (top, bottom)
            visible = canvas.winfo_rooty() <= last.winfo_rooty() and \
                last.winfo_rooty() + last.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height() + 1
            assert visible, "the last page can be scrolled into view"

            # A page arriving from the scanner scrolls the grid to itself.
            canvas.yview_moveto(0.0)
            settle(app)
            scanned = nx.ScanSessionStore.DIR / "page_new.jpg"
            page_image(scanned, 41)
            sec._add_scanned_page({"path": str(scanned)})
            newest = sec._page_cards[str(scanned)]["card"]
            assert pump(app, lambda: canvas.yview()[1] == 1.0 and newest.winfo_rooty() >= canvas.winfo_rooty()), \
                f"the newest page is not shown: {canvas.yview()}"
            # The count is about pages, not about the (unused) file list.
            assert sec.pages_count.cget("text") == "41", sec.pages_count.cget("text")

            # A picture that is already a page is not added twice.
            sec.add_paths([pages[0]])
            assert len(sec.pages) == 41 and sec.pages.count(pages[0]) == 1

        elif name == "selection":
            app.show("scan")
            sec = app.current
            pages = [page_image(source / f"p{i}.jpg", i) for i in range(1, 7)]
            sec.add_paths(pages)
            assert pump(app, sec._cards_ready), "page cards were not created"
            settle(app)
            p = list(sec.pages)
            sec._page_click(p[0])
            assert sec._page_sel == {p[0]}
            sec._page_click(p[2], state=0x0001)            # Shift: range
            assert sec._page_sel == set(p[0:3]), sec._page_sel
            sec._page_click(p[5], state=0x0004)            # Ctrl: add
            assert sec._page_sel == set(p[0:3]) | {p[5]}
            assert "4" in sec.pages_count.cget("text") and "6" in sec.pages_count.cget("text"), \
                sec.pages_count.cget("text")
            sec._page_click(p[1])                          # plain click: only this one
            assert sec._page_sel == {p[1]}
            sec._page_click(p[1])                          # again: nothing selected
            assert sec._page_sel == set()
            sec._toggle_page(p[3])                         # the check mark adds
            sec._toggle_page(p[4])
            assert sec._page_sel == {p[3], p[4]}
            card = sec._page_cards[p[3]]
            assert card["check"].get() == 1, "a selected page shows a check mark"
            assert sec._page_cards[p[0]]["check"].get() == 0
            sec._page_cards[p[3]]["check"].toggle()        # a real click on the mark
            assert sec._page_sel == {p[4]} and card["check"].get() == 0
            sec._page_cards[p[3]]["check"].toggle()
            assert sec._page_sel == {p[3], p[4]} and card["check"].get() == 1
            sec._clear_selection()
            assert sec._page_sel == set()

            # Per-page rotation needs no selection; a picture dropped in from
            # outside is copied into the session first, the original stays.
            before = Path(p[0]).read_bytes()
            sec._rotate_pages([p[0]], 90)
            assert pump(app, lambda: not sec._edit_busy and sec.pages[0] != p[0]), "rotation did not apply"
            first = sec.pages[0]
            assert nx.ScanSessionStore.rotation(first) == 90 and Path(p[0]).read_bytes() == before
            with nx.ScanSessionStore.open_image(first) as image:
                assert image.width > image.height

            # 180° for a whole stack that came in upside down.
            sec._select_all_pages()
            sec._rotate_selected(180)
            assert pump(app, lambda: not sec._edit_busy and all(
                nx.ScanSessionStore.owns(x) for x in sec.pages)), "180° did not apply"
            assert nx.ScanSessionStore.rotation(sec.pages[0]) == 270
            assert all(nx.ScanSessionStore.rotation(x) == 180 for x in sec.pages[1:])
            assert len(sec._page_sel) == 6, "the selection follows the rotated copies"

            # Rotation is not blocked by an earlier Stop.
            nx.stop_flag.set()
            sec._rotate_pages([sec.pages[1]], 90)
            assert pump(app, lambda: not sec._edit_busy and nx.ScanSessionStore.rotation(sec.pages[1]) == 270)
            nx.stop_flag.clear()

            # Reorder: forward onto a card takes its place, so a page can
            # reach the end; a selection moves as one block.
            q = list(sec.pages)
            sec._clear_selection()
            sec._move_pages(q[0], q[2])
            assert sec.pages == [q[1], q[2], q[0], q[3], q[4], q[5]], sec.pages
            sec._move_pages(q[3], q[5])
            assert sec.pages[-1] == q[3]
            sec._move_pages(q[5], q[1])
            assert sec.pages[0] == q[5]
            order = list(sec.pages)
            sec._page_sel = {order[0], order[1]}
            sec._move_pages(order[0], None)                # onto empty space: to the end
            assert sec.pages == order[2:] + order[:2], sec.pages
            assert nx.ScanSessionStore.load() == sec.pages, "the order is saved"
            nums = [sec._page_cards[x]["num"].cget("text") for x in sec.pages]
            assert nums == [str(i) for i in range(1, 7)], nums

            # Remove and Restore without a selection explain what to do.
            sec._clear_selection()
            sec._remove_pages()
            assert len(sec.pages) == 6

        elif name == "drag-out":
            import fitz
            app.show("scan")
            sec = app.current
            pages = [page_image(source / f"p{i}.jpg", i) for i in range(1, 5)]
            sec.add_paths(pages)
            assert pump(app, sec._cards_ready), "page cards were not created"
            settle(app)
            sec._rotate_pages([sec.pages[2]], 270)
            assert pump(app, lambda: not sec._edit_busy and nx.ScanSessionStore.owns(sec.pages[2]))
            p = list(sec.pages)
            if getattr(app, "TkdndVersion", None):
                # the thumbnail itself is the drag source, the buttons are not
                img = sec._page_cards[p[0]]["img"]
                assert "TkDND_Drag1" in img._label.bindtags(), img._label.bindtags()
                assert app.tk.call("bind", img._label._w, "<<DragInitCmd>>")
                assert sec.drag_fmt_var.get() == "PDF"
                print("tkdnd", app.TkdndVersion, flush=True)
            else:
                print("tkdnd unavailable: drag source registration not checked", flush=True)

            # One page, not selected: a one-page PDF named after its number.
            sec.drag_fmt_var.set("PDF")
            result = sec._on_drag_init(None, p[1])
            actions, types, files = result
            assert "copy" in actions and nx.DND_FILES in types, result
            assert len(files) == 1 and files[0].endswith("_p02.pdf"), files
            with fitz.open(files[0]) as doc:
                assert doc.page_count == 1
            sec._on_drag_end(None)

            # A selection travels together, in page order, rotated as shown.
            sec._page_sel = {p[2], p[0]}
            _a, _t, files = sec._on_drag_init(None, p[2])
            assert len(files) == 1, files
            with fitz.open(files[0]) as doc:
                assert doc.page_count == 2
                assert doc[0].rect.width < doc[0].rect.height
                assert doc[1].rect.width > doc[1].rect.height, "rotation is applied in the export"
            sec._on_drag_end(None)

            sec.drag_fmt_var.set("JPG")
            _a, _t, files = sec._on_drag_init(None, p[0])
            assert [Path(f).suffix for f in files] == [".jpg", ".jpg"], files
            with Image.open(files[1]) as image:
                assert image.width > image.height
            assert all(Path(x).read_bytes() for x in p), "pages are untouched"
            assert nx.load_settings().get("scan_drag_format") == "jpg", "the format is remembered"

            # Dropped back on a card of the same window: reorder, no copies.
            sec._page_sel = set()
            sec._on_drag_end(None)
            sec.drag_fmt_var.set("PDF")
            _a, _t, files = sec._on_drag_init(None, p[0])
            target = sec._page_cards[p[3]]["img"]
            settle(app)

            class Drop:
                pass

            event = Drop()
            event.data = " ".join("{%s}" % f for f in files)
            event.x_root = target.winfo_rootx() + 20
            event.y_root = target.winfo_rooty() + 20
            event.action = "copy"
            sec._drop_hover(event.x_root, event.y_root)
            assert sec._page_cards[p[3]]["card"].cget("border_color") == nx.CLR_WARN, "drop target is marked"
            app._on_tkdnd(event)
            sec._on_drag_end(None)
            assert sec.pages == [p[1], p[2], p[3], p[0]], sec.pages
            assert len(sec.pages) == 4, "own export was not imported as new pages"

            # Dropped somewhere else in the window: nothing happens.
            _a, _t, files = sec._on_drag_init(None, p[1])
            event.data = " ".join("{%s}" % f for f in files)
            event.x_root = sec.log.winfo_rootx() + 10
            event.y_root = sec.log.winfo_rooty() + 10
            assert sec._drop_hover(event.x_root, event.y_root) == "refuse_drop"
            app._on_tkdnd(event)
            sec._on_drag_end(None)
            assert sec.pages == [p[1], p[2], p[3], p[0]] and len(sec.pages) == 4

            # Files dragged in from outside are still imported.
            outside = page_image(source / "outside.jpg", 9)
            event.data = "{%s}" % outside
            app._on_tkdnd(event)
            assert sec.pages[-1] == outside and len(sec.pages) == 5

        elif name == "photo-merge":
            import fitz
            app.show("img")
            sec = app.current
            big = []
            for i in range(3):
                path = source / f"photo{i}.jpg"
                Image.effect_noise((1800, 1300), 40 + i).convert("RGB").save(path, quality=97)
                big.append(str(path))
            small = source / "small.jpg"
            Image.new("RGB", (120, 90), (200, 30, 30)).save(small, quality=30)
            sec.add_paths(big + [str(small)])
            sec.multi_var.set(False)
            sec._start()
            assert sec.is_running
            assert pump(app, lambda: not sec.is_running, 120), "compression did not finish"
            states = {Path(x).name: sec._rows[x]["state"] for x in sec.files}
            assert sum(state == "done" for state in states.values()) >= 3, states
            assert set(states.values()) <= {"done", "unchanged"}, states
            assert all(os.path.isfile(x) for x in sec.files), \
                f"the list points to files that are gone: {sec.files}"
            assert all("_nexus" in Path(x).name for x in sec.files if sec._rows[x]["state"] == "done")

            out = root / "merged.pdf"
            nx.filedialog.asksaveasfilename = lambda **kw: str(out)
            sec._to_pdf()
            assert pump(app, lambda: not sec.is_running and out.exists(), 60), "no PDF"
            with fitz.open(out) as doc:
                assert doc.page_count == 4, f"every photo is in the PDF, got {doc.page_count}"

            # Start again: compressed files are not compressed once more.
            before = sorted(x.name for x in source.rglob("*"))
            sec._start()
            settle(app, 1.0)
            assert not sec.is_running
            assert sorted(x.name for x in source.rglob("*")) == before, "nothing new was written"
        else:
            raise SystemExit(f"unknown scenario {name}")
        settle(app)
        assert not swallowed, f"errors caught and logged during the scenario: {swallowed}"
    finally:
        try:
            app.current.on_leave()
        except Exception:
            pass
        app.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--root")
    parser.add_argument("only", nargs="*")
    args = parser.parse_args()
    if args.scenario:
        scenario(args.scenario, Path(args.root))
    else:
        failed = 0
        with tempfile.TemporaryDirectory(prefix="nexus-pages-") as directory:
            for name in args.only or ("scroll", "selection", "drag-out", "photo-merge"):
                case = Path(directory) / name
                case.mkdir()
                run = subprocess.run([sys.executable, __file__, "--scenario", name, "--root", str(case)],
                                     capture_output=True, text=True, timeout=240)
                if run.returncode:
                    failed += 1
                    tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-6:])
                    print(f"FAIL test_{name.replace('-', '_')}\n{tail}", flush=True)
                else:
                    print(f"PASS test_{name.replace('-', '_')}", flush=True)
        print("ALL OK" if not failed else f"{failed} FAILED", flush=True)
        sys.exit(1 if failed else 0)
