"""A scan session survives a crash, and is erased after a normal close."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from nexus_support import AtomicOutput, copy_atomic, process_alive, remove_owned_tree


class ScanStore:
    DIR = FILE = BASE = JOURNAL = None
    _lock = threading.RLock()

    @classmethod
    def _file(cls):
        return cls.FILE if Path(cls.FILE).parent == Path(cls.DIR) else Path(cls.DIR) / "pages.json"

    @classmethod
    def _read(cls):
        try:
            value = json.loads(cls._file().read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
        except (OSError, ValueError):
            value = {}
        for key, default in (("pages", []), ("pending", []), ("recovery", []),
                             ("rotations", {}), ("originals", {})):
            if not isinstance(value.get(key), type(default)):
                value[key] = default
        value["version"] = 2
        return value

    @classmethod
    def _write(cls, data):
        Path(cls.DIR).mkdir(parents=True, exist_ok=True)
        with AtomicOutput(cls._file(), cls.JOURNAL) as stage:
            Path(stage.path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            stage.commit()

    @classmethod
    def _key(cls, path):
        return Path(path).name if cls.owns(path) else os.path.abspath(path)

    @classmethod
    def _path(cls, key):
        return str(Path(cls.DIR) / key) if not os.path.isabs(key) else key

    @classmethod
    def owns(cls, path):
        try:
            return os.path.normcase(os.path.realpath(Path(path).parent)) == os.path.normcase(os.path.realpath(cls.DIR))
        except (OSError, TypeError):
            return False

    @classmethod
    def prepare(cls):
        """Separate living instances; adopt the latest abandoned scan session."""
        base = Path(cls.BASE)
        base.mkdir(parents=True, exist_ok=True)
        candidates = []
        for folder in base.glob("session_*"):
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                owner = json.loads((folder / "owner.json").read_text(encoding="utf-8"))
                if not process_alive(owner.get("pid", 0)):
                    candidates.append(folder)
            except (OSError, ValueError):
                candidates.append(folder)
        cls.DIR = base / f"session_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        cls.FILE = cls.DIR / "pages.json"
        if candidates:
            old = max(candidates, key=lambda item: item.stat().st_mtime)
            os.replace(old, cls.DIR)
        else:
            cls.DIR.mkdir(parents=True, exist_ok=True)
            # Upgrade the flat 3.9 session without touching external pictures.
            for item in list(base.iterdir()):
                if item.is_file() and (item.name == "pages.json" or item.name.startswith(("page_", "raw_"))):
                    os.replace(item, cls.DIR / item.name)
        (cls.DIR / "owner.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        # Reconcile a received raster left between processing and its UI ack.
        cls.sweep(cls.load())

    @classmethod
    def load(cls):
        with cls._lock:
            data = cls._read()
            keys = list(dict.fromkeys(data["pages"] + data["pending"]))
            return [cls._path(key) for key in keys if os.path.isfile(cls._path(key))]

    @classmethod
    def save(cls, pages):
        with cls._lock:
            data = cls._read()
            data["pages"] = [cls._key(path) for path in pages]
            data["pending"] = [key for key in data["pending"] if key not in data["pages"]]
            cls._write(data)

    @classmethod
    def record_result(cls, result):
        with cls._lock:
            data = cls._read()
            path, raw = result.get("path"), result.get("raw")
            if path:
                key = cls._key(path)
                if key not in data["pages"] + data["pending"]:
                    data["pending"].append(key)
                if raw:
                    data["originals"][key] = cls._key(raw)
            elif result.get("recovery_path"):
                key = cls._key(result["recovery_path"])
                reason = "blank" if result.get("blank") else "error"
                if not any(item["path"] == key for item in data["recovery"]):
                    data["recovery"].append({"path": key, "reason": reason})
            cls._write(data)

    @classmethod
    def recovery(cls):
        with cls._lock:
            return [{**item, "path": cls._path(item["path"])} for item in cls._read()["recovery"]
                    if isinstance(item, dict) and "path" in item and os.path.isfile(cls._path(item["path"]))]

    @classmethod
    def recover_page(cls, path, reason="removed"):
        with cls._lock:
            data = cls._read()
            key = cls._key(path)
            if not any(item["path"] == key for item in data["recovery"]):
                data["recovery"].append({"path": key, "reason": reason})
            data["pending"] = [item for item in data["pending"] if item != key]
            data["pages"] = [item for item in data["pages"] if item != key]
            cls._write(data)

    @classmethod
    def restore(cls, paths):
        restored = []
        for path in paths:
            try:
                with Image.open(path) as image:
                    image.load()
                restored.append(path)
            except (OSError, ValueError):
                continue
        with cls._lock:
            data = cls._read()
            keys = {cls._key(path) for path in restored}
            data["recovery"] = [item for item in data["recovery"] if item["path"] not in keys]
            data["pending"] = list(dict.fromkeys(data["pending"] + [cls._key(path) for path in restored]))
            cls._write(data)
        return restored

    @classmethod
    def rotation(cls, path):
        if not cls.owns(path):
            return 0
        with cls._lock:
            return int(cls._read()["rotations"].get(cls._key(path), 0)) % 360

    @classmethod
    def original(cls, path):
        with cls._lock:
            key = cls._read()["originals"].get(cls._key(path))
            result = cls._path(key) if key else None
            return result if result and os.path.isfile(result) else path

    @classmethod
    def rotate(cls, path, degrees):
        with cls._lock:
            data = cls._read()
            key = cls._key(path)
            data["rotations"][key] = (int(data["rotations"].get(key, 0)) + int(degrees)) % 360
            cls._write(data)

    @classmethod
    def sweep(cls, keep):
        with cls._lock:
            data = cls._read()
            kept = {cls._key(path) for path in keep} | set(data["pending"])
            kept |= {item["path"] for item in data["recovery"] if isinstance(item, dict) and "path" in item}
            originals = {raw for key, raw in data["originals"].items() if key in kept}
            data["originals"] = {key: raw for key, raw in data["originals"].items() if key in kept}
            data["rotations"] = {key: value for key, value in data["rotations"].items() if key in kept}
            kept |= originals
            if Path(cls.DIR).exists():
                for item in Path(cls.DIR).iterdir():
                    if item.is_symlink() or not item.is_file() or item.name in kept:
                        continue
                    if item.name.startswith("raw_"):
                        data["recovery"].append({"path": item.name, "reason": "interrupted"})
                    elif item.name.startswith("page_"):
                        item.unlink(missing_ok=True)
                cls._write(data)

    @classmethod
    def clear(cls):
        """Only owned storage is erased; imported/exported user files stay put."""
        with cls._lock:
            remove_owned_tree(cls.DIR)

    @classmethod
    def open_image(cls, path, draft_size=None):
        with Image.open(path) as original:
            if draft_size and original.format == "JPEG":
                original.draft("RGB", draft_size)
            image = ImageOps.exif_transpose(original)
            image.load()
        angle = cls.rotation(path)
        operation = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
                     270: Image.Transpose.ROTATE_270}.get(angle)
        return image.transpose(operation) if operation else image
