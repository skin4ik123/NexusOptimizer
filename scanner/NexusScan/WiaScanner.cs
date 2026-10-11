using System;
using System.Collections;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;

namespace NexusScan
{
    /// <summary>
    /// Scanning through WIA (Windows Image Acquisition): local USB scanners and
    /// network ones (WSD/eSCL) alike.
    ///
    /// Talks to the WIA automation library late-bound, from the worker's main
    /// thread, which is a single-threaded COM apartment ([STAThread]). The old
    /// in-process code called WIA from a plain worker thread where COM had never
    /// been initialised — that was the "CoInitialize has not been called" error.
    /// </summary>
    internal sealed class WiaScanner
    {
        // device properties
        private const int DIP_VEND_DESC = 3, DIP_DEV_DESC = 4, DIP_DEV_NAME = 7;
        private const int DPS_HORIZONTAL_BED_SIZE = 3074, DPS_VERTICAL_BED_SIZE = 3075;
        private const int DPS_HORIZONTAL_SHEET_FEED_SIZE = 3076, DPS_VERTICAL_SHEET_FEED_SIZE = 3077;
        private const int DPS_DOCUMENT_HANDLING_CAPABILITIES = 3086, DPS_DOCUMENT_HANDLING_STATUS = 3087;
        private const int DPS_DOCUMENT_HANDLING_SELECT = 3088, DPS_PAGES = 3096;
        // item properties
        private const int IPA_DATATYPE = 4103, IPA_DEPTH = 4104;
        private const int IPS_CUR_INTENT = 6146, IPS_XRES = 6147, IPS_YRES = 6148;
        private const int IPS_XPOS = 6149, IPS_YPOS = 6150, IPS_XEXTENT = 6151, IPS_YEXTENT = 6152;
        // values
        private const int FEEDER = 0x001, FLATBED = 0x002, DUPLEX = 0x004;
        private const int CAP_FEED = 0x001, CAP_DUP = 0x004;
        private const int STATUS_FEED_READY = 0x001;
        private const int DATA_THRESHOLD = 0, DATA_GRAYSCALE = 2, DATA_COLOR = 3;
        private const int INTENT_COLOR = 1, INTENT_GRAY = 2, INTENT_TEXT = 4;
        private const int BIAS_MAXIMIZE_QUALITY = 131072;
        private const string FORMAT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}";

        private const uint WIA_ERROR_PAPER_EMPTY = 0x80210003;

        private readonly ScanRequest _req;
        private readonly JsonOut _out;
        private int _pages;

        public WiaScanner(ScanRequest req, JsonOut output)
        {
            _req = req;
            _out = output;
        }

        // ---- factory: the real automation library, or the test device ----

        private static dynamic CreateManager()
        {
#if TEST_HOOKS
            if (Environment.GetEnvironmentVariable("NEXUS_SCAN_FAKE_WIA") == "1")
                return new FakeWia.DeviceManager();
#endif
            return CreateCom("WIA.DeviceManager");
        }

        private static dynamic CreateDialog()
        {
#if TEST_HOOKS
            if (Environment.GetEnvironmentVariable("NEXUS_SCAN_FAKE_WIA") == "1")
                return new FakeWia.CommonDialog();
#endif
            return CreateCom("WIA.CommonDialog");
        }

        private static dynamic CreateCom(string progId)
        {
            Type? type = Type.GetTypeFromProgID(progId, false);
            if (type == null)
                throw new ScanException("wia_unavailable", progId + " is not registered");
            return Activator.CreateInstance(type)!;
        }

        // ---- listing ----

