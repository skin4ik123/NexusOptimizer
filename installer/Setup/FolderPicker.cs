using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace NexusOptimizer.Setup;

/// <summary>
/// The Explorer-style folder picker (IFileOpenDialog with FOS_PICKFOLDERS).
/// WPF on .NET Framework has no OpenFolderDialog, and the WinForms
/// FolderBrowserDialog there is the old tree view that nobody can navigate.
/// </summary>
internal static class FolderPicker
{
    private const int ErrorCancelled = unchecked((int)0x800704C7);

    /// <summary>The chosen folder, or null when the dialog was cancelled.</summary>
    public static string? Pick(Window owner, string title, string? initialDir)
    {
        var dialog = (IFileOpenDialog)new FileOpenDialogCoClass();
        try
        {
            dialog.GetOptions(out var options);
            dialog.SetOptions(options | Fos.PickFolders | Fos.ForceFileSystem |
                              Fos.NoChangeDir | Fos.PathMustExist);
            dialog.SetTitle(title);

            if (!string.IsNullOrEmpty(initialDir) && Directory.Exists(initialDir))
            {
                var iid = typeof(IShellItem).GUID;
                if (SHCreateItemFromParsingName(initialDir!, IntPtr.Zero, ref iid, out var start) == 0)
                {
                    dialog.SetFolder(start);
                    Marshal.ReleaseComObject(start);
                }
            }

            var hr = dialog.Show(new WindowInteropHelper(owner).Handle);
            if (hr == ErrorCancelled || hr != 0)
                return null;

            dialog.GetResult(out var item);
            try
            {
                item.GetDisplayName(Sigdn.FileSysPath, out var pathPtr);
                try
                {
                    return Marshal.PtrToStringUni(pathPtr);
                }
                finally
                {
                    Marshal.FreeCoTaskMem(pathPtr);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(item);
            }
        }
        finally
        {
            Marshal.ReleaseComObject(dialog);
        }
    }

    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
    private class FileOpenDialogCoClass
    {
    }

    // Vtable order matters: IModalWindow, then IFileDialog, then IFileOpenDialog.
    [ComImport, Guid("d57c7288-d4ad-4768-be02-9d969532d960"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileOpenDialog
    {
        [PreserveSig] int Show(IntPtr parent);
        void SetFileTypes(uint count, IntPtr filterSpec);
        void SetFileTypeIndex(uint index);
        void GetFileTypeIndex(out uint index);
        void Advise(IntPtr events, out uint cookie);
        void Unadvise(uint cookie);
        void SetOptions(Fos options);
        void GetOptions(out Fos options);
        void SetDefaultFolder(IShellItem item);
        void SetFolder(IShellItem item);
        void GetFolder(out IShellItem item);
        void GetCurrentSelection(out IShellItem item);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
        void GetResult(out IShellItem item);
        void AddPlace(IShellItem item, int place);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr filter);
        void GetResults(out IntPtr items);
        void GetSelectedItems(out IntPtr items);
    }

    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr bindContext, ref Guid handler, ref Guid iid, out IntPtr result);
        void GetParent(out IShellItem parent);
        void GetDisplayName(Sigdn form, out IntPtr name);
        void GetAttributes(uint mask, out uint attributes);
        void Compare(IShellItem other, uint hint, out int order);
    }

    private enum Sigdn : uint
    {
        FileSysPath = 0x80058000,
    }

    [Flags]
    private enum Fos : uint
    {
        NoChangeDir = 0x00000008,
        PickFolders = 0x00000020,
        ForceFileSystem = 0x00000040,
        PathMustExist = 0x00000800,
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
    private static extern int SHCreateItemFromParsingName(
        string path, IntPtr bindContext, ref Guid iid, out IShellItem item);
}
