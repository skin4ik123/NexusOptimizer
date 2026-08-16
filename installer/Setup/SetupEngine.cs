using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;

namespace NexusOptimizer.Setup;

public enum InstalledContext
{
    None,
    PerUser,
    PerMachine,
}

/// <summary>
/// Finds an existing installation by UpgradeCode. Windows Installer does not
/// upgrade across the per-user/per-machine boundary, so a change of mode has to
/// be detected up front — otherwise the machine ends up with two parallel
/// installations and neither can remove the other's files.
/// </summary>
public static class InstalledProductInspector
{
    private const string UpgradeCode = "{9F2C41D7-6B58-4E93-A2F1-7C0D5E8B3A44}";
    private const uint ErrorNoMoreItems = 259;

    [DllImport("msi.dll", CharSet = CharSet.Unicode)]
    private static extern uint MsiEnumRelatedProducts(string lpUpgradeCode, uint dwReserved, uint iProductIndex, StringBuilder lpProductBuf);

    [DllImport("msi.dll", CharSet = CharSet.Unicode)]
    private static extern uint MsiGetProductInfo(string szProduct, string szProperty, StringBuilder lpValueBuf, ref uint pcchValueBuf);

    public static InstalledContext Detect()
    {
        var productCode = new StringBuilder(39);
        var status = MsiEnumRelatedProducts(UpgradeCode, 0, 0, productCode);
        if (status == ErrorNoMoreItems)
            return InstalledContext.None;
        if (status != 0)
            return InstalledContext.None; // undetermined: do not block the install

        var value = new StringBuilder(8);
        var length = (uint)value.Capacity;
        // INSTALLPROPERTY_ASSIGNMENTTYPE: "0" per-user, "1" per-machine
        if (MsiGetProductInfo(productCode.ToString(), "AssignmentType", value, ref length) != 0)
            return InstalledContext.None;
        return value.ToString() == "1" ? InstalledContext.PerMachine : InstalledContext.PerUser;
    }
}

public sealed class SetupOptions
{
    public bool Silent { get; init; }
    public bool AllUsers { get; set; }
    public bool DesktopShortcut { get; set; } = true;
    public bool ContextMenu { get; set; } = true;
    public string? CustomDir { get; set; }

    public static SetupOptions Parse(string[] args)
    {
        var options = new SetupOptions
        {
            Silent = args.Any(a =>
                a.Equals("/S", StringComparison.Ordinal) ||
                a.Equals("/quiet", StringComparison.OrdinalIgnoreCase) ||
                a.Equals("/qn", StringComparison.OrdinalIgnoreCase)),
        };
        foreach (var arg in args)
        {
            if (arg.Equals("/allusers", StringComparison.OrdinalIgnoreCase))
                options.AllUsers = true;
            else if (arg.Equals("/currentuser", StringComparison.OrdinalIgnoreCase))
                options.AllUsers = false;
            else if (arg.Equals("/nodesktop", StringComparison.OrdinalIgnoreCase))
                options.DesktopShortcut = false;
            else if (arg.Equals("/nomenu", StringComparison.OrdinalIgnoreCase))
                options.ContextMenu = false;
            else if (arg.StartsWith("/dir=", StringComparison.OrdinalIgnoreCase))
                options.CustomDir = arg[5..].Trim('"');
        }
        return options;
    }

    public string EffectiveInstallDir =>
        string.IsNullOrWhiteSpace(CustomDir) ? DefaultInstallDir(AllUsers) : CustomDir;

    public static string DefaultInstallDir(bool allUsers) => allUsers
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Nexus Optimizer")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Nexus Optimizer");
}

public sealed record InstallResult(int ExitCode, string LogPath);

public static class SetupEngine
{
    public const string AppExeName = "NexusOptimizer.exe";

    /// <summary>
    /// Read from the assembly, which the build script fills in from APP_VERSION
    /// in the application source. A constant here would drift, and MSI compares
    /// versions: an equal version is not an upgrade.
    /// </summary>
    public static string ProductVersion { get; } = ReadVersion();

    private static string ReadVersion()
    {
        var informational = Assembly.GetExecutingAssembly()
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        if (string.IsNullOrWhiteSpace(informational))
            return Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "";
        var plus = informational.IndexOf('+');
        return plus > 0 ? informational[..plus] : informational;
    }

