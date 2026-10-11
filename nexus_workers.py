"""Persistent, headless PDF processes, including windowed PyInstaller builds.

The GUI uses threads only to coordinate jobs. Every MuPDF document lives in a
separate process; socket IPC also works when a windowed exe has no stdout.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nexus_support import ProcessingCancelled, cleanup_stage_journal

_runtime = {}
_local = threading.local()


def configure(command, temp_root, stop, register, unregister, creationflags=0, startupinfo=None):
    _runtime.update(command=list(command), temp_root=Path(temp_root), stop=stop,
                    register=register, unregister=unregister, flags=creationflags,
                    startupinfo=startupinfo)


class PdfClient:
    def __init__(self):
        self.proc = self.conn = None
        self.buffer = b""
        self.journal = None
        self._start()

    def _start(self):
        if _runtime["stop"].is_set():
            raise ProcessingCancelled()
        token = secrets.token_hex(32)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.2)
        self.journal = _runtime["temp_root"] / f"pdf-worker-{token[:16]}"
        env = dict(os.environ)
        env.update(NEXUS_PDF_WORKER_TOKEN=token, NEXUS_MULTI="1",
                   NEXUS_WORKER_TEMP=str(self.journal))
        try:
            self.proc = subprocess.Popen(
                [*_runtime["command"], "--nexus-pdf-worker", str(listener.getsockname()[1])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, creationflags=_runtime["flags"], startupinfo=_runtime["startupinfo"])
            _runtime["register"](self.proc)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                self._check()
                try:
                    self.conn, _ = listener.accept()
                    self.conn.settimeout(0.2)
                    if self._receive().get("token") != token:
                        raise RuntimeError("err_worker")
                    return
                except socket.timeout:
                    continue
            raise RuntimeError("err_worker")
        except Exception:
            self.close()
            raise
        finally:
            listener.close()

    def _check(self):
        if _runtime["stop"].is_set():
            raise ProcessingCancelled()
        if self.proc is not None and self.proc.poll() is not None:
            raise RuntimeError("err_worker")

    def _receive(self):
        deadline = time.monotonic() + 600
        while True:
            self._check()
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                return json.loads(line.decode("utf-8"))
            if time.monotonic() > deadline:
                raise RuntimeError("err_worker_timeout")
            try:
                data = self.conn.recv(65536)
                if not data:
                    raise RuntimeError("err_worker")
                self.buffer += data
                if len(self.buffer) > 32 << 20:
                    raise RuntimeError("err_worker")
            except socket.timeout:
                continue

    def request(self, payload, progress=None):
        self._check()
        try:
            self.conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            while True:
                event = self._receive()
                if "progress" in event:
                    if progress:
                        progress(event["progress"])
                elif "result" in event:
                    return event["result"]
                elif "error" in event:
                    raise RuntimeError(event["error"])
        except Exception:
            self.close()
            raise

    def close(self):
        if self.proc is not None and _runtime["stop"].is_set() and self.proc.poll() is None:
            self.proc.kill()
        if self.conn is not None:
            try:
                self.conn.sendall(b'{"shutdown":true}\n')
            except OSError:
                pass
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            _runtime["unregister"](self.proc)
            self.proc = None
        if self.journal:
            cleanup_stage_journal(self.journal)
            import shutil
            shutil.rmtree(self.journal, ignore_errors=True)


class NexusExecutor(ThreadPoolExecutor):
    def __init__(self, max_workers):
        self.clients = []
        self.clients_lock = threading.Lock()
        super().__init__(max_workers=max_workers, initializer=self._initialize)

    def _initialize(self):
        _local.pool = self
        _local.client = None

    def client(self):
        current = getattr(_local, "client", None)
        if current is None or current.proc is None:
            current = PdfClient()
            _local.client = current
            with self.clients_lock:
                self.clients.append(current)
        return current

    def shutdown(self, wait=True, *, cancel_futures=False):
        try:
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
        finally:
            for client in self.clients:
                client.close()
            self.clients.clear()


def request_pdf(payload, progress=None):
    pool = getattr(_local, "pool", None)
    if pool is not None:
        return pool.client().request(payload, progress)
    client = PdfClient()
    try:
        return client.request(payload, progress)
    finally:
        client.close()


def run_pdf(inp, out, preset, dpi, quality, gray, progress=None, threads=1, target_bytes=0):
    try:
        return tuple(request_pdf({"op": "compress", "input": inp, "output": out,
                                  "preset": preset, "dpi": dpi, "quality": quality,
                                  "gray": gray, "threads": threads,
                                  "target_bytes": target_bytes}, progress))
    except ProcessingCancelled:
        return inp, False, "stopped", os.path.getsize(inp) if os.path.isfile(inp) else 0, 0
    except Exception as error:
        return inp, False, str(error)[:120], os.path.getsize(inp) if os.path.isfile(inp) else 0, 0


def serve(module, port):
    """Entry point in the same exe. No splash, singleton, or Tk root."""
    conn = socket.create_connection(("127.0.0.1", int(port)), timeout=30)
    conn.settimeout(None)
    stream = conn.makefile("rb")

    def send(event):
        try:
            conn.sendall((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            module.stop_flag.set()
            raise

    send({"token": os.environ.pop("NEXUS_PDF_WORKER_TOKEN", "")})
    try:
        for line in stream:
            payload = json.loads(line.decode("utf-8"))
            if payload.get("shutdown"):
                break
            module.stop_flag.clear()
            try:
                if payload["op"] == "compress":
                    result = module.process_pdf(
                        payload["input"], payload["output"], payload["preset"],
                        payload["dpi"], payload["quality"], payload["gray"],
                        progress_cb=lambda fraction: send({"progress": fraction}),
                        threads=payload.get("threads", 1), target_bytes=payload.get("target_bytes", 0))
                elif payload["op"] == "preview":
                    result = module.pdf_preview(payload)
                else:
                    raise ValueError("err_worker")
                send({"result": result})
            except Exception as error:
                send({"error": str(error)[:160]})
            finally:
                if module.fitz:
                    try:
                        module.fitz.TOOLS.store_shrink(100)
                    except Exception:
                        pass
    finally:
        stream.close()
        conn.close()
