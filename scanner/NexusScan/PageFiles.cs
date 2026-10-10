using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;

namespace NexusScan
{
    /// <summary>A page written to disk, as reported to the program.</summary>
    internal sealed class PageFile
    {
        public string Path = "";
        public int Width;
        public int Height;
        public int Bits;
        public double Dpi;
    }

    /// <summary>
    /// Pages are written as BMP: a lossless format every driver's data maps onto
    /// directly and that the program reads in one call. The program converts
    /// them to their final form (JPEG or 1-bit PNG) right after.
    /// </summary>
    internal static unsafe class PageFiles
    {
        [StructLayout(LayoutKind.Sequential, Pack = 2)]
        private struct BITMAPINFOHEADER
        {
            public uint biSize;
            public int biWidth;
            public int biHeight;
            public ushort biPlanes;
            public ushort biBitCount;
            public uint biCompression;
            public uint biSizeImage;
            public int biXPelsPerMeter;
            public int biYPelsPerMeter;
            public uint biClrUsed;
            public uint biClrImportant;
        }

        private const uint BI_RGB = 0;
        private const uint BI_BITFIELDS = 3;

        /// <summary>
        /// A packed DIB from a native transfer to a BMP file. The driver hands
        /// over the DIB without a file header; the header written here is what
        /// makes it a file.
        /// </summary>
        public static PageFile WriteDib(IntPtr locked, long available, string path, double fallbackDpi)
        {
            if (available < sizeof(BITMAPINFOHEADER))
                throw new ScanException("driver_error", "image block too small: " + available);
            var bih = (BITMAPINFOHEADER*)locked;
            uint header = bih->biSize;
            if (header < 40 || header > 1024)
                throw new ScanException("driver_error", "not a DIB (header size " + header + ")");
            int bits = bih->biBitCount;
            uint compression = bih->biCompression;
            if (compression != BI_RGB && compression != BI_BITFIELDS)
                throw new ScanException("driver_error", "compressed DIB not supported (" + compression + ")");
            long width = bih->biWidth;
            long height = Math.Abs((long)bih->biHeight);
            if (width <= 0 || height <= 0 || width > 100000 || height > 100000)
                throw new ScanException("driver_error", "bad image size " + width + "x" + height);

            uint colors = bih->biClrUsed != 0 ? bih->biClrUsed : (bits <= 8 ? 1u << bits : 0u);
            uint masks = (compression == BI_BITFIELDS && header == 40) ? 12u : 0u;
            long stride = ((width * bits + 31) / 32) * 4;
            long pixels = stride * height;
            long body = header + colors * 4L + masks + pixels;
            if (body > available)
            {
                // Some drivers report a smaller block than the bitmap needs;
                // a short image is better than none, but only if the headers fit.
                if (available < header + colors * 4L + masks + stride)
                    throw new ScanException("driver_error", "image block truncated");
                body = available;
            }
            long offset = 14 + header + colors * 4L + masks;

            using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20))
            {
                var fh = new byte[14];
                fh[0] = (byte)'B';
                fh[1] = (byte)'M';
                BitConverter.GetBytes((uint)(14 + body)).CopyTo(fh, 2);
                BitConverter.GetBytes((uint)offset).CopyTo(fh, 10);
                fs.Write(fh, 0, 14);
                using (var src = new UnmanagedMemoryStream((byte*)locked, body))
                    src.CopyTo(fs, 1 << 20);
            }

            double dpi = bih->biXPelsPerMeter > 0 ? bih->biXPelsPerMeter * 0.0254 : fallbackDpi;
            return new PageFile { Path = path, Width = (int)width, Height = (int)height, Bits = bits, Dpi = Math.Round(dpi) };
        }

        /// <summary>
        /// Collects the strips of a memory transfer (top-down rows, RGB order)
        /// and writes them as a top-down BMP.
        /// </summary>
        public sealed class StripImage
        {
            private readonly int _width;
            private readonly int _bits;        // 1, 8 or 24
            private readonly bool _vanilla;    // TWPF_VANILLA: zero is white
            private readonly List<byte[]> _rows = new List<byte[]>();
            private readonly int _srcRowBytes;

            public StripImage(int width, int bits, bool vanilla)
            {
                if (bits != 1 && bits != 8 && bits != 24)
                    throw new ScanException("driver_error", "unsupported bit depth " + bits);
                _width = width;
                _bits = bits;
                _vanilla = vanilla;
                _srcRowBytes = (width * bits + 7) / 8;
            }

            public int Rows => _rows.Count;

            public void AddStrip(byte* data, uint bytesPerRow, uint rows, uint yOffset)
            {
                if (bytesPerRow < _srcRowBytes)
                    throw new ScanException("driver_error", "strip rows shorter than the image width");
                for (uint r = 0; r < rows; r++)
                {
                    var row = new byte[_srcRowBytes];
                    Marshal.Copy((IntPtr)(data + r * bytesPerRow), row, 0, _srcRowBytes);
                    long y = (long)yOffset + r;
                    while (_rows.Count < y) _rows.Add(new byte[_srcRowBytes]);  // a gap: fill with zeros
                    if (y < _rows.Count) _rows[(int)y] = row;
                    else _rows.Add(row);
                }
            }

            public PageFile Write(string path, double dpi)
            {
                if (_rows.Count == 0) throw new ScanException("driver_error", "no image rows received");
                int colors = _bits == 24 ? 0 : (_bits == 8 ? 256 : 2);
                int stride = ((_width * _bits + 31) / 32) * 4;
                int header = 40;
                long offset = 14 + header + colors * 4;
                long size = offset + (long)stride * _rows.Count;
                int ppm = (int)Math.Round(dpi / 0.0254);
                using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20))
                using (var w = new BinaryWriter(fs))
                {
                    w.Write((byte)'B'); w.Write((byte)'M');
                    w.Write((uint)size); w.Write(0u); w.Write((uint)offset);
                    w.Write(header); w.Write(_width); w.Write(-_rows.Count);  // negative: top-down
                    w.Write((ushort)1); w.Write((ushort)_bits); w.Write(BI_RGB);
                    w.Write((uint)((long)stride * _rows.Count)); w.Write(ppm); w.Write(ppm);
                    w.Write((uint)colors); w.Write(0u);
                    for (int i = 0; i < colors; i++)
                    {
                        byte v = colors == 2 ? (byte)(i * 255) : (byte)i;
                        if (_vanilla) v = (byte)(255 - v);
                        w.Write(v); w.Write(v); w.Write(v); w.Write((byte)0);
                    }
                    var line = new byte[stride];
                    foreach (var row in _rows)
                    {
                        Array.Clear(line, 0, line.Length);
                        if (_bits == 24)
                        {
                            for (int x = 0; x < _width; x++)
                            {
                                line[x * 3] = row[x * 3 + 2];
                                line[x * 3 + 1] = row[x * 3 + 1];
                                line[x * 3 + 2] = row[x * 3];
                            }
                        }
                        else
                        {
                            Buffer.BlockCopy(row, 0, line, 0, row.Length);
                        }
                        w.Write(line);
                    }
                }
                return new PageFile { Path = path, Width = _width, Height = _rows.Count, Bits = _bits, Dpi = Math.Round(dpi) };
            }
        }
    }
}