    /// <summary>Extracts the embedded MSI to a temporary folder.</summary>
    public static string ExtractMsi()
    {
        using var stream = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream("NexusOptimizer.Setup.Payload.NexusOptimizer.msi");
        if (stream == null)
            throw new InvalidOperationException(
                "This installer file is incomplete: the installation package is missing from it. " +
                "Download NexusOptimizerSetup.exe again.");

        var dir = Path.Combine(Path.GetTempPath(), "NexusOptimizerSetup-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        var msiPath = Path.Combine(dir, "NexusOptimizer.msi");
        using var file = File.Create(msiPath);
        stream.CopyTo(file);
        return msiPath;
    }

    public static string LoadLicenseText()
    {
        using var stream = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream("NexusOptimizer.Setup.Payload.license.txt");
        if (stream == null) return "";
        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }

    public static bool IsElevated =>
        new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);

    /// <summary>
    /// Windows 11 24H2 (build 26100+) ignores per-user Explorer menu entries;
    /// only the system-wide HKLM branch is shown.
    /// </summary>
    public static bool NeedsSystemWideMenu =>
        Environment.OSVersion.Version.Build >= 26100;

    public static async Task<InstallResult> InstallAsync(SetupOptions options, string msiPath)
    {
        var logPath = Path.Combine(Path.GetTempPath(),
            $"NexusOptimizer-install-{DateTime.Now:yyyyMMdd-HHmmss}.log");

        // Silent mode never shows UAC: an all-users install requires an already
        // elevated process, otherwise it honestly returns 740.
        if (options.Silent && options.AllUsers && !IsElevated)
        {
            Console.Error.WriteLine(
                "NexusOptimizerSetup: /allusers in silent mode requires administrator rights (code 740).");
            return new InstallResult(740, logPath);
        }

        var args = new List<string>
        {
            "/i", Quote(msiPath),
            "/qn", "/norestart",
            "/l*v", Quote(logPath),
            "DESKTOP_SHORTCUT=" + (options.DesktopShortcut ? "1" : "0"),
            "CONTEXT_MENU=" + (options.ContextMenu ? "1" : "0"),
        };
        if (options.AllUsers)
        {
            args.Add("ALLUSERS=1");
        }
        else
        {
            // ALLUSERS=2 is required together with MSIINSTALLPERUSER=1. Without
            // it Windows Installer treats the install as per-machine and runs
            // its script as the system account: the Installed apps entry lands
            // in HKLM and per-user keys are looked up in a different HKCU hive,
            // so uninstall silently leaves the menu entry and settings behind.
            args.Add("ALLUSERS=2");
            args.Add("MSIINSTALLPERUSER=1");
        }
        args.Add("INSTALLFOLDER=" + Quote(options.EffectiveInstallDir));

        var needRunas = options.AllUsers && !IsElevated; // interactive UAC only with a window
        var psi = new ProcessStartInfo
        {
            FileName = "msiexec.exe",
            Arguments = string.Join(" ", args),
            UseShellExecute = needRunas,
        };
        if (needRunas)
            psi.Verb = "runas";
        else
            psi.CreateNoWindow = true;

        try
        {
            using var process = Process.Start(psi)
                ?? throw new InvalidOperationException("Could not start msiexec.");
            await process.WaitForExitAsync();
            return new InstallResult(process.ExitCode, logPath);
        }
        catch (System.ComponentModel.Win32Exception ex) when (ex.NativeErrorCode == 1223)
        {
            return new InstallResult(1602, logPath); // the elevation prompt was declined
        }
        catch (System.ComponentModel.Win32Exception ex)
        {
            Console.Error.WriteLine($"NexusOptimizerSetup: could not start msiexec: {ex.Message}");
            return new InstallResult(1603, logPath);
        }
    }

    // ---- migration from the previous installer (Inno Setup) ----
    //
    // Before 3.7 the program was installed by Inno Setup. Windows Installer
    // knows nothing about such an installation: without this the machine would
    // end up with two copies in one folder and two entries in Installed apps,
    // neither of which removes the other's files.

    private const string LegacyAppId = "{7E1A9C64-52B0-4B1E-9B9C-6E1F0A7C2D53}_is1";

