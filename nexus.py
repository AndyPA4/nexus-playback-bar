"""One-shot Corsair NEXUS screen and touch test for Windows."""
import ctypes as c
from ctypes import wintypes as w
import re
import struct
import subprocess
import threading
from pathlib import Path
import tkinter as tk

# Display and touch report layout based on willneedit/NexusTool (MIT).
# https://github.com/willneedit/NexusTool
WIDTH, HEIGHT = 640, 48
HID = "{4d1e55b2-f16f-11cf-88cb-001111000030}"
LOG = Path(__file__).with_name("nexus-test-result.txt")
kernel = c.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
kernel.CreateFileW.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]
hid = c.WinDLL("hid", use_last_error=True)
hid.HidD_GetPreparsedData.argtypes = [w.HANDLE, c.POINTER(c.c_void_p)]
hid.HidD_GetPreparsedData.restype = c.c_ubyte
hid.HidD_FreePreparsedData.argtypes = [c.c_void_p]
hid.HidD_FreePreparsedData.restype = c.c_ubyte
hid.HidP_GetCaps.argtypes = [c.c_void_p, c.POINTER(c.c_ushort)]
hid.HidP_GetCaps.restype = c.c_long
for name in ("ReadFile", "WriteFile"):
    method = getattr(kernel, name)
    method.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID]
    method.restype = w.BOOL


def input_report_length(handle):
    descriptor = c.c_void_p()
    if not hid.HidD_GetPreparsedData(handle, c.byref(descriptor)):
        raise c.WinError(c.get_last_error())
    try:
        # HIDP_CAPS consists of 32 USHORT fields, including reserved fields.
        caps = (c.c_ushort * 32)()
        status = hid.HidP_GetCaps(descriptor, caps)
        if status != 0x00110000:
            raise RuntimeError(f"HidP_GetCaps failed: 0x{status & 0xffffffff:08x}")
        length = caps[2]  # InputReportByteLength, including report ID.
        if length < 10:
            raise RuntimeError(f"Unexpected NEXUS input report length: {length}")
        return length
    finally:
        hid.HidD_FreePreparsedData(descriptor)


THEMES = {"Mint": (105, 236, 182), "Sky": (100, 186, 255), "Amber": (255, 182, 87)}
GLYPHS = dict(zip("0123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ .-!?·'", (
    "75557", "26227", "71747", "71717", "55711", "74717",
    "74757", "71111", "75757", "75717", "02020",
    "25755", "65656", "74447", "65556", "74647", "74644", "74557",
    "55755", "72227", "11157", "55655", "44447", "57755", "57775",
    "75557", "75744", "75571", "75655", "74717", "72222", "55557",
    "55552", "55577", "55255", "55222", "71247",
    "00000", "00002", "00700", "22202", "71202", "00200", "22000",
)))
BAR_LEFT, BAR_RIGHT = 14, WIDTH - 15


