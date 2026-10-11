using System;
using System.Runtime.InteropServices;
using System.Text;

namespace NexusScan
{
    /// <summary>TWAIN 2.4 constants (twain.h). Values checked against the specification.</summary>
    internal static class Tw
    {
        public const ushort RC_SUCCESS = 0, RC_FAILURE = 1, RC_CHECKSTATUS = 2, RC_CANCEL = 3,
            RC_DSEVENT = 4, RC_NOTDSEVENT = 5, RC_XFERDONE = 6, RC_ENDOFLIST = 7;

        public const ushort CC_SUCCESS = 0, CC_BUMMER = 1, CC_LOWMEMORY = 2, CC_NODS = 3,
            CC_MAXCONNECTIONS = 4, CC_OPERATIONERROR = 5, CC_BADCAP = 6, CC_BADPROTOCOL = 9,
            CC_BADVALUE = 10, CC_SEQERROR = 11, CC_BADDEST = 12, CC_CAPUNSUPPORTED = 13,
            CC_CAPBADOPERATION = 14, CC_CAPSEQERROR = 15, CC_DENIED = 16, CC_PAPERJAM = 20,
            CC_PAPERDOUBLEFEED = 21, CC_FILEWRITEERROR = 22, CC_CHECKDEVICEONLINE = 23,
            CC_INTERLOCK = 24, CC_DAMAGEDCORNER = 25, CC_FOCUSERROR = 26, CC_DOCTOOLIGHT = 27,
            CC_DOCTOODARK = 28, CC_NOMEDIA = 29;

        public const uint DG_CONTROL = 0x0001, DG_IMAGE = 0x0002;
        public const uint DF_DSM2 = 0x10000000, DF_APP2 = 0x20000000, DF_DS2 = 0x40000000;

        public const ushort DAT_NULL = 0x0000, DAT_CAPABILITY = 0x0001, DAT_EVENT = 0x0002,
            DAT_IDENTITY = 0x0003, DAT_PARENT = 0x0004, DAT_PENDINGXFERS = 0x0005,
            DAT_SETUPMEMXFER = 0x0006, DAT_STATUS = 0x0008, DAT_USERINTERFACE = 0x0009,
            DAT_CALLBACK = 0x0010, DAT_IMAGEINFO = 0x0101, DAT_IMAGELAYOUT = 0x0102,
            DAT_IMAGEMEMXFER = 0x0103, DAT_IMAGENATIVEXFER = 0x0104, DAT_ENTRYPOINT = 0x0403;

        public const ushort MSG_NULL = 0x0000, MSG_GET = 0x0001, MSG_GETCURRENT = 0x0002,
            MSG_GETDEFAULT = 0x0003, MSG_GETFIRST = 0x0004, MSG_GETNEXT = 0x0005, MSG_SET = 0x0006,
            MSG_RESET = 0x0007, MSG_XFERREADY = 0x0101, MSG_CLOSEDSREQ = 0x0102,
            MSG_CLOSEDSOK = 0x0103, MSG_DEVICEEVENT = 0x0104, MSG_OPENDSM = 0x0301,
            MSG_CLOSEDSM = 0x0302, MSG_OPENDS = 0x0401, MSG_CLOSEDS = 0x0402,
            MSG_DISABLEDS = 0x0501, MSG_ENABLEDS = 0x0502, MSG_PROCESSEVENT = 0x0601,
            MSG_ENDXFER = 0x0701, MSG_REGISTER_CALLBACK = 0x0902;

        public const ushort ON_ARRAY = 3, ON_ENUMERATION = 4, ON_ONEVALUE = 5, ON_RANGE = 6;
        public const ushort ON_DONTCARE16 = 0xFFFF;
        public const uint DONTCARE32 = 0xFFFFFFFF;

        public const ushort TY_INT8 = 0, TY_INT16 = 1, TY_INT32 = 2, TY_UINT8 = 3,
            TY_UINT16 = 4, TY_UINT32 = 5, TY_BOOL = 6, TY_FIX32 = 7, TY_FRAME = 8,
            TY_STR32 = 9, TY_STR64 = 10, TY_STR128 = 11, TY_STR255 = 12;

