using System;
using System.Runtime.InteropServices;

namespace NexusScan
{
    /// <summary>
    /// The worker's own window: parent for the driver's dialogs and target of
    /// the messages that wake the scan loop.
    ///
    /// It is a 1x1 fully transparent window at the centre of the program's
    /// window. Drivers centre their dialogs on it, so they appear over the
    /// program; it has a taskbar button, so a driver dialog that slipped behind
    /// other windows can always be brought back; and it never catches a click.
    /// </summary>
    internal sealed class HostWindow : IDisposable
    {
        private const string ClassName = "NexusScanHost";
        private static Native.WndProc? _proc;   // rooted for the life of the process
        private static bool _registered;
        private UIntPtr _focusTimer;
        private int _focusTicks;

        public IntPtr Handle { get; private set; }

        public HostWindow(IntPtr programWindow, bool visibleInTaskbar)
        {
            if (!_registered)
            {
                _proc = WindowProc;
                var wc = new Native.WNDCLASS
                {
                    lpfnWndProc = _proc,
                    hInstance = Native.GetModuleHandleW(null),
                    lpszClassName = ClassName,
                };
                Native.RegisterClassW(ref wc);
                _registered = true;
            }

            int x = 0, y = 0;
            if (programWindow != IntPtr.Zero && Native.IsWindow(programWindow) &&
                Native.GetWindowRect(programWindow, out var r))
            {
                x = (r.Left + r.Right) / 2;
                y = (r.Top + r.Bottom) / 2;
            }
            uint ex = Native.WS_EX_LAYERED | Native.WS_EX_TRANSPARENT |
                      (visibleInTaskbar ? Native.WS_EX_APPWINDOW : Native.WS_EX_TOOLWINDOW);
            Handle = Native.CreateWindowExW(ex, ClassName, "Nexus Optimizer - scanner",
                Native.WS_POPUP | (visibleInTaskbar ? Native.WS_VISIBLE : 0), x, y, 1, 1,
                IntPtr.Zero, IntPtr.Zero, Native.GetModuleHandleW(null), IntPtr.Zero);
            if (Handle == IntPtr.Zero)
                throw new ScanException("internal", "cannot create a window (error " + Marshal.GetLastWin32Error() + ")");
            Native.SetLayeredWindowAttributes(Handle, 0, 0, Native.LWA_ALPHA);
        }

        /// <summary>
        /// For a few seconds, bring the first visible window of this thread
        /// that is not ours (the driver's dialog) to the front. Windows lets a
        /// process started by the foreground program do that once.
        /// </summary>
        public void FocusDriverWindowSoon()
        {
            _focusTicks = 0;
            _focusTimer = Native.SetTimer(Handle, (UIntPtr)7, 250, IntPtr.Zero);
        }

        /// <summary>Called for WM_TIMER from the loop that owns this window.</summary>
        public void OnTimer(IntPtr id)
        {
            if (id != (IntPtr)7) return;
            if (++_focusTicks > 16 || TryFocusDriverWindow())
            {
                Native.KillTimer(Handle, _focusTimer);
                _focusTimer = UIntPtr.Zero;
            }
        }

        private bool TryFocusDriverWindow()
        {
            IntPtr found = IntPtr.Zero;
            IntPtr own = Handle;
            Native.EnumThreadWindows(Native.GetCurrentThreadId(), (h, l) =>
            {
                if (h != own && Native.IsWindowVisible(h))
                {
                    found = h;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            if (found == IntPtr.Zero) return false;
            Native.BringWindowToTop(found);
            Native.SetForegroundWindow(found);
            Log.Write("driver window brought to front: 0x" + found.ToInt64().ToString("x"));
            return true;
        }

        private static IntPtr WindowProc(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam)
        {
            // A cancel that arrives while a driver dialog runs its own modal loop
            // (the WIA scan dialog) lands here: close that dialog the way its
            // own close button would, and the driver reports a cancel.
            if (msg == Native.WM_CANCEL)
            {
                Native.EnumThreadWindows(Native.GetCurrentThreadId(), (h, l) =>
                {
                    if (h != hwnd && Native.IsWindowVisible(h))
                        Native.PostMessageW(h, 0x0010 /* WM_CLOSE */, IntPtr.Zero, IntPtr.Zero);
                    return true;
                }, IntPtr.Zero);
                return IntPtr.Zero;
            }
            return Native.DefWindowProcW(hwnd, msg, wParam, lParam);
        }

        public void Dispose()
        {
            if (Handle != IntPtr.Zero)
            {
                if (_focusTimer != UIntPtr.Zero) Native.KillTimer(Handle, _focusTimer);
                Native.DestroyWindow(Handle);
                Handle = IntPtr.Zero;
            }
        }
    }
}