def clock(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{seconds:02}"


def render(marker, seekable, position, duration, theme, title, background, layout, alignment,
           brightness=100, credit="", credit_mode="Beside title",
           text_color=(255, 255, 255), text_brightness=100,
           text_border=False, bar_border=False, bar_size="Default", scroll=None):
    frame = bytearray(background)
    if brightness != 100:
        levels = bytes(round(channel * brightness / 100) for channel in range(256))
        for channel in range(3):
            frame[channel::4] = frame[channel::4].translate(levels)

    def rectangle(x, y, width, height, color):
        row = bytes((*color, 255)) * width
        for line in range(y, y + height):
            start = (line * WIDTH + x) * 4
            frame[start:start + len(row)] = row

    def disc(x, y, radius, color):
        for dy in range(-radius, radius + 1):
            span = int((radius * radius - dy * dy) ** 0.5)
            rectangle(x - span, y + dy, 2 * span + 1, 1, color)

    def text(label, x, y, scale, ink, clip=(0, WIDTH)):
        for char in label:
            for line, row in enumerate(GLYPHS.get(char, GLYPHS["?"])):
                for column in range(3):
                    if int(row) & (4 >> column):
                        start = x + column * scale
                        left_edge = max(start, clip[0])
                        right_edge = min(start + scale, clip[1])
                        if right_edge > left_edge:
                            rectangle(left_edge, y + line * scale, right_edge - left_edge, scale, ink)
            x += 4 * scale

    accent = (THEMES[theme] if isinstance(theme, str) else theme) if seekable else (113, 126, 144)
    center = BAR_LEFT + round(marker * (BAR_RIGHT - BAR_LEFT) / (WIDTH - 1))
    track = (75, 88, 103)
    title_y, bar_y, time_y = {"Split": (5, 25, 36), "Above": (2, 38, 16),
                             "Below": (20, 8, 34), "Inline above": (7, 32, 7),
                             "Inline below": (31, 15, 31)}[layout]
    inline = title_y == time_y
    radius = (5 if inline else 2) + {"Thin": -1, "Default": 0, "Thick": 2}[bar_size]
    thumb = (9 if inline else 6) + {"Thin": -2, "Default": 0, "Thick": 1}[bar_size]
    if bar_border:
        edge = (0, 0, 0)
        rectangle(BAR_LEFT, bar_y - radius - 1, BAR_RIGHT - BAR_LEFT + 1, 2 * radius + 3, edge)
        disc(BAR_LEFT, bar_y, radius + 1, edge)
        disc(BAR_RIGHT, bar_y, radius + 1, edge)
    rectangle(BAR_LEFT, bar_y - radius, BAR_RIGHT - BAR_LEFT + 1, 2 * radius + 1, track)
    disc(BAR_LEFT, bar_y, radius, track)
    disc(BAR_RIGHT, bar_y, radius, track)
    rectangle(BAR_LEFT, bar_y - radius, center - BAR_LEFT + 1, 2 * radius + 1, accent)
    disc(BAR_LEFT, bar_y, radius, accent)
    if bar_border:
        disc(center, bar_y, thumb + 1, (0, 0, 0))
    disc(center, bar_y, thumb, (238, 247, 255))
    disc(center, bar_y, thumb - 2, accent)
    def luminance(rgb):
        return sum(weight * (value / 3294.6 if value <= 10 else
                   ((value / 255 + 0.055) / 1.055) ** 2.4)
                   for value, weight in zip(rgb, (0.2126, 0.7152, 0.0722)))

    automatic = text_color is None
    if automatic:
        levels = [luminance(frame[i:i + 3])
                  for row in {title_y, time_y}
                  for y in range(row, row + 10, 3)
                  for i in range(y * WIDTH * 4, (y + 1) * WIDTH * 4, 64)]
        text_color = (0, 0, 0) if sum(levels) / len(levels) > 0.179 else (255, 255, 255)
    ink = tuple(round(channel * text_brightness / 100) for channel in text_color)
    outline = tuple(round(255 * text_brightness / 100) - channel for channel in ink)

    def draw_text(label, x, y, clip=(0, WIDTH)):
        if (automatic or text_border) and text_brightness:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                text(label, x + dx, y + dy, 2, outline if automatic else (0, 0, 0), clip)
        if text_brightness:
            text(label, x, y, 2, ink, clip)

    elapsed, total = clock(position), clock(duration)
    total_x = WIDTH - 12 - len(total) * 8
    left = 12 + len(elapsed) * 8 + 12 if inline else 12
    right = total_x - 12 if inline else WIDTH - 12
    def fit(label, width):
        label = label.upper()
        capacity = max(0, (width + 2) // 8)
        if len(label) <= capacity:
            return label
        return label[:capacity - 3] + "..." if capacity >= 3 else label[:capacity]

    def draw_title(label, start, end, align):
        label = label.upper()
        width = len(label) * 8 - 2
        if inline and scroll is not None and width > end - start:
            lap = width + 32
            phase = scroll % (lap + 32)
            offset = max(0, phase - 32)
            clip = (start, end)
            draw_text(label, start + offset - lap, title_y, clip)
            draw_text(label, start + offset, title_y, clip)
            return
        label = fit(label, end - start)
        width = max(0, len(label) * 8 - 2)
        x = {"Left": start, "Center": (start + end - width) // 2, "Right": end - width}[align]
        draw_text(label, x, title_y)

    if credit and credit_mode == "Opposite sides":
        gap = 16
        available = right - left - gap
        credit_width = min(len(credit.upper()) * 8 - 2, available * 2 // 5)
        credit_label = fit(credit, credit_width)
        draw_title(title, left, right - gap - credit_width, "Left")
        draw_text(credit_label, right - len(credit_label) * 8 + 2, title_y)
    else:
        draw_title(f"{title} · {credit}" if credit and credit_mode == "Beside title" else title,
                   left, right, alignment)
    draw_text(elapsed, 12, time_y)
    draw_text(total, total_x, time_y)
    return frame


def draw(handle, *frame_state):
    frame = render(*frame_state)
    # The NEXUS interprets USB pixel data in BGR order. The preview uses RGB.
    frame[0::4], frame[2::4] = frame[2::4], frame[0::4]
    buffer = (c.c_ubyte * 1024)()
    written = w.DWORD()
    for block, offset in enumerate(range(0, len(frame), 1016)):
        data = frame[offset:offset + 1016]
        struct.pack_into("<BBBBHH", buffer, 0, 2, 5, 64,
                         int(offset + len(data) == len(frame)), block, len(data))
        buffer[8:8 + len(data)] = data
        if len(data) < 1016:
            buffer[8 + len(data):] = bytes(1016 - len(data))
        if not kernel.WriteFile(handle, buffer, len(buffer), c.byref(written), None):
            raise c.WinError(c.get_last_error())
        if written.value != len(buffer):
            raise RuntimeError("Incomplete USB display write")


def find_nexus(progress):
    progress("Finding the NEXUS USB interface...")
    flags = subprocess.CREATE_NO_WINDOW
    listing = subprocess.check_output(
        ["pnputil", "/enum-interfaces", "/enabled", "/class", HID],
        text=True, creationflags=flags, timeout=5,
    )
    matches = re.findall(r"^Interface Path:\s+(.*VID_1B1C&PID_1B8E&MI_00.*)$", listing, re.I | re.M)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one NEXUS display interface, found {len(matches)}")
    return matches[0].strip()


def open_nexus(path, access):
    handle = kernel.CreateFileW(path, access, 3, None, 3, 0, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    return handle