        public const ushort CAP_XFERCOUNT = 0x0001, ICAP_COMPRESSION = 0x0100,
            ICAP_PIXELTYPE = 0x0101, ICAP_UNITS = 0x0102, ICAP_XFERMECH = 0x0103,
            CAP_FEEDERENABLED = 0x1002, CAP_FEEDERLOADED = 0x1003, CAP_AUTOFEED = 0x1007,
            CAP_INDICATORS = 0x100b, CAP_PAPERDETECTABLE = 0x100d, CAP_UICONTROLLABLE = 0x100e,
            CAP_DEVICEONLINE = 0x100f, CAP_DUPLEX = 0x1012, CAP_DUPLEXENABLED = 0x1013,
            ICAP_XRESOLUTION = 0x1118, ICAP_YRESOLUTION = 0x1119, ICAP_PIXELFLAVOR = 0x111f,
            ICAP_SUPPORTEDSIZES = 0x1122, ICAP_BITDEPTH = 0x112b;

        public const ushort PT_BW = 0, PT_GRAY = 1, PT_RGB = 2;
        public const ushort UN_INCHES = 0;
        public const ushort SX_NATIVE = 0, SX_MEMORY = 2;
        public const ushort CP_NONE = 0;
        public const ushort PF_CHOCOLATE = 0, PF_VANILLA = 1;
        public const ushort SS_NONE = 0, SS_A4 = 1, SS_USLETTER = 3, SS_USLEGAL = 4;
        public const uint MF_APPOWNS = 0x0001, MF_POINTER = 0x0008;
        public const ushort LG_USA = 13, CY_USA = 1;

        public static int TypeSize(ushort type)
        {
            switch (type)
            {
                case TY_INT8: case TY_UINT8: return 1;
                case TY_INT16: case TY_UINT16: case TY_BOOL: return 2;
                case TY_FRAME: return 16;
                case TY_STR32: return 34;
                case TY_STR64: return 66;
                case TY_STR128: return 130;
                case TY_STR255: return 256;
                default: return 4;
            }
        }

        public static double Fix(uint raw) => (short)(raw & 0xFFFF) + (raw >> 16) / 65536.0;

        public static uint FixRaw(double v)
        {
            int value = (int)Math.Round(v * 65536.0);
            ushort whole = (ushort)(value >> 16);
            ushort frac = (ushort)(value & 0xFFFF);
            return whole | ((uint)frac << 16);
        }

        public static string CcName(ushort cc)
        {
            switch (cc)
            {
                case CC_SUCCESS: return "success";
                case CC_BUMMER: return "bummer";
                case CC_LOWMEMORY: return "lowmemory";
                case CC_NODS: return "nods";
                case CC_MAXCONNECTIONS: return "maxconnections";
                case CC_OPERATIONERROR: return "operationerror";
                case CC_BADCAP: return "badcap";
                case CC_BADPROTOCOL: return "badprotocol";
                case CC_BADVALUE: return "badvalue";
                case CC_SEQERROR: return "seqerror";
                case CC_CAPUNSUPPORTED: return "capunsupported";
                case CC_CAPBADOPERATION: return "capbadoperation";
                case CC_PAPERJAM: return "paperjam";
                case CC_PAPERDOUBLEFEED: return "paperdoublefeed";
                case CC_CHECKDEVICEONLINE: return "checkdeviceonline";
                case CC_INTERLOCK: return "interlock";
                case CC_NOMEDIA: return "nomedia";
                default: return "cc" + cc;
            }
        }

