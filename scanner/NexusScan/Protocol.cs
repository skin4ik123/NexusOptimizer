using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace NexusScan
{
    /// <summary>
    /// Events for the program: one JSON object per line on stdout, ASCII only
    /// (everything else is escaped), so the pipe's encoding never matters.
    /// </summary>
    internal sealed class JsonOut
    {
        private readonly TextWriter _writer;
        private readonly object _lock = new object();

        public JsonOut(Stream stream)
        {
            _writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" };
        }

        public void Emit(string evt, params object?[] pairs)
        {
            var sb = new StringBuilder(128);
            sb.Append("{\"event\":");
            AppendString(sb, evt);
            for (int i = 0; i + 1 < pairs.Length; i += 2)
            {
                sb.Append(',');
                AppendString(sb, (string)pairs[i]!);
                sb.Append(':');
                AppendValue(sb, pairs[i + 1]);
            }
            sb.Append('}');
            var line = sb.ToString();
            Log.Write("-> " + line);
            lock (_lock)
            {
                try
                {
                    _writer.WriteLine(line);
                }
                catch (IOException)
                {
                    // the program closed the pipe: nobody is listening any more
                }
            }
        }

        internal static void AppendString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20 || c > 0x7E)
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        private static void AppendValue(StringBuilder sb, object? v)
        {
            switch (v)
            {
                case null: sb.Append("null"); break;
                case bool b: sb.Append(b ? "true" : "false"); break;
                case string s: AppendString(sb, s); break;
                case int i: sb.Append(i.ToString(CultureInfo.InvariantCulture)); break;
                case long l: sb.Append(l.ToString(CultureInfo.InvariantCulture)); break;
                case uint u: sb.Append(u.ToString(CultureInfo.InvariantCulture)); break;
                case double d: sb.Append(d.ToString("0.###", CultureInfo.InvariantCulture)); break;
                default: AppendString(sb, Convert.ToString(v, CultureInfo.InvariantCulture) ?? ""); break;
            }
        }
    }

    /// <summary>--key value command-line options; a key without a value is a flag.</summary>
    internal sealed class Options
    {
        private readonly Dictionary<string, string> _values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public string Command { get; private set; } = "";

        public static Options Parse(string[] args)
        {
            var o = new Options();
            int i = 0;
            if (args.Length > 0 && !args[0].StartsWith("--", StringComparison.Ordinal))
            {
                o.Command = args[0].ToLowerInvariant();
                i = 1;
            }
            for (; i < args.Length; i++)
            {
                var a = args[i];
                if (!a.StartsWith("--", StringComparison.Ordinal)) continue;
                var key = a.Substring(2);
                if (i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
                    o._values[key] = args[++i];
                else
                    o._values[key] = "true";
            }
            return o;
        }

        public string Get(string key, string def = "") => _values.TryGetValue(key, out var v) ? v : def;

        public bool Flag(string key) => _values.TryGetValue(key, out var v) &&
                                        (v == "true" || v == "1" || v == "yes");

        public int Int(string key, int def)
            => int.TryParse(Get(key), NumberStyles.Integer, CultureInfo.InvariantCulture, out var v) ? v : def;

        public long Long(string key, long def)
            => long.TryParse(Get(key), NumberStyles.Integer, CultureInfo.InvariantCulture, out var v) ? v : def;
    }

    /// <summary>What to scan and how. Values come from the program, already validated there.</summary>
    internal sealed class ScanRequest
    {
        public string Backend = "twain";  // twain | wia
        public string Device = "";        // TWAIN product name or WIA device id
        public int Dpi = 300;
        public string Color = "color";    // color | gray | bw
        public string Source = "flatbed"; // flatbed | feeder | duplex
        public string Paper = "auto";     // auto | a4 | letter | legal
        public bool ShowUI;
        public string Transfer = "native";// native | memory (TWAIN)
        public string OutDir = "";
        public IntPtr Parent;

        public bool Feeder => Source == "feeder" || Source == "duplex";
        public bool Duplex => Source == "duplex";

        public static ScanRequest From(Options o)
        {
            var r = new ScanRequest
            {
                Backend = o.Get("backend", "twain").ToLowerInvariant(),
                Device = o.Get("device"),
                Dpi = Math.Max(50, Math.Min(1200, o.Int("dpi", 300))),
                Color = o.Get("color", "color").ToLowerInvariant(),
                Source = o.Get("source", "flatbed").ToLowerInvariant(),
                Paper = o.Get("paper", "auto").ToLowerInvariant(),
                ShowUI = o.Flag("ui"),
                Transfer = o.Get("transfer", "native").ToLowerInvariant(),
                OutDir = o.Get("out"),
                Parent = new IntPtr(o.Long("parent", 0)),
            };
            if (r.Color != "gray" && r.Color != "bw") r.Color = "color";
            if (r.Source != "feeder" && r.Source != "duplex") r.Source = "flatbed";
            return r;
        }

        /// <summary>Page size in inches; null means "the scanner's default".</summary>
        public double[]? PaperInches()
        {
            switch (Paper)
            {
                case "a4": return new[] { 8.27, 11.69 };
                case "letter": return new[] { 8.5, 11.0 };
                case "legal": return new[] { 8.5, 14.0 };
                default: return null;
            }
        }
    }

    /// <summary>A failure with a stable code the program translates for the user.</summary>
    internal sealed class ScanException : Exception
    {
        public string Code { get; }
        public string Detail { get; }

        public ScanException(string code, string detail) : base(code + ": " + detail)
        {
            Code = code;
            Detail = detail;
        }
    }

    /// <summary>Optional protocol log for diagnosing a particular driver.</summary>
    internal static class Log
    {
        private static StreamWriter? _writer;
        private static readonly object Lock = new object();

        public static void Open(string path)
        {
            if (string.IsNullOrEmpty(path)) return;
            try
            {
                var info = new FileInfo(path);
                // keep it small: start over when it grows past 2 MB
                bool append = !(info.Exists && info.Length > 2 * 1024 * 1024);
                _writer = new StreamWriter(path, append, new UTF8Encoding(false)) { AutoFlush = true };
            }
            catch (Exception)
            {
                _writer = null;
            }
        }

        public static void Write(string line)
        {
            var w = _writer;
            if (w == null) return;
            lock (Lock)
            {
                try
                {
                    w.WriteLine(DateTime.Now.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture) +
                                " [" + (IntPtr.Size * 8) + "] " + line);
                }
                catch (Exception)
                {
                    // a log must never break a scan
                }
            }
        }
    }
}
