using System.IO;
using System.Runtime.InteropServices;

namespace NexusOptimizer.Setup;

/// <summary>
/// The installer is a windowed program, so it has no console of its own and
/// everything written to Console.Error was lost. In silent mode (/S) that meant
/// an administrator running it from a script got a bare exit code — 87, 740,
/// 1638 or 1603 — and not one line about what went wrong.
///
/// Attaching to the caller's console and reopening the error stream on it fixes
/// that. When there is no console (double click, a deployment service),
/// AttachConsole returns false and nothing changes.
/// </summary>
internal static class ParentConsole
{
    private const int AttachParentProcess = -1;

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AttachConsole(int processId);

    public static void AttachToParent()
    {
        try
        {
            if (!AttachConsole(AttachParentProcess))
                return;

            // Reopening is deliberate: by now .NET may have bound Console.Error
            // to a null device, and writes would still go nowhere.
            var writer = new StreamWriter(Console.OpenStandardError()) { AutoFlush = true };
            Console.SetError(writer);
        }
        catch (IOException)
        {
            // No console available: messages stay invisible, but the exit code
            // is returned either way.
        }
    }
}
