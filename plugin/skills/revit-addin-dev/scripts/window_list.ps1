# Lists the visible top-level windows of one process (class and title), e.g. to find the
# modal dialog a stuck Revit is waiting on. Read-only.
# Usage: powershell -NoProfile -File window_list.ps1 -ProcessId <pid>
param([Parameter(Mandatory = $true)] [int] $ProcessId)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class RevitWindows {
    delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hWnd, StringBuilder s, int n);
    public static List<string> Of(uint target) {
        var list = new List<string>();
        EnumWindows((h, l) => {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (pid == target && IsWindowVisible(h)) {
                var t = new StringBuilder(512); GetWindowText(h, t, 512);
                var c = new StringBuilder(256); GetClassName(h, c, 256);
                list.Add("[" + c + "] " + t);
            }
            return true;
        }, IntPtr.Zero);
        return list;
    }
}
'@
$windows = [RevitWindows]::Of([uint32]$ProcessId)
if ($windows.Count -eq 0) { Write-Output "no visible window for pid $ProcessId" } else { $windows }
