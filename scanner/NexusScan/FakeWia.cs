#if TEST_HOOKS
using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace NexusScan
{
    /// <summary>
    /// A WIA device with the same object model as the automation library
    /// (DeviceManager - DeviceInfo - Device - Item - ImageFile, Properties with
    /// SubType constraints). The scanner code reaches it through the same
    /// dynamic calls it uses for real devices, so the tests exercise the real
    /// logic: property order, nearest resolution, feeder loop, error mapping.
    /// Only in builds made with -p:TestHooks=true.
    ///
    ///   NEXUS_FAKEWIA_PAGES=n       sheets in the feeder (default 3)
    ///   NEXUS_FAKEWIA_JAM_AT=n      WIA_ERROR_PAPER_JAM on transfer n
    ///   NEXUS_FAKEWIA_NO_FEEDER=1   flatbed only
    ///   NEXUS_FAKEWIA_NO_DUPLEX=1   feeder without duplex
    ///   NEXUS_FAKEWIA_BUSY=1        Connect fails with WIA_ERROR_BUSY
    ///   NEXUS_FAKEWIA_CANCEL_UI=1   the scan dialog is cancelled
    /// </summary>
    internal static class FakeWia
    {
        private static int Env(string name, int def)
            => int.TryParse(Environment.GetEnvironmentVariable(name), out var v) ? v : def;

        public sealed class Property
        {
            public int PropertyID { get; set; }
            public object Value { get; set; }
            public int SubType { get; set; }
            public int SubTypeMin { get; set; }
            public int SubTypeMax { get; set; }
            public int SubTypeStep { get; set; } = 1;
            public IEnumerable SubTypeValues { get; set; } = new object[0];
            public bool ReadOnly { get; set; }
            internal Func<int, bool>? Accept;

            public Property(int id, object value) { PropertyID = id; Value = value; }
        }

        /// <summary>Like the real collection: setting a refused value throws.</summary>
        public sealed class Properties : IEnumerable
        {
            private readonly List<GuardedProperty> _items = new List<GuardedProperty>();
            public void Add(Property p) => _items.Add(new GuardedProperty(p));
            public IEnumerator GetEnumerator() => _items.GetEnumerator();
            internal Property? Get(int id) => _items.Find(x => x.PropertyID == id)?.Inner;
        }

        public sealed class GuardedProperty
        {
            internal readonly Property Inner;
            public GuardedProperty(Property inner) { Inner = inner; }
            public int PropertyID => Inner.PropertyID;
            public int SubType => Inner.SubType;
            public int SubTypeMin => Inner.SubTypeMin;
            public int SubTypeMax => Inner.SubTypeMax;
            public int SubTypeStep => Inner.SubTypeStep;
            public IEnumerable SubTypeValues => Inner.SubTypeValues;

            public object Value
            {
                get => Inner.Value;
                set
                {
                    int v = Convert.ToInt32(value);
                    if (Inner.ReadOnly || (Inner.Accept != null && !Inner.Accept(v)))
                        throw new COMException("The parameter is incorrect.", unchecked((int)0x80070057));
                    Inner.Value = v;
                }
            }
        }

        public sealed class DeviceManager
        {
            public IEnumerable DeviceInfos => new[] { new DeviceInfo() };
        }

        public sealed class DeviceInfo
        {
            public string DeviceID => "{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\9999";
            public int Type => 1;
            public Properties Properties { get; } = new Properties();

            public DeviceInfo()
            {
                Properties.Add(new Property(7, "Nexus Fake WIA Scanner"));
                Properties.Add(new Property(3, "Nexus Test Lab"));
                Properties.Add(new Property(4, "Test device for the WIA code path"));
            }

            public Device Connect()
            {
                if (Env("NEXUS_FAKEWIA_BUSY", 0) == 1)
                    throw new COMException("The device is busy.", unchecked((int)0x80210006));
                return new Device();
            }
        }

        public sealed class Device
        {
            public Properties Properties { get; } = new Properties();
            private readonly Item _item;
            internal int Transfers;
            internal int Sheets = Env("NEXUS_FAKEWIA_PAGES", 3);

            public Device()
            {
                bool feeder = Env("NEXUS_FAKEWIA_NO_FEEDER", 0) != 1;
                bool duplex = feeder && Env("NEXUS_FAKEWIA_NO_DUPLEX", 0) != 1;
                int caps = 0x002 | (feeder ? 0x001 : 0) | (duplex ? 0x004 : 0);
                Properties.Add(new Property(3086, caps) { ReadOnly = true });
                Properties.Add(new Property(3087, feeder && Sheets > 0 ? 0x003 : 0x002) { ReadOnly = true });
                Properties.Add(new Property(3088, 0x002) { Accept = v => (v & ~caps) == 0 && v != 0 });
                Properties.Add(new Property(3096, 1));
                Properties.Add(new Property(3074, 8500) { ReadOnly = true });
                Properties.Add(new Property(3075, 11690) { ReadOnly = true });
                Properties.Add(new Property(3076, 8500) { ReadOnly = true });
                Properties.Add(new Property(3077, 14000) { ReadOnly = true });
                _item = new Item(this);
            }

            public IEnumerable Items => new[] { _item };

            internal int Select => Convert.ToInt32(Properties.Get(3088)!.Value);
        }

        public sealed class Item
        {
            private readonly Device _device;
            public Properties Properties { get; } = new Properties();

            public Item(Device device)
            {
                _device = device;
                Properties.Add(new Property(6146, 1));
                Properties.Add(new Property(4103, 3) { Accept = v => v == 0 || v == 2 || v == 3 });
                Properties.Add(new Property(4104, 24) { Accept = v => v == 1 || v == 8 || v == 24 });
                // a list, like most drivers: 150 is accepted, 250 is not
                var dpis = new object[] { 100, 150, 200, 300, 600 };
                var xext = new Property(6151, 1275) { SubType = 1, SubTypeMin = 1, SubTypeMax = 1275 };
                var yext = new Property(6152, 1754) { SubType = 1, SubTypeMin = 1, SubTypeMax = 1754 };
                // Like real drivers: a new resolution rescales the allowed area
                // but leaves the current extent in pixels as it was.
                Func<int, bool> acceptDpi = v =>
                {
                    if (Array.IndexOf(dpis, v) < 0) return false;
                    xext.SubTypeMax = 8500 * v / 1000;
                    yext.SubTypeMax = 11690 * v / 1000;
                    xext.Value = Math.Min(Convert.ToInt32(xext.Value), xext.SubTypeMax);
                    yext.Value = Math.Min(Convert.ToInt32(yext.Value), yext.SubTypeMax);
                    return true;
                };
                Properties.Add(new Property(6147, 150) { SubType = 2, SubTypeValues = dpis, Accept = acceptDpi });
                Properties.Add(new Property(6148, 150) { SubType = 2, SubTypeValues = dpis, Accept = v => Array.IndexOf(dpis, v) >= 0 });
                Properties.Add(new Property(6149, 0));
                Properties.Add(new Property(6150, 0));
                xext.Accept = v => v >= 1 && v <= xext.SubTypeMax;
                yext.Accept = v => v >= 1 && v <= yext.SubTypeMax;
                Properties.Add(xext);
                Properties.Add(yext);
            }

            public ImageFile? Transfer(string format = "")
            {
                int select = _device.Select;
                bool feeder = (select & 0x001) != 0;
                bool duplex = (select & 0x004) != 0;
                int sides = duplex ? 2 : 1;
                int number = ++_device.Transfers;
                int jam = Env("NEXUS_FAKEWIA_JAM_AT", 0);
                if (jam > 0 && number == jam)
                    throw new COMException("Paper is jammed in the scanner's document feeder.", unchecked((int)0x80210002));
                if (feeder && number > _device.Sheets * sides)
                    throw new COMException("There are no documents in the document feeder.", unchecked((int)0x80210003));
                if (!feeder && number > 1)
                    throw new InvalidOperationException("the flatbed scans one page per acquisition");

                int dpi = Convert.ToInt32(Properties.Get(6147)!.Value);
                int type = Convert.ToInt32(Properties.Get(4103)!.Value);
                int w = Convert.ToInt32(Properties.Get(6151)!.Value);
                int h = Convert.ToInt32(Properties.Get(6152)!.Value);
                int sheet = duplex ? (number + 1) / 2 : number;
                string side = duplex && number % 2 == 0 ? "BACK" : "FRONT";
                return new ImageFile(w, h, dpi, type, "FAKE WIA PAGE " + sheet + " " + side);
            }
        }

        public sealed class ImageFile
        {
            private readonly string _text;
            private readonly int _type;
            public int Width { get; }
            public int Height { get; }
            public double HorizontalResolution { get; }
            public int PixelDepth => _type == 0 ? 1 : _type == 2 ? 8 : 24;
            public string FileExtension => "bmp";

            public ImageFile(int w, int h, int dpi, int type, string text)
            {
                Width = w;
                Height = h;
                HorizontalResolution = dpi;
                _type = type;
                _text = text;
            }

            public void SaveFile(string path)
            {
                using (var bmp = new Bitmap(Width, Height, PixelFormat.Format24bppRgb))
                {
                    bmp.SetResolution((float)HorizontalResolution, (float)HorizontalResolution);
                    using (var g = Graphics.FromImage(bmp))
                    using (var font = new Font("Arial", (float)(HorizontalResolution / 4), FontStyle.Bold, GraphicsUnit.Pixel))
                    {
                        g.Clear(Color.FromArgb(244, 242, 236));
                        g.DrawString(_text, font, Brushes.Black, (float)(HorizontalResolution * 0.8), (float)(HorizontalResolution * 0.8));
                        for (int i = 0; i < 20; i++)
                            g.FillRectangle(Brushes.DimGray, (float)(HorizontalResolution * 0.8),
                                (float)(HorizontalResolution * (1.6 + i * 0.3)), (float)(Width * 0.6), (float)(HorizontalResolution * 0.1));
                    }
                    bmp.Save(path, ImageFormat.Bmp);
                }
            }
        }

        public sealed class CommonDialog
        {
            public IEnumerable? ShowSelectItems(object device, int intent, int bias, bool single, bool commonUI, bool cancelError)
            {
                if (Env("NEXUS_FAKEWIA_CANCEL_UI", 0) == 1) return null;
                var d = (Device)device;
                // as if the user picked the feeder, greyscale, 200 dpi in the dialog
                if ((Convert.ToInt32(d.Properties.Get(3086)!.Value) & 0x001) != 0)
                    d.Properties.Get(3088)!.Value = 0x001;
                var item = (Item)First(d.Items)!;
                item.Properties.Get(4103)!.Value = 2;
                item.Properties.Get(6147)!.Value = 200;
                item.Properties.Get(6148)!.Value = 200;
                return d.Items;
            }

            private static object? First(IEnumerable e)
            {
                foreach (var x in e) return x;
                return null;
            }
        }
    }
}
#endif
