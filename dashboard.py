"""Windows utilization counters and idle screen for the NEXUS."""
import ctypes as c
from ctypes import wintypes as w
from collections import defaultdict, deque
from datetime import datetime
from functools import lru_cache
import base64
import subprocess
import math
import struct
import time
from bisect import bisect_right
import os
import shutil

from nexus import WIDTH, HEIGHT, TEXT_SIZES, kernel, raster_text

DASHBOARD_WIDGETS = {
    "CPU usage": "CPU", "GPU usage": "GPU", "RAM usage": "RAM",
    "GPU temperature": "GPU TEMP", "VRAM usage": "VRAM", "RAM used": "RAM USED",
    "CPU frequency": "CPU CLOCK", "GPU frequency": "GPU CLOCK", "GPU fan": "GPU FAN",
    "Disk activity": "DISK", "Disk free": "FREE SPACE", "Battery": "BATTERY", "Uptime": "UPTIME",
    "Time": "TIME", "Date": "DATE", "Clock and date": "CLOCK",
    "CPU usage + frequency": "CPU", "GPU usage + frequency": "GPU",
    "RAM usage + capacity": "RAM", "VRAM used / free": "VRAM",
    "Upload + download": "NETWORK", "Ethernet / Wi-Fi": "CONNECTION",
    "Network usage": "NETWORK", "Network usage + speeds": "NETWORK",
}
IMAGE_WIDGET = "Picture / GIF"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".ico")
DASHBOARD_WIDGETS[IMAGE_WIDGET] = "IMAGE"

APP_SHORTCUTS = {
    "Calculator": "calc.exe", "File Explorer": "explorer.exe",
    "Windows Settings": "ms-settings:", "Task Manager": "Taskmgr.exe",
}
DASHBOARD_WIDGETS.update({name: "OPEN" for name in (*APP_SHORTCUTS, "Custom app")})
WIDGET_GROUPS = {
    "CPU": ("CPU usage", "CPU usage + frequency", "CPU frequency"),
    "GPU": ("GPU usage", "GPU usage + frequency", "GPU frequency", "GPU temperature", "GPU fan"),
    "RAM": ("RAM usage", "RAM usage + capacity", "RAM used"),
    "VRAM": ("VRAM usage", "VRAM used / free"),
    "Storage": ("Disk activity", "Disk free"),
    "Network": ("Network usage", "Network usage + speeds", "Upload + download", "Ethernet / Wi-Fi"),
    "Power & system": ("Battery", "Uptime"),
    "Clock": ("Time", "Date", "Clock and date"),
    "Images": (IMAGE_WIDGET,),
    "App shortcuts": (*APP_SHORTCUTS, "Custom app"),
}
WIDGET_COLORS = {
    "Red": (255, 100, 117), "Orange": (255, 160, 83), "Yellow": (249, 215, 96),
    "Green": (83, 225, 161), "Cyan": (75, 222, 234), "Blue": (74, 189, 255),
    "Indigo": (135, 145, 255), "Violet": (202, 156, 255), "Pink": (255, 140, 201),
}
DEFAULT_WIDGET_LAYOUT = tuple((name, 3, "Auto", "Bar") for name in ("CPU usage", "GPU usage", "RAM usage", "Clock and date"))
PERCENT_WIDGETS = {"CPU usage", "GPU usage", "RAM usage", "VRAM usage", "Disk activity", "Battery", "GPU fan"}
PERCENT_WIDGETS.add("Network usage")
GRAPH_SOURCES = {name: name for name in PERCENT_WIDGETS}
GRAPH_SOURCES.update({
    "CPU usage + frequency": "CPU usage", "GPU usage + frequency": "GPU usage",
    "RAM usage + capacity": "RAM usage", "VRAM used / free": "VRAM usage",
    "Network usage + speeds": "Network usage", "Ethernet / Wi-Fi": "Network usage",
})
COUNTER_PATHS = {
    "gpu": r"\GPU Engine(*)\Utilization Percentage",
    "disk": r"\PhysicalDisk(_Total)\% Idle Time",
    "cpu_frequency": r"\Processor Information(_Total)\Processor Frequency",
}


class MemoryStatus(c.Structure):
    _fields_ = [("length", w.DWORD), ("load", w.DWORD)] + [
        (name, c.c_ulonglong)
        for name in ("total", "available", "page_total", "page_available",
                     "virtual_total", "virtual_available", "extended_available")
    ]


