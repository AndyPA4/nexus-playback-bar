"""One-shot Corsair NEXUS screen and touch test for Windows."""
import ctypes as c
from ctypes import wintypes as w
import re
import struct
import subprocess
import threading
import unicodedata
from functools import lru_cache
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
hid.HidD_SetFeature.argtypes = [w.HANDLE, c.c_void_p, w.ULONG]
hid.HidD_SetFeature.restype = w.BOOL
for name in ("ReadFile", "WriteFile"):
    method = getattr(kernel, name)
    method.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID]
    method.restype = w.BOOL


def report_lengths(handle):
    descriptor = c.c_void_p()
    if not hid.HidD_GetPreparsedData(handle, c.byref(descriptor)):
        raise c.WinError(c.get_last_error())
    try:
        # HIDP_CAPS consists of 32 USHORT fields, including reserved fields.
        caps = (c.c_ushort * 32)()
        status = hid.HidP_GetCaps(descriptor, caps)
        if status != 0x00110000:
            raise RuntimeError(f"HidP_GetCaps failed: 0x{status & 0xffffffff:08x}")
        return caps[2], caps[4]  # InputReportByteLength, FeatureReportByteLength
    finally:
        hid.HidD_FreePreparsedData(descriptor)


def input_report_length(handle):
    length, _ = report_lengths(handle)
    if length < 10:
        raise RuntimeError(f"Unexpected NEXUS input report length: {length}")
    return length


def firmware_display(handle, command, *arguments):
    """Send a NexusTool-compatible display feature report."""
    _, length = report_lengths(handle)
    report = bytes((3, command, *arguments))
    if length < len(report):
        raise RuntimeError(f"Unexpected NEXUS feature report length: {length}")
    buffer = (c.c_ubyte * length).from_buffer_copy(report.ljust(length, b"\0"))
    if not hid.HidD_SetFeature(handle, buffer, length):
        raise c.WinError(c.get_last_error())


THEMES = {"Mint": (105, 236, 182), "Sky": (100, 186, 255), "Amber": (255, 182, 87)}
DEFAULT_DISPLAY_FONT = "NEXUS Pixel"
DISPLAY_FONTS = (DEFAULT_DISPLAY_FONT, "Consolas", "Cascadia Mono", "Lucida Console", "Courier New",
                 "Segoe UI", "Arial", "Verdana", "Tahoma", "Trebuchet MS", "Times New Roman",
                 "Comic Sans MS", "Calibri", "Cambria", "Georgia", "Palatino Linotype",
                 "Segoe Print", "Microsoft Sans Serif", "Century Gothic")
TEXT_SIZES = {"Small": (7, 8), "Default": (8, 10), "Large": (10, 12)}
gdi = c.WinDLL("gdi32")
user32 = c.WinDLL("user32")
for name, arguments, result in (
    ("CreateCompatibleDC", (w.HDC,), w.HDC),
    ("CreateBitmap", (c.c_int, c.c_int, w.UINT, w.UINT, c.c_void_p), w.HBITMAP),
    ("CreateFontW", (c.c_int,) * 5 + (w.DWORD,) * 8 + (w.LPCWSTR,), w.HANDLE),
    ("SelectObject", (w.HDC, w.HANDLE), w.HANDLE),
    ("DeleteObject", (w.HANDLE,), w.BOOL),
    ("DeleteDC", (w.HDC,), w.BOOL),
    ("PatBlt", (w.HDC, c.c_int, c.c_int, c.c_int, c.c_int, w.DWORD), w.BOOL),
    ("GetPixel", (w.HDC, c.c_int, c.c_int), w.DWORD),
):
    method = getattr(gdi, name)
    method.argtypes, method.restype = arguments, result
user32.DrawTextW.argtypes = (w.HDC, w.LPCWSTR, c.c_int, c.POINTER(w.RECT), w.UINT)
user32.DrawTextW.restype = c.c_int


@lru_cache(maxsize=1024)
def display_glyph(family, character):
    """Rasterize a Windows font into a fixed 8-by-10 NEXUS cell."""
    dc = gdi.CreateCompatibleDC(None)
    bitmap = gdi.CreateBitmap(8, 10, 1, 1, None)
    font = gdi.CreateFontW(12, 5, 0, 0, 400, 0, 0, 0, 1, 0, 0, 3, 0, family)
    try:
        if not (dc and bitmap and font):
            return None
        old_bitmap = gdi.SelectObject(dc, bitmap)
        old_font = gdi.SelectObject(dc, font)
        try:
            gdi.PatBlt(dc, 0, 0, 8, 10, 0x00FF0062)
            user32.DrawTextW(dc, character, 1, c.byref(w.RECT(0, 0, 7, 10)), 0x825)
            return tuple(sum(1 << (7 - x) for x in range(8) if gdi.GetPixel(dc, x, y) == 0)
                         for y in range(10))
        finally:
            gdi.SelectObject(dc, old_font)
            gdi.SelectObject(dc, old_bitmap)
    finally:
        if font:
            gdi.DeleteObject(font)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if dc:
            gdi.DeleteDC(dc)