        public static void List(JsonOut output)
        {
            dynamic manager;
            try
            {
                manager = CreateManager();
            }
            catch (Exception ex)
            {
                var err = Describe(ex);
                output.Emit("note", "backend", "wia", "code", err.Code == "driver_error" ? "wia_unavailable" : err.Code, "detail", err.Detail);
                return;
            }
            try
            {
                foreach (dynamic info in (IEnumerable)manager.DeviceInfos)
                {
                    int type = ToInt(info.Type, 0);
                    if (type == 2 || type == 3) continue;  // cameras and video devices do not scan pages
                    dynamic props = info.Properties;
                    output.Emit("device", "backend", "wia", "arch", IntPtr.Size * 8,
                        "id", (string)info.DeviceID,
                        "name", PropString(props, DIP_DEV_NAME),
                        "manufacturer", PropString(props, DIP_VEND_DESC),
                        "family", PropString(props, DIP_DEV_DESC));
                }
            }
            catch (Exception ex)
            {
                var err = Describe(ex);
                output.Emit("note", "backend", "wia", "code", err.Code, "detail", err.Detail);
            }
        }

        // ---- scanning ----

        public int Run()
        {
            string? errorCode = null, errorDetail = null;
            bool cancelled = false;
            HostWindow? window = null;
            try
            {
                window = new HostWindow(_req.Parent, _req.ShowUI);
                Program.WakeWindow = window.Handle;
                _out.Emit("status", "code", "opening");
                dynamic manager = CreateManager();
                dynamic? info = null;
                foreach (dynamic di in (IEnumerable)manager.DeviceInfos)
                {
                    if ((string)di.DeviceID == _req.Device) { info = di; break; }
                }
                if (info == null)
                    throw new ScanException("no_device", "WIA device '" + _req.Device + "' is not connected");

                dynamic device = info.Connect();
                dynamic? item;
                bool feeder;
                if (_req.ShowUI)
                {
                    _out.Emit("status", "code", "waiting_ui");
                    window.FocusDriverWindowSoon();
                    dynamic dialog = CreateDialog();
                    dynamic? chosen = dialog.ShowSelectItems(device, 0, BIAS_MAXIMIZE_QUALITY, true, true, false);
                    item = First(chosen);
                    if (item == null)
                    {
                        cancelled = true;
                        return Finish(null, null, true);
                    }
                    int select = ToInt(GetValue(device.Properties, DPS_DOCUMENT_HANDLING_SELECT), FLATBED);
                    feeder = (select & FEEDER) != 0;
                    _out.Emit("settings", "dpi", ToInt(GetValue(item.Properties, IPS_XRES), 0),
                        "color", ColorName(ToInt(GetValue(item.Properties, IPA_DATATYPE), DATA_COLOR)),
                        "feeder", feeder, "duplex", (select & DUPLEX) != 0, "ui", true, "transfer", "file");
                }
                else
                {
                    feeder = SetSource(device);
                    item = First(device.Items);
                    if (item == null) throw new ScanException("driver_error", "the scanner reports no scan area");
                    ApplyItemSettings(item, device, feeder);
                }

                TransferLoop(device, item, feeder);
            }
            catch (ScanException ex)
            {
                errorCode = ex.Code;
                errorDetail = ex.Detail;
            }
            catch (Exception ex)
            {
                var err = Describe(ex);
                errorCode = err.Code;
                errorDetail = err.Detail;
            }
            finally
            {
                Program.WakeWindow = IntPtr.Zero;
                window?.Dispose();
            }
            return Finish(errorCode, errorDetail, cancelled);
        }

        private int Finish(string? code, string? detail, bool cancelled)
        {
            if (code != null)
            {
                _out.Emit("error", "code", code, "detail", detail ?? "");
                _out.Emit("done", "pages", _pages, "cancelled", false);
                return 1;
            }
            _out.Emit("done", "pages", _pages, "cancelled", cancelled || Program.CancelRequested);
            return 0;
        }

