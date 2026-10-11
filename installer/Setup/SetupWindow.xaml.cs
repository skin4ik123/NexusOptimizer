using System.IO;
using System.Windows;
using System.Windows.Input;
using System.Windows.Threading;

namespace NexusOptimizer.Setup;

public partial class SetupWindow : Window
{
    private static readonly string[] ProgressPhrases =
    {
        "Unpacking components…",
        "Copying files…",
        "Setting up the Explorer menu…",
        "Creating shortcuts…",
        "Almost done…",
    };

    private readonly SetupOptions _options;
    private readonly DispatcherTimer _phraseTimer;
    private int _phraseIndex;
    private bool _pathEditedByUser;
    private bool _suppressPathEvent;
    private bool _installing;
    private string? _lastLogPath;
    private string? _lastError;
    private readonly string? _legacyUninstaller;

    public SetupWindow(SetupOptions options)
    {
        _options = options;
        InitializeComponent();
        LicenseText.Text = SetupEngine.LoadLicenseText();
        VersionText.Text = $"version {SetupEngine.ProductVersion}";
        PathBox.Text = SetupOptions.DefaultInstallDir(allUsers: false);
        _pathEditedByUser = false;

        // Warn about the elevation prompt up front: an unexpected UAC window
        // at the end of an install looks unrelated and gets declined.
        if (SetupEngine.NeedsSystemWideMenu)
            MenuNote.Visibility = Visibility.Visible;

        _legacyUninstaller = SetupEngine.FindLegacyUninstaller();
        if (_legacyUninstaller != null)
        {
            ContextNote.Text = "A copy installed by the previous installer was found — it will be removed first. Your settings and history are kept.";
            ContextNote.Visibility = Visibility.Visible;
        }

        // Windows Installer does not upgrade across the per-user/per-machine
        // boundary, so an existing copy pins the mode.
        var installed = InstalledProductInspector.Detect();
        if (installed == InstalledContext.PerUser)
        {
            PerUserRadio.IsChecked = true;
            PerMachineRadio.IsEnabled = false;
            ContextNote.Text = "An existing per-user installation was found — it will be upgraded in the same mode. To change the mode, remove the current copy first.";
            ContextNote.Visibility = Visibility.Visible;
        }
        else if (installed == InstalledContext.PerMachine)
        {
            PerMachineRadio.IsChecked = true;
            PerUserRadio.IsEnabled = false;
            ContextNote.Text = "An existing all-users installation was found — it will be upgraded in the same mode. To change the mode, remove the current copy first.";
            ContextNote.Visibility = Visibility.Visible;
        }

        _phraseTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1.7) };
        _phraseTimer.Tick += (_, _) =>
        {
            _phraseIndex = (_phraseIndex + 1) % ProgressPhrases.Length;
            ProgressStatus.Text = ProgressPhrases[_phraseIndex];
        };
    }

    private void OnDragWindow(object sender, MouseButtonEventArgs e)
    {
        if (e.ButtonState == MouseButtonState.Pressed)
            DragMove();
    }

    private void OnScopeChanged(object sender, RoutedEventArgs e)
    {
        if (PathBox == null || _pathEditedByUser) return;
        var allUsers = PerMachineRadio.IsChecked == true;
        _suppressPathEvent = true;
        PathBox.Text = SetupOptions.DefaultInstallDir(allUsers);
        _suppressPathEvent = false;
    }

    private void OnPathEdited(object sender, RoutedEventArgs e)
    {
        if (!_suppressPathEvent && PathBox.IsKeyboardFocused)
            _pathEditedByUser = true;
    }

    private void OnBrowse(object sender, RoutedEventArgs e)
    {
        string? start = null;
        try
        {
            start = Path.GetDirectoryName(PathBox.Text);
        }
        catch (ArgumentException)
        {
            // a half-typed path: open the dialog at its default place
        }

        var folder = FolderPicker.Pick(this, "Nexus Optimizer install folder", start);
        if (folder != null)
        {
            _pathEditedByUser = true;
            PathBox.Text = Path.Combine(folder, "Nexus Optimizer");
        }
    }

    private async void OnInstall(object sender, RoutedEventArgs e)
    {
        if (_installing) return;
        _installing = true;

        _options.AllUsers = PerMachineRadio.IsChecked == true;
        _options.DesktopShortcut = DesktopCheck.IsChecked == true;
        _options.ContextMenu = MenuCheck.IsChecked == true;
        var defaultDir = SetupOptions.DefaultInstallDir(_options.AllUsers);

        PathError.Visibility = Visibility.Collapsed;
        var rawPath = PathBox.Text.Trim();
        if (rawPath.Length == 0)
        {
            _options.CustomDir = null;
        }
        else
        {
            string fullPath;
            try
            {
                if (!Path.IsPathRooted(rawPath))
                    throw new ArgumentException("the path is not absolute");
                fullPath = Path.GetFullPath(rawPath);
            }
            catch
            {
                PathError.Text = "Enter a full path to a folder, for example C:\\Apps\\Nexus Optimizer.";
                PathError.Visibility = Visibility.Visible;
                _installing = false;
                return;
            }
            _options.CustomDir = string.Equals(fullPath, defaultDir, StringComparison.OrdinalIgnoreCase)
                ? null
                : fullPath;
        }

        ShowPage(ProgressPage);
        HeaderText.Text = "Installing…";
        CloseButton.IsEnabled = false;
        _phraseIndex = 0;
        ProgressStatus.Text = ProgressPhrases[0];
        _phraseTimer.Start();

        try
        {
            // A copy left by the previous installer goes before the new files
            // are laid down: otherwise two versions share one folder and two
            // entries appear under Installed apps.
            if (_legacyUninstaller != null)
            {
                ProgressStatus.Text = "Removing the previous version…";
                _phraseTimer.Stop();
                await Task.Run(() => SetupEngine.RemoveLegacy(_legacyUninstaller));
                _phraseTimer.Start();
            }

            var msi = await Task.Run(SetupEngine.ExtractMsi);
            var result = await SetupEngine.InstallAsync(_options, msi);
            _lastLogPath = result.LogPath;

            if (result.ExitCode is 0 or 3010)
            {
                HeaderText.Text = "Installation complete";
                if (result.ExitCode == 3010)
                    DoneText.Text = "Nexus Optimizer is installed. A restart may be needed to finish.";

                await FinishExplorerMenuAsync();

                LaunchButton.Visibility = LaunchCheck.IsChecked == true
                    ? Visibility.Visible
                    : Visibility.Collapsed;
                ShowPage(DonePage);
            }
            else if (result.ExitCode == 1602)
            {
                HeaderText.Text = "Install";
                ShowPage(OptionsPage); // cancelled, or UAC declined: back to the options
            }
            else
            {
                // A bare Windows Installer code tells the reader nothing, so the
                // common ones are named; the rest stay as a number in the report
                // that the button copies.
                var reason = result.ExitCode switch
                {
                    1603 => "Installation failed. Close Nexus Optimizer if it is running and try again.",
                    1618 => "Another installation is already running. Wait for it to finish.",
                    1638 => "Another version of Nexus Optimizer is installed. Remove it first.",
                    _ => $"Installation failed (Windows Installer code {result.ExitCode}).",
                };
                ShowError(reason + (_lastLogPath != null ? $"\nLog: {_lastLogPath}" : ""));
            }
        }
        catch (Exception ex)
        {
            _lastError = ex.ToString();
            ShowError(ex.Message);
        }
        finally
        {
            _phraseTimer.Stop();
            CloseButton.IsEnabled = true;
            _installing = false;
        }
    }

    /// <summary>
    /// System-wide part of the Explorer menu for a per-user install on
    /// Windows 11 24H2 and later. One UAC prompt; declining is a normal
    /// outcome worth a sentence, not an error screen.
    /// </summary>
    private async Task FinishExplorerMenuAsync()
    {
        if (!_options.ContextMenu)
            return;

        ProgressStatus.Text = "Setting up the Explorer menu…";
        // The per-user part always runs: the Send to shortcut works on every
        // version of Windows, and before 24H2 the menu entry shows as well.
        await Task.Run(() => SetupEngine.TryRegisterUserMenu(_options));

        if (_options.AllUsers || !SetupEngine.NeedsSystemWideMenu)
            return;

        var ok = await Task.Run(() => SetupEngine.TryRegisterSystemWideMenu(_options));
        MenuResultText.Text = ok
            ? "The \"Compress\" menu is registered for Explorer."
            : "The Explorer menu was not registered — \"Send to → Nexus Optimizer\" works anyway, and the menu can be enabled later in Settings.";
        MenuResultText.Visibility = Visibility.Visible;
    }

    private void ShowError(string message)
    {
        HeaderText.Text = "Error";
        ErrorText.Text = message;
        ShowPage(ErrorPage);
    }

    private void ShowPage(FrameworkElement page)
    {
        OptionsPage.Visibility = Visibility.Collapsed;
        ProgressPage.Visibility = Visibility.Collapsed;
        DonePage.Visibility = Visibility.Collapsed;
        ErrorPage.Visibility = Visibility.Collapsed;
        page.Visibility = Visibility.Visible;
    }

    private void OnRetry(object sender, RoutedEventArgs e)
    {
        HeaderText.Text = "Install";
        ShowPage(OptionsPage);
    }

    private void OnCopyReport(object sender, RoutedEventArgs e)
    {
        try
        {
            var report = ErrorText.Text;
            if (_lastError != null) report += Environment.NewLine + _lastError;
            if (_lastLogPath != null && File.Exists(_lastLogPath))
                report += Environment.NewLine + "--- msiexec log (tail) ---" + Environment.NewLine +
                          string.Join(Environment.NewLine, Tail(_lastLogPath, 80));
            Clipboard.SetText(report);
        }
        catch
        {
            // the clipboard is busy: not important
        }
    }

    /// <summary>The last lines of a text file (Enumerable.TakeLast is not on .NET Framework).</summary>
    private static IEnumerable<string> Tail(string path, int count)
    {
        var lines = new Queue<string>(count);
        foreach (var line in File.ReadLines(path))
        {
            if (lines.Count == count)
                lines.Dequeue();
            lines.Enqueue(line);
        }
        return lines;
    }

    private void OnLaunch(object sender, RoutedEventArgs e)
    {
        SetupEngine.LaunchInstalledApp(_options);
        Close();
    }

    private void OnClose(object sender, RoutedEventArgs e) => Close();
}
