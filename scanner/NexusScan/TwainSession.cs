using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;

namespace NexusScan
{
    /// <summary>A TWAIN source found by the manager.</summary>
    internal sealed unsafe class TwainSource
    {
        public TW_IDENTITY Identity;
        public string Name = "";
        public string Manufacturer = "";
        public string Family = "";
        public string Version = "";
        public string Protocol = "";
        public bool Twain2;

        public static TwainSource From(TW_IDENTITY id)
        {
            var s = new TwainSource { Identity = id };
            s.Name = TW_IDENTITY.Read(id.ProductName);
            s.Manufacturer = TW_IDENTITY.Read(id.Manufacturer);
            s.Family = TW_IDENTITY.Read(id.ProductFamily);
            s.Version = id.MajorNum + "." + id.MinorNum + " " + TW_IDENTITY.Read(id.Info);
            s.Protocol = id.ProtocolMajor + "." + id.ProtocolMinor;
            s.Twain2 = (id.SupportedGroups & Tw.DF_DS2) != 0;
            return s;
        }

        /// <summary>
        /// Windows exposes every WIA device to 32-bit TWAIN programs once more,
        /// through its compatibility source (wiatwain.ds) named "WIA-...".
        /// The program talks to those devices through WIA directly.
        /// </summary>
        public bool IsWiaBridge => Name.StartsWith("WIA-", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>A capability value as the source reported it.</summary>
    internal sealed class CapValue
    {
        public ushort ConType;
        public ushort ItemType;
        public readonly List<uint> Items = new List<uint>();
        public uint Current;
        public uint Default;
        public uint Min, Max, Step;

        public double AsNumber(uint raw) => ItemType == Tw.TY_FIX32 ? Tw.Fix(raw)
            : ItemType == Tw.TY_INT16 ? (short)raw : ItemType == Tw.TY_INT32 ? (int)raw : raw;
    }

    /// <summary>
    /// One conversation with the TWAIN Data Source Manager: the manager is
    /// loaded by full path, so the bundled copy is used and its bitness always
    /// matches the process.
    /// </summary>
    internal sealed unsafe class TwainSession : IDisposable
    {
        private readonly IntPtr _module;
        private readonly DsmEntryProc _entry;
        private TW_IDENTITY* _app;
        private TW_IDENTITY* _src;
        private DsmMemAllocate? _memAlloc;
        private DsmMemFree? _memFree;
        private DsmMemLock? _memLock;
        private DsmMemUnlock? _memUnlock;

        public string DsmPath { get; }
        public bool DsmOpen { get; private set; }
        public bool SourceOpen { get; private set; }
        public bool Enabled { get; private set; }
        public bool Dsm2 => _app != null && (_app->SupportedGroups & Tw.DF_DSM2) != 0;
        public bool SourceTwain2 => _src != null && (_src->SupportedGroups & Tw.DF_DS2) != 0;

        /// <summary>
        /// The bundled manager first: a 64-bit one is not part of Windows and a
        /// 32-bit one is installed only by some drivers. The old Windows TWAIN_32
        /// manager is the last resort for the 32-bit worker.
        /// </summary>
        public static string? Locate(List<string> tried)
        {
            string baseDir = AppDomain.CurrentDomain.BaseDirectory;
            string arch = IntPtr.Size == 8 ? "x64" : "x86";
            var candidates = new List<string>
            {
                Path.Combine(Path.Combine(baseDir, arch), "TWAINDSM.dll"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "TWAINDSM.dll"),
            };
            if (IntPtr.Size == 4)
                candidates.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "twain_32.dll"));
            foreach (var c in candidates)
            {
                tried.Add(c);
                if (File.Exists(c)) return c;
            }
            return null;
        }

