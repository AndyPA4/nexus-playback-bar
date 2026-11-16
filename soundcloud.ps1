$soundCloudControls = $null
$soundCloudTime = $null
$soundCloudNextRead = [DateTime]::MinValue

function Read-SoundCloudTime([string]$label) {
    if ($label -notmatch '^(?:\d{1,3}:)?\d{1,3}:[0-5]\d$') { return $null }
    $seconds = 0
    foreach ($part in $label.Split(':')) { $seconds = $seconds * 60 + [int]$part }
    return $seconds
}

function Find-SoundCloudControls([string]$title, [string]$artist) {
    Add-Type -AssemblyName UIAutomationClient
    $root = [Windows.Automation.AutomationElement]::RootElement
    $window = $root.FindAll('Children', [Windows.Automation.Condition]::TrueCondition) |
        Where-Object {
            $_.Current.ClassName -eq 'ApplicationFrameWindow' -and
            $_.Current.Name -match '^SoundCloud\s*-\s*Music'
        } | Select-Object -First 1
    $coreCondition = [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::ClassNameProperty, 'Windows.UI.Core.CoreWindow')
    $core = if ($window) { $window.FindFirst('Descendants', $coreCondition) }
    if (!$core) {
        $process = Get-Process soundcouch -ErrorAction SilentlyContinue | Select-Object -First 1
        if (!$process) { return }
        if (!('NpbSoundCloudWindow' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class NpbSoundCloudWindow {
    delegate bool WindowCallback(IntPtr window, IntPtr state);
    [DllImport("user32.dll")] static extern bool EnumWindows(WindowCallback callback, IntPtr state);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr window, WindowCallback callback, IntPtr state);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr window, StringBuilder name, int size);

    public static IntPtr Find(uint process) {
        IntPtr result = IntPtr.Zero;
        var name = new StringBuilder(128);
        WindowCallback check = (window, state) => {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            if (owner != process) return true;
            GetClassName(window, name, name.Capacity);
            if (name.ToString() != "Windows.UI.Core.CoreWindow") return true;
            result = window;
            return false;
        };
        EnumWindows((window, state) => {
            if (!check(window, state)) return false;
            EnumChildWindows(window, check, state);
            return result == IntPtr.Zero;
        }, IntPtr.Zero);
        return result;
    }
}
'@
        }
        $handle = [NpbSoundCloudWindow]::Find($process.Id)
        if ($handle -eq [IntPtr]::Zero) { return }
        $core = [Windows.Automation.AutomationElement]::FromHandle($handle)
    }
    if (!$core -or (Get-Process -Id $core.Current.ProcessId -ErrorAction SilentlyContinue).ProcessName -ne 'soundcouch') {
        return
    }
    $texts = [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::Text)
    $labels = @($core.FindAll('Descendants', $texts))
    $track = $labels | Where-Object { $_.Current.Name.Trim() -eq $title } | Select-Object -First 1
    $credit = $labels | Where-Object { $_.Current.Name.Trim() -eq $artist } | Select-Object -First 1
    if (!$track -or !$credit) { return }
    $timers = @($labels | Where-Object { $null -ne (Read-SoundCloudTime $_.Current.Name) })
    if ($timers.Count -ne 2) { return }
    $left = $timers[0].Current.BoundingRectangle
    $right = $timers[1].Current.BoundingRectangle
    if (!$left.IsEmpty -and !$right.IsEmpty) {
        if ([Math]::Abs($left.Y - $right.Y) -gt 8 -or $left.X -eq $right.X) { return }
        if ($left.X -gt $right.X) { [array]::Reverse($timers) }
    }
    @{ track = $track; credit = $credit; elapsed = $timers[0]; duration = $timers[1] }
}

function Get-SoundCloudTimeline([string]$title, [string]$artist) {
    if (!$title -or !$artist) { return }
    if ([DateTime]::UtcNow -lt $soundCloudNextRead -and $soundCloudTime -and
        $soundCloudTime.title -eq $title -and $soundCloudTime.artist -eq $artist) {
        return $soundCloudTime
    }
    if ([DateTime]::UtcNow -lt $soundCloudNextRead) { return }
    $script:soundCloudNextRead = [DateTime]::UtcNow.AddMilliseconds(700)
    $script:soundCloudTime = $null
    try {
        if (!$soundCloudControls -or $soundCloudControls.track.Current.Name.Trim() -ne $title -or
            $soundCloudControls.credit.Current.Name.Trim() -ne $artist) {
            $script:soundCloudControls = Find-SoundCloudControls $title $artist
        }
        if (!$soundCloudControls) { return }
        $duration = Read-SoundCloudTime $soundCloudControls.duration.Current.Name
        $elapsed = Read-SoundCloudTime $soundCloudControls.elapsed.Current.Name
        if (!$duration -or $null -eq $elapsed -or $elapsed -gt $duration) { return }
        $script:soundCloudTime = @{title = $title; artist = $artist; position = $elapsed; duration = $duration}
        return $soundCloudTime
    } catch {
        $script:soundCloudControls = $null
    }
}