    /// <summary>Path to the previous version's uninstaller, or null.</summary>
    public static string? FindLegacyUninstaller()
    {
        foreach (var root in new[] { Microsoft.Win32.Registry.CurrentUser, Microsoft.Win32.Registry.LocalMachine })
        {
            try
            {
                using var key = root.OpenSubKey(
                    @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + LegacyAppId);
                var raw = key?.GetValue("UninstallString") as string;
                if (string.IsNullOrWhiteSpace(raw)) continue;
                var path = raw.Trim().Trim('"');
                if (File.Exists(path)) return path;
            }
            catch
            {
                // unreadable registry branch: keep looking
            }
        }

        // The registry entry can be gone while the files remain, so fall back to
        // the uninstaller in the usual install location.
        foreach (var dir in new[]
                 {
                     SetupOptions.DefaultInstallDir(allUsers: false),
                     SetupOptions.DefaultInstallDir(allUsers: true),
                 })
        {
            var candidate = Path.Combine(dir, "unins000.exe");
            if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    /// <summary>
    /// Removes the previous version, keeping settings and history.
    ///
    /// The Inno uninstaller deletes %APPDATA%\NexusOptimizer by design, which is
    /// right for an uninstall but would mean losing every setting during an
    /// upgrade, so the folder is set aside and put back.
    /// </summary>
    public static bool RemoveLegacy(string uninstaller)
    {
        var data = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "NexusOptimizer");
        var backup = Path.Combine(Path.GetTempPath(), "NexusOptimizer-settings-" + Guid.NewGuid().ToString("N"));
        var saved = false;
        try
        {
            if (Directory.Exists(data))
            {
                CopyDirectory(data, backup);
                saved = true;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("NexusOptimizerSetup: settings backup: " + ex.Message);
        }

        var removed = false;
        try
        {
            var psi = new ProcessStartInfo(uninstaller,
                "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var process = Process.Start(psi);
            if (process != null)
            {
                // Inno starts a second copy of itself in a temporary folder and
                // returns at once, so wait for the files to actually disappear.
                process.WaitForExit(120_000);
                var dir = Path.GetDirectoryName(uninstaller);
                for (var i = 0; i < 60 && dir != null && File.Exists(Path.Combine(dir, AppExeName)); i++)
                    Thread.Sleep(1000);
                removed = dir == null || !File.Exists(Path.Combine(dir, AppExeName));
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("NexusOptimizerSetup: legacy uninstall: " + ex.Message);
        }

        try
        {
            if (saved && Directory.Exists(backup))
            {
                CopyDirectory(backup, data);
                Directory.Delete(backup, recursive: true);
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("NexusOptimizerSetup: settings restore: " + ex.Message);
        }
        return removed;
    }

    private static void CopyDirectory(string from, string to)
    {
        Directory.CreateDirectory(to);
        foreach (var file in Directory.GetFiles(from))
            File.Copy(file, Path.Combine(to, Path.GetFileName(file)), overwrite: true);
        foreach (var dir in Directory.GetDirectories(from))
            CopyDirectory(dir, Path.Combine(to, Path.GetFileName(dir)));
    }

    /// <summary>
    /// Per-user part of the Explorer menu: HKCU keys, the Send to shortcut and
    /// Open with. Done from here rather than from an MSI custom action, which
    /// runs inside the installer service and writes to a different HKCU hive
    /// while still reporting success.
    /// </summary>
    public static bool TryRegisterUserMenu(SetupOptions options)
    {
        var exe = Path.Combine(options.EffectiveInstallDir, AppExeName);
        if (!File.Exists(exe))
            return false;
        try
        {
            var psi = new ProcessStartInfo(exe, "--register-menu")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = options.EffectiveInstallDir,
            };
            using var process = Process.Start(psi);
            if (process == null) return false;
            process.WaitForExit(60_000);
            return process.HasExited && process.ExitCode == 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("NexusOptimizerSetup: explorer menu: " + ex.Message);
            return false;
        }
    }

    /// <summary>
    /// System-wide part of the Explorer menu for a per-user install: one UAC
    /// prompt. Declining is a normal outcome, not an error — the Send to
    /// shortcut keeps working and the menu can be enabled later in Settings.
    /// </summary>
    public static bool TryRegisterSystemWideMenu(SetupOptions options)
    {
        var exe = Path.Combine(options.EffectiveInstallDir, AppExeName);
        if (!File.Exists(exe))
            return false;
        try
        {
            var psi = new ProcessStartInfo(exe, "--register-menu-hklm")
            {
                UseShellExecute = true,
                Verb = "runas",
                WorkingDirectory = options.EffectiveInstallDir,
            };
            using var process = Process.Start(psi);
            if (process == null) return false;
            process.WaitForExit(30_000);
            return process.HasExited && process.ExitCode == 0;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return false; // declined UAC
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("NexusOptimizerSetup: system-wide menu: " + ex.Message);
            return false;
        }
    }

    public static void LaunchInstalledApp(SetupOptions options)
    {
        var exe = Path.Combine(options.EffectiveInstallDir, AppExeName);
        if (File.Exists(exe))
            Process.Start(new ProcessStartInfo(exe)
            {
                UseShellExecute = true,
                WorkingDirectory = options.EffectiveInstallDir,
            });
    }

    private static string Quote(string value) => "\"" + value + "\"";
}