        public TwainSession(string dsmPath)
        {
            DsmPath = dsmPath;
            _module = Native.LoadLibraryExW(dsmPath, IntPtr.Zero, Native.LOAD_WITH_ALTERED_SEARCH_PATH);
            if (_module == IntPtr.Zero)
                throw new ScanException("dsm_missing", "cannot load " + dsmPath + " (error " + Marshal.GetLastWin32Error() + ")");
            IntPtr proc = Native.GetProcAddress(_module, "DSM_Entry");
            if (proc == IntPtr.Zero)
                throw new ScanException("dsm_missing", "DSM_Entry not found in " + dsmPath);
            _entry = (DsmEntryProc)Marshal.GetDelegateForFunctionPointer(proc, typeof(DsmEntryProc));

            // The identity lives in unmanaged memory for the whole session: the
            // manager writes the application Id into it and the same pointer is
            // passed back with every call.
            _app = (TW_IDENTITY*)Marshal.AllocHGlobal(sizeof(TW_IDENTITY));
            *_app = default;
            _app->Id = 0;
            _app->MajorNum = 3;
            _app->MinorNum = 9;
            _app->Language = Tw.LG_USA;
            _app->Country = Tw.CY_USA;
            TW_IDENTITY.Write(_app->Info, "Nexus Optimizer scanner");
            _app->ProtocolMajor = 2;
            _app->ProtocolMinor = 4;
            _app->SupportedGroups = Tw.DG_CONTROL | Tw.DG_IMAGE | Tw.DF_APP2;
            TW_IDENTITY.Write(_app->Manufacturer, "Artur Yurchuk");
            TW_IDENTITY.Write(_app->ProductFamily, "Nexus");
            TW_IDENTITY.Write(_app->ProductName, "Nexus Optimizer");
        }

        public ushort DsmCall(uint dg, ushort dat, ushort msg, void* data)
            => _entry(_app, null, dg, dat, msg, data);

        public ushort DsCall(uint dg, ushort dat, ushort msg, void* data)
            => _entry(_app, _src, dg, dat, msg, data);

        /// <summary>The condition code of the last failure (and clears it).</summary>
        public ushort Status(bool ofSource)
        {
            TW_STATUS st = default;
            ushort rc = _entry(_app, ofSource ? _src : null, Tw.DG_CONTROL, Tw.DAT_STATUS, Tw.MSG_GET, &st);
            return rc == Tw.RC_SUCCESS ? st.ConditionCode : (ushort)0xFFFF;
        }

        public void OpenDsm(IntPtr hwnd)
        {
            IntPtr parent = hwnd;
            ushort rc = DsmCall(Tw.DG_CONTROL, Tw.DAT_PARENT, Tw.MSG_OPENDSM, &parent);
            if (rc != Tw.RC_SUCCESS)
                throw new ScanException("driver_error", "the TWAIN manager did not open (rc " + rc + ", " + Tw.CcName(Status(false)) + ")");
            DsmOpen = true;
            Log.Write("DSM open: " + DsmPath + ", TWAIN 2: " + Dsm2);
            if (!Dsm2) return;

            // A TWAIN 2 manager hands out its memory functions; containers
            // exchanged with a TWAIN 2 source must be allocated through them.
            TW_ENTRYPOINT ep = default;
            ep.Size = (uint)sizeof(TW_ENTRYPOINT);
            if (DsmCall(Tw.DG_CONTROL, Tw.DAT_ENTRYPOINT, Tw.MSG_GET, &ep) == Tw.RC_SUCCESS)
            {
                if (ep.DSM_MemAllocate != IntPtr.Zero)
                    _memAlloc = (DsmMemAllocate)Marshal.GetDelegateForFunctionPointer(ep.DSM_MemAllocate, typeof(DsmMemAllocate));
                if (ep.DSM_MemFree != IntPtr.Zero)
                    _memFree = (DsmMemFree)Marshal.GetDelegateForFunctionPointer(ep.DSM_MemFree, typeof(DsmMemFree));
                if (ep.DSM_MemLock != IntPtr.Zero)
                    _memLock = (DsmMemLock)Marshal.GetDelegateForFunctionPointer(ep.DSM_MemLock, typeof(DsmMemLock));
                if (ep.DSM_MemUnlock != IntPtr.Zero)
                    _memUnlock = (DsmMemUnlock)Marshal.GetDelegateForFunctionPointer(ep.DSM_MemUnlock, typeof(DsmMemUnlock));
            }
        }

        public List<TwainSource> Enumerate()
        {
            var list = new List<TwainSource>();
            TW_IDENTITY id = default;
            ushort rc = DsmCall(Tw.DG_CONTROL, Tw.DAT_IDENTITY, Tw.MSG_GETFIRST, &id);
            int guard = 0;
            while (rc == Tw.RC_SUCCESS && guard++ < 256)
            {
                list.Add(TwainSource.From(id));
                id = default;
                rc = DsmCall(Tw.DG_CONTROL, Tw.DAT_IDENTITY, Tw.MSG_GETNEXT, &id);
            }
            if (rc == Tw.RC_FAILURE)
                Log.Write("enumeration ended with failure: " + Tw.CcName(Status(false)));
            return list;
        }

