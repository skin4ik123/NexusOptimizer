// Records and init-only properties need this marker type. .NET Framework does
// not have it; the compiler accepts a private copy.
namespace System.Runtime.CompilerServices
{
    internal static class IsExternalInit
    {
    }
}
