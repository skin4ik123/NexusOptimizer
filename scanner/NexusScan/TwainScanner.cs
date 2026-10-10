using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;

namespace NexusScan
{
    /// <summary>
    /// A scan through TWAIN, from opening the manager to closing the source.
    ///
    /// The source tells the program "images are ready" in one of two ways, and
    /// both are handled: a TWAIN 2 manager calls back (the callback only posts
    /// a message, the transfer itself happens in the loop), a TWAIN 1.x source
    /// answers through DAT_EVENT while every window message is handed to it. A
    /// timer keeps messages flowing for sources that wait for one.
    /// </summary>
    internal sealed unsafe class TwainScanner
    {
        private readonly ScanRequest _req;
        private readonly JsonOut _out;
        private TwainSession _session = null!;
        private HostWindow _window = null!;
        private DsmEntryProc? _callback;  // rooted while registered
        private bool _transferring;
        private bool _finished;
        private int _pages;
        private bool _cancelled;
        private string? _errorCode;
        private string? _errorDetail;
        private ushort _mech = Tw.SX_NATIVE;
        private bool _vanilla;
        private double _dpi;

        public TwainScanner(ScanRequest req, JsonOut output)
        {
            _req = req;
            _out = output;
        }

        public static void List(JsonOut output)
        {
            var tried = new List<string>();
            string? dsm = TwainSession.Locate(tried);
            if (dsm == null)
            {
                output.Emit("note", "backend", "twain", "code", "dsm_missing", "detail", string.Join("; ", tried));
                return;
            }
            using (var window = new HostWindow(IntPtr.Zero, false))
            using (var session = new TwainSession(dsm))
            {
                try
                {
                    session.OpenDsm(window.Handle);
                    foreach (var s in session.Enumerate())
                    {
                        output.Emit("device", "backend", "twain", "arch", IntPtr.Size * 8,
                            "name", s.Name, "manufacturer", s.Manufacturer, "family", s.Family,
                            "version", s.Version, "protocol", s.Protocol, "twain2", s.Twain2,
                            "bridge", s.IsWiaBridge);
                    }
                }
                finally
                {
                    session.CloseDsm(window.Handle);
                }
            }
        }

        public int Run()
        {
            var tried = new List<string>();
            string? dsm = TwainSession.Locate(tried);
            if (dsm == null)
                throw new ScanException("dsm_missing", string.Join("; ", tried));

            using (_window = new HostWindow(_req.Parent, _req.ShowUI))
            using (_session = new TwainSession(dsm))
            {
                Program.WakeWindow = _window.Handle;
                try
                {
                    Scan();
                }
                catch (ScanException ex)
                {
                    _errorCode = ex.Code;
                    _errorDetail = ex.Detail;
                }
                finally
                {
                    Program.WakeWindow = IntPtr.Zero;
                    Shutdown();
                }
                // "done" only after the source is closed: the program may start
                // the next scan the moment it reads it.
                return Finish();
            }
        }

        private void Scan()
        {
            _out.Emit("status", "code", "opening");
            _session.OpenDsm(_window.Handle);
            var sources = _session.Enumerate();
            var source = TwainSession.Match(sources, _req.Device);
            if (source == null)
                throw new ScanException("no_device", "TWAIN source '" + _req.Device + "' is not installed");
            _session.OpenSource(source);

            bool showUI = Negotiate();
            _callback = OnCallback;
            _session.RegisterCallback(_callback);

            if (Program.CancelRequested)
            {
                _cancelled = true;
                return;
            }

            ushort rc = _session.Enable(showUI, _window.Handle, out bool uiShown);
            if (rc != Tw.RC_SUCCESS && rc != Tw.RC_CHECKSTATUS)
            {
                ushort cc = _session.Status(true);
                throw new ScanException(Tw.CcToCode(cc), "the scanner did not start (" + Tw.CcName(cc) + ")");
            }
            if (uiShown)
            {
                _out.Emit("status", "code", "waiting_ui");
                _window.FocusDriverWindowSoon();
            }
            else
            {
                _out.Emit("status", "code", "scanning", "page", 1);
            }
            MessageLoop();
        }

        // ---- negotiation ----

