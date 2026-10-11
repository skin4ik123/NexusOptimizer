using System.Windows;

namespace NexusOptimizer.Setup;

public partial class App : System.Windows.Application
{
    private async void OnAppStartup(object sender, StartupEventArgs e)
    {
        var options = SetupOptions.Parse(e.Args);
        if (options.Silent)
        {
            // Silent mode: /S [/allusers | /currentuser] [/dir=path]
            //                 [/nodesktop] [/nomenu]
            // Without attaching to the parent console the messages below are
            // lost and only the exit code survives.
            ParentConsole.AttachToParent();
            int exitCode;
            try
            {
                if (!string.IsNullOrWhiteSpace(options.CustomDir) &&
                    !System.IO.Path.IsPathRooted(options.CustomDir))
                {
                    Console.Error.WriteLine("NexusOptimizerSetup: /dir= requires a full path (code 87).");
                    Shutdown(87);
                    return;
                }

                var installed = InstalledProductInspector.Detect();
                if (installed == InstalledContext.PerUser && options.AllUsers ||
                    installed == InstalledContext.PerMachine && !options.AllUsers)
                {
                    Console.Error.WriteLine(
                        "NexusOptimizerSetup: the product is already installed in the other mode " +
                        "(per-user/per-machine). Remove the existing copy first (code 1638).");
                    Shutdown(1638);
                    return;
                }

                // A copy left by the previous installer goes before the new
                // files are laid down, otherwise two versions share one folder.
                var legacy = SetupEngine.FindLegacyUninstaller();
                if (legacy != null)
                    SetupEngine.RemoveLegacy(legacy);

                var msi = SetupEngine.ExtractMsi();
                var result = await SetupEngine.InstallAsync(options, msi);
                exitCode = result.ExitCode;

                if (exitCode is 0 or 3010 && options.ContextMenu)
                {
                    SetupEngine.TryRegisterUserMenu(options);

                    // The system-wide part is only touched in silent mode when
                    // the process is already elevated: a UAC prompt in the
                    // middle of an unattended install means a stuck install.
                    if (!options.AllUsers && SetupEngine.NeedsSystemWideMenu && SetupEngine.IsElevated)
                        SetupEngine.TryRegisterSystemWideMenu(options);
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.Message);
                exitCode = 1603;
            }
            Shutdown(exitCode);
            return;
        }

        MainWindow = new SetupWindow(options);
        MainWindow.Show();
    }
}
