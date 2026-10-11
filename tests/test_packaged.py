"""Run the shipped windowed executable as real persistent PDF workers."""
import argparse
import io
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEXUS_MULTI", "1")
import NexusPDFOptimizer as nx
import nexus_workers
from PIL import Image


def main(executable):
    with tempfile.TemporaryDirectory(prefix="nexus-packaged-") as directory:
        root = Path(directory)
        saved = dict(nexus_workers._runtime)
        nx.stop_flag.clear()
        nexus_workers.configure([str(Path(executable).resolve())], root / "workers", nx.stop_flag,
                                nx._register_proc, nx._unregister_proc, nx.CNW, nx._startupinfo())
        try:
            source = root / "source.pdf"
            with nx.fitz.open() as document:
                for index in range(12):
                    data = io.BytesIO()
                    Image.effect_noise((1600, 2200), 50 + index).convert("RGB").save(data, "JPEG", quality=96)
                    page = document.new_page(width=500, height=700)
                    page.insert_image(page.rect, stream=data.getvalue())
                    page.insert_text((20, 30), "Keep searchable text " + str(index))
                document.set_toc([[1, "Bookmark", 1]])
                document.save(source)
            source_bytes = source.read_bytes()
            with nexus_workers.NexusExecutor(max_workers=2) as executor:
                barrier = threading.Barrier(2)
                def work(index):
                    barrier.wait(timeout=30)
                    output = root / (str(index) + ".pdf")
                    progress = []
                    result = nexus_workers.run_pdf(str(source), str(output), "custom", 130, 55,
                                                   False, progress.append)
                    assert result[1] and progress, result
                    pid = nexus_workers._local.client.proc.pid
                    repeated = nexus_workers.run_pdf(str(source), str(output), "custom", 130, 55, False)
                    assert repeated[1] and nexus_workers._local.client.proc.pid == pid
                    return pid, output
                futures = [executor.submit(work, index) for index in range(2)]
                results = [future.result(timeout=180) for future in futures]
                pids = {pid for pid, output in results}
                assert len(pids) == 2
                processes = [client.proc for client in executor.clients]
            assert all(process.poll() == 0 for process in processes)
            for pid, output in results:
                assert nx._pdf_output_ok(str(output), 12, str(source))
            assert not list((root / "workers").glob("pdf-worker-*"))
            print("PASS test_windowed_packaged_workers_reuse_and_validation", flush=True)

            output = root / "existing.pdf"
            output.write_bytes(b"existing destination")
            def cancel(fraction):
                nx.stop_flag.set()
            result = nexus_workers.run_pdf(str(source), str(output), "custom", 130, 55, False, cancel)
            assert result[2] == "stopped", result
            assert output.read_bytes() == b"existing destination"
            assert source.read_bytes() == source_bytes
            assert not list(root.glob(".nexus-*"))
            assert not list((root / "workers").glob("pdf-worker-*"))
            print("PASS test_windowed_packaged_cancel_preserves_files", flush=True)
        finally:
            nx.stop_flag.clear()
            nexus_workers._runtime.clear()
            nexus_workers._runtime.update(saved)
        print("ALL OK", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    main(parser.parse_args().exe)