        /// <summary>A TWAIN condition code as an error code the program understands.</summary>
        public static string CcToCode(ushort cc)
        {
            switch (cc)
            {
                case CC_PAPERJAM: return "paper_jam";
                case CC_PAPERDOUBLEFEED: return "double_feed";
                case CC_CHECKDEVICEONLINE: return "device_offline";
                case CC_INTERLOCK: return "cover_open";
                case CC_NOMEDIA: return "feeder_empty";
                case CC_LOWMEMORY: return "out_of_memory";
                case CC_MAXCONNECTIONS: return "device_busy";
                case CC_DENIED: return "access_denied";
                case CC_NODS: return "no_device";
                default: return "driver_error";
            }
        }
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal unsafe struct TW_IDENTITY
    {
        public uint Id;
        public ushort MajorNum;
        public ushort MinorNum;
        public ushort Language;
        public ushort Country;
        public fixed byte Info[34];
        public ushort ProtocolMajor;
        public ushort ProtocolMinor;
        public uint SupportedGroups;
        public fixed byte Manufacturer[34];
        public fixed byte ProductFamily[34];
        public fixed byte ProductName[34];

        /// <summary>TWAIN strings are ANSI in the system code page.</summary>
        public static string Read(byte* p)
        {
            int n = 0;
            while (n < 34 && p[n] != 0) n++;
            return n == 0 ? "" : Encoding.Default.GetString(ToArray(p, n)).Trim();
        }

        public static void Write(byte* p, string s)
        {
            var bytes = Encoding.Default.GetBytes(s ?? "");
            int n = Math.Min(bytes.Length, 33);
            for (int i = 0; i < 34; i++) p[i] = i < n ? bytes[i] : (byte)0;
        }

        private static byte[] ToArray(byte* p, int n)
        {
            var a = new byte[n];
            for (int i = 0; i < n; i++) a[i] = p[i];
            return a;
        }
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_USERINTERFACE { public ushort ShowUI; public ushort ModalUI; public IntPtr hParent; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_CAPABILITY { public ushort Cap; public ushort ConType; public IntPtr hContainer; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_EVENT { public IntPtr pEvent; public ushort TWMessage; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_PENDINGXFERS { public ushort Count; public uint EOJ; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_STATUS { public ushort ConditionCode; public ushort Data; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_FIX32
    {
        public short Whole;
        public ushort Frac;
        public double Value => Whole + Frac / 65536.0;

        public static TW_FIX32 From(double v)
        {
            uint raw = Tw.FixRaw(v);
            return new TW_FIX32 { Whole = (short)(raw & 0xFFFF), Frac = (ushort)(raw >> 16) };
        }
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_FRAME { public TW_FIX32 Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_IMAGELAYOUT { public TW_FRAME Frame; public uint DocumentNumber, PageNumber, FrameNumber; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal unsafe struct TW_IMAGEINFO
    {
        public TW_FIX32 XResolution;
        public TW_FIX32 YResolution;
        public int ImageWidth;
        public int ImageLength;
        public short SamplesPerPixel;
        public fixed short BitsPerSample[8];
        public short BitsPerPixel;
        public ushort Planar;
        public short PixelType;
        public ushort Compression;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_SETUPMEMXFER { public uint MinBufSize, MaxBufSize, Preferred; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_MEMORY { public uint Flags; public uint Length; public IntPtr TheMem; }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_IMAGEMEMXFER
    {
        public ushort Compression;
        public uint BytesPerRow, Columns, Rows, XOffset, YOffset, BytesWritten;
        public TW_MEMORY Memory;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_ENTRYPOINT
    {
        public uint Size;
        public IntPtr DSM_Entry;
        public IntPtr DSM_MemAllocate;
        public IntPtr DSM_MemFree;
        public IntPtr DSM_MemLock;
        public IntPtr DSM_MemUnlock;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    internal struct TW_CALLBACK { public IntPtr CallBackProc; public uint RefCon; public short Message; }

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal unsafe delegate ushort DsmEntryProc(TW_IDENTITY* origin, TW_IDENTITY* dest, uint dg, ushort dat, ushort msg, void* data);

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate IntPtr DsmMemAllocate(uint size);

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate void DsmMemFree(IntPtr handle);

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate IntPtr DsmMemLock(IntPtr handle);

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    internal delegate void DsmMemUnlock(IntPtr handle);

    internal static unsafe class TwLayout
    {
        /// <summary>
        /// Structure sizes against the specification. A mismatch raises no error
        /// at run time: the driver silently reads shifted fields, so the sizes are
        /// checked as numbers before anything is sent.
        /// </summary>
        public static string? Check()
        {
            int ptr = IntPtr.Size;
            // ValueTuple is not part of .NET Framework 4.6.2, hence parallel arrays
            string[] names =
            {
                "TW_IDENTITY", "TW_FIX32", "TW_PENDINGXFERS", "TW_STATUS", "TW_IMAGEINFO",
                "TW_IMAGELAYOUT", "TW_SETUPMEMXFER", "TW_USERINTERFACE", "TW_CAPABILITY",
                "TW_EVENT", "TW_IMAGEMEMXFER", "TW_ENTRYPOINT", "TW_CALLBACK",
            };
            int[] got =
            {
                sizeof(TW_IDENTITY), sizeof(TW_FIX32), sizeof(TW_PENDINGXFERS), sizeof(TW_STATUS),
                sizeof(TW_IMAGEINFO), sizeof(TW_IMAGELAYOUT), sizeof(TW_SETUPMEMXFER),
                sizeof(TW_USERINTERFACE), sizeof(TW_CAPABILITY), sizeof(TW_EVENT),
                sizeof(TW_IMAGEMEMXFER), sizeof(TW_ENTRYPOINT), sizeof(TW_CALLBACK),
            };
            int[] want =
            {
                156, 4, 6, 4, 42, 28, 12, 4 + ptr, 4 + ptr, ptr + 2, 34 + ptr, 4 + 5 * ptr, ptr + 6,
            };
            for (int i = 0; i < names.Length; i++)
                if (got[i] != want[i])
                    return names[i] + " is " + got[i] + " bytes instead of " + want[i];
            return null;
        }
    }
}