        public void OpenSource(TwainSource source)
        {
            _src = (TW_IDENTITY*)Marshal.AllocHGlobal(sizeof(TW_IDENTITY));
            *_src = source.Identity;
            ushort rc = DsmCall(Tw.DG_CONTROL, Tw.DAT_IDENTITY, Tw.MSG_OPENDS, _src);
            if (rc != Tw.RC_SUCCESS)
            {
                ushort cc = Status(false);
                string code = cc == Tw.CC_MAXCONNECTIONS ? "device_busy"
                            : cc == Tw.CC_CHECKDEVICEONLINE ? "device_offline"
                            : cc == Tw.CC_NODS ? "no_device" : "device_open_failed";
                throw new ScanException(code, "the scanner did not open (rc " + rc + ", " + Tw.CcName(cc) + ")");
            }
            SourceOpen = true;
            Log.Write("source open: " + source.Name + " (protocol " + source.Protocol + ", TWAIN 2: " + SourceTwain2 + ")");
        }

        public bool RegisterCallback(DsmEntryProc callback)
        {
            if (!Dsm2 || !SourceTwain2) return false;
            TW_CALLBACK cb = default;
            cb.CallBackProc = Marshal.GetFunctionPointerForDelegate(callback);
            ushort rc = DsCall(Tw.DG_CONTROL, Tw.DAT_CALLBACK, Tw.MSG_REGISTER_CALLBACK, &cb);
            Log.Write("register callback: rc " + rc);
            return rc == Tw.RC_SUCCESS;
        }

        public ushort Enable(bool showUI, IntPtr hwnd, out bool uiShown)
        {
            TW_USERINTERFACE ui = default;
            ui.ShowUI = (ushort)(showUI ? 1 : 0);
            ui.ModalUI = (ushort)(showUI ? 1 : 0);
            ui.hParent = hwnd;
            ushort rc = DsCall(Tw.DG_CONTROL, Tw.DAT_USERINTERFACE, Tw.MSG_ENABLEDS, &ui);
            // TWRC_CHECKSTATUS: the source could not honour ShowUI and changed it
            uiShown = ui.ShowUI != 0;
            if (rc == Tw.RC_SUCCESS || rc == Tw.RC_CHECKSTATUS) Enabled = true;
            Log.Write("enable: rc " + rc + ", ShowUI " + showUI + " -> " + uiShown);
            return rc;
        }

        public void Disable(IntPtr hwnd)
        {
            if (!Enabled) return;
            TW_USERINTERFACE ui = default;
            ui.hParent = hwnd;
            ushort rc = DsCall(Tw.DG_CONTROL, Tw.DAT_USERINTERFACE, Tw.MSG_DISABLEDS, &ui);
            Log.Write("disable: rc " + rc);
            Enabled = false;
        }

        public void CloseSource()
        {
            if (!SourceOpen) return;
            ushort rc = DsmCall(Tw.DG_CONTROL, Tw.DAT_IDENTITY, Tw.MSG_CLOSEDS, _src);
            Log.Write("close source: rc " + rc);
            SourceOpen = false;
        }

        public void CloseDsm(IntPtr hwnd)
        {
            if (!DsmOpen) return;
            IntPtr parent = hwnd;
            ushort rc = DsmCall(Tw.DG_CONTROL, Tw.DAT_PARENT, Tw.MSG_CLOSEDSM, &parent);
            Log.Write("close DSM: rc " + rc);
            DsmOpen = false;
        }

        // ---- memory: through the manager when it offers it, Global* otherwise ----

        public IntPtr Alloc(uint size) => _memAlloc != null ? _memAlloc(size) : Native.GlobalAlloc(Native.GHND, (UIntPtr)size);

        public void Free(IntPtr h)
        {
            if (h == IntPtr.Zero) return;
            if (_memFree != null) _memFree(h);
            else Native.GlobalFree(h);
        }

        public IntPtr Lock(IntPtr h) => _memLock != null ? _memLock(h) : Native.GlobalLock(h);

        public void Unlock(IntPtr h)
        {
            if (_memUnlock != null) _memUnlock(h);
            else Native.GlobalUnlock(h);
        }

        // ---- capabilities ----