        /// <summary>Applies the requested settings. Returns whether the driver window must be shown.</summary>
        private bool Negotiate()
        {
            bool showUI = _req.ShowUI;

            if (_session.GetBool(Tw.CAP_DEVICEONLINE) == false)
                throw new ScanException("device_offline", "CAP_DEVICEONLINE is FALSE");

            if (!showUI)
            {
                if (_session.GetBool(Tw.CAP_UICONTROLLABLE) == false)
                {
                    showUI = true;
                    _out.Emit("warning", "code", "ui_required");
                }
            }

            // Transfer mechanism first: some sources restrict other values by it.
            if (_req.Transfer == "memory" && _session.SetOne(Tw.ICAP_XFERMECH, Tw.TY_UINT16, Tw.SX_MEMORY))
                _mech = Tw.SX_MEMORY;
            else
                _session.SetOne(Tw.ICAP_XFERMECH, Tw.TY_UINT16, Tw.SX_NATIVE);

            // In the driver's own window the user chooses everything; without it
            // the program's settings apply.
            if (!showUI)
            {
                _session.SetOne(Tw.ICAP_UNITS, Tw.TY_UINT16, Tw.UN_INCHES);
                SetPixelType();
                _dpi = SetResolution(_req.Dpi);
                SetSource();
                SetPaper();
                _session.SetOne(Tw.CAP_XFERCOUNT, Tw.TY_INT16, 0xFFFF);  // -1: everything in the feeder
                _session.SetOne(Tw.CAP_INDICATORS, Tw.TY_BOOL, 0);       // no driver progress popups
            }
            else
            {
                _session.SetOne(Tw.CAP_XFERCOUNT, Tw.TY_INT16, 0xFFFF);
            }

            if (_mech == Tw.SX_MEMORY)
            {
                var pf = _session.Get(Tw.ICAP_PIXELFLAVOR, Tw.MSG_GETCURRENT);
                _vanilla = pf != null && pf.Current == Tw.PF_VANILLA;
            }

            var res = _session.Get(Tw.ICAP_XRESOLUTION, Tw.MSG_GETCURRENT);
            if (res != null) _dpi = res.AsNumber(res.Current);
            var pt = _session.Get(Tw.ICAP_PIXELTYPE, Tw.MSG_GETCURRENT);
            string color = pt == null ? _req.Color : pt.Current == Tw.PT_BW ? "bw" : pt.Current == Tw.PT_GRAY ? "gray" : "color";
            var feeder = _session.GetBool(Tw.CAP_FEEDERENABLED);
            var duplex = _session.GetBool(Tw.CAP_DUPLEXENABLED);
            _out.Emit("settings", "dpi", Math.Round(_dpi), "color", color,
                "feeder", feeder == true, "duplex", duplex == true, "ui", showUI,
                "transfer", _mech == Tw.SX_MEMORY ? "memory" : "native");
            return showUI;
        }

        private void SetPixelType()
        {
            ushort want = _req.Color == "bw" ? Tw.PT_BW : _req.Color == "gray" ? Tw.PT_GRAY : Tw.PT_RGB;
            // If the exact type is refused, take the next richer one: the
            // program converts it to what was asked for.
            ushort[] order = want == Tw.PT_BW ? new[] { Tw.PT_BW, Tw.PT_GRAY, Tw.PT_RGB }
                           : want == Tw.PT_GRAY ? new[] { Tw.PT_GRAY, Tw.PT_RGB }
                           : new[] { Tw.PT_RGB, Tw.PT_GRAY };
            foreach (var type in order)
            {
                if (!_session.SetOne(Tw.ICAP_PIXELTYPE, Tw.TY_UINT16, type)) continue;
                _session.SetOne(Tw.ICAP_BITDEPTH, Tw.TY_UINT16, type == Tw.PT_BW ? 1u : 8u);
                if (type != want) _out.Emit("warning", "code", "color_substituted");
                return;
            }
        }

        private double SetResolution(int want)
        {
            double chosen = want;
            var v = _session.Get(Tw.ICAP_XRESOLUTION, Tw.MSG_GET);
            if (v != null)
            {
                if (v.ConType == Tw.ON_RANGE)
                {
                    double min = v.AsNumber(v.Min), max = v.AsNumber(v.Max), step = v.AsNumber(v.Step);
                    if (step <= 0) step = 1;
                    double clamped = Math.Max(min, Math.Min(max, want));
                    chosen = min + Math.Round((clamped - min) / step) * step;
                }
                else if (v.Items.Count > 0)
                {
                    // The nearest supported value; on a tie the higher one.
                    chosen = v.Items.Select(v.AsNumber).OrderBy(d => Math.Abs(d - want)).ThenByDescending(d => d).First();
                }
            }
            bool x = _session.SetOne(Tw.ICAP_XRESOLUTION, Tw.TY_FIX32, Tw.FixRaw(chosen));
            bool y = _session.SetOne(Tw.ICAP_YRESOLUTION, Tw.TY_FIX32, Tw.FixRaw(chosen));
            if (!x || !y) _out.Emit("warning", "code", "resolution_refused", "dpi", chosen);
            return chosen;
        }

