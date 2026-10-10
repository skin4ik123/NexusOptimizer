"""Reported failures, with real PDF streams, renderers and atomic output."""
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import NexusPDFOptimizer as nexus
import fitz
from PIL import Image


def jpeg(quality=96):
    img = Image.effect_noise((1000, 1400), 55).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=quality, optimize=True)
    return buf.getvalue()


def document(path, masked=False, transparent=False):
    with fitz.open() as doc:
        page = doc.new_page(width=720, height=1008)  # image at exactly 100 DPI
        image = page.insert_image(page.rect, stream=jpeg())
        if masked:
            mask = doc.get_new_xref()
            doc.update_object(mask, '<</Type /XObject /Subtype /Image /Width 1000 /Height 1400 /BitsPerComponent 8 /ColorSpace /DeviceGray>>')
            pixels = (bytes(range(256)) * (1400000 // 256 + 1))[:1400000] if transparent else b'\xff' * 1400000
            doc.update_stream(mask, pixels, compress=True)
            doc.xref_set_key(image, 'SMask', f'{mask} 0 R')
        page.insert_text((36, 36), 'Invoice 4471')
        page.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(36, 24, 140, 40), 'uri': 'https://example.com'})
        widget = fitz.Widget()
        widget.field_name = 'customer'
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.field_value = 'Acme Ltd'
        widget.rect = fitz.Rect(36, 45, 180, 65)
        page.add_widget(widget)
        doc.set_toc([[1, 'Invoice', 1]])
        doc.save(path)


def compress(source, output, threads=1):
    return nexus.process_pdf(str(source), str(output), 'custom', 100, 40, False, threads=threads)


def test_quality_applies_at_target_dpi_and_repeated_runs_keep_content():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / 'source.pdf'
        document(source)
        original = source.read_bytes()
        for index in range(4):
            output = root / f'pass{index}.pdf'
            result = compress(source, output, 4)
            if index == 0:
                assert result[1] and result[4] < len(original) * .55, result
            if result[1]:
                source = output
            else:
                assert result[2] == 'err_no_gain' and not output.exists(), result
            with fitz.open(source) as doc:
                assert doc.page_count == 1 and 'Invoice 4471' in doc[0].get_text()
                assert len(doc[0].get_links()) == 1 and doc.get_toc() == [[1, 'Invoice', 1]]
                assert next(doc[0].widgets()).field_value == 'Acme Ltd'
                info = doc[0].get_image_info()[0]
                assert (info['width'], info['height']) == (1000, 1400)
                samples = doc[0].get_pixmap(dpi=36, colorspace=fitz.csGRAY).samples
                assert max(samples) - min(samples) > 100
        assert (root / 'source.pdf').read_bytes() == original


def test_opaque_legacy_mask_is_compressed_but_real_alpha_is_kept():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for transparent in (False, True):
            source, output = root / f'{transparent}.pdf', root / f'{transparent}-out.pdf'
            document(source, masked=True, transparent=transparent)
            with fitz.open(source) as doc:
                image = doc[0].get_images(full=True)[0]
                original_jpeg = doc.xref_stream_raw(image[0])
                original_mask = doc.xref_stream_raw(image[1])
            result = compress(source, output)
            if not transparent:
                assert result[1] and result[4] < result[3] * .55, result
                with fitz.open(output) as doc:
                    assert doc[0].get_images(full=True)[0][1] == 0
            else:
                final = output if result[1] else source
                with fitz.open(final) as doc:
                    image = doc[0].get_images(full=True)[0]
                    assert doc.xref_stream_raw(image[0]) == original_jpeg
                    assert doc.xref_stream_raw(image[1]) == original_mask


def test_failed_partial_rewrite_keeps_source_existing_output_and_temps():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source, output = root / 'source.pdf', root / 'output.pdf'
        document(source)
        original = source.read_bytes()
        output.write_bytes(b'existing output')
        temps = set(nexus.TEMP_DIR.glob('*'))
        apply = nexus._pdf_apply_image
        def fail_after_change(*args, **kwargs):
            apply(*args, **kwargs)
            raise RuntimeError('injected failure after native mutation')
        with patch.object(nexus, '_pdf_apply_image', fail_after_change):
            result = compress(source, output)
        assert not result[1], result
        assert source.read_bytes() == original and output.read_bytes() == b'existing output'
        assert set(nexus.TEMP_DIR.glob('*')) == temps
        assert not list(root.glob('.nexus-*'))


def test_same_path_and_hardlink_cannot_delete_original():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source, alias = root / 'source.pdf', root / 'alias.pdf'
        document(source)
        original = source.read_bytes()
        os.link(source, alias)
        for target in (source, alias):
            result = compress(source, target)
            assert not result[1] and result[2] == 'err_same_file', result
            assert source.read_bytes() == original and alias.read_bytes() == original


def test_cancellation_and_no_gain_keep_existing_destination():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source, output = root / 'source.pdf', root / 'output.pdf'
        with fitz.open() as doc:
            doc.new_page().insert_text((30, 30), 'Already compact')
            doc.save(source, garbage=4, deflate=True, use_objstms=1)
        normalized = root / 'normalized.pdf'
        if compress(source, normalized)[1]:
            source = normalized
        original = source.read_bytes()
        output.write_bytes(b'existing output')
        result = compress(source, output)
        assert not result[1] and result[2] == 'err_no_gain', result
        nexus.stop_flag.set()
        try:
            result = compress(source, output)
            assert not result[1] and result[2] == 'stopped', result
        finally:
            nexus.stop_flag.clear()
        assert source.read_bytes() == original and output.read_bytes() == b'existing output'


def test_page_count_does_not_accept_empty_image_streams():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source, broken = root / 'source.pdf', root / 'broken.pdf'
        document(source)
        with fitz.open(source) as doc:
            image = doc[0].get_images(full=True)[0][0]
            doc.update_stream(image, b'', compress=False)
            doc.save(broken)
        assert not nexus._pdf_output_ok(str(broken), 1, str(source))


def test_jpeg_to_pdf_keeps_encoded_bytes_and_small_file_size():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        image, output = root / 'photo.jpg', root / 'photo.pdf'
        encoded = jpeg(25)
        image.write_bytes(encoded)
        result = nexus.images_to_pdf([str(image)], str(output))
        assert result[0] and result[2:] == (1, 0), result
        assert output.stat().st_size < len(encoded) + 10000
        with fitz.open(output) as doc:
            assert doc.xref_stream_raw(doc[0].get_images(full=True)[0][0]) == encoded


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_'):
            fn()
            print('PASS', name, flush=True)