GLYPHS = dict(zip("0123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ .-!?\u00b7'", (
    "75557", "26227", "71747", "71717", "55711", "74717",
    "74757", "71111", "75757", "75717", "02020",
    "25755", "65656", "74447", "65556", "74647", "74644", "74557",
    "55755", "72227", "11157", "55655", "44447", "57755", "57775",
    "75557", "75744", "75571", "75655", "74717", "72222", "55557",
    "55552", "55577", "55255", "55222", "71247",
    "00000", "00002", "00700", "22202", "71202", "00200", "22000",
)))
GLYPHS.update({
    "+": "02720", "&": "25257", "/": "11244", "\\": "44211",
    "(": "24442", ")": "21112", "[": "64446", "]": "31113",
    "{": "32423", "}": "62126", "=": "07070", "_": "00007",
    "@": "75747", "#": "57575", "%": "51245", "$": "27632",
    "*": "05250", "|": "22222", ",": "00024", ";": "02024",
    '"': "55000", "~": "03500", "<": "12421", ">": "42124",
    "^": "25000", "\u2665": "57772", "\u266a": "32264",
    "\u00d7": "05050", "\u00f7": "20702", "\u00b0": "25000",
    "\u25a1": "75557",
})
TEXT_SYMBOLS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2026": "...",
    "\u2022": "\u00b7", "\u266b": "\u266a", "\u00d8": "O", "\u0141": "L",
})


@lru_cache(maxsize=128)
def display_text(label):
    result = []
    for char in label.upper().translate(TEXT_SYMBOLS):
        if char in GLYPHS:
            result.append(char)
            continue
        for letter in unicodedata.normalize("NFKD", char):
            if unicodedata.category(letter).startswith(("M", "C")):
                continue
            result.append(letter if letter in GLYPHS else "\u25a1")
    return "".join(result)


BAR_LEFT, BAR_RIGHT = 14, WIDTH - 15


def clock(seconds, time_format="Minutes"):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    if time_format == "Hours":
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