        private void SetSource()
        {
            if (!_req.Feeder)
            {
                _session.SetOne(Tw.CAP_FEEDERENABLED, Tw.TY_BOOL, 0);  // a flatbed-only scanner refuses: fine
                SetSimplex();
                return;
            }
            if (!_session.SetOne(Tw.CAP_FEEDERENABLED, Tw.TY_BOOL, 1))
                throw new ScanException("no_feeder", "CAP_FEEDERENABLED refused");
            _session.SetOne(Tw.CAP_AUTOFEED, Tw.TY_BOOL, 1);
            if (_req.Duplex)
            {
                var dx = _session.Get(Tw.CAP_DUPLEX, Tw.MSG_GETCURRENT);
                bool can = dx != null && dx.Current != 0;
                if (!can || !_session.SetOne(Tw.CAP_DUPLEXENABLED, Tw.TY_BOOL, 1))
                {
                    SetSimplex();
                    _out.Emit("warning", "code", "no_duplex");
                }
            }
            else SetSimplex();
            // Report an empty feeder now rather than as an obscure failure later,
            // but only when the scanner can actually detect paper.
            if (_session.GetBool(Tw.CAP_PAPERDETECTABLE) == true && _session.GetBool(Tw.CAP_FEEDERLOADED) == false)
                throw new ScanException("feeder_empty", "CAP_FEEDERLOADED is FALSE");
        }

        private void SetSimplex()
        {
            // Sources can restore duplex from their last scan, or enable it
            // when the feeder is selected. Set FALSE after selecting the source.
            bool set = _session.SetOne(Tw.CAP_DUPLEXENABLED, Tw.TY_BOOL, 0);
            bool? current = _session.GetBool(Tw.CAP_DUPLEXENABLED);
            if (current == true)
                throw new ScanException("simplex_refused", "the driver kept duplex enabled after a one-sided scan was requested");
            if (!set && current == null)
            {
                var support = _session.Get(Tw.CAP_DUPLEX, Tw.MSG_GETCURRENT);
                if (support != null && support.Current != 0)
                    throw new ScanException("simplex_refused", "the duplex driver refused one-sided mode and did not report its current mode");
            }
        }

        private void SetPaper()
        {
            var inches = _req.PaperInches();
            if (inches == null) return;
            ushort ss = _req.Paper == "a4" ? Tw.SS_A4 : _req.Paper == "legal" ? Tw.SS_USLEGAL : Tw.SS_USLETTER;
            if (_session.SetOne(Tw.ICAP_SUPPORTEDSIZES, Tw.TY_UINT16, ss)) return;
            TW_IMAGELAYOUT layout = default;
            layout.Frame.Right = TW_FIX32.From(inches[0]);
            layout.Frame.Bottom = TW_FIX32.From(inches[1]);
            layout.DocumentNumber = layout.PageNumber = layout.FrameNumber = Tw.DONTCARE32;
            ushort rc = _session.DsCall(Tw.DG_IMAGE, Tw.DAT_IMAGELAYOUT, Tw.MSG_SET, &layout);
            Log.Write("image layout: rc " + rc);
        }

        // ---- the loop ----

        private ushort OnCallback(TW_IDENTITY* origin, TW_IDENTITY* dest, uint dg, ushort dat, ushort msg, void* data)
        {
            // May arrive on a driver thread: just wake the loop.
            if (msg != Tw.MSG_NULL)
                Native.PostMessageW(_window.Handle, Native.WM_TWAIN, (IntPtr)msg, IntPtr.Zero);
            return Tw.RC_SUCCESS;
        }

