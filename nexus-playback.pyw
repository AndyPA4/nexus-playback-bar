"""Launch from a normal Windows desktop using Python 3."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import base64
import json
import math
import os
from pathlib import Path
import threading
import time
import subprocess
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox

import preferences
from tray import Tray

from nexus import BAR_LEFT, BAR_RIGHT, HEIGHT, THEMES, WIDTH, c, clock, w, draw, find_nexus, input_report_length, kernel, open_nexus, render

KEY = Path(__file__).with_name("bridge-key.txt").read_text().strip()
SOURCE_NAMES = {"www.youtube.com": "YouTube", "music.amazon.com": "Amazon Music",
                "www.primevideo.com": "Prime Video", "www.amazon.com": "Prime Video"}
lock = threading.Lock()
players = {}
pending = {}
seek_preview = {}
held_seek = None
live_scrubbing = False
touch_changed = threading.Event()
POSTER_PREFIX = "data:image/jpeg;base64,"


def seek_target(item, x):
    fraction = max(0, min(1, (x - BAR_LEFT) / (BAR_RIGHT - BAR_LEFT)))
    return item["duration"] * fraction


def request_seek(item, target, now):
    command = (target, now)
    pending[item["id"]] = command
    seek_preview[item["id"]] = command
displayed = None
last_tap = ""
status = "Starting..."
startup_stage = "Starting the Chrome connection..."
stage_started = time.monotonic()
display_mode = "Auto"
static_page = False
SMOOTH_FRAME_INTERVAL = 1 / 60


def playback_position(item, now):
    if held_seek and held_seek[0] == item["id"]:
        return held_seek[1]
    requested = seek_preview.get(item["id"])
    if requested and now - requested[1] < 0.7:
        return requested[0]
    elapsed = max(0, min(1, now - item["seen"])) if item["playing"] else 0
    return max(0, min(item["duration"], item["position"] + elapsed * item.get("rate", 1)))
theme = "Automatic"
custom_bar_color = THEMES["Sky"]
BLACK = bytes((0, 0, 0, 255)) * WIDTH * HEIGHT
background = BLACK
background_brightness = 36
credit_mode = "Beside title"
text_color = (255, 255, 255)
text_brightness = 100
outline_options = (False, False, "Default")
automatic_art = True
thumbnail_fit = "Stretch"
layout, alignment = "Split", "Left"
art_cache = {}
art_loading = set()


class DirectArtwork(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def dominant_color(pixels):
    buckets = {}
    for offset in range(0, len(pixels), 4):
        red, green, blue = pixels[offset:offset + 3]
        if max(red, green, blue) < 24:
            continue
        key = (red // 32, green // 32, blue // 32)
        totals = buckets.setdefault(key, [0, 0, 0, 0])
        totals[0] += 1
        totals[1] += red
        totals[2] += green
        totals[3] += blue
    if not buckets:
        return THEMES["Sky"]
    count, red, green, blue = max(buckets.values(), key=lambda totals: totals[0])
    return tuple(round(channel / count) for channel in (red, green, blue))


def bar_color(art):
    if theme == "Automatic":
        return art[1] if art else THEMES["Sky"]
    if theme == "Custom":
        return custom_bar_color
    return THEMES[theme]


def load_artwork(url, fit):
    key = (url, fit)
    result = (BLACK, THEMES["Sky"])
    try:
        if url.startswith(POSTER_PREFIX):
            data = base64.b64decode(url[len(POSTER_PREFIX):], validate=True)
        else:
            with build_opener(DirectArtwork()).open(url, timeout=5) as response:
                data = response.read(2_000_001)
        if len(data) <= 2_000_000:
            command = r"""