        /// <summary>Flatbed or feeder (and duplex). Returns whether the feeder is used.</summary>
        private bool SetSource(dynamic device)
        {
            dynamic props = device.Properties;
            int caps = ToInt(GetValue(props, DPS_DOCUMENT_HANDLING_CAPABILITIES), 0);
            if (!_req.Feeder)
            {
                TrySet(props, DPS_DOCUMENT_HANDLING_SELECT, FLATBED);
                return false;
            }
            if ((caps & CAP_FEED) == 0 && Find(props, DPS_DOCUMENT_HANDLING_SELECT) == null)
                throw new ScanException("no_feeder", "the scanner reports no document feeder");
            int select = FEEDER;
            if (_req.Duplex)
            {
                if ((caps & CAP_DUP) != 0) select |= DUPLEX;
                else _out.Emit("warning", "code", "no_duplex");
            }
            if (!TrySet(props, DPS_DOCUMENT_HANDLING_SELECT, select))
            {
                if ((select & DUPLEX) != 0 && TrySet(props, DPS_DOCUMENT_HANDLING_SELECT, FEEDER))
                    _out.Emit("warning", "code", "no_duplex");
                else
                    throw new ScanException("no_feeder", "the scanner refused feeder mode");
            }
            TrySet(props, DPS_PAGES, 1);
            object? status = GetValue(props, DPS_DOCUMENT_HANDLING_STATUS);
            if (status != null && (ToInt(status, STATUS_FEED_READY) & STATUS_FEED_READY) == 0)
                throw new ScanException("feeder_empty", "WIA_DPS_DOCUMENT_HANDLING_STATUS has no FEED_READY");
            return true;
        }

        private void ApplyItemSettings(dynamic item, dynamic device, bool feeder)
        {
            dynamic props = item.Properties;
            // The intent first: it resets the other values to its own defaults.
            int intent = _req.Color == "bw" ? INTENT_TEXT : _req.Color == "gray" ? INTENT_GRAY : INTENT_COLOR;
            TrySet(props, IPS_CUR_INTENT, intent);
            int dataType = _req.Color == "bw" ? DATA_THRESHOLD : _req.Color == "gray" ? DATA_GRAYSCALE : DATA_COLOR;
            if (!TrySet(props, IPA_DATATYPE, dataType)) _out.Emit("warning", "code", "color_substituted");
            TrySet(props, IPA_DEPTH, _req.Color == "bw" ? 1 : _req.Color == "gray" ? 8 : 24);

            int dpi = Nearest(Find(props, IPS_XRES), _req.Dpi);
            bool xs = TrySet(props, IPS_XRES, dpi);
            bool ys = TrySet(props, IPS_YRES, Nearest(Find(props, IPS_YRES), dpi));
            if (!xs || !ys) _out.Emit("warning", "code", "resolution_refused", "dpi", dpi);

            // The scan area: the requested paper, or the whole bed or sheet.
            double[]? inches = _req.PaperInches();
            dynamic dprops = device.Properties;
            int bedW = ToInt(GetValue(dprops, feeder ? DPS_HORIZONTAL_SHEET_FEED_SIZE : DPS_HORIZONTAL_BED_SIZE), 0);
            int bedH = ToInt(GetValue(dprops, feeder ? DPS_VERTICAL_SHEET_FEED_SIZE : DPS_VERTICAL_BED_SIZE), 0);
            // Always set the area explicitly: many drivers keep the previous
            // extent in pixels when the resolution changes, and the page comes
            // out cropped to a corner.
            int w, h;
            if (inches != null)
            {
                w = (int)Math.Round(inches[0] * dpi);
                h = (int)Math.Round(inches[1] * dpi);
                if (bedW > 0) w = Math.Min(w, bedW * dpi / 1000);  // bed size is in 1/1000 inch
                if (bedH > 0) h = Math.Min(h, bedH * dpi / 1000);
            }
            else
            {
                w = bedW > 0 ? bedW * dpi / 1000 : int.MaxValue;   // the whole bed or sheet
                h = bedH > 0 ? bedH * dpi / 1000 : int.MaxValue;
            }
            TrySet(props, IPS_XPOS, 0);
            TrySet(props, IPS_YPOS, 0);
            int cw = Clamp(Find(props, IPS_XEXTENT), w), ch = Clamp(Find(props, IPS_YEXTENT), h);
            if (cw != int.MaxValue) TrySet(props, IPS_XEXTENT, cw);
            if (ch != int.MaxValue) TrySet(props, IPS_YEXTENT, ch);

            int got = ToInt(GetValue(props, IPS_XRES), dpi);
            _out.Emit("settings", "dpi", got, "color", ColorName(ToInt(GetValue(props, IPA_DATATYPE), dataType)),
                "feeder", feeder, "duplex", feeder && _req.Duplex, "ui", false, "transfer", "file");
        }