class CounterValue(c.Structure):
    _fields_ = [("status", w.DWORD), ("value", c.c_double)]


class CounterItem(c.Structure):
    _fields_ = [("name", w.LPWSTR), ("counter", CounterValue)]


class NvidiaUtilization(c.Structure):
    _fields_ = [("gpu", c.c_uint), ("memory", c.c_uint)]


class NvidiaMemory(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in ("total", "free", "used")]


class PowerStatus(c.Structure):
    _fields_ = [(name, c.c_ubyte) for name in ("ac", "flags", "battery", "status")] + [
        ("remaining", w.DWORD), ("full", w.DWORD)
    ]


class InterfaceRow(c.Structure):
    _fields_ = [("name", w.WCHAR * 256)] + [
        (field, w.DWORD) for field in ("index", "kind", "mtu", "speed", "address_length")
    ] + [("address", c.c_ubyte * 8)] + [
        (field, w.DWORD) for field in (
            "admin", "operational", "last_change", "received", "in_unicast", "in_non_unicast",
            "in_discards", "in_errors", "in_unknown", "sent", "out_unicast", "out_non_unicast",
            "out_discards", "out_errors", "out_queue", "description_length",
        )
    ] + [("description", c.c_ubyte * 256)]


class ProcessorPower(c.Structure):
    _fields_ = [(field, w.ULONG) for field in ("number", "max", "current", "limit", "max_idle", "idle")]