        public bool SetOne(ushort cap, ushort type, uint value)
        {
            IntPtr h = Alloc(6);
            if (h == IntPtr.Zero) return false;
            try
            {
                byte* p = (byte*)Lock(h);
                *(ushort*)p = type;
                *(uint*)(p + 2) = value;
                Unlock(h);
                TW_CAPABILITY c = default;
                c.Cap = cap;
                c.ConType = Tw.ON_ONEVALUE;
                c.hContainer = h;
                ushort rc = DsCall(Tw.DG_CONTROL, Tw.DAT_CAPABILITY, Tw.MSG_SET, &c);
                bool ok = rc == Tw.RC_SUCCESS || rc == Tw.RC_CHECKSTATUS;
                string cc = ok ? "" : ", " + Tw.CcName(Status(true));
                Log.Write("set cap 0x" + cap.ToString("x4") + " = " + value + ": rc " + rc + cc);
                return ok;
            }
            finally
            {
                Free(h);
            }
        }

        public CapValue? Get(ushort cap, ushort msg)
        {
            TW_CAPABILITY c = default;
            c.Cap = cap;
            c.ConType = Tw.ON_DONTCARE16;
            ushort rc = DsCall(Tw.DG_CONTROL, Tw.DAT_CAPABILITY, msg, &c);
            if (rc != Tw.RC_SUCCESS)
            {
                Log.Write("get cap 0x" + cap.ToString("x4") + ": rc " + rc + ", " + Tw.CcName(Status(true)));
                return null;
            }
            if (c.hContainer == IntPtr.Zero) return null;
            try
            {
                byte* p = (byte*)Lock(c.hContainer);
                if (p == null) return null;
                var v = new CapValue { ConType = c.ConType, ItemType = *(ushort*)p };
                int size = Tw.TypeSize(v.ItemType);
                switch (c.ConType)
                {
                    case Tw.ON_ONEVALUE:
                        v.Current = v.Default = ReadItem(p + 2, v.ItemType);
                        v.Items.Add(v.Current);
                        break;
                    case Tw.ON_ENUMERATION:
                    {
                        uint n = Math.Min(*(uint*)(p + 2), 4096u);
                        uint cur = *(uint*)(p + 6), def = *(uint*)(p + 10);
                        for (uint i = 0; i < n; i++) v.Items.Add(ReadItem(p + 14 + i * size, v.ItemType));
                        v.Current = cur < n ? v.Items[(int)cur] : 0;
                        v.Default = def < n ? v.Items[(int)def] : 0;
                        break;
                    }
                    case Tw.ON_ARRAY:
                    {
                        uint n = Math.Min(*(uint*)(p + 2), 4096u);
                        for (uint i = 0; i < n; i++) v.Items.Add(ReadItem(p + 6 + i * size, v.ItemType));
                        break;
                    }
                    case Tw.ON_RANGE:
                        v.Min = *(uint*)(p + 2);
                        v.Max = *(uint*)(p + 6);
                        v.Step = *(uint*)(p + 10);
                        v.Default = *(uint*)(p + 14);
                        v.Current = *(uint*)(p + 18);
                        break;
                }
                Unlock(c.hContainer);
                return v;
            }
            finally
            {
                Free(c.hContainer);
            }
        }

        public bool? GetBool(ushort cap)
        {
            var v = Get(cap, Tw.MSG_GETCURRENT) ?? Get(cap, Tw.MSG_GET);
            if (v == null) return null;
            return v.Current != 0;
        }

        private static uint ReadItem(byte* p, ushort type)
        {
            switch (type)
            {
                case Tw.TY_INT8: return (uint)(sbyte)*p;
                case Tw.TY_UINT8: return *p;
                case Tw.TY_INT16: return (uint)*(short*)p;
                case Tw.TY_UINT16:
                case Tw.TY_BOOL: return *(ushort*)p;
                default: return *(uint*)p;
            }
        }

        public void Dispose()
        {
            // The manager stays loaded: unloading it while a driver thread may
            // still be running in it can crash the process on the way out.
            if (_src != null) { Marshal.FreeHGlobal((IntPtr)_src); _src = null; }
            if (_app != null) { Marshal.FreeHGlobal((IntPtr)_app); _app = null; }
        }

        public static TwainSource? Match(List<TwainSource> sources, string wanted)
        {
            return sources.FirstOrDefault(s => s.Name == wanted)
                ?? sources.FirstOrDefault(s => string.Equals(s.Name, wanted, StringComparison.OrdinalIgnoreCase))
                ?? sources.FirstOrDefault(s => s.Name.IndexOf(wanted, StringComparison.OrdinalIgnoreCase) >= 0);
        }
    }
}