        private void TransferLoop(dynamic device, dynamic item, bool feeder)
        {
            while (true)
            {
                if (Program.CancelRequested) return;
                _out.Emit("status", "code", "scanning", "page", _pages + 1);
                dynamic image;
                try
                {
                    image = item.Transfer(FORMAT_BMP);
                }
                catch (Exception ex)
                {
                    uint hr = HResult(ex);
                    if (feeder && hr == WIA_ERROR_PAPER_EMPTY)
                    {
                        if (_pages > 0) return;  // the feeder ran out: the normal end
                        throw new ScanException("feeder_empty", "WIA_ERROR_PAPER_EMPTY");
                    }
                    if (_pages == 0 && hr == 0x80004005)
                    {
                        // Some drivers refuse BMP with a generic failure: take their default format.
                        Log.Write("BMP refused, retrying with the driver's default format");
                        image = item.Transfer();
                    }
                    else
                    {
                        throw;
                    }
                }
                if (image == null) return;  // cancelled in the driver's progress window

                string ext = "bmp";
                try { ext = ((string)image.FileExtension).Trim('.').ToLowerInvariant(); } catch (Exception) { }
                string path = Path.Combine(_req.OutDir, "page_" + (_pages + 1).ToString("0000") + "." + ext);
                if (File.Exists(path)) File.Delete(path);
                image.SaveFile(path);
                _pages++;
                _out.Emit("page", "path", path, "index", _pages,
                    "width", ToInt(image.Width, 0), "height", ToInt(image.Height, 0),
                    "bits", ToInt(image.PixelDepth, 0), "dpi", Math.Round(ToDouble(image.HorizontalResolution, 0)));
                ComExtensions.Release((object)image);
                if (!feeder) return;
                // After a page the feeder item must be fetched again on many drivers.
                dynamic? next = First(device.Items);
                if (next != null) item = next;
            }
        }

        // ---- helpers ----

        private static dynamic? First(dynamic? collection)
        {
            if (collection == null) return null;
            foreach (dynamic x in (IEnumerable)collection) return x;
            return null;
        }

        private static dynamic? Find(dynamic props, int id)
        {
            foreach (dynamic p in (IEnumerable)props)
            {
                try
                {
                    if (ToInt(p.PropertyID, -1) == id) return p;
                }
                catch (Exception)
                {
                    // a property that cannot even report its id: skip it
                }
            }
            return null;
        }

        private static object? GetValue(dynamic props, int id)
        {
            dynamic? p = Find(props, id);
            if (p == null) return null;
            try { return (object?)p.Value; } catch (Exception) { return null; }
        }

        private static bool TrySet(dynamic props, int id, int value)
        {
            dynamic? p = Find(props, id);
            if (p == null) return false;
            try
            {
                p.Value = value;
                Log.Write("WIA property " + id + " = " + value);
                return true;
            }
            catch (Exception ex)
            {
                Log.Write("WIA property " + id + " = " + value + " refused: " + Describe(ex).Detail);
                return false;
            }
        }

