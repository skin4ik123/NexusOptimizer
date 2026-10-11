using System;
using System.IO;
using System.Text;
using System.Threading;

namespace NexusScan
{
    /// <summary>
    /// Nexus Optimizer scanner worker.
    ///
    ///   NexusScan64.exe list [--skip-wia] [--log file]
    ///   NexusScan64.exe scan --backend twain|wia --device NAME|ID --out DIR
    ///       [--dpi 300] [--color color|gray|bw] [--source flatbed|feeder|duplex]
    ///       [--paper auto|a4|letter|legal] [--ui] [--transfer native|memory]
    ///       [--parent HWND] [--log file]
    ///   NexusScan64.exe layout
    ///
    /// Results are JSON lines on stdout (see Protocol.cs). During a scan the
    /// program may write "cancel" to stdin; closing stdin also cancels.
    /// Exit code: 0 done or cancelled, 1 scan error, 2 worker failure.
    /// </summary>
    internal static class Program
    {
        public const string Version = "1.0.0";

        private static volatile bool _cancel;
        private static IntPtr _wake;

        public static bool CancelRequested => _cancel;

        /// <summary>The window to wake when a cancel request arrives.</summary>
        public static IntPtr WakeWindow
        {
            get => _wake;
            set => _wake = value;
        }

        [STAThread]
        private static int Main(string[] args)
        {
            var output = new JsonOut(Console.OpenStandardOutput());
            var options = Options.Parse(args);
            Log.Open(options.Get("log"));
            Log.Write("start: " + string.Join(" ", args));
            try
            {
                string? layout = TwLayout.Check();
                if (layout != null)
                {
                    output.Emit("error", "code", "internal", "detail", "TWAIN structure layout: " + layout);
                    return 2;
                }
                output.Emit("hello", "version", Version, "arch", IntPtr.Size * 8,
#if TEST_HOOKS
                    "test_hooks", true
#else
                    "test_hooks", false
#endif
                    );

                switch (options.Command)
                {
                    case "list":
                        if (!options.Flag("skip-wia")) WiaScanner.List(output);
                        try
                        {
                            TwainScanner.List(output);
                        }
                        catch (ScanException ex)
                        {
                            output.Emit("note", "backend", "twain", "code", ex.Code, "detail", ex.Detail);
                        }
                        output.Emit("done");
                        return 0;

                    case "scan":
                    {
                        var request = ScanRequest.From(options);
                        if (string.IsNullOrEmpty(request.OutDir) || !Directory.Exists(request.OutDir))
                        {
                            output.Emit("error", "code", "internal", "detail", "--out must be an existing folder");
                            return 2;
                        }
                        if (!options.Flag("no-stdin")) WatchStdin();
                        return request.Backend == "wia"
                            ? new WiaScanner(request, output).Run()
                            : new TwainScanner(request, output).Run();
                    }

                    case "layout":
                        output.Emit("layout", "ok", true);
                        return 0;

                    default:
                        output.Emit("error", "code", "internal", "detail", "unknown command '" + options.Command + "'");
                        return 2;
                }
            }
            catch (ScanException ex)
            {
                output.Emit("error", "code", ex.Code, "detail", ex.Detail);
                output.Emit("done", "pages", 0, "cancelled", false);
                return 1;
            }
            catch (Exception ex)
            {
                Log.Write("fatal: " + ex);
                output.Emit("error", "code", "internal", "detail", ex.GetType().Name + ": " + ex.Message);
                return 2;
            }
        }

        /// <summary>
        /// "cancel" on stdin stops the scan between pages; so does a closed
        /// stdin, which is what happens when the program quits or dies.
        /// </summary>
        private static void WatchStdin()
        {
            var t = new Thread(() =>
            {
                try
                {
                    using (var reader = new StreamReader(Console.OpenStandardInput(), Encoding.UTF8))
                    {
                        string? line;
                        while ((line = reader.ReadLine()) != null)
                        {
                            if (line.Trim().Equals("cancel", StringComparison.OrdinalIgnoreCase))
                                RequestCancel("cancel command");
                        }
                    }
                    RequestCancel("stdin closed");
                }
                catch (Exception ex)
                {
                    RequestCancel("stdin error: " + ex.Message);
                }
            })
            { IsBackground = true, Name = "stdin" };
            t.Start();
        }

        private static void RequestCancel(string why)
        {
            if (_cancel) return;
            _cancel = true;
            Log.Write("cancel requested: " + why);
            IntPtr wake = _wake;
            if (wake != IntPtr.Zero) Native.PostMessageW(wake, Native.WM_CANCEL, IntPtr.Zero, IntPtr.Zero);
        }
    }
}
