param(
    [string]$Out = "mr_shot.png",
    [int]$X = 60, [int]$Y = 60,
    [string]$Key = "",          # VK 名，如 PGDN / RIGHT / HOME / END
    [int]$KeyCount = 1,
    [int]$Wheel = 0,            # 滚轮格数，正 = 向下
    [int]$ClickX = 0, [int]$ClickY = 0
)

Add-Type -AssemblyName System.Drawing

$sig = @"
using System;
using System.Runtime.InteropServices;
public class W5 {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern IntPtr GetFocus();
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$proc = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq "MiniReader" } | Select-Object -First 1
if (-not $proc) { Write-Output "NO WINDOW"; exit 1 }
$h = $proc.MainWindowHandle

# 真正挪到 (X,Y) 并置顶：0x40 SHOWWINDOW | 0x01 NOSIZE（**不带** NOMOVE）
[W5]::SetWindowPos($h, [IntPtr](-1), $X, $Y, 0, 0, 0x41) | Out-Null
[W5]::ShowWindow($h, 9) | Out-Null
[W5]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 900

$r = New-Object W5+RECT
[W5]::GetWindowRect($h, [ref]$r) | Out-Null
Write-Output "rect = $($r.Left),$($r.Top)"

# 把输入线程挂到目标窗口线程上，SetFocus 才能真正生效（跨线程 SetFocus 会被忽略）
$targetThread = [W5]::GetWindowThreadProcessId($h, [IntPtr]::Zero)
$myThread = [W5]::GetCurrentThreadId()
[W5]::AttachThreadInput($myThread, $targetThread, $true) | Out-Null
[W5]::SetFocus($h) | Out-Null
Start-Sleep -Milliseconds 200

# 用 PostMessage 发输入：不依赖前台焦点，游戏抢不走
$VK = @{ PGDN = 0x22; PGUP = 0x21; RIGHT = 0x27; LEFT = 0x25; HOME = 0x24; END = 0x23; DOWN = 0x28; UP = 0x26; SPACE = 0x20 }
# WM_KEYDOWN 0x0100 / WM_KEYUP 0x0101 / WM_MOUSEWHEEL 0x020A / WM_LBUTTONDOWN 0x0201 / WM_LBUTTONUP 0x0202

if ($ClickX -gt 0) {
    $lp = [IntPtr](($ClickY -shl 16) -bor ($ClickX -band 0xFFFF))
    [W5]::PostMessage($h, 0x0201, [IntPtr]1, $lp) | Out-Null
    Start-Sleep -Milliseconds 80
    [W5]::PostMessage($h, 0x0202, [IntPtr]0, $lp) | Out-Null
    Start-Sleep -Milliseconds 300
}

if ($Wheel -ne 0) {
    $delta = if ($Wheel -gt 0) { -120 } else { 120 }
    $wp = [IntPtr](([int]$delta -shl 16) -bor 0)
    for ($i = 0; $i -lt [Math]::Abs($Wheel); $i++) {
        [W5]::PostMessage($h, 0x020A, $wp, [IntPtr]0) | Out-Null
        Start-Sleep -Milliseconds 200
    }
    Start-Sleep -Milliseconds 400
}

if ($Key -ne "") {
    $code = $VK[$Key.ToUpper()]
    if (-not $code) { Write-Output "UNKNOWN KEY $Key"; exit 1 }
    for ($i = 0; $i -lt $KeyCount; $i++) {
        [W5]::PostMessage($h, 0x0100, [IntPtr]$code, [IntPtr]0) | Out-Null
        Start-Sleep -Milliseconds 70
        [W5]::PostMessage($h, 0x0101, [IntPtr]$code, [IntPtr]0) | Out-Null
        Start-Sleep -Milliseconds 250
    }
    Start-Sleep -Milliseconds 500
}

[W5]::AttachThreadInput($myThread, $targetThread, $false) | Out-Null

$r2 = New-Object W5+RECT
[W5]::GetWindowRect($h, [ref]$r2) | Out-Null
$ww = $r2.Right - $r2.Left
$hh = $r2.Bottom - $r2.Top

$bmp = New-Object System.Drawing.Bitmap $ww, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[W5]::PrintWindow($h, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc)
$g.Dispose()
$out = Join-Path $env:TEMP $Out
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output "saved = $out (${ww}x${hh})"