        private static string PropString(dynamic props, int id)
        {
            object? v = GetValue(props, id);
            return v == null ? "" : Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture)?.Trim() ?? "";
        }

        /// <summary>The supported value nearest to the wanted one (WIA list or range).</summary>
        private static int Nearest(dynamic? prop, int want)
        {
            if (prop == null) return want;
            try
            {
                int subType = ToInt(prop.SubType, 0);
                if (subType == 1)  // range
                {
                    int min = ToInt(prop.SubTypeMin, want), max = ToInt(prop.SubTypeMax, want), step = Math.Max(1, ToInt(prop.SubTypeStep, 1));
                    int clamped = Math.Max(min, Math.Min(max, want));
                    return min + (int)Math.Round((clamped - min) / (double)step) * step;
                }
                if (subType == 2)  // list
                {
                    var values = ((IEnumerable)prop.SubTypeValues).Cast<object>().Select(v => ToInt(v, 0)).Where(v => v > 0).ToList();
                    if (values.Count > 0) return values.OrderBy(v => Math.Abs(v - want)).ThenByDescending(v => v).First();
                }
            }
            catch (Exception)
            {
                // no usable constraints: try the value itself
            }
            return want;
        }

        private static int Clamp(dynamic? prop, int want)
        {
            if (prop == null) return want;
            try
            {
                if (ToInt(prop.SubType, 0) == 1)
                    return Math.Max(ToInt(prop.SubTypeMin, want), Math.Min(ToInt(prop.SubTypeMax, want), want));
            }
            catch (Exception)
            {
                // unconstrained
            }
            return want;
        }

        private static string ColorName(int dataType)
            => dataType == DATA_THRESHOLD ? "bw" : dataType == DATA_GRAYSCALE ? "gray" : "color";

        private static int ToInt(object? v, int def)
        {
            try { return v == null ? def : Convert.ToInt32(v, System.Globalization.CultureInfo.InvariantCulture); }
            catch (Exception) { return def; }
        }

        private static double ToDouble(object? v, double def)
        {
            try { return v == null ? def : Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture); }
            catch (Exception) { return def; }
        }

        private static uint HResult(Exception ex)
        {
            while (ex is TargetInvocationException && ex.InnerException != null) ex = ex.InnerException;
            return unchecked((uint)ex.HResult);
        }

        /// <summary>A WIA failure as an error code the program understands.</summary>
        internal static WiaError Describe(Exception ex)
        {
            while (ex is TargetInvocationException && ex.InnerException != null) ex = ex.InnerException;
            if (ex is ScanException se) return new WiaError(se.Code, se.Detail);
            uint hr = unchecked((uint)ex.HResult);
            string detail = "0x" + hr.ToString("X8") + " " + ex.Message.Trim();
            switch (hr)
            {
                case 0x80210002: return new WiaError("paper_jam", detail);
                case 0x80210003: return new WiaError("feeder_empty", detail);
                case 0x80210004: return new WiaError("paper_problem", detail);
                case 0x80210005: return new WiaError("device_offline", detail);
                case 0x80210006: return new WiaError("device_busy", detail);
                case 0x80210007: return new WiaError("warming_up", detail);
                case 0x80210008: return new WiaError("user_intervention", detail);
                case 0x80210009: return new WiaError("no_device", detail);
                case 0x8021000A: return new WiaError("communication", detail);
                case 0x8021000C: return new WiaError("bad_setting", detail);
                case 0x8021000D: return new WiaError("device_busy", detail);
                case 0x80210015: return new WiaError("no_device", detail);
                case 0x80210016: return new WiaError("cover_open", detail);
                case 0x80210017: return new WiaError("warming_up", detail);
                case 0x80210066: return new WiaError("double_feed", detail);
                case 0x80070005: return new WiaError("access_denied", detail);
                case 0x80040154: return new WiaError("wia_unavailable", detail);
                case 0x800706BA:
                case 0x80070426:
                case 0x80070422: return new WiaError("wia_service", detail);
                default: return new WiaError("driver_error", detail);
            }
        }
    }

    internal sealed class WiaError
    {
        public string Code { get; }
        public string Detail { get; }

        public WiaError(string code, string detail)
        {
            Code = code;
            Detail = detail;
        }
    }

    internal static class ComExtensions
    {
        public static void Release(object? o)
        {
            if (o != null && Marshal.IsComObject(o))
            {
                try { Marshal.FinalReleaseComObject(o); } catch (Exception) { }
            }
        }
    }
}
