param([string]$Out = "mr_shot.png", [int]$X = 40, [int]$Y = 40, [int]$W = 0, [int]$H = 0)

Add-Type -AssemblyName System.Drawing

$sig = @"
using System;
using System.Runtime.InteropServices;
public class W3 {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
}
"@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$proc = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq "MiniReader" } | Select-Object -First 1
if (-not $proc) { Write-Output "NO WINDOW"; exit 1 }
$h = $proc.MainWindowHandle
Write-Output "hwnd = $h"

# HWND_TOPMOST = -1; SWP_SHOWWINDOW 0x40 | SWP_NOSIZE 0x01 | SWP_NOMOVE 0x02
if ($W -gt 0) {
    [W3]::SetWindowPos($h, [IntPtr](-1), $X, $Y, $W, $H, 0x40) | Out-Null
} else {
    [W3]::SetWindowPos($h, [IntPtr](-1), $X, $Y, 0, 0, 0x43) | Out-Null
}
[W3]::ShowWindow($h, 9) | Out-Null
[W3]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 1500

$r = New-Object W3+RECT
[W3]::GetWindowRect($h, [ref]$r) | Out-Null
$ww = $r.Right - $r.Left
$hh = $r.Bottom - $r.Top
Write-Output "rect = $($r.Left),$($r.Top) ${ww}x${hh}"
if ($ww -le 0 -or $hh -le 0) { Write-Output "BAD RECT"; exit 1 }

$bmp = New-Object System.Drawing.Bitmap $ww, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
# flag 2 = PW_RENDERFULLCONTENT（Qt 窗口需要它，否则常拿到空白）
[W3]::PrintWindow($h, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc)
$g.Dispose()

$out = Join-Path $env:TEMP $Out
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output "saved = $out"
