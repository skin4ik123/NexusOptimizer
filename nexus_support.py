"""Small, GUI-independent safeguards shared by the media pipelines."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import tempfile
import threading
import uuid
import stat
from pathlib import Path


class ProcessingCancelled(Exception):
    pass


def paths_alias(source, destination):
    if os.path.normcase(os.path.realpath(source)) == os.path.normcase(os.path.realpath(destination)):
        return True
    try:
        return os.path.samefile(source, destination)
    except OSError:
        return False


def process_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # access denied: still alive
            try:
                code = ctypes.c_ulong()
                if not kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                    return True
                return code.value == 259
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def available_memory():
    """Current available RAM, not total RAM; works without an extra package."""
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            *[(name, ctypes.c_ulonglong) for name in
                              ("total", "available", "page_total", "page_available",
                               "virtual_total", "virtual_available", "extended")]]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available)
        else:
            with open("/proc/meminfo", encoding="ascii") as stream:
                for line in stream:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
    except (OSError, AttributeError, ValueError):
        pass
    return 1 << 30


def resource_workers(requested, paths, kind):
    """Leave RAM for Windows, the UI, native decoders and encoder buffers."""
    requested = max(1, int(requested))
    estimate = 384 << 20
    if kind == "pdf":
        biggest = max((os.path.getsize(p) for p in paths if os.path.isfile(p)), default=0)
        estimate = max(512 << 20, min(2 << 30, biggest * 6 + (320 << 20)))
        requested = min(requested, 6)
    elif kind == "image":
        from PIL import Image
        for path in paths:
            try:
                with Image.open(path) as image:
                    estimate = max(estimate, image.width * image.height * 12 + (96 << 20))
            except (OSError, ValueError):
                continue
    elif kind == "video":
        estimate = 768 << 20
        requested = min(requested, 2)
    return max(1, min(requested, max(1, int(available_memory() * 0.60) // estimate)))


def allocate_stage(destination, journal_root):
    destination = Path(destination).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    root = Path(journal_root)
    root.mkdir(parents=True, exist_ok=True)
    ident = uuid.uuid4().hex
    fd, name = tempfile.mkstemp(prefix=f".nexus-{ident}-", suffix=destination.suffix,
                                dir=destination.parent)
    os.close(fd)
    record = root / f"stage-{ident}.json"
    try:
        record.write_text(json.dumps({"id": ident, "path": name}), encoding="utf-8")
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise
    return name, record


def discard_stage(path, record=None):
    try:
        unlink_owned(path)
    finally:
        if record:
            unlink_owned(record)


def unlink_owned(path):
    path = Path(path)
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        if not path.is_symlink():
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        path.unlink(missing_ok=True)


def remove_owned_tree(path):
    def retry(function, item, exc):
        if os.path.islink(item):
            os.unlink(item)
            return
        os.chmod(item, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(item)
    if Path(path).exists():
        shutil.rmtree(path, onerror=retry)


def cleanup_stage_journal(root):
    """Only delete our uniquely named candidates, never a published output."""
    root = Path(root)
    if not root.exists():
        return
    for record in root.rglob("stage-*.json"):
        try:
            if record.is_symlink():
                continue
            data = json.loads(record.read_text(encoding="utf-8"))
            ident = data["id"]
            path = Path(data["path"])
            if len(ident) != 32 or not all(c in "0123456789abcdef" for c in ident):
                continue
            if not path.name.startswith(f".nexus-{ident}-"):
                continue
            unlink_owned(path)
            unlink_owned(record)
        except (OSError, ValueError, KeyError, TypeError):
            continue


class AtomicOutput:
    """A candidate on the destination volume; commit only after validation."""
    def __init__(self, destination, journal_root, stop=None):
        self.destination = os.path.abspath(destination)
        self.journal_root = journal_root
        self.stop = stop
        self.path = self.record = None

    def check_cancelled(self):
        if self.stop is not None and self.stop.is_set():
            raise ProcessingCancelled()

    def __enter__(self):
        self.check_cancelled()
        self.path, self.record = allocate_stage(self.destination, self.journal_root)
        return self

    def commit(self):
        self.check_cancelled()
        os.replace(self.path, self.destination)
        self.path = None
        self.record.unlink(missing_ok=True)
        self.record = None

    def __exit__(self, *exc):
        if self.path is not None:
            discard_stage(self.path, self.record)


def copy_atomic(source, destination, journal_root, stop=None):
    if paths_alias(source, destination):
        raise ValueError("err_same_file")
    with AtomicOutput(destination, journal_root, stop) as stage:
        shutil.copy2(source, stage.path)
        if os.path.getsize(source) != os.path.getsize(stage.path):
            raise OSError("err_bad_result")
        stage.commit()


def parse_target_bytes(value):
    try:
        mb = float(str(value or "0").strip().replace(",", "."))
        return int(mb * (1 << 20)) if 0 < mb <= 100000 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def limited_video_scale(height):
    # Never upscale. Even dimensions remain acceptable to 4:2:0 encoders.
    return f"scale=-2:trunc(min(ih\\,{int(height)})/2)*2"