Add-Type -AssemblyName System.Drawing
$stream = New-Object IO.MemoryStream(,[Convert]::FromBase64String([Console]::In.ReadToEnd()))
$image = [Drawing.Image]::FromStream($stream)
$bitmap = New-Object Drawing.Bitmap(640,48)
$graphics = [Drawing.Graphics]::FromImage($bitmap)
$scale = [Math]::Max(640/$image.Width,48/$image.Height)
$width = [int][Math]::Ceiling($image.Width*$scale)
# Show at least the middle 60 percent of the thumbnail's height.
$height = [int][Math]::Min(80,[Math]::Ceiling($image.Height*$scale))
if ($env:NEXUS_THUMBNAIL_FIT -eq 'Centered') {
  $scale = [Math]::Min(640/$image.Width,48/$image.Height)
  $width = [Math]::Max(1,[int][Math]::Round($image.Width*$scale))
  $height = [Math]::Max(1,[int][Math]::Round($image.Height*$scale))
}
$graphics.DrawImage($image,[int]((640-$width)/2),[int]((48-$height)/2),$width,$height)
$pixels = New-Object byte[] (640*48*4)
$i=0
for ($y=0;$y -lt 48;$y++) {
  for ($x=0;$x -lt 640;$x++) {
    $p=$bitmap.GetPixel($x,$y)
    $pixels[$i]=$p.R
    $pixels[$i+1]=$p.G
    $pixels[$i+2]=$p.B
    $pixels[$i+3]=255
    $i+=4
  }
}
$sample = New-Object Drawing.Bitmap($image,24,24)
$palette = New-Object byte[] (24*24*4)
$i=0
for ($y=0;$y -lt 24;$y++) {
  for ($x=0;$x -lt 24;$x++) {
    $p=$sample.GetPixel($x,$y)
    $palette[$i]=$p.R
    $palette[$i+1]=$p.G
    $palette[$i+2]=$p.B
    $palette[$i+3]=255
    $i+=4
  }
}
[Console]::Write([Convert]::ToBase64String($pixels + $palette))
"""
            encoded = base64.b64encode(command.encode("utf-16-le")).decode()
            output = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                input=base64.b64encode(data), timeout=12, creationflags=subprocess.CREATE_NO_WINDOW,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "NEXUS_THUMBNAIL_FIT": fit},
            )
            pixels = base64.b64decode(output, validate=True)
            if len(pixels) == len(BLACK) + 24 * 24 * 4:
                result = (pixels[:len(BLACK)], dominant_color(pixels[len(BLACK):]))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        with lock:
            art_loading.discard(key)
            if len(art_cache) >= 12:
                art_cache.pop(next(iter(art_cache)))
            art_cache[key] = result


def set_static_page(enabled):
    global static_page
    with lock:
        static_page = enabled


def show_media(mode, item, now):
    if mode == "Corsair" or item is None:
        return False
    age = now - item["seen"]
    if mode == "Media":
        return age <= 3
    return item["playing"] and age <= 3


def select_display(mode):
    global display_mode, displayed, last_tap, held_seek
    with lock:
        display_mode = mode
        displayed = None
        pending.clear()
        seek_preview.clear()
        held_seek = None
        last_tap = ""


def startup_progress(message):
    global startup_stage, stage_started
    startup_stage = message
    stage_started = time.monotonic()


def active(now):
    for key in list(players):
        if now - players[key]["seen"] > 75:
            del players[key]
            pending.pop(key, None)
            seek_preview.pop(key, None)
    return max(players.values(), key=lambda item: (now - item["seen"] <= 3, item["playing"], item["played"]), default=None)


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        supplied = self.headers.get("X-Nexus-Bridge", "")
        if self.path != "/media" or not hmac.compare_digest(supplied.encode(), KEY.encode()):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 40000:
                raise ValueError()
            item = json.loads(self.rfile.read(length))
            if not isinstance(item, dict):
                raise ValueError()
            site = item["site"]
            if not isinstance(site, str) or not 0 < len(site) <= 253 or not all(
                label and len(label) <= 63 and all(char in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label)
                for label in site.split(".")
            ):
                raise ValueError()
            key = item["id"]
            if not isinstance(key, str) or len(key) > 80:
                raise ValueError()
            for field in ("position", "duration", "played"):
                if type(item[field]) not in (int, float) or not math.isfinite(item[field]):
                    raise ValueError()
            if item["duration"] <= 0 or type(item["playing"]) is not bool or type(item["seekable"]) is not bool:
                raise ValueError()
            title = item.get("title")
            item["title"] = (" ".join(title.split())[:160] if isinstance(title, str) else "") or SOURCE_NAMES.get(site, site)
            credit = item.get("credit")
            credit = " ".join(credit.split())[:100] if isinstance(credit, str) else ""
            item["credit"] = credit if credit.casefold() != item["title"].casefold() else ""
            rate = item.get("rate", 1)
            item["rate"] = rate if type(rate) in (int, float) and 0 <= rate <= 16 else 1
            artwork = item.get("artwork")
            item["artwork"] = ""
            if isinstance(artwork, str) and artwork.startswith(POSTER_PREFIX) and len(artwork) <= 32024:
                item["artwork"] = artwork
            elif isinstance(artwork, str) and len(artwork) <= 800:
                parsed = urlsplit(artwork)
                if parsed.scheme == "https" and parsed.netloc in (
                    "i.ytimg.com", "m.media-amazon.com", "images-na.ssl-images-amazon.com",
                    "images.amazon.com", "images-eu.ssl-images-amazon.com",
                ):
                    item["artwork"] = artwork
        except (TypeError, ValueError, KeyError, OverflowError):
            self.send_error(400)
            return
        item["seen"] = now = time.monotonic()
        with lock:
            item["title_started"] = players.get(key, {}).get("title_started", now)
            players[key] = item
            current = active(now)
            command = pending.pop(key, None)
            requested = seek_preview.get(key)
            if requested and (now - requested[1] >= 0.7 or
                              (command is None and abs(item["position"] - requested[0]) < 0.35)):
                seek_preview.pop(key, None)
            if not current or current["id"] != key or not show_media(display_mode, current, now):
                command = None
        answer = json.dumps({"seek": command[0]} if command and now - command[1] < 2 else {}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)


def listen(path):
    global status, last_tap
    try:
        handle = open_nexus(path, 0x80000000)
        try:
            read_taps(handle, input_report_length(handle))
        finally:
            kernel.CloseHandle(handle)
    except Exception as error:
        status = f"NEXUS touch stopped: {error}"


def read_taps(handle, size):
    global last_tap, held_seek
    packet = c.create_string_buffer(size)
    count = w.DWORD()
    held = False
    drag_player = None
    previous_x = None
    moved = False
    last_live_seek = 0
    while kernel.ReadFile(handle, packet, size, c.byref(count), None):
        data = packet.raw[:count.value]
        if len(data) < 10 or data[:3] != b"\x01\x02\x21":
            continue
        touching = bool(data[5])
        if touching:
            x = int.from_bytes(data[6:8], "little")
            now = time.monotonic()
            with lock:
                current = active(now)
                if not held:
                    previous_x = None
                    moved = False
                    last_live_seek = 0
                    held_seek = None
                    drag_player = displayed["id"] if displayed else None
                    last_tap = f"Tap x={x}: no seekable video"
                if (0 <= x < WIDTH and x != previous_x and drag_player and displayed
                        and current and display_mode != "Corsair" and now - current["seen"] <= 3
                        and current["id"] == displayed["id"] == drag_player and current["seekable"]):
                    target = seek_target(current, x)
                    moved |= previous_x is not None
                    held_seek = (drag_player, target)
                    touch_changed.set()
                    if live_scrubbing and now - last_live_seek >= 0.15:
                        request_seek(current, target, now)
                        last_live_seek = now
                    previous_x = x
                    last_tap = f"Seek requested: {clock(target)}"
        elif held:
            now = time.monotonic()
            with lock:
                current = active(now)
                if ((moved or not live_scrubbing) and held_seek and displayed and current and display_mode != "Corsair"
                        and now - current["seen"] <= 3
                        and current["id"] == displayed["id"] == held_seek[0] and current["seekable"]):
                    request_seek(current, held_seek[1], now)
                held_seek = None
                touch_changed.set()
        held = touching
    with lock:
        held_seek = None
        touch_changed.set()
    raise c.WinError(c.get_last_error())


def update():
    global displayed, status
    handle = None
    try:
        with ThreadingHTTPServer(("127.0.0.1", 18763), Bridge) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            path = find_nexus(startup_progress)
            startup_progress("Opening the NEXUS display...")
            handle = open_nexus(path, 0xc0000000)
            reader = threading.Thread(target=listen, args=(path,), daemon=True)
            previous = None
            next_draw = 0
            previous_quiet = None
            while reader.ident is None or reader.is_alive():
                now = time.monotonic()
                with lock:
                    touch_changed.clear()
                    item = active(now)
                    mode = display_mode
                    quiet = static_page
                    art = None
                    backdrop = background
                    brightness = background_brightness
                    visible = show_media(mode, item, now)
                    arrangement, title_edge = layout, alignment
                    credit_style = credit_mode
                    ink, ink_brightness = text_color, text_brightness
                    borders = outline_options
                    if visible and (automatic_art or theme == "Automatic"):
                        url = item["artwork"]
                        key = (url, thumbnail_fit)
                        art = art_cache.get(key)
                        if automatic_art and art:
                            backdrop = art[0]
                        if url and key not in art_cache and key not in art_loading and len(art_loading) < 2:
                            art_loading.add(key)
                            threading.Thread(target=load_artwork, args=key, daemon=True).start()
                    color = bar_color(art)
                    if not visible:
                        displayed = None
                        pending.clear()
                        seek_preview.clear()
                    position = playback_position(item, now) if visible else 0
                    dragging = visible and held_seek is not None and held_seek[0] == item["id"]
                if not visible:
                    previous = None
                    next_draw = 0
                    status = ("Waiting for Chrome playback. Media drawing is off."
                              if mode == "Auto" else
                              "Media drawing is off. Switch pages on the NEXUS if needed." if mode == "Corsair" else
                              "Waiting for a media player in Chrome.")
                    time.sleep(0.2)
                    continue
                limited = quiet and not dragging
                interval = 1 if limited else SMOOTH_FRAME_INTERVAL
                if limited != previous_quiet:
                    next_draw = now
                    previous_quiet = limited
                ratio = position / item["duration"]
                marker = round(max(0, min(1, ratio)) * (WIDTH - 1))
                frame = (marker, item["seekable"], int(position), int(item["duration"]),
                         color, item["title"], backdrop, arrangement, title_edge, brightness,
                         item["credit"], credit_style, ink, ink_brightness, *borders,
                         int((now - item["title_started"]) * 40) if arrangement.startswith("Inline") else None)
                if (dragging or now >= next_draw) and frame != previous:
                    next_draw = now + interval
                    draw(handle, *frame)
                    previous = frame
                    if reader.ident is None:
                        reader.start()
                with lock:
                    if display_mode == mode:
                        displayed = item
                if item:
                    status = f"{item['site']}  |  {item['position']:.0f} / {item['duration']:.0f} seconds"
                    if time.monotonic() - item["seen"] > 3:
                        status += "  |  Waiting for a fresh Chrome update"
                    if not item["seekable"]:
                        status += "  |  Seeking unavailable"
                else:
                    status = "Connected to NEXUS. Waiting for Chrome playback."
                remaining = next_draw - time.monotonic()
                touch_changed.wait(0.016 if dragging else min(remaining, 0.016) if remaining > 0 else 0.016)
    except Exception as error:
        status = f"Playback bar stopped: {error}"
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    colors = {"bg": "#0c121a", "panel": "#141e2a", "text": "#ebf4fe", "muted": "#99acc2"}
    backgrounds = {"Thumbnail": BLACK, "Black": BLACK}
    saved = preferences.load()
    keys = ("display_mode", "static_page", "theme", "custom_bar_color",
            "background_brightness", "thumbnail_fit", "layout", "alignment",
            "credit_mode", "text_color", "text_brightness", "outline_options", "live_scrubbing")
    for key in keys:
        if key not in saved:
            continue
        value = saved[key]
        default = globals()[key]
        if isinstance(default, tuple) and isinstance(value, list) and len(value) == len(default):
            value = tuple(value)
        if type(value) is type(default) or (key == "text_color" and value is None):
            globals()[key] = value
    for name in ("Color", "Image"):
        try:
            pixels = base64.b64decode(saved.get("backgrounds", {}).get(name, ""), validate=True)
            if len(pixels) == len(BLACK):
                backgrounds[name] = pixels
        except (TypeError, ValueError, AttributeError):
            pass
    background_initial = saved.get("background_mode", "Thumbnail")
    if not isinstance(background_initial, str) or background_initial not in backgrounds:
        background_initial = "Thumbnail"
    background = backgrounds[background_initial]
    automatic_art = background_initial == "Thumbnail"


    def pick_background():
        global background, automatic_art
        mode = background_mode.get()
        if mode not in backgrounds:
            if mode == "Color":
                choose_color()
            else:
                choose_image()
            return
        with lock:
            background = backgrounds[mode]
            automatic_art = mode == "Thumbnail"

    def choose_color():
        rgb, _ = colorchooser.askcolor(parent=window, title="NEXUS background color")
        if rgb is None:
            background_mode.set(applied_background.get())
            return
        backgrounds["Color"] = bytes((*map(round, rgb), 255)) * WIDTH * HEIGHT
        apply_background("Color")

    def choose_image():
        path = filedialog.askopenfilename(parent=window, filetypes=[("PNG or GIF images", "*.png *.gif")])
        if not path:
            background_mode.set(applied_background.get())
            return
        try:
            image = tk.PhotoImage(file=path)
            scale = max(WIDTH / image.width(), HEIGHT / image.height())
            left = (image.width() - WIDTH / scale) / 2
            top = (image.height() - HEIGHT / scale) / 2
            xs = [min(image.width() - 1, int(left + (x + 0.5) / scale)) for x in range(WIDTH)]
            pixels = bytearray()
            for y in range(HEIGHT):
                sy = min(image.height() - 1, int(top + (y + 0.5) / scale))
                for sx in xs:
                    pixels.extend(image.get(sx, sy))
                    pixels.append(255)
        except (tk.TclError, ValueError, ZeroDivisionError) as error:
            messagebox.showerror("Could not open background", str(error), parent=window)
            background_mode.set(applied_background.get())
            return
        backgrounds["Image"] = bytes(pixels)
        apply_background("Image")

    def apply_background(mode):
        background_mode.set(mode)
        applied_background.set(mode)
        pick_background()

    def set_theme(value):
        global theme
        if value == "Custom":
            choose_bar_color()
            return
        with lock:
            theme = value
        selected_theme.set(value)

    def choose_bar_color():
        global theme, custom_bar_color
        rgb, _ = colorchooser.askcolor("#%02x%02x%02x" % custom_bar_color,
                                      parent=window, title="Seek bar color")
        if rgb is not None:
            with lock:
                custom_bar_color = tuple(map(round, rgb))
                theme = "Custom"
        selected_theme.set(theme)

    def preview_seek(event):
        now = time.monotonic()
        with lock:
            item = active(now)
            if not displayed or not show_media(display_mode, item, now):
                return
            if item["id"] != displayed["id"] or not item["seekable"]:
                return
            ratio = max(0, min(1, (event.x - (preview.winfo_width() - WIDTH) / 2 - BAR_LEFT) / (BAR_RIGHT - BAR_LEFT)))
            request_seek(item, item["duration"] * ratio, now)

    window = tk.Tk()
    window.title("NEXUS Playback Bar")
    window.geometry("740x725")
    window.minsize(688, 725)
    window.configure(bg=colors["bg"])
    tk.Label(window, text="NEXUS Playback Bar", font=("Segoe UI", 22, "bold"),
             fg=colors["text"], bg=colors["bg"]).pack(anchor="w", padx=24, pady=(18, 8))
    def section(label):
        tk.Label(window, text=label, font=("Segoe UI", 11, "bold"),
                 fg=colors["text"], bg=colors["bg"]).pack(anchor="w", padx=24, pady=(12, 4))

    section("NEXUS preview")
    preview = tk.Canvas(window, height=68, bg=colors["panel"], highlightthickness=0,
                        cursor="hand2")
    preview.pack(fill="x", padx=24)
    preview.bind("<Button-1>", preview_seek)
    preview.bind("<B1-Motion>", preview_seek)
    preview_item = preview.create_image(0, 34, anchor="center")
    preview.bind("<Configure>", lambda event: preview.coords(preview_item, event.width / 2, 34))
    preview_frame = None
    preview_image = None
    section("Display mode")
    modes = tk.Frame(window, bg=colors["bg"])
    modes.pack(anchor="w", padx=24, pady=(0, 4))
    selection = tk.StringVar(value=display_mode)
    for value, label in (("Auto", "Only while playing"), ("Media", "Show when paused too"), ("Corsair", "Stop drawing")):
        tk.Radiobutton(modes, text=label, variable=selection, value=value,
                       bg=colors["panel"], fg=colors["text"], selectcolor="#253b50",
                       activebackground="#253b50", activeforeground=colors["text"],
                       indicatoron=False, relief="flat", padx=12, pady=7,
                       command=lambda: select_display(selection.get())).pack(side="left", padx=(0, 6))
    section("Appearance")
    settings = tk.Frame(window, bg=colors["bg"])
    settings.pack(fill="x", padx=24, pady=(8, 0))
    tk.Label(settings, text="Bar color", bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=(0, 8))
    selected_theme = tk.StringVar(value=theme)
    palette = tk.OptionMenu(settings, selected_theme, "Automatic", *THEMES, "Custom", command=set_theme)
    palette.configure(bg=colors["panel"], fg=colors["text"], activebackground="#253b50",
                      activeforeground=colors["text"], highlightthickness=0, relief="flat", width=11)
    palette.pack(side="left")
    static_selection = tk.BooleanVar(value=static_page)
    tk.Checkbutton(settings, text="Limit updates to once a second", variable=static_selection,
                   bg=colors["bg"], fg=colors["muted"], selectcolor=colors["panel"],
                   activebackground=colors["bg"], activeforeground=colors["text"],
                   command=lambda: set_static_page(static_selection.get())).pack(side="left", padx=14)
    tk.Button(settings, text="Hide", command=lambda: hide(), bg=colors["panel"], fg=colors["text"],
              activebackground="#253b50", activeforeground=colors["text"], relief="flat",
              padx=12, pady=5).pack(side="right")
    background_settings = tk.Frame(window, bg=colors["bg"])
    background_settings.pack(anchor="w", padx=24, pady=(8, 0))
    tk.Label(background_settings, text="Background", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(0, 8))
    background_mode = tk.StringVar(value=background_initial)
    applied_background = tk.StringVar(value=background_initial)
    for mode in ("Thumbnail", "Black", "Color", "Image"):
        tk.Radiobutton(background_settings, text=mode, variable=background_mode, value=mode,
                       indicatoron=False, relief="flat", bg=colors["panel"], fg=colors["text"],
                       selectcolor="#253b50", activebackground="#253b50", activeforeground=colors["text"],
                       padx=9, command=lambda mode=mode: apply_background(mode) if mode in backgrounds
                       else (choose_color() if mode == "Color" else choose_image())).pack(side="left", padx=(0, 4))
    for label, command in (("Pick color…", choose_color), ("Pick image…", choose_image)):
        tk.Button(background_settings, text=label, command=command, relief="flat",
                  bg=colors["panel"], fg=colors["text"], activebackground="#253b50",
                  activeforeground=colors["text"]).pack(side="left", padx=(4, 0))
    brightness_settings = tk.Frame(window, bg=colors["bg"])
    brightness_settings.pack(fill="x", padx=24, pady=(6, 0))
    tk.Label(brightness_settings, text="Background brightness", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(0, 8))
    brightness_choice = tk.IntVar(value=background_brightness)
    brightness_label = tk.StringVar(value=f"{background_brightness}%")

    def set_brightness(value):
        global background_brightness
        with lock:
            background_brightness = round(float(value))
        brightness_label.set(f"{background_brightness}%")

    tk.Scale(brightness_settings, from_=0, to=100, orient="horizontal", showvalue=False,
             variable=brightness_choice, command=set_brightness, bg=colors["bg"],
             troughcolor=colors["panel"], highlightthickness=0,
             activebackground="#64baff").pack(side="left", fill="x", expand=True)
    tk.Label(brightness_settings, textvariable=brightness_label, width=5,
             bg=colors["bg"], fg=colors["text"]).pack(side="left")
    fit_settings = tk.Frame(window, bg=colors["bg"])
    fit_settings.pack(anchor="w", padx=24, pady=(8, 0))
    tk.Label(fit_settings, text="Thumbnail fit", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(0, 8))
    fit_choice = tk.StringVar(value=thumbnail_fit)

    def set_thumbnail_fit(value):
        global thumbnail_fit
        with lock:
            thumbnail_fit = value

    fit_menu = tk.OptionMenu(fit_settings, fit_choice, "Stretch", "Centered", command=set_thumbnail_fit)
    fit_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    fit_menu.pack(side="left")
    text_settings = tk.Frame(window, bg=colors["bg"])
    text_settings.pack(anchor="w", padx=24, pady=(8, 0))
    layout_choice = tk.StringVar(value=layout)
    alignment_choice = tk.StringVar(value=alignment)
    credit_choice = tk.StringVar(value=credit_mode)

    def apply_text_options(_):
        global layout, alignment, credit_mode
        with lock:
            layout = layout_choice.get()
            alignment = alignment_choice.get()
            credit_mode = credit_choice.get()

    for label, choice, options in (
        ("Title and time", layout_choice, ("Split", "Above", "Below", "Inline above", "Inline below")),
        ("Title alignment", alignment_choice, ("Left", "Center", "Right")),
    ):
        tk.Label(text_settings, text=label, bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=(0, 8))
        menu = tk.OptionMenu(text_settings, choice, *options, command=apply_text_options)
        menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
        menu.pack(side="left", padx=(0, 16))
    credit_settings = tk.Frame(window, bg=colors["bg"])
    credit_settings.pack(anchor="w", padx=24, pady=(8, 0))
    tk.Label(credit_settings, text="Channel / artist", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(0, 8))
    credit_menu = tk.OptionMenu(credit_settings, credit_choice, "Beside title", "Hidden",
                                "Opposite sides", command=apply_text_options)
    credit_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    credit_menu.pack(side="left")
    ink_settings = tk.Frame(window, bg=colors["bg"])
    ink_settings.pack(fill="x", padx=24, pady=(8, 0))
    ink_mode = "Auto contrast" if text_color is None else ("White" if text_color == (255, 255, 255) else "Custom")
    ink_choice = tk.StringVar(value=ink_mode)
    ink_level = tk.StringVar(value=f"{text_brightness}%")

    def choose_text_color(mode):
        global text_color, ink_mode
        color = None if mode == "Auto contrast" else (255, 255, 255)
        if mode == "Custom":
            rgb, _ = colorchooser.askcolor(parent=window, title="NEXUS text color")
            if rgb is None:
                ink_choice.set(ink_mode)
                return
            color = tuple(map(round, rgb))
        with lock:
            text_color = color
        ink_mode = mode

    def dim_text(value):
        global text_brightness
        with lock:
            text_brightness = round(float(value))
        ink_level.set(f"{text_brightness}%")

    tk.Label(ink_settings, text="Text color", bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=(0, 8))
    ink_menu = tk.OptionMenu(ink_settings, ink_choice, "White", "Auto contrast", "Custom", command=choose_text_color)
    ink_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    ink_menu.pack(side="left", padx=(0, 12))
    tk.Label(ink_settings, text="Text brightness", bg=colors["bg"], fg=colors["muted"]).pack(side="left")
    ink_scale = tk.Scale(ink_settings, from_=0, to=100, orient="horizontal", showvalue=False,
                         command=dim_text, bg=colors["bg"], troughcolor=colors["panel"], highlightthickness=0)
    ink_scale.set(text_brightness)
    ink_scale.pack(side="left", fill="x", expand=True)
    outlines = tk.Frame(window, bg=colors["bg"])
    outlines.pack(anchor="w", padx=24, pady=(8, 0))
    scrub_choice = tk.BooleanVar(value=live_scrubbing)

    def set_scrubbing():
        global live_scrubbing
        with lock:
            live_scrubbing = scrub_choice.get()

    tk.Checkbutton(window, text="Seek live while dragging on NEXUS", variable=scrub_choice,
                   command=set_scrubbing, bg=colors["bg"], fg=colors["text"],
                   selectcolor=colors["panel"], activebackground=colors["bg"],
                   activeforeground=colors["text"]).pack(anchor="w", padx=24)
    text_outline = tk.BooleanVar(value=outline_options[0])
    bar_outline = tk.BooleanVar(value=outline_options[1])
    thickness = tk.StringVar(value=outline_options[2])

    def apply_outlines(_=None):
        global outline_options
        with lock:
            outline_options = (text_outline.get(), bar_outline.get(), thickness.get())

    for label, choice in (("Black text outline", text_outline), ("Seek bar border", bar_outline)):
        tk.Checkbutton(outlines, text=label, variable=choice, command=apply_outlines,
                       bg=colors["bg"], fg=colors["text"], selectcolor=colors["panel"],
                       activebackground=colors["bg"], activeforeground=colors["text"]).pack(side="left", padx=(0, 12))
    tk.Label(outlines, text="Bar thickness", bg=colors["bg"], fg=colors["muted"]).pack(side="left")
    size_menu = tk.OptionMenu(outlines, thickness, "Thin", "Default", "Thick", command=apply_outlines)
    size_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    size_menu.pack(side="left")
    tk.Label(ink_settings, textvariable=ink_level, width=5, bg=colors["bg"], fg=colors["text"]).pack(side="left")
    text = tk.StringVar(value=status)
    tk.Label(window, textvariable=text, anchor="w", justify="left", wraplength=590,
             fg=colors["muted"], bg=colors["bg"]).pack(fill="x", padx=24, pady=(12, 0))

    def refresh():
        global preview_frame, preview_image
        now = time.monotonic()
        with lock:
            item = active(now)
            fresh = item and now - item["seen"] <= 3
            art = art_cache.get((item["artwork"], thumbnail_fit)) if fresh else None
            backdrop = art[0] if art and automatic_art else background
            color = bar_color(art)
            if fresh:
                position = playback_position(item, now)
                ratio = position / item["duration"]
                frame = (round(ratio * (WIDTH - 1)), item["seekable"], int(position),
                         int(item["duration"]), color, item["title"], backdrop, layout, alignment,
                         background_brightness, item["credit"], credit_mode, text_color, text_brightness,
                         *outline_options, int((now - item["title_started"]) * 40) if layout.startswith("Inline") else None)
            else:
                frame = (0, False, 0, 0, color, "Waiting for playback", backdrop, layout, alignment,
                         background_brightness, "", credit_mode, text_color, text_brightness, *outline_options)
        message = status
        if message == "Starting...":
            message = f"{startup_stage} ({int(now - stage_started)}s)"
        text.set(message + ("\n" + last_tap if last_tap else ""))
        if frame != preview_frame:
            rgba = render(*frame)
            rgb = bytearray(WIDTH * HEIGHT * 3)
            for channel in range(3):
                rgb[channel::3] = rgba[channel::4]
            header = f"P6 {WIDTH} {HEIGHT} 255\n".encode("ascii")
            preview_image = tk.PhotoImage(data=header + rgb, format="PPM")
            preview_frame = frame
            preview.itemconfigure(preview_item, image=preview_image)
        window.after(33 if held_seek else 100, refresh)

    def settings_snapshot():
        return ({key: globals()[key] for key in keys}, applied_background.get(),
                {name: backgrounds[name] for name in ("Color", "Image") if name in backgrounds})

    last_saved = settings_snapshot()

    def save_settings(show_feedback=False):
        global last_saved
        snapshot = settings_snapshot()
        if snapshot == last_saved and not show_feedback:
            return True
        data, mode, images = snapshot
        data = {**data, "background_mode": mode, "backgrounds": {
            name: base64.b64encode(pixels).decode("ascii") for name, pixels in images.items()
        }}
        try:
            preferences.save(data)
        except OSError as error:
            saved_label.set(f"Settings could not be saved: {error}")
            return False
        last_saved = snapshot
        if show_feedback:
            saved_label.set("Settings saved")
        return True

    def autosave():
        save_settings()
        window.after(400, autosave)

    def open_settings():
        window.deiconify()
        window.lift()
        window.focus_force()

    def hide():
        if save_settings():
            if tray and tray.ready:
                window.withdraw()
            else:
                window.iconify()

    def quit_app():
        if save_settings():
            if tray:
                tray.close()
            window.destroy()

    saved_label = tk.StringVar()
    footer = tk.Frame(window, bg=colors["bg"])
    footer.pack(fill="x", padx=24, pady=(8, 0))
    tk.Button(footer, text="Save settings", command=lambda: save_settings(True)).pack(side="left")
    tk.Label(footer, textvariable=saved_label, bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=10)
    tk.Button(footer, text="Quit app", command=quit_app).pack(side="right")
    window.update_idletasks()
    try:
        tray = Tray(window, open_settings, quit_app)
    except OSError:
        tray = None
    window.protocol("WM_DELETE_WINDOW", hide)
    window.after(400, autosave)

    threading.Thread(target=update, daemon=True).start()
    window.after(200, refresh)
    window.mainloop()