class Dashboard:
    def __init__(self):
        kernel.GetSystemTimes.argtypes = [c.POINTER(w.FILETIME)] * 3
        kernel.GetSystemTimes.restype = w.BOOL
        kernel.GlobalMemoryStatusEx.argtypes = [c.POINTER(MemoryStatus)]
        kernel.GlobalMemoryStatusEx.restype = w.BOOL
        self.cpu_times = None
        self.history = {name: deque(maxlen=42) for name in PERCENT_WIDGETS}
        self.query = w.HANDLE()
        self.counters = {}
        self.nvidia = None
        self.nvidia_checked = False
        self.nvidia_devices = []
        self.interfaces = c.WinDLL("iphlpapi")
        self.interfaces.GetIfTable.argtypes = [c.c_void_p, c.POINTER(w.ULONG), w.BOOL]
        self.network_previous = None
        self.power = c.WinDLL("powrprof")
        self.power.CallNtPowerInformation.argtypes = [c.c_int, c.c_void_p, w.ULONG, c.c_void_p, w.ULONG]
        self.pdh = c.WinDLL("pdh")
        self.pdh.PdhOpenQueryW.argtypes = [w.LPCWSTR, c.c_size_t, c.POINTER(w.HANDLE)]
        self.pdh.PdhAddEnglishCounterW.argtypes = [w.HANDLE, w.LPCWSTR, c.c_size_t, c.POINTER(w.HANDLE)]
        self.pdh.PdhCollectQueryData.argtypes = [w.HANDLE]
        self.pdh.PdhGetFormattedCounterArrayW.argtypes = [
            w.HANDLE, w.DWORD, c.POINTER(w.DWORD), c.POINTER(w.DWORD), c.c_void_p,
        ]
        self.pdh.PdhCloseQuery.argtypes = [w.HANDLE]
        self.pdh.PdhGetFormattedCounterValue.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, c.POINTER(CounterValue)]
        kernel.GetTickCount64.argtypes = []
        kernel.GetTickCount64.restype = c.c_ulonglong
        kernel.GetSystemPowerStatus.argtypes = [c.POINTER(PowerStatus)]
        kernel.GetSystemPowerStatus.restype = w.BOOL
        if self.pdh.PdhOpenQueryW(None, 0, c.byref(self.query)) == 0:
            for name, path in COUNTER_PATHS.items():
                counter = w.HANDLE()
                if self.pdh.PdhAddEnglishCounterW(self.query, path, 0, c.byref(counter)) == 0:
                    self.counters[name] = counter

    def close(self):
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
            self.query = w.HANDLE()
            self.counters.clear()
        if self.nvidia:
            self.nvidia.nvmlShutdown()
            self.nvidia = None
            self.nvidia_devices.clear()

    def cpu_percent(self):
        times = [w.FILETIME() for _ in range(3)]
        if not kernel.GetSystemTimes(*(c.byref(value) for value in times)):
            return None
        idle, system, user = (value.dwHighDateTime << 32 | value.dwLowDateTime for value in times)
        previous = self.cpu_times
        self.cpu_times = idle, system + user
        if previous and self.cpu_times[1] > previous[1]:
            return round(100 * (1 - (idle - previous[0]) / (self.cpu_times[1] - previous[1])))
        return None

    def counter_items(self, name):
        counter = self.counters.get(name)
        if not counter:
            return []
        size, count = w.DWORD(), w.DWORD()
        args = (counter, 0x200 | 0x8000, c.byref(size), c.byref(count))
        if self.pdh.PdhGetFormattedCounterArrayW(*args, None) & 0xffffffff != 0x800007d2:
            return []
        if not 0 < size.value <= 8_000_000:
            return []
        buffer = c.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(*args, buffer) != 0:
            return []
        if count.value * c.sizeof(CounterItem) > size.value:
            return []
        return [(item.name, max(0, item.counter.value))
                for item in (CounterItem * count.value).from_buffer(buffer)
                if item.counter.status in (0, 1) and item.name and math.isfinite(item.counter.value)]

    def gpu_percent(self):
        engines = defaultdict(float)
        for name, value in self.counter_items("gpu"):
            engine = name.partition("_luid_")[2] or name
            engines[engine] += value
        return min(100, round(max(engines.values()))) if engines else None

    def nvidia_stats(self, enabled):
        if not self.nvidia_checked:
            self.nvidia_checked = True
            try:
                api = c.CDLL("nvml.dll", winmode=0x800)
                api.nvmlDeviceGetUtilizationRates.argtypes = [c.c_void_p, c.POINTER(NvidiaUtilization)]
                api.nvmlDeviceGetHandleByIndex_v2.argtypes = [c.c_uint, c.POINTER(c.c_void_p)]
                api.nvmlDeviceGetCount_v2.argtypes = [c.POINTER(c.c_uint)]
                api.nvmlDeviceGetTemperature.argtypes = [c.c_void_p, c.c_uint, c.POINTER(c.c_uint)]
                api.nvmlDeviceGetMemoryInfo.argtypes = [c.c_void_p, c.POINTER(NvidiaMemory)]
                api.nvmlDeviceGetClockInfo.argtypes = [c.c_void_p, c.c_uint, c.POINTER(c.c_uint)]
                api.nvmlDeviceGetFanSpeed.argtypes = [c.c_void_p, c.POINTER(c.c_uint)]
                if api.nvmlInit_v2() == 0:
                    self.nvidia = api
                    count = c.c_uint()
                    if api.nvmlDeviceGetCount_v2(c.byref(count)) == 0:
                        for index in range(count.value):
                            device = c.c_void_p()
                            if api.nvmlDeviceGetHandleByIndex_v2(index, c.byref(device)) == 0:
                                self.nvidia_devices.append(device)
            except (AttributeError, OSError):
                pass
        levels = []
        temperatures, memory_usage, clocks, fans, capacities = [], [], [], [], []
        for device in self.nvidia_devices:
            value = NvidiaUtilization()
            if self.nvidia.nvmlDeviceGetUtilizationRates(device, c.byref(value)) == 0:
                levels.append(value.gpu)
            if "GPU temperature" in enabled:
                temp = c.c_uint()
                if self.nvidia.nvmlDeviceGetTemperature(device, 0, c.byref(temp)) == 0:
                    temperatures.append(temp.value)
            if enabled & {"VRAM usage", "VRAM used / free"}:
                memory = NvidiaMemory()
                if self.nvidia.nvmlDeviceGetMemoryInfo(device, c.byref(memory)) == 0 and memory.total:
                    memory_usage.append(round(100 * memory.used / memory.total))
                    capacities.append((memory.used, memory.free))
            if "GPU frequency" in enabled:
                clock = c.c_uint()
                if self.nvidia.nvmlDeviceGetClockInfo(device, 0, c.byref(clock)) == 0:
                    clocks.append(clock.value)
            if "GPU fan" in enabled:
                fan = c.c_uint()
                if self.nvidia.nvmlDeviceGetFanSpeed(device, c.byref(fan)) == 0:
                    fans.append(min(100, fan.value))
        return tuple(max(values, default=None) for values in (levels, temperatures, memory_usage, clocks, fans, capacities))

    def network_stats(self):
        size = w.ULONG()
        if self.interfaces.GetIfTable(None, c.byref(size), False) != 122 or not 4 <= size.value < 4_000_000:
            return None, None, "--", None
        buffer = c.create_string_buffer(size.value)
        if self.interfaces.GetIfTable(buffer, c.byref(size), False):
            return None, None, "--", None
        count = w.DWORD.from_buffer(buffer).value
        if 4 + count * c.sizeof(InterfaceRow) > len(buffer):
            return None, None, "--", None
        rows = (InterfaceRow * count).from_buffer(buffer, 4)
        active = {row.index: (row.received, row.sent, row.speed) for row in rows if row.operational == 5 and row.kind in (6, 71)}
        links = {row.kind for row in rows if row.index in active}
        label = "Ethernet + Wi-Fi" if links == {6, 71} else "Ethernet" if 6 in links else "Wi-Fi" if links else "Offline"
        now = kernel.GetTickCount64() / 1000
        previous, self.network_previous = self.network_previous, (now, active)
        if not previous or now <= previous[0]:
            return None, None, label, None
        elapsed = now - previous[0]
        down = up = capacity = 0
        for index in active.keys() & previous[1].keys():
            received, sent, speed = active[index]
            before_down, before_up, before_speed = previous[1][index]
            if speed != before_speed:
                continue
            down += (received - before_down) % 2**32 / elapsed
            up += (sent - before_up) % 2**32 / elapsed
            capacity += speed
        usage = min(100, round(800 * max(down, up) / capacity)) if capacity else None
        return down, up, label, usage


    def counter_value(self, name):
        if name not in self.counters:
            return None
        value = CounterValue()
        if (self.pdh.PdhGetFormattedCounterValue(self.counters[name], 0x8200, None, c.byref(value)) == 0
                and value.status in (0, 1) and math.isfinite(value.value)):
            return value.value
        return None

    def sample(self, widgets):
        enabled = {name for name, *_ in widgets}
        memory = MemoryStatus(length=c.sizeof(MemoryStatus))
        ram = memory.load if kernel.GlobalMemoryStatusEx(c.byref(memory)) else None
        counters_ready = bool(self.counters) and self.pdh.PdhCollectQueryData(self.query) == 0
        gpu = self.gpu_percent() if counters_ready else None
        temp = vram = clock = fan = None
        capacity = None
        if (enabled & {"GPU usage", "GPU usage + frequency"} and gpu is None) or enabled & {
            "GPU temperature", "VRAM usage", "VRAM used / free", "GPU frequency", "GPU usage + frequency", "GPU fan"
        }:
            nvidia, temp, vram, clock, fan, capacity = self.nvidia_stats(
                enabled | ({"GPU frequency"} if "GPU usage + frequency" in enabled else set()))
            if gpu is None:
                gpu = nvidia
        values = {
            "CPU usage": self.cpu_percent(), "GPU usage": gpu, "RAM usage": ram,
            "GPU temperature": None if temp is None else f"{temp}°C", "VRAM usage": vram,
            "GPU frequency": None if clock is None else f"{clock} MHz", "GPU fan": fan,
            "RAM used": None if ram is None else f"{(memory.total - memory.available) / 2**30:.1f} GB",
        }
        if capacity:
            values["VRAM used / free"] = tuple(
                f"{label} {amount / 2**30:.1f} GB" for label, amount in zip(("Used", "Free"), capacity))
        if ram is not None:
            values["RAM usage + capacity"] = (
                f"{ram}%", f"{(memory.total - memory.available) / 2**30:.1f}/{memory.total / 2**30:.1f} GB"
            )
        if "Disk free" in enabled:
            try:
                free = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free
                values["Disk free"] = f"{free / 2**30:.1f} GB"
            except OSError:
                pass
        if counters_ready and "Disk activity" in enabled:
            value = self.counter_value("disk")
            if value is not None:
                values["Disk activity"] = min(100, max(0, round(100 - value)))
        if enabled & {"CPU frequency", "CPU usage + frequency"}:
            value = self.counter_value("cpu_frequency") if counters_ready else None
            if not value:
                processors = (ProcessorPower * (os.cpu_count() or 1))()
                if self.power.CallNtPowerInformation(11, None, 0, processors, c.sizeof(processors)) == 0:
                    readings = [entry.current for entry in processors if entry.current]
                    value = sum(readings) / len(readings) if readings else None
            if value:
                values["CPU frequency"] = f"{value / 1000:.2f} GHz"
        for device in ("CPU", "GPU"):
            utilization = values[f"{device} usage"]
            if utilization is not None:
                values[f"{device} usage + frequency"] = (f"{utilization}%", values.get(f"{device} frequency") or "--")
        if enabled & {"Upload + download", "Ethernet / Wi-Fi", "Network usage", "Network usage + speeds"}:
            download, upload, connection, usage = self.network_stats()
            values["Network usage"] = usage
            values["Ethernet / Wi-Fi"] = connection if usage is None else f"{connection} {usage}%"
            values["Network usage + speeds"] = (f"{usage}%" if usage is not None else "--", "D --", "U --")
            if download is not None and upload is not None:
                def rate_text(rate):
                    return f"{rate / 2**20:.1f} MB/s" if rate >= 2**20 else f"{rate / 1024:.0f} KB/s"
                values["Upload + download"] = (f"D {rate_text(download)}", f"U {rate_text(upload)}")
                values["Network usage + speeds"] = (values["Network usage + speeds"][0], *values["Upload + download"])
        if "Battery" in enabled:
            power = PowerStatus()
            if kernel.GetSystemPowerStatus(c.byref(power)) and power.battery <= 100:
                values["Battery"] = power.battery
        if "Uptime" in enabled:
            hours, minutes = divmod(kernel.GetTickCount64() // 60000, 60)
            values["Uptime"] = f"{hours}h {minutes:02}m"
        for name, history in self.history.items():
            history.append(values.get(name))
        return values, {name: tuple(history) for name, history in self.history.items()}


def widget_positions(widgets):
    widgets = tuple(widget for widget in widgets if widget[0] in DASHBOARD_WIDGETS)
    extra = WIDTH - 4 - 73 * len(widgets)
    weight = sum(size for _, size, *_ in widgets)
    x, cumulative = 4, 0
    for name, size, preset, graph, *app_path in widgets:
        share = round(extra * (cumulative + size) / weight) - round(extra * cumulative / weight)
        cumulative += size
        card_width = 69 + share
        yield name, preset, x, card_width, graph, (app_path[0] if app_path else "")
        x += card_width + 4


@lru_cache(maxsize=32)
def app_icon(path):
    script = r"""
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NpbShellIcon {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern uint ExtractIconEx(string path, int index, out IntPtr large, IntPtr small, uint count);
    [DllImport("user32.dll")]
    public static extern bool DestroyIcon(IntPtr icon);
}
'@
$target = $env:NPB_ICON_PATH
$resourceIndex = 0
$icon = $null
if ([IO.Path]::GetExtension($target) -ieq '.lnk') {
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($target)
    $location = $shortcut.IconLocation
    if ($location -match '^(.*),\s*(-?\d+)\s*$') {
        $iconPath = [Environment]::ExpandEnvironmentVariables($Matches[1].Trim('" '))
        if ($iconPath -and (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
            $target = $iconPath
            $resourceIndex = [int]$Matches[2]
        }
    }
    if ($target -eq $env:NPB_ICON_PATH -and $shortcut.TargetPath) { $target = $shortcut.TargetPath }
}
if ($target.EndsWith('.ico', [StringComparison]::OrdinalIgnoreCase)) {
    $icon = [Drawing.Icon]::new($target, 32, 32)
} else {
    $handle = [IntPtr]::Zero
    try {
        [void][NpbShellIcon]::ExtractIconEx($target, $resourceIndex, [ref]$handle, [IntPtr]::Zero, 1)
        if ($handle -ne [IntPtr]::Zero) { $icon = [Drawing.Icon]::FromHandle($handle).Clone() }
    } finally {
        if ($handle -ne [IntPtr]::Zero) { [void][NpbShellIcon]::DestroyIcon($handle) }
    }
}
if (!$icon) { $icon = [Drawing.Icon]::ExtractAssociatedIcon($target) }
if (!$icon) { exit 1 }
try {
    $original = $icon.ToBitmap()
    $bitmap = [Drawing.Bitmap]::new($original, 32, 32)
    try {
        $bytes = [byte[]]::new(4096)
        for ($y=0; $y -lt 32; $y++) {
            for ($x=0; $x -lt 32; $x++) {
                $c = $bitmap.GetPixel($x,$y)
                $i = 4*($y*32+$x)
                $bytes[$i]=$c.R; $bytes[$i+1]=$c.G; $bytes[$i+2]=$c.B; $bytes[$i+3]=$c.A
            }
        }
        [Console]::Write([Convert]::ToBase64String($bytes))
    } finally { $bitmap.Dispose(); $original.Dispose() }
} finally { $icon.Dispose() }
"""
    try:
        shell = os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        output = subprocess.check_output([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                        base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                        env={**os.environ, "NPB_ICON_PATH": path}, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        pixels = base64.b64decode(output, validate=True)
        return pixels if len(pixels) == 4096 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=8)
def load_widget_image(path, width, stretch):
    """Decode Windows image formats and GIF frame delays at display resolution."""
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$size = [int]$env:NPB_IMAGE_WIDTH
$rowBytes = $size * 4
$height = 42
$rect = [Drawing.Rectangle]::new(0, 0, $size, $height)
$format = [Drawing.Imaging.PixelFormat]::Format32bppArgb
$image = [Drawing.Image]::FromFile($env:NPB_IMAGE_PATH)
try {
    $dimension = [Drawing.Imaging.FrameDimension]::Time
    $animated = $image.FrameDimensionsList -contains $dimension.Guid
    $count = if ($animated) { $image.GetFrameCount($dimension) } else { 1 }
    $delays = if ($animated -and $image.PropertyIdList -contains 0x5100) {
        ,$image.GetPropertyItem(0x5100).Value
    }
    $drawWidth = $size
    $drawHeight = $height
    if ($env:NPB_IMAGE_STRETCH -ne '1') {
        $scale = [Math]::Min($size / $image.Width, $height / $image.Height)
        $drawWidth = [Math]::Max(1, [int]($image.Width * $scale))
        $drawHeight = [Math]::Max(1, [int]($image.Height * $scale))
    }
    $destination = [Drawing.Rectangle]::new(
        [int][Math]::Floor(($size - $drawWidth) / 2),
        [int][Math]::Floor(($height - $drawHeight) / 2), $drawWidth, $drawHeight)
    $bitmap = [Drawing.Bitmap]::new($size, $height, $format)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $stream = [Console]::OpenStandardOutput()
    $bytes = [byte[]]::new($rowBytes * $height)
    try {
        for ($i = 0; $i -lt $count; $i++) {
            if ($animated) { [void]$image.SelectActiveFrame($dimension, $i) }
            $graphics.Clear([Drawing.Color]::Transparent)
            $graphics.DrawImage($image, $destination)
            $data = $bitmap.LockBits($rect, [Drawing.Imaging.ImageLockMode]::ReadOnly, $format)
            try {
                for ($y = 0; $y -lt $height; $y++) {
                    $line = [IntPtr]::Add($data.Scan0, $y * $data.Stride)
                    [Runtime.InteropServices.Marshal]::Copy($line, $bytes, $y * $rowBytes, $rowBytes)
                }
            } finally { $bitmap.UnlockBits($data) }
            $delay = 100
            if ($delays -and $delays.Length -ge (4 * ($i + 1))) {
                $delay = [Math]::Max(40, [BitConverter]::ToInt32($delays, 4 * $i) * 10)
            }
            $timing = [BitConverter]::GetBytes([int]$delay)
            $stream.Write($timing, 0, 4)
            $stream.Write($bytes, 0, $bytes.Length)
        }
    } finally { $graphics.Dispose(); $bitmap.Dispose(); $stream.Dispose() }
} finally { $image.Dispose() }
"""
    try:
        shell = os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        data = subprocess.check_output(
            [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            env={**os.environ, "NPB_IMAGE_PATH": path, "NPB_IMAGE_WIDTH": str(width),
                 "NPB_IMAGE_STRETCH": "1" if stretch else "0"},
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=12)
    except (OSError, subprocess.SubprocessError):
        return None
    stride = width * 42 * 4 + 4
    if not data or len(data) % stride:
        return None
    ends = []
    elapsed = 0
    for offset in range(0, len(data), stride):
        elapsed += struct.unpack_from("<I", data, offset)[0]
        ends.append(elapsed)
    return data, stride, ends


def render_dashboard(snapshot, font, widgets, clock_format, text_size,
                     background_brightness, text_color, text_brightness, text_outline,
                     page_index=0, page_count=1):
    widgets = tuple(widget for widget in widgets if widget[0] in DASHBOARD_WIDGETS)
    if not widgets and page_count == 1:
        return bytearray(bytes((0, 0, 0, 255)) * WIDTH * HEIGHT)

    def dim(color, brightness):
        return tuple(round(channel * brightness / 100) for channel in color)

    frame = bytearray(bytes((*dim((7, 13, 21), background_brightness), 255)) * WIDTH * HEIGHT)

    def rectangle(x, y, width, height, color):
        row = bytes((*color, 255)) * width
        for line in range(y, y + height):
            offset = (line * WIDTH + x) * 4
            frame[offset:offset + len(row)] = row

    def label(text, x, y, width, height, color, size, align_right=False):
        pixels = raster_text(text, font, width, height, size, align_right)
        if text_outline:
            for left, top, opacity in pixels:
                if opacity >= 120:
                    rectangle(x + left - 1, y + top - 1, 3, 3, (0, 0, 0))
        for left, top, opacity in pixels:
            index = ((y + top) * WIDTH + x + left) * 4
            frame[index:index + 3] = bytes(
                (frame[index + channel] * (255 - opacity) + value * opacity + 127) // 255
                for channel, value in enumerate(color)
            )

    ink = dim(text_color or (231, 241, 255), text_brightness)
    muted = dim(text_color or (185, 207, 229), text_brightness)
    panel = dim((16, 29, 44), background_brightness)
    _, base_height = TEXT_SIZES[text_size]
    values, histories = snapshot
    now = datetime.now()
    clock = now.strftime("%H:%M") if clock_format == "24-hour" else now.strftime("%I:%M %p").lstrip("0")
    date = now.strftime("%a %b %d %Y")
    values = {**values, "Time": clock, "Date": date}
    accents = ((74, 189, 255), (83, 225, 161), (202, 156, 255), (244, 186, 100))
    for index, (name, preset, x, card_width, graph, app_path) in enumerate(widget_positions(widgets)):
        rectangle(x, 3, card_width, 42, panel)
        if name == IMAGE_WIDGET:
            image = load_widget_image(app_path, card_width, graph == "Stretch") if app_path else None
            if image:
                data, stride, ends = image
                index_at_time = bisect_right(ends, int(time.monotonic() * 1000) % ends[-1])
                start = index_at_time * stride + 4
                light = int(preset)
                for row in range(42):
                    target = ((row + 3) * WIDTH + x) * 4
                    source = start + row * card_width * 4
                    for _ in range(card_width):
                        blue, green, red, opacity = data[source:source + 4]
                        if opacity:
                            frame[target:target + 3] = bytes(
                                (frame[target + channel] * (255 - opacity) * 100
                                 + value * light * opacity + 12750) // 25500
                                for channel, value in enumerate((red, green, blue)))
                        source += 4
                        target += 4
            else:
                label("Image unavailable", x + 6, 10, card_width - 12, 24, muted, base_height)
            continue
        accent = WIDGET_COLORS.get(preset, accents[index % len(accents)])
        rectangle(x, 3, 2, 42, accent)
        if name == "Custom app":
            pixels = app_icon(app_path) if app_path else None
            if pixels:
                icon_width = card_width if graph == "Stretch" else min(42, card_width)
                icon_left = x + (card_width - icon_width) // 2
                for row in range(42):
                    for column in range(icon_width):
                        source_pixel = (row * 32 // 42 * 32 + column * 32 // icon_width) * 4
                        target_pixel = ((row + 3) * WIDTH + icon_left + column) * 4
                        opacity = pixels[source_pixel + 3] * 180 // 255
                        frame[target_pixel:target_pixel + 3] = bytes(
                            (frame[target_pixel + channel] * (255 - opacity) + pixels[source_pixel + channel] * opacity) // 255
                            for channel in range(3))
            if preset:
                size = base_height + 1
                available = card_width - 12
                title = preset
                glyphs = raster_text(title, font, available, 15, size, False)
                for length in range(len(title) - 1, -1, -1):
                    if glyphs:
                        break
                    title = preset[:length].rstrip() + "..."
                    glyphs = raster_text(title, font, available, 15, size, False)
                if glyphs:
                    right = x + 6 + max(px for px, _, _ in glyphs) + 4
                    for row in range(29, 44):
                        for column in range(x + 3, min(right, x + card_width - 2)):
                            offset = (row * WIDTH + column) * 4
                            frame[offset:offset + 3] = bytes(channel // 2 for channel in frame[offset:offset + 3])
                    label(title, x + 6, 29, available, 15, ink, size)
            continue
        frequencies = values.get(name) if name in (
            "CPU usage + frequency", "GPU usage + frequency", "RAM usage + capacity"
        ) else None
        frequency_left = False
        frequency_stacked = False
        if frequencies:
            frequency_left = widgets[index][1] == 1
            size = base_height + 1

            def measure(text):
                return max((px for px, _, _ in raster_text(text, font, 300, 16, size, False)), default=300) + 1

            heading = f"{DASHBOARD_WIDGETS[name]}  {frequencies[0]}"
            detail = frequencies[1]
            heading_width = measure(heading) + 4
            detail_width = card_width - 12 - heading_width
            # The label rasterizer can use a slightly smaller font on narrow cards.
            frequency_stacked = frequency_left or detail_width <= 0 or not raster_text(
                detail, font, detail_width, 14, size, True)
        source = GRAPH_SOURCES.get(name)
        percent = name in PERCENT_WIDGETS
        wide_graph = percent and card_width >= 135
        if source:
            history = histories.get(source, ())[-42:]
            graph_width = card_width - 14
            graph_height = (8 if frequency_stacked else 19) if frequencies else 15 if wide_graph else 7 if percent else 24
            graph_ink = accent
            step = graph_width / 42
            rectangle(x + 7, 41, graph_width, 1, (49, 67, 88))
            last = None
            for offset, sample in enumerate(reversed(history)):
                if sample is None:
                    last = None
                    continue
                bar = max(1, int(step) - 1)
                left = x + card_width - 7 - bar - round(offset * step)
                height = max(1, round(sample * graph_height / 100))
                top = 41 - height
                if graph == "Bar":
                    rectangle(left, top, bar, height, graph_ink)
                else:
                    rectangle(left, top, 1, 1, graph_ink)
                    if last:
                        px, py = last
                        for point in range(left + 1, px + 1):
                            before = round(top + (py - top) * (point - 1 - left) / (px - left))
                            after = round(top + (py - top) * (point - left) / (px - left))
                            rectangle(point, min(before, after), 1, abs(after - before) + 1, graph_ink)
                    last = left, top
        if name == "Clock and date":
            label(clock, x + 6, 4, card_width - 12, 21, ink, base_height + 7)
            label(date if card_width >= 112 else now.strftime("%b %d %Y"), x + 6, 26,
                  card_width - 12, 16, muted, base_height + 1)
        elif name in APP_SHORTCUTS:
            label(name, x + 6, 6, card_width - 12, 19, ink, base_height + 2)
            label("Tap to open", x + 6, 26, card_width - 12, 15, muted, base_height)
        elif frequencies:
            content_width = card_width - 12
            if frequency_stacked:
                title = DASHBOARD_WIDGETS[name]
                title_width = measure(title) + 4
                label(title, x + 6, 4, title_width, 14, ink, size)
                label(frequencies[0], x + 6 + title_width, 4, content_width - title_width,
                      14, ink, size, align_right=True)
                indent = 4 if frequency_left else 0
                label(detail, x + 6 + indent, 16, content_width - indent, 14,
                      ink, size, align_right=not frequency_left)
            else:
                label(heading, x + 6, 4, heading_width, 14, ink, size)
                label(detail, x + 6 + heading_width, 4, content_width - heading_width,
                      14, ink, size, align_right=True)
        elif isinstance(values.get(name), tuple):
            readings = values[name]
            if len(readings) == 3:
                label(f"NET {readings[0]}", x + 6, 4, card_width - 12, 12, muted, base_height)
                readings = readings[1:]
            else:
                label(DASHBOARD_WIDGETS[name], x + 6, 4, card_width - 12, 12, muted, base_height)
            for row, value in enumerate(readings):
                label(value, x + 6, 16 + row * 13, card_width - 12, 13, ink, base_height + 1)
        else:
            value = values.get(name)
            reading = "--" if value is None else f"{value}%" if percent else str(value)
            if name == "Date" and card_width < 112:
                reading = now.strftime("%b %d %Y")
            if wide_graph:
                half = card_width // 2
                label(DASHBOARD_WIDGETS[name], x + 8, 5, half - 8, 18, muted, base_height + 2)
                label(reading, x + half, 5, card_width - half - 8, 18, ink,
                      base_height + 3, align_right=True)
            else:
                label(DASHBOARD_WIDGETS[name], x + 6, 5, card_width - 12, 15, muted, base_height)
                label(reading, x + 6, 18, card_width - 12, 17, ink, base_height + 3)
    if page_count > 1:
        track = min(160, page_count * 8)
        start = (WIDTH - track) // 2
        left = start + page_index * track // page_count
        right = start + (page_index + 1) * track // page_count
        rectangle(start, 46, track, 2, (49, 67, 88))
        rectangle(left, 46, max(1, right - left), 2, ink)
    return frame
