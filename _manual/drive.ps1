param(
    [string]$Out = "mr_shot.png",
    [int]$ClickX = 0, [int]$ClickY = 0,     # 相对窗口客户区的点击点（0 表示不点）
    [int]$Wheel = 0,                        # 滚轮格数，正 = 向下
    [string]$Keys = ""                      # SendKeys 串，如 "{PGDN}"
)

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$sig = @"
using System;
using System.Runtime.InteropServices;
public class W4 {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, int data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$proc = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq "MiniReader" } | Select-Object -First 1
if (-not $proc) { Write-Output "NO WINDOW"; exit 1 }
$h = $proc.MainWindowHandle

[W4]::SetWindowPos($h, [IntPtr](-1), 60, 60, 0, 0, 0x43) | Out-Null
[W4]::ShowWindow($h, 9) | Out-Null
[W4]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 900

$r = New-Object W4+RECT
[W4]::GetWindowRect($h, [ref]$r) | Out-Null

# 先点一下阅读区把焦点给进去（Qt 里滚轮/按键都发给有焦点的控件）
if ($ClickX -gt 0) {
    [W4]::SetCursorPos($r.Left + $ClickX, $r.Top + $ClickY) | Out-Null
    Start-Sleep -Milliseconds 200
    # LEFTDOWN 0x02 | LEFTUP 0x04
    [W4]::mouse_event(0x02, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 60
    [W4]::mouse_event(0x04, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 250
}

if ($Wheel -ne 0) {
    # MOUSEEVENTF_WHEEL 0x0800；data 正 = 向上，120 一格
    $data = -120 * $Wheel
    for ($i = 0; $i -lt [Math]::Abs($Wheel); $i++) {
        [W4]::mouse_event(0x0800, 0, 0, $(if ($data -gt 0) { 120 } else { -120 }), [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 180
    }
    Start-Sleep -Milliseconds 500
}

if ($Keys -ne "") {
    [System.Windows.Forms.SendKeys]::SendWait($Keys)
    Start-Sleep -Milliseconds 700
}

$r2 = New-Object W4+RECT
[W4]::GetWindowRect($h, [ref]$r2) | Out-Null
$ww = $r2.Right - $r2.Left
$hh = $r2.Bottom - $r2.Top

$bmp = New-Object System.Drawing.Bitmap $ww, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[W4]::PrintWindow($h, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc)
$g.Dispose()
$out = Join-Path $env:TEMP $Out
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output "saved = $out (${ww}x${hh})"