@lru_cache(maxsize=3072)
def glyph_runs(family, character, width, height):
    mask = display_glyph(family, character) if family and family != DEFAULT_DISPLAY_FONT else None
    if mask is None:
        mask = tuple(int(row) << 1 for row in GLYPHS.get(character, GLYPHS["\u25a1"]))
        columns = 4
    else:
        columns = 8
    runs = []
    for y in range(height):
        row = mask[y * len(mask) // height]
        start = None
        for x in range(width + 1):
            dark = x < width and row & (1 << (columns - 1 - x * columns // width))
            if dark and start is None:
                start = x
            elif not dark and start is not None:
                runs.append((y, start, x))
                start = None
    return tuple(runs)


class BitmapHeader(c.Structure):
    _fields_ = [
        ("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
        ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
        ("image_size", w.DWORD), ("x_resolution", w.LONG), ("y_resolution", w.LONG),
        ("colors_used", w.DWORD), ("important_colors", w.DWORD),
    ]


gdi.CreateDIBSection.argtypes = [w.HDC, c.POINTER(BitmapHeader), w.UINT,
                                c.POINTER(c.c_void_p), w.HANDLE, w.DWORD]
gdi.CreateDIBSection.restype = w.HBITMAP
gdi.GdiFlush.argtypes = []
gdi.GdiFlush.restype = w.BOOL


@lru_cache(maxsize=512)
def raster_text(text, family, width, height, size, align_right):
    """Rasterize an entire label at the display's native pixel size."""
    # The bundled pixel face is designed for fixed cells. Use a small Windows
    # UI face for dashboard labels under the default font setting.
    family = "Segoe UI" if family in (None, DEFAULT_DISPLAY_FONT) else family
    dc = gdi.CreateCompatibleDC(None)
    if not dc:
        return ()
    bits = c.c_void_p()
    header = BitmapHeader(size=c.sizeof(BitmapHeader), width=width, height=-height, planes=1, bits=32)
    bitmap = gdi.CreateDIBSection(dc, c.byref(header), 0, c.byref(bits), None, 0)
    if not bitmap:
        gdi.DeleteDC(dc)
        return ()
    old_bitmap = gdi.SelectObject(dc, bitmap)
    try:
        for font_size in range(size, 6, -1):
            font = gdi.CreateFontW(-font_size, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 4, 0, family)
            if not font:
                continue
            old_font = gdi.SelectObject(dc, font)
            try:
                measured = w.RECT()
                user32.DrawTextW(dc, text, -1, c.byref(measured), 0xC20)
                if measured.right + 4 > width or measured.bottom > height:
                    continue
                gdi.PatBlt(dc, 0, 0, width, height, 0x00FF0062)
                rect = w.RECT(2, 0, width - 2, height)
                user32.DrawTextW(dc, text, -1, c.byref(rect), 0x824 | (2 if align_right else 0))
                gdi.GdiFlush()
                mask = c.string_at(bits, width * height * 4)
                return tuple((index % width, index // width, 255 - value)
                             for index, value in enumerate(mask[::4]) if value < 255)
            finally:
                gdi.SelectObject(dc, old_font)
                gdi.DeleteObject(font)
        return ()
    finally:
        gdi.SelectObject(dc, old_bitmap)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)


def render(marker, seekable, position, duration, theme, title, background, layout, alignment,
           brightness=100, credit="", credit_mode="Beside title",
           text_color=(255, 255, 255), text_brightness=100,
           text_border=False, bar_border=False, bar_size="Default", scroll=None,
           time_format="Minutes", display_font=None, text_size="Default"):
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

    cell_width, cell_height = TEXT_SIZES[text_size]
    font_height = cell_height + 3

    def shape(label):
        return raster_text(label, display_font, max(8, (cell_height + 3) * len(label) + 8),
                           font_height, cell_height + 1, False)

    def measure(label):
        return max((x for x, _, _ in shape(label)), default=1) - 1

    def text(label, x, y, ink, clip):
        for left, top, opacity in shape(label):
            left += x - 2
            top += y - 1
            if clip[0] <= left < clip[1] and 0 <= top < HEIGHT:
                offset = (top * WIDTH + left) * 4
                frame[offset:offset + 3] = bytes(
                    (frame[offset + channel] * (255 - opacity) + value * opacity + 127) // 255
                    for channel, value in enumerate(ink)
                )

    accent = (THEMES[theme] if isinstance(theme, str) else theme) if seekable else (113, 126, 144)
    center = BAR_LEFT + round(marker * (BAR_RIGHT - BAR_LEFT) / (WIDTH - 1))
    track = (75, 88, 103)
    title_y, bar_y, time_y = {"Split": (5, 25, 36), "Above": (2, 38, 16),
                             "Below": (20, 8, 34), "Inline above": (7, 32, 7),
                             "Inline below": (31, 15, 31)}[layout]
    shift = (10 - cell_height) // 2
    title_y = max(1, title_y + shift)
    time_y = max(1, time_y + shift)
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
                  for y in range(row, row + cell_height, 3)
                  for i in range(y * WIDTH * 4, (y + 1) * WIDTH * 4, 64)]
        text_color = (0, 0, 0) if sum(levels) / len(levels) > 0.179 else (255, 255, 255)
    ink = tuple(round(channel * text_brightness / 100) for channel in text_color)
    outline = tuple(round(255 * text_brightness / 100) - channel for channel in ink)

    def draw_text(label, x, y, clip=(0, WIDTH)):
        if (automatic or text_border) and text_brightness:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                text(label, x + dx, y + dy, outline if automatic else (0, 0, 0), clip)
        if text_brightness:
            text(label, x, y, ink, clip)

    elapsed, total = ((clock(position, time_format), clock(duration, time_format))
                      if duration else ("--:--", "--:--"))
    total_x = WIDTH - 12 - measure(total)
    left = 12 + measure(elapsed) + 12 if inline else 12
    right = total_x - 12 if inline else WIDTH - 12
    def fit(label, width):
        if measure(label) <= width:
            return label
        if measure("...") > width:
            return ""
        lower, upper = 0, len(label)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if measure(label[:middle] + "...") <= width:
                lower = middle
            else:
                upper = middle - 1
        return label[:lower] + "..."

    def draw_label(label, start, end, align):
        width = measure(label)
        if scroll is not None and width > end - start:
            lap = width + 32
            phase = scroll % (lap + 32)
            offset = max(0, phase - 32)
            clip = (start, end)
            draw_text(label, start + offset - lap, title_y, clip)
            draw_text(label, start + offset, title_y, clip)
            return
        label = fit(label, end - start)
        width = max(0, measure(label))
        x = {"Left": start, "Center": (start + end - width) // 2, "Right": end - width}[align]
        draw_text(label, x, title_y, (start, end))

    title, credit = title.upper(), credit.upper()
    if credit and credit_mode == "Opposite sides":
        gap = 16
        available = right - left - gap
        credit_width = min(measure(credit), available * 2 // 5)
        if alignment == "Right":
            draw_label(credit, left, left + credit_width, "Left")
            draw_label(title, left + credit_width + gap, right, "Right")
        else:
            title_end = right - gap - credit_width
            if alignment == "Center":
                middle = WIDTH // 2
                radius = max(0, min(middle - left, title_end - middle))
                draw_label(title, middle - radius, middle + radius, "Center")
            else:
                draw_label(title, left, title_end, "Left")
            draw_label(credit, right - credit_width, right, "Right")
    else:
        draw_label(f"{title} \u00b7 {credit}" if credit and credit_mode == "Beside title" else title,
                   left, right, alignment)
    draw_text(elapsed, 12, time_y)
    draw_text(total, total_x, time_y)
    return frame


def draw(handle, *frame_state):
    write_frame(handle, render(*frame_state))


def write_frame(handle, frame):
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