        private void MessageLoop()
        {
            UIntPtr pulse = Native.SetTimer(_window.Handle, (UIntPtr)1, 200, IntPtr.Zero);
            try
            {
                while (!_finished)
                {
                    if (Native.GetMessageW(out var msg, IntPtr.Zero, 0, 0) <= 0) break;

                    if (msg.hwnd == _window.Handle && msg.message == Native.WM_TWAIN)
                    {
                        OnTwainMessage((ushort)msg.wParam.ToInt64());
                        continue;
                    }
                    if (msg.hwnd == _window.Handle && msg.message == Native.WM_CANCEL)
                    {
                        if (!_transferring)
                        {
                            _cancelled = true;
                            _finished = true;
                        }
                        continue;
                    }

                    bool handled = false;
                    if (_session.Enabled)
                    {
                        TW_EVENT ev = default;
                        ev.pEvent = (IntPtr)(&msg);
                        ushort rc = _session.DsCall(Tw.DG_CONTROL, Tw.DAT_EVENT, Tw.MSG_PROCESSEVENT, &ev);
                        handled = rc == Tw.RC_DSEVENT;
                        if (ev.TWMessage != Tw.MSG_NULL) OnTwainMessage(ev.TWMessage);
                    }
                    if (!handled)
                    {
                        if (msg.hwnd == _window.Handle && msg.message == Native.WM_TIMER)
                            _window.OnTimer(msg.wParam);
                        Native.TranslateMessage(ref msg);
                        Native.DispatchMessageW(ref msg);
                    }
                }
            }
            finally
            {
                Native.KillTimer(_window.Handle, pulse);
            }
        }

        private void OnTwainMessage(ushort message)
        {
            Log.Write("TWAIN message 0x" + message.ToString("x4"));
            switch (message)
            {
                case Tw.MSG_XFERREADY:
                    if (_transferring || _finished || !_session.Enabled) return;
                    _transferring = true;
                    try
                    {
                        TransferAll();
                    }
                    finally
                    {
                        _transferring = false;
                        _finished = true;  // one batch per scan: the program can start another
                    }
                    break;
                case Tw.MSG_CLOSEDSREQ:
                case Tw.MSG_CLOSEDSOK:
                    if (_pages == 0) _cancelled = true;
                    _finished = true;
                    break;
            }
        }

        private void TransferAll()
        {
            while (true)
            {
                if (Program.CancelRequested)
                {
                    _cancelled = true;  // pages already received are kept
                    ResetPending();
                    return;
                }

                TW_IMAGEINFO info = default;
                bool haveInfo = _session.DsCall(Tw.DG_IMAGE, Tw.DAT_IMAGEINFO, Tw.MSG_GET, &info) == Tw.RC_SUCCESS;
                double dpi = haveInfo && info.XResolution.Value > 0 ? info.XResolution.Value : _dpi;
                _out.Emit("status", "code", "scanning", "page", _pages + 1);

                string path = Path.Combine(_req.OutDir, "page_" + (_pages + 1).ToString("0000") + ".bmp");
                ushort rc;
                PageFile? page = null;
                try
                {
                    if (_mech == Tw.SX_MEMORY && haveInfo)
                        rc = MemoryTransfer(info, dpi, path, out page);
                    else
                        rc = NativeTransfer(dpi, path, out page);
                }
                catch (ScanException ex)
                {
                    _errorCode = ex.Code;
                    _errorDetail = ex.Detail;
                    EndTransfer();
                    ResetPending();
                    return;
                }

                if (rc == Tw.RC_XFERDONE && page != null)
                {
                    _pages++;
                    _out.Emit("page", "path", page.Path, "index", _pages, "width", page.Width,
                        "height", page.Height, "bits", page.Bits, "dpi", page.Dpi);
                }
                else if (rc == Tw.RC_CANCEL)
                {
                    // cancelled in the driver's own window (state 7: end the transfer first)
                    _cancelled = true;
                    EndTransfer();
                    ResetPending();
                    return;
                }
                else
                {
                    ushort cc = _session.Status(true);
                    // An empty feeder after the first sheet is the normal end of a batch.
                    if (cc == Tw.CC_NOMEDIA && _pages > 0)
                    {
                        ResetPending();
                        return;
                    }
                    _errorCode = Tw.CcToCode(cc);
                    _errorDetail = "transfer failed (rc " + rc + ", " + Tw.CcName(cc) + ")";
                    ResetPending();
                    return;
                }

                int left = EndTransfer();
                if (left == 0) return;
            }
        }

        private ushort NativeTransfer(double dpi, string path, out PageFile? page)
        {
            page = null;
            IntPtr dib = IntPtr.Zero;
            ushort rc = _session.DsCall(Tw.DG_IMAGE, Tw.DAT_IMAGENATIVEXFER, Tw.MSG_GET, &dib);
            if (rc != Tw.RC_XFERDONE)
            {
                if (dib != IntPtr.Zero) Native.GlobalFree(dib);
                return rc;
            }
            if (dib == IntPtr.Zero) throw new ScanException("driver_error", "the driver returned an empty image");
            try
            {
                IntPtr p = Native.GlobalLock(dib);
                if (p == IntPtr.Zero) throw new ScanException("driver_error", "cannot lock the image");
                try
                {
                    long size = (long)Native.GlobalSize(dib).ToUInt64();
                    page = PageFiles.WriteDib(p, size, path, dpi);
                }
                finally
                {
                    Native.GlobalUnlock(dib);
                }
            }
            finally
            {
                Native.GlobalFree(dib);
            }
            return rc;
        }

        private ushort MemoryTransfer(TW_IMAGEINFO info, double dpi, string path, out PageFile? page)
        {
            page = null;
            TW_SETUPMEMXFER setup = default;
            _session.DsCall(Tw.DG_CONTROL, Tw.DAT_SETUPMEMXFER, Tw.MSG_GET, &setup);
            uint size = setup.Preferred;
            if (size == 0 || size == Tw.DONTCARE32) size = Math.Max(setup.MinBufSize, 256u * 1024u);
            if (setup.MaxBufSize != 0 && setup.MaxBufSize != Tw.DONTCARE32 && size > setup.MaxBufSize) size = setup.MaxBufSize;
            size = Math.Max(size, setup.MinBufSize);
            size = Math.Min(size, 8u * 1024u * 1024u);

            int bits = info.BitsPerPixel;
            var image = new PageFiles.StripImage(info.ImageWidth, bits, _vanilla);
            IntPtr buffer = Marshal.AllocHGlobal((int)size);
            try
            {
                while (true)
                {
                    TW_IMAGEMEMXFER mx = default;
                    mx.Compression = Tw.ON_DONTCARE16;
                    mx.BytesPerRow = mx.Columns = mx.Rows = mx.XOffset = mx.YOffset = mx.BytesWritten = Tw.DONTCARE32;
                    mx.Memory.Flags = Tw.MF_APPOWNS | Tw.MF_POINTER;
                    mx.Memory.Length = size;
                    mx.Memory.TheMem = buffer;
                    ushort rc = _session.DsCall(Tw.DG_IMAGE, Tw.DAT_IMAGEMEMXFER, Tw.MSG_GET, &mx);
                    if (rc != Tw.RC_SUCCESS && rc != Tw.RC_XFERDONE) return rc;
                    if (mx.Compression != Tw.CP_NONE)
                        throw new ScanException("driver_error", "compressed memory strips are not supported");
                    image.AddStrip((byte*)buffer, mx.BytesPerRow, mx.Rows, mx.YOffset);
                    if (rc == Tw.RC_XFERDONE)
                    {
                        page = image.Write(path, dpi);
                        return rc;
                    }
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        private int EndTransfer()
        {
            TW_PENDINGXFERS px = default;
            ushort rc = _session.DsCall(Tw.DG_CONTROL, Tw.DAT_PENDINGXFERS, Tw.MSG_ENDXFER, &px);
            Log.Write("end transfer: rc " + rc + ", pending " + px.Count);
            if (rc != Tw.RC_SUCCESS) return 0;
            return px.Count;  // 0xFFFF means "unknown, more to come"
        }

        private void ResetPending()
        {
            TW_PENDINGXFERS px = default;
            ushort rc = _session.DsCall(Tw.DG_CONTROL, Tw.DAT_PENDINGXFERS, Tw.MSG_RESET, &px);
            Log.Write("reset pending: rc " + rc);
        }

        private int Finish()
        {
            if (_errorCode != null)
            {
                _out.Emit("error", "code", _errorCode, "detail", _errorDetail ?? "");
                _out.Emit("done", "pages", _pages, "cancelled", false);
                return 1;
            }
            _out.Emit("done", "pages", _pages, "cancelled", _cancelled);
            return 0;
        }

        /// <summary>Leaves the driver in a clean state whatever happened before.</summary>
        private void Shutdown()
        {
            try
            {
                if (_session.Enabled) _session.Disable(_window.Handle);
                _session.CloseSource();
                _session.CloseDsm(_window.Handle);
            }
            catch (Exception ex)
            {
                Log.Write("shutdown: " + ex.Message);
            }
        }
    }
}
