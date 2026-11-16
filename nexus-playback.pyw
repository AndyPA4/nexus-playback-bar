"""Launch from a normal Windows desktop using Python 3."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import hmac
import base64
import json
import math
import os
import shutil
import uuid
from pathlib import Path
import threading
import time
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler
import tkinter as tk
from tkinter import simpledialog
from tkinter import colorchooser, filedialog, font as tkfont, messagebox, ttk

import preferences
import startup
from dashboard import (APP_SHORTCUTS, DASHBOARD_WIDGETS, DEFAULT_WIDGET_LAYOUT, WIDGET_COLORS,
                       WIDGET_GROUPS, GRAPH_SOURCES, IMAGE_WIDGET, IMAGE_EXTENSIONS, Dashboard, render_dashboard, widget_positions)
from tray import Tray, activate_existing

from nexus import (BAR_LEFT, BAR_RIGHT, DEFAULT_DISPLAY_FONT, DISPLAY_FONTS, TEXT_SIZES,
                   HEIGHT, THEMES, WIDTH, c, clock, w, draw, find_nexus, firmware_display,
                   input_report_length, kernel, open_nexus, render, write_frame)

def load_bridge_key():
    try:
        return Path(__file__).with_name("bridge-key.txt").read_text(encoding="ascii").strip()
    except OSError:
        return ""


KEY = load_bridge_key()
SOURCE_NAMES = {"www.youtube.com": "YouTube", "music.amazon.com": "Amazon Music",
                "www.primevideo.com": "Prime Video", "www.amazon.com": "Prime Video"}
lock = threading.Lock()
players = {}
desktop_item = None
local_player_item = None
local_player_seek_changed = threading.Event()
desktop_status = ""
desktop_sessions = ()
amazon_action = None
amazon_connected = False
amazon_status = ""
pending = {}
seek_preview = {}
held_seek = None
live_scrubbing = True
combine_rapid_taps = True
touch_changed = threading.Event()
playback_shutdown = threading.Event()
desktop_seek_changed = threading.Event()
DESKTOP_TAP_SETTLE = 0.12
DESKTOP_SEEK_INTERVAL = 0.35
DESKTOP_LIVE_SEEK_INTERVAL = 0.12
DESKTOP_SUSTAINED_SEEK_INTERVAL = 0.24
DESKTOP_PREVIEW_RESUME_DELAY = 0.5
desktop_preview_resume_at = 0
POSTER_PREFIX = "data:image/jpeg;base64,"
VIDEO_THUMBNAIL_PREFIX = "local-video:"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".m4v", ".mpeg", ".mpg")
WINDOWS_POWERSHELL = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
WINDOWS_MEDIA_SCRIPT = Path(__file__).with_name("windows-media.ps1")
local_thumbnail = None


def touch_fraction(x):
    return max(0, min(1, (x - BAR_LEFT) / (BAR_RIGHT - BAR_LEFT)))


def request_seek(item, target, now, drag_release=False, gesture=None, drag_motion=False):
    desktop = "timeline_anchor" in item
    if desktop:
        command = (target, now, drag_release, gesture)
    elif item.get("prime"):
        phase = "release" if drag_release else "motion" if drag_motion else "tap"
        command = (target, now, phase)
    else:
        command = (target, now)
    pending[item["id"]] = command
    seek_preview[item["id"]] = command
    if desktop:
        (local_player_seek_changed if item["id"][0] == "local_player" else desktop_seek_changed).set()
displayed = None
dashboard_frame = b""
idle_pages = (DEFAULT_WIDGET_LAYOUT,)
idle_page_index = 0
dashboard_visible = False
screen_override = None
idle_clock_format = "12-hour"
last_tap = ""
status = "Starting..."
startup_stage = "Starting the Chrome connection..."
stage_started = time.monotonic()
display_mode = "Auto"
static_page = False
SMOOTH_FRAME_INTERVAL = 1 / 60


def playback_position(item, now):
    if held_seek and held_seek[0] == item["id"]:
        return held_seek[1] * item["duration"]
    requested = seek_preview.get(item["id"])
    if requested and ("timeline_anchor" in item or now - requested[1] < 0.7
                      or (item.get("prime") and len(requested) == 3 and requested[2] == "release"
                          and now - requested[1] < 2.5)):
        return requested[0]
    if "timeline_anchor" in item:
        return windows_position(item, now)
    elapsed = max(0, min(1, now - item["seen"])) if item["playing"] else 0
    return max(0, min(item["duration"], item["position"] + elapsed * item.get("rate", 1)))
theme = "Automatic"
custom_bar_color = THEMES["Sky"]
BLACK = bytes((0, 0, 0, 255)) * WIDTH * HEIGHT
DESKTOP_BACKDROP = b"".join(
    bytes((12 + x * 12 // WIDTH, 22 + x * 23 // WIDTH, 36 + x * 39 // WIDTH, 255))
    for x in range(WIDTH)
) * HEIGHT
background = BLACK
background_brightness = 36
credit_mode = "Beside title"
text_color = (255, 255, 255)
text_brightness = 100
outline_options = (False, False, "Default")
automatic_art = True
thumbnail_fit = "Stretch"
layout, alignment = "Split", "Left"
time_format = "Minutes"
display_font = DEFAULT_DISPLAY_FONT
text_size = "Default"
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
        elif url.startswith(VIDEO_THUMBNAIL_PREFIX):
            output = subprocess.check_output(
                [str(WINDOWS_POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(WINDOWS_MEDIA_SCRIPT), "-VideoPath", url[len(VIDEO_THUMBNAIL_PREFIX):]],
                timeout=8, creationflags=subprocess.CREATE_NO_WINDOW, stderr=subprocess.DEVNULL,
            )
            data = base64.b64decode(output.strip(), validate=True)
        else:
            with build_opener(DirectArtwork()).open(url, timeout=5) as response:
                data = response.read(2_000_001)
        if 0 < len(data) <= 2_000_000:
            command = r"""
Add-Type -AssemblyName System.Drawing
$stream = New-Object IO.MemoryStream(,[Convert]::FromBase64String([Console]::In.ReadToEnd()))
$image = [Drawing.Image]::FromStream($stream)
if ($image.Width -gt 8192 -or $image.Height -gt 8192 -or
    [long]$image.Width * $image.Height -gt 20000000) { exit 1 }
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
    if mode == "Corsair" or item is None or screen_override == "dashboard":
        return False
    age = now - item["seen"]
    if mode == "Media" or screen_override == "media":
        return age <= 3
    if item.get("prime") and held_seek and held_seek[0] == item["id"]:
        return age <= 3
    if item["id"][0] == "amazon_music" and now - item["last_playing"] <= 1.25:
        return age <= 3
    if item.get("prime") and now - item.get("last_playing", float("-inf")) <= 0.65:
        return age <= 3
    return item["playing"] and age <= 3


def select_display(mode):
    global display_mode, displayed, last_tap, held_seek, screen_override
    with lock:
        display_mode = mode
        screen_override = None
        displayed = None
        pending.clear()
        seek_preview.clear()
        held_seek = None
        last_tap = ""
        touch_changed.set()


def startup_progress(message):
    global startup_stage, stage_started
    startup_stage = message
    stage_started = time.monotonic()


def prime_movie_title(title):
    prefix, separator, movie = title.partition(":")
    return movie.strip() if separator and prefix.strip().casefold() == "prime video" and movie.strip() else title


def matching_prime_title(left, right):
    title = prime_movie_title(left).casefold()
    return title not in ("", "prime video", "primevideo.com") and title == prime_movie_title(right).casefold()


def active(now):
    for key in list(players):
        if now - players[key]["seen"] > 75:
            del players[key]
            pending.pop(key, None)
            seek_preview.pop(key, None)
    candidates = list(players.values())
    if held_seek:
        held_player = players.get(held_seek[0])
        if held_player and held_player.get("prime") and now - held_player["seen"] <= 3:
            return held_player
    # An Edge app window can publish both a Windows session and extension data.
    duplicate_prime = desktop_item is not None and desktop_item["site"] == "Prime Video" and any(
        player.get("prime") is True and now - player["seen"] <= 3
        and matching_prime_title(player["title"], desktop_item["title"])
        for player in candidates
    )
    if desktop_item is not None and not duplicate_prime:
        candidates.append(desktop_item)
    if local_player_item is not None:
        candidates.append(local_player_item)
    return max(candidates, key=lambda item: (now - item["seen"] <= 3, item["playing"], item["played"]), default=None)


def read_windows_media():
    global desktop_item, desktop_status
    try:
        with subprocess.Popen(
            [str(WINDOWS_POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(WINDOWS_MEDIA_SCRIPT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        ) as helper:
            try:
                poll_windows_media(helper)
            finally:
                if helper.poll() is None:
                    try:
                        helper.stdin.close()
                        helper.wait(timeout=8)
                    except (OSError, subprocess.TimeoutExpired):
                        helper.terminate()
    except (OSError, ValueError, KeyError):
        pass
    finally:
        with lock:
            if desktop_item is not None:
                pending.pop(desktop_item["id"], None)
                seek_preview.pop(desktop_item["id"], None)
            desktop_item = None
            desktop_status = "Windows media controls are unavailable. Chrome playback still works."


def poll_windows_media(helper):
    global desktop_item, desktop_preview_resume_at, desktop_sessions
    global amazon_action, amazon_connected, amazon_status
    last_seek_at = float("-inf")
    live_burst_started = None
    seek_round_trip = 0
    last_accepted_drag = None
    while not playback_shutdown.is_set():
        with lock:
            desktop_seek_changed.clear()
            previous = desktop_item
            now = time.monotonic()
            command = {}
            if previous is not None:
                key = previous["id"]
                request = pending.get(key)
                current = active(now)
                if request:
                    if (now - request[1] >= 2 or current is not previous or not previous["seekable"]
                            or not show_media(display_mode, previous, now)):
                        pending.pop(key, None)
                        seek_preview.pop(key, None)
                    elif (request[2] and request[3] is not None
                          and last_accepted_drag == (key, request[3], request[0])):
                        pending.pop(key)
                        seek_preview.pop(key, None)
                        touch_changed.set()
                    elif now >= desktop_seek_ready_at(key, request, last_seek_at,
                                                      live_burst_started, seek_round_trip):
                        pending.pop(key)
                        dragging = live_scrubbing and held_seek is not None and held_seek[0] == key
                        if dragging:
                            if live_burst_started is None or now - last_seek_at >= 0.7:
                                live_burst_started = now
                        else:
                            live_burst_started = None
                        last_seek_at = now
                        command = {"generation": key[1], "target": request[0]}
            action = amazon_action
            amazon_action = None
        helper.stdin.write(json.dumps({**command, "amazonAction": action}) + "\n")
        helper.stdin.flush()
        line = helper.stdout.readline(2_700_000)
        if not line:
            return
        sample = json.loads(line)
        if "error" in sample:
            return
        now = time.monotonic()
        with lock:
            amazon_connected = sample.get("amazonConnected", amazon_connected)
            if "amazonStatus" in sample and amazon_action is None:
                amazon_status = sample["amazonStatus"]
            if "sessions" in sample:
                desktop_sessions = tuple(sample["sessions"])
            if command:
                seek_round_trip = now - last_seek_at
                desktop_preview_resume_at = now + DESKTOP_PREVIEW_RESUME_DELAY
            accepted_target = command.get("target") if sample.get("seekAccepted") else None
            item = windows_media_item(sample, previous, now, accepted_target)
            if sample.get("amazonTransition") and previous and previous["id"][0] == "amazon_music":
                item = previous
            if command:
                last_accepted_drag = None
                if accepted_target is not None and request[3] is not None and item and item["id"] == key:
                    last_accepted_drag = (key, request[3], accepted_target)
            if command and previous is not None:
                preview = seek_preview.get(previous["id"])
                if preview and preview == request:
                    seek_preview.pop(previous["id"], None)
            if previous is not None and (item is None or previous["id"] != item["id"]):
                pending.pop(previous["id"], None)
                seek_preview.pop(previous["id"], None)
            desktop_item = item
            delay = 0.2 if item and item["playing"] else 0.75
            if item:
                request = pending.get(item["id"])
                if request:
                    ready_at = desktop_seek_ready_at(item["id"], request, last_seek_at,
                                                     live_burst_started, seek_round_trip)
                    delay = min(delay, max(0.025, ready_at - now))
        desktop_seek_changed.wait(delay)


def desktop_seek_ready_at(key, request, last_seek_at, live_burst_started, seek_round_trip):
    dragging = held_seek is not None and held_seek[0] == key
    if dragging and not live_scrubbing:
        return float("inf")
    if request[2]:
        return max(request[1], last_seek_at + DESKTOP_LIVE_SEEK_INTERVAL)
    if dragging:
        interval = DESKTOP_LIVE_SEEK_INTERVAL
        burst_continues = live_burst_started is not None and request[1] - last_seek_at < 0.7
        if burst_continues and last_seek_at - live_burst_started >= 0.85:
            interval = DESKTOP_SUSTAINED_SEEK_INTERVAL
        if burst_continues and seek_round_trip >= DESKTOP_LIVE_SEEK_INTERVAL:
            interval = max(interval, seek_round_trip + min(0.12, seek_round_trip / 2))
        return max(request[1], last_seek_at + min(0.6, interval))
    if not combine_rapid_taps:
        return request[1]
    return max(request[1] + DESKTOP_TAP_SETTLE, last_seek_at + DESKTOP_SEEK_INTERVAL)


def windows_position(item, now):
    elapsed = max(0, now - item["timeline_anchor"]) if item["playing"] else 0
    return min(item["duration"], item["position"] + elapsed * item["rate"])


def pause_settings_preview(item, now):
    if item is None or "timeline_anchor" not in item:
        return False
    key = item["id"]
    return ((held_seek is not None and held_seek[0] == key)
            or key in seek_preview or now < desktop_preview_resume_at)


def windows_app_name(app):
    source = app.split("!", 1)[0].removesuffix(".exe")
    for identifier, label in (("amazonmusic", "Amazon Music"), ("primevideo", "Prime Video"),
                              ("spotify", "Spotify"), ("soundcloud", "SoundCloud")):
        if identifier in source.casefold():
            return label
    return source


def windows_media_item(sample, previous, now, accepted_target=None):
    if "app" not in sample:
        return None
    key = (sample.get("source", "windows"), sample["generation"])
    if previous is None or previous["id"] != key:
        previous = {}
    source = windows_app_name(sample["app"])
    title = " ".join((sample["title"] or "").split())[:160] or source
    if source == "Prime Video":
        title = prime_movie_title(title)
    credit = " ".join((sample["credit"] or "").split())[:100]
    duration = max(0, sample["duration"])
    playing = sample["playing"]
    rate = max(0, min(16, sample["rate"]))
    raw_position = max(0, min(duration, sample["position"]))
    position, anchor = raw_position, now
    raw_changed = now
    if accepted_target is not None:
        position = max(0, min(duration, accepted_target))
    elif previous and playing and previous["playing"] and duration == previous["duration"]:
        last_raw = previous["raw_position"]
        estimated = windows_position(previous, now)
        if last_raw == raw_position:
            raw_changed = previous["raw_changed"]
            position = estimated
            rate = previous["rate"] if rate == previous["reported_rate"] else rate
        else:
            expected_change = (now - previous["raw_changed"]) * previous["rate"]
            if last_raw - 1.5 <= raw_position <= last_raw + expected_change + 2:
                # Correct drift gradually without making the clock run backward.
                position = estimated
                rate *= 1 + max(-0.15, min(0.15, (raw_position - estimated) / 8))
    elif previous and not playing and previous["playing"] and raw_position == previous["raw_position"]:
        position = windows_position(previous, now)
    played = previous.get("played", 0)
    if playing and not previous.get("playing"):
        played = time.time() * 1000
    artwork = previous.get("artwork", "")
    if "artwork" in sample:
        artwork = POSTER_PREFIX + sample["artwork"] if sample["artwork"] else ""
    if "artworkUrl" in sample and not artwork.startswith(POSTER_PREFIX):
        url = sample["artworkUrl"]
        if isinstance(url, str) and len(url) <= 2048:
            try:
                parsed = urlsplit(url)
                host = parsed.hostname or ""
                if (parsed.scheme == "https" and not parsed.username and parsed.port in (None, 443)
                        and host.endswith((".media-amazon.com", ".ssl-images-amazon.com"))):
                    artwork = url
            except ValueError:
                pass
    path = Path(sample.get("path") or sample["title"] or "")
    file_thumbnail = ""
    if path.is_absolute() and len(path.drive) == 2 and path.suffix.lower() in VIDEO_EXTENSIONS:
        file_thumbnail = VIDEO_THUMBNAIL_PREFIX + str(path)
    return {
        "id": key, "site": source, "title": title,
        "credit": credit if credit.casefold() != title.casefold() else "",
        "duration": duration, "position": position, "raw_position": raw_position,
        "timeline_anchor": anchor, "raw_changed": raw_changed,
        "playing": playing, "seekable": sample["seekable"] and duration > 0,
        "last_playing": now if playing else previous.get("last_playing", float("-inf")),
        "rate": rate, "reported_rate": sample["rate"], "played": played, "seen": now,
        "title_started": previous.get("title_started", now), "artwork": artwork,
        "file_thumbnail": file_thumbnail,
    }


def read_local_player():
    global local_player_item
    directory = Path(os.environ["LOCALAPPDATA"]) / "NexusMediaPlayer" / "bridge"
    endpoint = None
    last_seek = float("-inf")
    sent_request = None
    sent_id = None
    while True:
        local_player_seek_changed.clear()
        now = time.monotonic()
        state = None
        try:
            # Prefer the most recently updated player with a readable state.
            candidates = sorted(directory.glob("*.state.json"),
                                key=lambda path: path.stat().st_mtime, reverse=True)
            for candidate in candidates:
                if time.time() - candidate.stat().st_mtime > 2:
                    break
                try:
                    sample = json.loads(candidate.read_text(encoding="utf-8"))
                    if sample["duration"] > 0 and isinstance(sample["path"], str):
                        endpoint, state = candidate, sample
                        break
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        except OSError:
            pass
        command = None
        delay = 0.1 if state else 0.5
        with lock:
            previous = local_player_item
            key = ("local_player", (endpoint.name, state["generation"])) if state else None
            if previous and previous["id"] != key:
                pending.pop(previous["id"], None)
                seek_preview.pop(previous["id"], None)
                sent_request = sent_id = None
                last_seek = float("-inf")
            if state:
                path = Path(state["path"])
                sample = dict(source="local_player", generation=key[1], app="NEXUS Media Player",
                              title=path.stem, path=str(path), credit="", duration=state["duration"],
                              position=state["position"], playing=not state["paused"], seekable=True,
                              rate=state["rate"])
                acknowledged = sent_id is not None and state.get("ack") == sent_id
                target = state.get("seekPosition") if acknowledged else None
                local_player_item = windows_media_item(sample, previous, now, target)
                if sent_id is not None and (acknowledged or now - last_seek > 2):
                    if seek_preview.get(key) == sent_request:
                        seek_preview.pop(key, None)
                    sent_request = sent_id = None
                request = pending.get(key)
                if request:
                    if now - request[1] > 2 or active(now) is not local_player_item or not show_media(display_mode, local_player_item, now):
                        pending.pop(key, None)
                        seek_preview.pop(key, None)
                    elif sent_id is None:
                        ready_at = desktop_seek_ready_at(key, request, last_seek, None, 0)
                        if now >= ready_at:
                            sent_request = pending.pop(key)
                            sent_id = str(time.time_ns())
                            last_seek = now
                            command = dict(id=sent_id, generation=state["generation"], target=request[0],
                                           created=time.time())
                        else:
                            delay = min(delay, max(0.02, ready_at - now))
            else:
                local_player_item = None
        if command is not None:
            try:
                command_path = endpoint.with_name(endpoint.name.replace(".state.json", ".seek.json"))
                command_path.write_text(json.dumps(command) + "\n", encoding="utf-8")
            except OSError:
                with lock:
                    if seek_preview.get(key) == sent_request:
                        seek_preview.pop(key, None)
                sent_request = sent_id = None
        local_player_seek_changed.wait(delay)


def artwork_source(item):
    if local_thumbnail and local_thumbnail[0] == item["id"]:
        return local_thumbnail[1]
    if item["site"] == "Prime Video" and not item["artwork"]:
        cover = max((player for player in players.values() if player.get("prime")
                     and player["artwork"] and matching_prime_title(item["title"], player["title"])),
                    key=lambda player: player["seen"], default=None)
        if cover:
            return cover["artwork"]
    if not item["artwork"] and "timeline_anchor" in item:
        return item["file_thumbnail"]
    return item["artwork"]


def prime_seek_command(item, request, now):
    """Return whether to consume a queued request and whether to send it."""
    target, queued_at = request[:2]
    phase = request[2] if len(request) == 3 else "tap"
    if now - queued_at >= 2:
        return True, False
    if phase == "tap":
        return True, True
    last_target, last_sent = item["prime_seek_sent"]
    elapsed = now - last_sent
    if last_target is not None and abs(target - last_target) < 0.05 and elapsed < 1:
        if abs(target - item["position"]) < 0.35 or (item["seeking"] and elapsed < 0.22):
            return True, False
    # Always commit the release immediately. While dragging, keep only the
    # latest position and briefly space out requests if the player is seeking.
    if phase == "motion" and elapsed < (0.18 if item["seeking"] else 0.10):
        return False, False
    return True, True


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        global last_tap
        supplied = self.headers.get("X-Nexus-Bridge", "")
        if self.path != "/media" or not KEY or not hmac.compare_digest(supplied.encode(), KEY.encode()):
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
            item["prime"] = item.get("prime") is True and (
                site == "www.amazon.com" or site == "primevideo.com" or site.endswith(".primevideo.com")
            )
            item["seeking"] = item.get("seeking") is True
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
            elif isinstance(artwork, str) and len(artwork) <= (1800 if item["prime"] else 800):
                parsed = urlsplit(artwork)
                prime_cdn = item["prime"] and parsed.netloc.endswith(
                    (".media-amazon.com", ".ssl-images-amazon.com")
                ) and parsed.hostname == parsed.netloc
                if parsed.scheme == "https" and (prime_cdn or parsed.netloc in (
                    "i.ytimg.com", "m.media-amazon.com", "images-na.ssl-images-amazon.com",
                    "images.amazon.com", "images-eu.ssl-images-amazon.com",
                    "i.scdn.co", "mosaic.scdn.co", "image-cdn-ak.spotifycdn.com",
                    "image-cdn-fa.spotifycdn.com", "image-cdn-spotify.spotifycdn.com",
                    "i1.sndcdn.com", "is1-ssl.mzstatic.com", "is2-ssl.mzstatic.com",
                    "is3-ssl.mzstatic.com", "is4-ssl.mzstatic.com", "is5-ssl.mzstatic.com",
                )):
                    item["artwork"] = artwork
        except (TypeError, ValueError, KeyError, OverflowError):
            self.send_error(400)
            return
        item["seen"] = now = time.monotonic()
        with lock:
            previous_item = players.get(key, {})
            if item["prime"]:
                item["title"] = prime_movie_title(item["title"])
                if item["title"] == "Prime Video" and previous_item.get("prime"):
                    item["title"] = previous_item["title"]
                if not item["artwork"] and matching_prime_title(item["title"], previous_item.get("title", "")):
                    item["artwork"] = previous_item["artwork"]
                item["last_playing"] = now if item["playing"] else previous_item.get("last_playing", float("-inf"))
            item["prime_seek_sent"] = previous_item.get("prime_seek_sent", (None, float("-inf")))
            same_text = (previous_item.get("title"), previous_item.get("credit")) == (item["title"], item["credit"])
            item["title_started"] = previous_item.get("title_started", now) if same_text else now
            players[key] = item
            current = active(now)
            if item["prime"] and current is item:
                if item.get("seekResult") == "confirmed":
                    last_tap = "Prime Video seek confirmed"
                elif item.get("seekResult") == "unconfirmed":
                    last_tap = "Prime Video did not confirm the seek"
            command = None
            if not current or current["id"] != key or not show_media(display_mode, current, now):
                pending.pop(key, None)
            elif item["prime"]:
                request = pending.get(key)
                if request:
                    consume, send = prime_seek_command(item, request, now)
                    if not item["seekable"] and now - request[1] < 2:
                        consume = send = False
                    if consume:
                        pending.pop(key, None)
                    if send:
                        command = request
                        item["prime_seek_sent"] = (request[0], now)
            else:
                command = pending.pop(key, None)
            requested = seek_preview.get(key)
            if requested and key not in pending:
                release = item["prime"] and len(requested) == 3 and requested[2] == "release"
                timeout, tolerance = (2.5, 1.5) if release else (0.7, 0.35)
                if now - requested[1] >= timeout or (
                    command is None and abs(item["position"] - requested[0]) < tolerance
                ):
                    seek_preview.pop(key, None)
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
    global last_tap, held_seek, idle_page_index, screen_override
    packet = c.create_string_buffer(size)
    count = w.DWORD()
    start = None
    while kernel.ReadFile(handle, packet, size, c.byref(count), None):
        data = packet.raw[:count.value]
        if len(data) < 10 or data[:3] != b"\x01\x02\x21":
            continue
        now = time.monotonic()
        fingers = data[5]
        if fingers:
            x = int.from_bytes(data[6:8], "little")
            if not 0 <= x < WIDTH:
                continue
            with lock:
                if start is None:
                    start = x
                    gesture = object()
                    last_live_seek = 0
                    previous_x = None
                    last_x = x
                    second_start = None
                    two_finger_sideways = False
                    dashboard_gesture = dashboard_visible
                    page = idle_page_index
                    page_layout = idle_pages[page]
                    drag_player = displayed["id"] if displayed else None
                last_x = x
                if fingers >= 2:
                    center = x
                    if len(data) >= 21:
                        other_x = int.from_bytes(data[19:21], "little")
                        if 0 <= other_x < WIDTH:
                            center = (x + other_x) // 2
                    if second_start is None:
                        second_start = center
                    two_finger_sideways |= abs(center - second_start) >= 65
                    held_seek = None
                    if drag_player:
                        pending.pop(drag_player, None)
                        seek_preview.pop(drag_player, None)
                elif (second_start is None and drag_player and displayed and x != previous_x
                      and display_mode != "Corsair"):
                    current = active(now)
                    if (current and current["id"] == displayed["id"] == drag_player
                            and now - current["seen"] <= 3 and (current["seekable"] or (current.get("prime") and held_seek))):
                        fraction = touch_fraction(x)
                        target = fraction * current["duration"]
                        held_seek = (drag_player, fraction)
                        # Wait for visible horizontal travel before sending a live seek so
                        # a second finger can land before any playback command is sent.
                        if live_scrubbing and abs(x - start) >= 9:
                            if "timeline_anchor" in current:
                                request_seek(current, target, now, gesture=gesture)
                            elif current.get("prime") or now - last_live_seek >= 0.15:
                                request_seek(current, target, now, drag_motion=True)
                                last_live_seek = now
                        previous_x = x
                touch_changed.set()
        elif start is not None:
            shortcut = None
            with lock:
                current = active(now)
                delta = last_x - start
                if display_mode != "Corsair":
                    if second_start is not None:
                        if not two_finger_sideways:
                            screen_override = "media" if dashboard_gesture else "dashboard"
                            pending.clear()
                            seek_preview.clear()
                            last_tap = ""
                    elif dashboard_gesture and dashboard_visible and not show_media(display_mode, current, now):
                        if page == idle_page_index and page_layout is idle_pages[page]:
                            if abs(delta) >= 65:
                                idle_page_index = (page + (1 if delta < 0 else -1)) % len(idle_pages)
                            elif abs(delta) < 9:
                                shortcut = next(((APP_SHORTCUTS.get(name) or path) for name, _, left, width, _, path
                                                 in widget_positions(page_layout)
                                                 if (name in APP_SHORTCUTS or name == "Custom app")
                                                 and left <= last_x < left + width), None)
                    elif (not dashboard_gesture and displayed and current and now - current["seen"] <= 3
                          and current["id"] == displayed["id"] == drag_player
                          and (current["seekable"] or (current.get("prime") and held_seek))):
                        target = touch_fraction(last_x) * current["duration"]
                        request_seek(current, target, now, drag_release=abs(delta) >= 9, gesture=gesture)
                        last_tap = f"Seek requested: {clock(target)}"
                held_seek = None
                touch_changed.set()
            if shortcut:
                try:
                    windows = Path(os.environ["SystemRoot"])
                    if not shortcut.endswith(":") and not Path(shortcut).is_absolute():
                        shortcut = str((windows if shortcut == "explorer.exe" else windows / "System32") / shortcut)
                    os.startfile(shortcut)
                except OSError:
                    pass
            start = None
    with lock:
        held_seek = None
        touch_changed.set()
    raise c.WinError(c.get_last_error())


def update():
    global displayed, status, dashboard_frame, dashboard_visible
    handle = None
    dashboard = None
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
            screen_blank = False
            firmware_stopped = False
            next_dashboard = 0
            next_dashboard_sample = 0
            dashboard_snapshot = ({}, {})
            dashboard_settings = None
            while not playback_shutdown.is_set() and (reader.ident is None or reader.is_alive()):
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
                    waiting_for_media = not visible and screen_override == "media" and mode != "Corsair"
                    dashboard_visible = not visible and mode != "Corsair" and not waiting_for_media
                    arrangement, title_edge = layout, alignment
                    clock_format, frame_font, frame_size = time_format, display_font, text_size
                    page_index, page_count = idle_page_index, len(idle_pages)
                    widgets, wall_clock = idle_pages[page_index], idle_clock_format
                    credit_style = credit_mode
                    ink, ink_brightness = text_color, text_brightness
                    borders = outline_options
                    if visible and (automatic_art or theme == "Automatic"):
                        url = artwork_source(item)
                        key = (url, thumbnail_fit)
                        art = art_cache.get(key)
                        if automatic_art:
                            if art and art[0] is not BLACK:
                                backdrop = art[0]
                            elif "timeline_anchor" in item:
                                backdrop = DESKTOP_BACKDROP
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
                    if mode == "Corsair":
                        if not screen_blank:
                            try:
                                firmware_display(handle, 15)
                            except (OSError, RuntimeError):
                                pass
                            write_frame(handle, bytearray(BLACK))
                            screen_blank = True
                        status = "NEXUS display is black."
                    else:
                        settings = (widgets, wall_clock, frame_font, frame_size, brightness,
                                    ink, ink_brightness, borders[0], page_index, page_count, waiting_for_media)
                        animated = dashboard_visible and any(
                            slot[0] == IMAGE_WIDGET and len(slot) == 5 and slot[4].lower().endswith(".gif")
                            for slot in widgets)
                        if screen_blank or now >= next_dashboard or settings != dashboard_settings:
                            if dashboard is None:
                                dashboard = Dashboard()
                            if not firmware_stopped:
                                try:
                                    firmware_display(handle, 15)
                                except (OSError, RuntimeError):
                                    pass
                                firmware_stopped = True
                            if waiting_for_media:
                                frame = render(0, False, 0, 0, "Sky", "Waiting for media", BLACK,
                                               "Split", "Center", display_font=frame_font, text_size=frame_size,
                                               text_color=ink, text_brightness=ink_brightness)
                            else:
                                if now >= next_dashboard_sample or settings != dashboard_settings:
                                    dashboard_snapshot = dashboard.sample(widgets) if widgets else ({}, {})
                                    next_dashboard_sample = now + 1
                                frame = render_dashboard(dashboard_snapshot,
                                                         frame_font, widgets, wall_clock, frame_size, brightness,
                                                         ink, ink_brightness, borders[0], page_index, page_count)
                            rendered = bytes(frame)
                            if screen_blank or rendered != dashboard_frame or dashboard_settings is None:
                                write_frame(handle, frame)
                            dashboard_frame = rendered
                            next_dashboard = now + (0.04 if animated else 1)
                            dashboard_settings = settings
                            if reader.ident is None:
                                reader.start()
                        screen_blank = False
                        status = "Waiting for media" if waiting_for_media else "Dashboard"
                    touch_changed.wait(0.03 if dashboard_visible and animated else 0.2)
                    continue
                screen_blank = False
                next_dashboard = next_dashboard_sample = 0
                dashboard_settings = None
                if not firmware_stopped:
                    try:
                        firmware_display(handle, 15)
                    except (OSError, RuntimeError):
                        pass
                    firmware_stopped = True
                limited = quiet and not dragging
                interval = 1 if limited else SMOOTH_FRAME_INTERVAL
                if limited != previous_quiet:
                    next_draw = now
                    previous_quiet = limited
                ratio = position / item["duration"] if item["duration"] else 0
                marker = round(max(0, min(1, ratio)) * (WIDTH - 1))
                frame = (marker, item["seekable"], int(position), int(item["duration"]),
                         color, item["title"], backdrop, arrangement, title_edge, brightness,
                         item["credit"], credit_style, ink, ink_brightness, *borders,
                         int((now - item["title_started"]) * 40), clock_format, frame_font, frame_size)
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
                    status = item["site"]
                    if item["duration"]:
                        status += f"  |  {item['position']:.0f} / {item['duration']:.0f} seconds"
                    if time.monotonic() - item["seen"] > 3:
                        status += "  |  Waiting for a fresh playback update"
                    if not item["seekable"]:
                        status += "  |  Seeking unavailable"
                else:
                    status = "Connected to NEXUS. Waiting for Chrome or Windows playback."
                remaining = next_draw - time.monotonic()
                touch_changed.wait(0.016 if dragging else min(remaining, 0.016) if remaining > 0 else 0.016)
    except Exception as error:
        status = f"Playback bar stopped: {error}"
    finally:
        dashboard_visible = False
        if dashboard is not None:
            dashboard.close()
        if handle is not None:
            if playback_shutdown.is_set():
                try:
                    firmware_display(handle, 13, 1, 1)
                except (OSError, RuntimeError) as error:
                    status = f"Could not start the NEXUS firmware animation: {error}"
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    explorer_video = sys.argv[2] if len(sys.argv) == 3 and sys.argv[1] == "--open-video" else None
    if activate_existing(explorer_video):
        raise SystemExit
    colors = {"bg": "#0c121a", "panel": "#141e2a", "text": "#ebf4fe", "muted": "#99acc2"}
    action_button_style = {
        "bg": colors["panel"], "fg": colors["text"], "activebackground": "#253b50",
        "activeforeground": colors["text"], "relief": "flat", "borderwidth": 0,
        "highlightbackground": colors["bg"], "highlightcolor": colors["muted"],
        "highlightthickness": 1, "padx": 12, "pady": 5,
    }
    backgrounds = {"Thumbnail": BLACK, "Black": BLACK}
    saved = preferences.load()
    keys = ("display_mode", "static_page", "theme", "custom_bar_color",
            "background_brightness", "thumbnail_fit", "layout", "alignment",
            "credit_mode", "text_color", "text_brightness", "outline_options", "live_scrubbing",
            "time_format", "combine_rapid_taps", "display_font", "text_size",
            "idle_pages", "idle_page_index", "idle_clock_format")
    def load_idle_page(page):
        if not isinstance(page, list):
            return ()
        slots = []
        network_added = False
        for slot in page[:8]:
            if not (isinstance(slot, list) and len(slot) in (2, 3, 4, 5) and isinstance(slot[0], str)
                    and type(slot[1]) is int and 1 <= slot[1] <= 5):
                continue
            name, width = slot[:2]
            if name in ("Download speed", "Upload speed"):
                name = "Empty" if network_added else "Upload + download"
            network_added |= name == "Upload + download"
            if name == IMAGE_WIDGET:
                if (len(slot) == 5 and isinstance(slot[4], str) and Path(slot[4]).is_absolute()
                        and Path(slot[4]).suffix.casefold() in IMAGE_EXTENSIONS):
                    light = max(0, min(100, slot[2])) if type(slot[2]) is int else 100
                    slots.append((name, width, light, "Stretch" if slot[3] == "Stretch" else "Fit", slot[4]))
                else:
                    slots.append(("Empty", width, "Auto", "Bar"))
                continue
            if name == "Custom app":
                if (len(slot) == 5 and isinstance(slot[4], str) and Path(slot[4]).is_absolute()
                        and Path(slot[4]).suffix.casefold() in (".exe", ".lnk")):
                    label = slot[2][:64] if isinstance(slot[2], str) else Path(slot[4]).stem[:64]
                    slots.append((name, width, label, "Stretch" if slot[3] == "Stretch" else "Fit", slot[4]))
                else:
                    slots.append(("Empty", width, "Auto", "Bar"))
                continue
            if name in DASHBOARD_WIDGETS or name == "Empty":
                color = slot[2] if len(slot) >= 3 and isinstance(slot[2], str) and slot[2] in WIDGET_COLORS else "Auto"
                slots.append((name, width, color, "Line" if len(slot) == 4 and slot[3] == "Line" else "Bar"))
        return tuple(slots)

    if isinstance(saved.get("idle_pages"), list) and saved["idle_pages"]:
        idle_pages = tuple(load_idle_page(page) for page in saved["idle_pages"])
    elif isinstance(saved.get("idle_layout"), list):
        idle_pages = (load_idle_page(saved["idle_layout"]),)
    elif isinstance(saved.get("idle_widgets"), list) and len(saved["idle_widgets"]) == 5:
        names = ["CPU usage", "GPU usage", "RAM usage", "Time", "Date"]
        migrated = [name for name, enabled in zip(names, saved["idle_widgets"]) if enabled is True]
        if "Time" in migrated and "Date" in migrated:
            migrated[-2:] = ["Clock and date"]
        idle_pages = (tuple((name, 3, "Auto", "Bar") for name in migrated),)
    for key in keys:
        if key not in saved:
            continue
        value = saved[key]
        if key == "time_format" and value not in ("Minutes", "Hours"):
            continue
        if key == "idle_clock_format" and value not in ("12-hour", "24-hour"):
            continue
        if key == "idle_pages":
            continue
        if key == "idle_page_index":
            idle_page_index = max(0, min(len(idle_pages) - 1, value)) if type(value) is int else 0
            continue
        if key == "display_font" and value not in DISPLAY_FONTS:
            continue
        if key == "text_size" and value not in TEXT_SIZES:
            continue
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

    def choose_video_thumbnail():
        global local_thumbnail
        with lock:
            item = active(time.monotonic())
        if item is None or "timeline_anchor" not in item:
            messagebox.showinfo("Video thumbnail", "Start playing a video in a Windows app first.", parent=window)
            return
        path = filedialog.askopenfilename(
            parent=window, title="Choose the video you are playing",
            filetypes=[("Video files", " ".join("*" + ext for ext in VIDEO_EXTENSIONS))],
        )
        if path:
            with lock:
                if desktop_item is None or desktop_item["id"] != item["id"]:
                    return
                local_thumbnail = (item["id"], VIDEO_THUMBNAIL_PREFIX + path)
            apply_background("Thumbnail")

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
    window.update_idletasks()
    parent_window = c.windll.user32.GetAncestor
    parent_window.argtypes = (w.HWND, w.UINT)
    parent_window.restype = w.HWND
    title_bar = c.windll.dwmapi.DwmSetWindowAttribute
    title_bar.argtypes = (w.HWND, w.DWORD, c.c_void_p, w.DWORD)
    hwnd = parent_window(window.winfo_id(), 2)
    for attribute, value in ((20, 1), (35, 0x1a120c), (36, 0xffffff)):
        color_value = c.c_int(value)
        title_bar(hwnd, attribute, c.byref(color_value), c.sizeof(color_value))
    window.withdraw()
    window.title("NEXUS Playback Bar")
    window.geometry("800x840")
    window.configure(bg=colors["bg"])
    pixel_font_loaded = False
    try:
        font_data = Path(__file__).with_name("NexusPixel.ttf").read_bytes()
        add_font = c.windll.gdi32.AddFontMemResourceEx
        add_font.argtypes = (c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(w.DWORD))
        add_font.restype = w.HANDLE
        loaded_count = w.DWORD()
        pixel_font_loaded = bool(add_font(font_data, len(font_data), None, c.byref(loaded_count)))
    except OSError:
        pass
    installed_fonts = {family.casefold() for family in tkfont.families(window)}
    available_fonts = tuple(name for name in DISPLAY_FONTS if name == DEFAULT_DISPLAY_FONT or name.casefold() in installed_fonts)
    if display_font not in available_fonts:
        display_font = available_fonts[0] if available_fonts else DEFAULT_DISPLAY_FONT
    ui_font = tkfont.nametofont("TkDefaultFont")
    settings_family = "Consolas" if display_font == DEFAULT_DISPLAY_FONT and not pixel_font_loaded else display_font
    ui_font.configure(family=settings_family, size=10)
    heading_font = tkfont.Font(family=settings_family, size=19, weight="bold")
    window.option_add("*Font", "TkDefaultFont")
    tk.Label(window, text="NEXUS Playback Bar", font=heading_font,
             fg=colors["text"], bg=colors["bg"]).pack(anchor="w", padx=24, pady=(16, 8))

    def animate_button(button):
        normal = button.cget("background")
        hover = "#253b50"
        frame = None
        choice = button.winfo_class() == "Radiobutton" and not button.cget("indicatoron")
        selected = button.cget("selectcolor") if choice else None

        def fade(target):
            nonlocal frame
            if frame is not None:
                button.after_cancel(frame)
                frame = None
            if button.cget("state") == "disabled":
                return
            start = button.winfo_rgb(button.cget("background"))
            finish = button.winfo_rgb(target)
            select_start = button.winfo_rgb(button.cget("selectcolor")) if choice else None
            select_end = button.winfo_rgb("#35516d" if target == hover else selected) if choice else None

            def step(index):
                nonlocal frame
                frame = None
                progress = 1 - (1 - index / 8) ** 2
                def blend(first, last):
                    return "#{:02x}{:02x}{:02x}".format(*(
                        round((before + (after - before) * progress) / 257)
                        for before, after in zip(first, last)))

                color = blend(start, finish)
                button.configure(background=color, activebackground=color)
                if choice:
                    button.configure(selectcolor=blend(select_start, select_end))
                if index < 8:
                    frame = button.after(16, step, index + 1)

            step(1)

        button.configure(activebackground=normal)
        button.bind("<Enter>", lambda event: fade(hover), add="+")
        button.bind("<Leave>", lambda event: fade(normal), add="+")
        return button

    def settings_button(parent, **options):
        return animate_button(tk.Button(parent, **{**action_button_style, **options}))

    def toggle_amazon_music():
        global amazon_action, amazon_status
        with lock:
            connected = amazon_connected
        if not connected and not messagebox.askokcancel(
            "Connect Amazon Music",
            "NPB will restart the Amazon Music Store app. While connected, other programs on this PC "
            "can access Amazon Music's debugging interface. Disconnecting restarts it normally.",
            parent=window,
        ):
            return
        with lock:
            amazon_action = "disconnect" if connected else "connect"
            amazon_status = "Restarting Amazon Music…"
            desktop_seek_changed.set()
        amazon_button.configure(state="disabled")

    def show_windows_sessions():
        with lock:
            error = desktop_status
            sessions = desktop_sessions
            current = desktop_item
        lines = []
        if current:
            lines.append(f'Current desktop source: {current["site"]} — {current["title"]}\n')
        for session in sessions:
            app = session["app"]
            if session["browser"]:
                capability = "browser session; use the Chrome extension for Chrome playback"
            elif session["seekable"]:
                capability = "timeline and seeking available"
            elif session["duration"] > 0:
                capability = "timeline available; seeking unavailable"
            else:
                capability = "no timeline reported"
            lines.append(f'{windows_app_name(app)} — {session["status"]} ({capability})')
        message = error or "\n".join(lines) or "No Windows media sessions detected. Start playback and check again."
        messagebox.showinfo("Windows media sessions", message, parent=window)

    preview = tk.Canvas(window, height=68, bg=colors["panel"], highlightthickness=0,
                        cursor="hand2")
    preview.pack(fill="x", padx=24)
    preview.bind("<Button-1>", preview_seek)
    preview.bind("<B1-Motion>", preview_seek)
    preview_item = preview.create_image(0, 34, anchor="center")
    preview.bind("<Configure>", lambda event: preview.coords(preview_item, event.width / 2, 34))
    preview_frame = None
    preview_image = None
    body = tk.Frame(window, bg=colors["bg"])
    body.pack(fill="x", padx=24)
    section_font = tkfont.Font(family=settings_family, size=11, weight="bold")
    pages = []

    def section(parent, label):
        page = tk.Frame(parent, bg=colors["bg"])
        page.pack(fill="x", pady=(7, 0))
        tk.Label(page, text=label, font=section_font, fg=colors["text"],
                 bg=colors["bg"]).pack(anchor="w", pady=(0, 3))
        pages.append(page)
        return page

    general = section(body, "General settings")
    app_page = general
    tab_bar = tk.Frame(body, bg=colors["bg"])
    tab_bar.pack(fill="x", pady=(10, 0))
    tab_body = tk.Frame(body, bg=colors["bg"])
    tab_body.pack(fill="x")
    playback_tab = tk.Frame(tab_body, bg=colors["bg"])
    idle_tab = tk.Frame(tab_body, bg=colors["bg"])
    active_tab = tk.StringVar(value="Playback")

    def select_tab():
        for name, page in (("Playback", playback_tab), ("Idle", idle_tab)):
            if active_tab.get() == name:
                page.pack(fill="x")
            else:
                page.pack_forget()
        fit_settings_window()
        window.geometry(f"{max(window.winfo_width(), window.winfo_reqwidth())}x{window.winfo_reqheight() + 12}")

    for name in ("Playback", "Idle"):
        button = tk.Radiobutton(tab_bar, text=name, value=name, variable=active_tab, indicatoron=False,
                                bg=colors["panel"], fg=colors["text"], selectcolor="#253b50",
                                activebackground="#253b50", activeforeground=colors["text"],
                                relief="flat", padx=14, pady=5, command=select_tab)
        animate_button(button).pack(side="left", padx=(0, 6))
    playback_tab.pack(fill="x")
    playback_page = section(playback_tab, "Media")
    appearance_page = section(playback_tab, "Playback appearance")
    text_page = section(playback_tab, "Titles")
    idle_page = section(idle_tab, "Idle widgets")
    page_controls = tk.Frame(idle_page, bg=colors["bg"])
    page_controls.pack(fill="x", pady=(0, 4))
    page_label = tk.StringVar()
    page_picker = tk.Frame(idle_page, bg=colors["bg"])
    page_picker.pack(anchor="w", pady=(0, 6))
    widget_row = tk.Frame(idle_page, bg=colors["bg"])
    widget_row.pack(anchor="w")
    widget_choices, widget_widths, widget_presets = [], [], []
    graph_vars, graph_buttons = [], []
    shortcut_paths, name_buttons, preset_buttons = [], [], []
    image_lights, image_sliders = [], []
    editor_page = None

    def sync_widget_graph(index):
        name = widget_choices[index].get()
        custom = name == "Custom app"
        image = name == IMAGE_WIDGET
        for control in (preset_buttons[index], name_buttons[index], image_sliders[index]):
            control.pack_forget()
        control = image_sliders[index] if image else name_buttons[index] if custom else preset_buttons[index]
        control.pack(side="left", padx=(4, 0), before=graph_buttons[index])
        graph_buttons[index].configure(state="normal" if custom or image or name in GRAPH_SOURCES else "disabled")

    def pick_widget(index, name):
        previous = widget_choices[index].get()
        if name == IMAGE_WIDGET:
            formats = " ".join("*" + ext for ext in IMAGE_EXTENSIONS)
            path = filedialog.askopenfilename(parent=window, title="Choose a picture or GIF",
                                              filetypes=(("Pictures and GIFs", formats),))
            if not path or Path(path).suffix.casefold() not in IMAGE_EXTENSIONS:
                return
            try:
                assets = preferences.PATH.parent / "dashboard-images"
                assets.mkdir(parents=True, exist_ok=True)
                imported = assets / (uuid.uuid4().hex + Path(path).suffix.lower())
                shutil.copyfile(path, imported)
            except OSError as error:
                messagebox.showerror("Could not import image", str(error), parent=window)
                return
            shortcut_paths[index] = str(imported)
            if previous != IMAGE_WIDGET:
                image_lights[index].set(100)
                graph_vars[index].set("Fit")
        elif name == "Custom app":
            path = filedialog.askopenfilename(parent=window, title="Choose an application",
                                              filetypes=(("Applications and shortcuts", "*.exe *.lnk"),))
            if not path or Path(path).suffix.casefold() not in (".exe", ".lnk"):
                return
            shortcut_paths[index] = path
            widget_presets[index].set(Path(path).stem[:64])
            if previous != "Custom app":
                graph_vars[index].set("Fit")
        elif previous in ("Custom app", IMAGE_WIDGET):
            widget_presets[index].set("Auto")
            graph_vars[index].set("Bar")
            shortcut_paths[index] = ""
        widget_choices[index].set(name)
        sync_widget_graph(index)
        apply_idle_widgets()

    def name_widget(index):
        class ShortcutNameDialog(simpledialog.Dialog):
            def body(self, parent):
                current = widget_presets[index].get()
                self.name = tk.StringVar(parent, value=current)
                self.hidden = tk.BooleanVar(parent, value=not current)
                entry = ttk.Entry(parent, textvariable=self.name, width=32)
                entry.grid(row=0, column=0, padx=8, pady=(8, 4))
                def sync():
                    entry.configure(state="disabled" if self.hidden.get() else "normal")
                ttk.Checkbutton(parent, text="No name (icon only)", variable=self.hidden,
                                command=sync).grid(row=1, column=0, padx=8, pady=(0, 8), sticky="w")
                sync()
                return entry

            def apply(self):
                self.result = "" if self.hidden.get() else self.name.get().strip()[:64]

        dialog = ShortcutNameDialog(window, title="Shortcut name")
        if dialog.result is not None:
            widget_presets[index].set(dialog.result)
            apply_idle_widgets()

    def toggle_widget_graph(index):
        if widget_choices[index].get() in ("Custom app", IMAGE_WIDGET):
            graph_vars[index].set("Stretch" if graph_vars[index].get() == "Fit" else "Fit")
        else:
            graph_vars[index].set("Line" if graph_vars[index].get() == "Bar" else "Bar")
        apply_idle_widgets()

    def sync_idle_editor():
        global editor_page
        with lock:
            index, count = idle_page_index, len(idle_pages)
            slots = idle_pages[index]
        editor_page = index
        page_label.set(f"Page {index + 1} of {count}")
        for button in page_picker.winfo_children():
            button.destroy()
        for page in range(count):
            button = tk.Canvas(page_picker, width=30, height=30, bg=colors["bg"],
                               highlightthickness=0, takefocus=True, cursor="hand2")
            button.create_oval(2, 2, 28, 28, fill="#253b50" if page == index else colors["panel"],
                               outline=colors["muted"])
            button.create_text(15, 15, text=str(page + 1), fill=colors["text"], font=ui_font)
            for event in ("<Button-1>", "<Return>", "<space>"):
                button.bind(event, lambda _, target=page: change_idle_page(target))
            button.grid(row=page // 18, column=page % 18, padx=(0, 4), pady=2)
        for slot_number, (choice, width, preset, graph) in enumerate(zip(widget_choices, widget_widths, widget_presets, graph_vars)):
            slot = slots[slot_number] if slot_number < len(slots) else ("Empty", 3, "Auto", "Bar")
            name, size, color, style = slot[:4]
            shortcut_paths[slot_number] = slot[4] if len(slot) == 5 else ""
            choice.set(name)
            width.set(size)
            preset.set("Auto" if name == IMAGE_WIDGET else color)
            image_lights[slot_number].set(color if name == IMAGE_WIDGET else 100)
            graph.set(style)
            sync_widget_graph(slot_number)

    def change_idle_page(action):
        global idle_pages, idle_page_index
        with lock:
            if action == "add":
                idle_pages += ((),)
                idle_page_index = len(idle_pages) - 1
            elif action == "delete":
                index = editor_page
                idle_pages = idle_pages[:index] + idle_pages[index + 1:] or ((),)
                idle_page_index = min(index, len(idle_pages) - 1)
            elif type(action) is int and 0 <= action < len(idle_pages):
                idle_page_index = action
        sync_idle_editor()
        touch_changed.set()

    def apply_idle_widgets(_=None):
        global idle_pages
        slots = tuple(
            (choice.get(), width.get(),
             image_lights[index].get() if choice.get() == IMAGE_WIDGET else preset.get(),
             graph.get(), shortcut_paths[index])
            if choice.get() in ("Custom app", IMAGE_WIDGET) else (choice.get(), width.get(), preset.get(), graph.get())
            for index, (choice, width, preset, graph) in enumerate(zip(widget_choices, widget_widths, widget_presets, graph_vars))
        )
        with lock:
            idle_pages = idle_pages[:editor_page] + (slots,) + idle_pages[editor_page + 1:]
        touch_changed.set()

    def follow_playback():
        global screen_override
        with lock:
            screen_override = None
        touch_changed.set()

    for label, action in (("Add page", "add"), ("Delete page", "delete")):
        settings_button(page_controls, text=label, command=lambda value=action: change_idle_page(value)
                        ).pack(side="left", padx=(0, 6))
    settings_button(page_controls, text="Follow playback", command=follow_playback).pack(side="left")
    tk.Label(page_controls, textvariable=page_label, bg=colors["bg"], fg=colors["text"]).pack(side="right")
    tk.Label(idle_page, text="Swipe sideways for dashboard pages. Touch with two fingers to switch between dashboard and playback.",
             bg=colors["bg"], fg=colors["muted"], wraplength=680, justify="left").pack(anchor="w", pady=(4, 0))
    for index in range(8):
        choice, width, preset = tk.StringVar(), tk.IntVar(), tk.StringVar()
        widget_choices.append(choice)
        widget_widths.append(width)
        widget_presets.append(preset)
        graph = tk.StringVar(value="Bar")
        graph_vars.append(graph)
        shortcut_paths.append("")
        cell = tk.Frame(widget_row, bg=colors["bg"])
        cell.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 15), pady=3)
        tk.Label(cell, text=str(index + 1), width=2, bg=colors["bg"], fg=colors["muted"]).pack(side="left")
        menu_style = dict(tearoff=False, bg=colors["panel"], fg=colors["text"],
                          activebackground="#253b50", activeforeground=colors["text"])
        picker = tk.Menubutton(cell, textvariable=choice, indicatoron=True, width=23,
                               bg=colors["panel"], fg=colors["text"], relief="flat")
        options = tk.Menu(picker, **menu_style)
        options.add_command(label="Empty", command=lambda slot=index: pick_widget(slot, "Empty"))
        for group, names in WIDGET_GROUPS.items():
            submenu = tk.Menu(options, **menu_style)
            for name in names:
                submenu.add_command(label=name, command=lambda slot=index, value=name: pick_widget(slot, value))
            options.add_cascade(label=group, menu=submenu)
        picker.configure(menu=options)
        animate_button(picker).pack(side="left", padx=(4, 0))
        for variable, options, size in ((width, (1, 2, 3, 4, 5), 2), (preset, ("Auto", *WIDGET_COLORS), 7)):
            option = tk.OptionMenu(cell, variable, *options, command=apply_idle_widgets)
            option.configure(width=size, bg=colors["panel"], fg=colors["text"], relief="flat", highlightthickness=0)
            option.pack(side="left", padx=(4, 0))
            animate_button(option)
            option.nametowidget(option.cget("menu")).configure(
                bg=colors["panel"], fg=colors["text"], activebackground="#253b50", activeforeground=colors["text"])
        preset_buttons.append(option)
        name_buttons.append(settings_button(cell, text="Name", width=5,
                                            command=lambda slot=index: name_widget(slot)))
        image_light = tk.IntVar(value=100)
        image_lights.append(image_light)
        image_slider = tk.Frame(cell, bg=colors["bg"])
        tk.Label(image_slider, text="Brightness", bg=colors["bg"], fg=colors["text"]).pack(anchor="w")
        tk.Scale(image_slider, orient="horizontal", from_=0, to=100, length=64, sliderlength=15,
                 showvalue=False, variable=image_light, command=apply_idle_widgets,
                 bg=colors["bg"], fg=colors["text"], troughcolor=colors["panel"],
                 activebackground="#253b50", highlightthickness=0).pack(anchor="w")
        image_sliders.append(image_slider)
        graph_button = settings_button(cell, textvariable=graph, width=7,
                                       command=lambda slot=index: toggle_widget_graph(slot))
        graph_button.pack(side="left", padx=(4, 0))
        graph_buttons.append(graph_button)
    sync_idle_editor()
    modes = tk.Frame(playback_page, bg=colors["bg"])
    modes.pack(anchor="w", pady=(0, 4))
    selection = tk.StringVar(value=display_mode)
    resume_mode = "Media" if display_mode == "Media" else "Auto"

    def apply_play_mode():
        global resume_mode
        resume_mode = selection.get()
        select_display(resume_mode)
        stop_button.configure(text="Stop drawing")

    def toggle_drawing():
        global resume_mode
        if selection.get() == "Corsair":
            selection.set(resume_mode)
            apply_play_mode()
        else:
            resume_mode = selection.get()
            selection.set("Corsair")
            select_display("Corsair")
            stop_button.configure(text="Resume drawing")

    for value, label in (("Auto", "Only while playing"), ("Media", "Show when paused too")):
        tk.Radiobutton(modes, text=label, variable=selection, value=value,
                       bg=colors["panel"], fg=colors["text"], selectcolor="#253b50",
                       activebackground="#253b50", activeforeground=colors["text"],
                       indicatoron=False, relief="flat", padx=12, pady=4,
                       command=apply_play_mode).pack(side="left", padx=(0, 6))
    settings = tk.Frame(appearance_page, bg=colors["bg"])
    settings.pack(fill="x", pady=(4, 0))
    tk.Label(settings, text="Bar color", bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=(0, 8))
    selected_theme = tk.StringVar(value=theme)
    palette = tk.OptionMenu(settings, selected_theme, "Automatic", *THEMES, "Custom", command=set_theme)
    palette.configure(bg=colors["panel"], fg=colors["text"], activebackground="#253b50",
                      activeforeground=colors["text"], highlightthickness=0, relief="flat", width=11)
    palette.pack(side="left")
    static_selection = tk.BooleanVar(value=static_page)
    performance = tk.Frame(playback_page, bg=colors["bg"])
    performance.pack(anchor="w", pady=(4, 0))
    tk.Checkbutton(performance, text="Limit updates to once a second", variable=static_selection,
                   bg=colors["bg"], fg=colors["muted"], selectcolor=colors["panel"],
                   activebackground=colors["bg"], activeforeground=colors["text"],
                   command=lambda: set_static_page(static_selection.get())).pack(side="left", padx=14)
    settings_button(performance, text="Windows media sessions", command=show_windows_sessions,
                    padx=6, pady=2).pack(side="left")
    amazon_button = settings_button(playback_page, text="Connect Amazon Music",
                                     command=toggle_amazon_music, padx=6, pady=2)
    amazon_button.pack(anchor="w", pady=(4, 0))
    
    background_settings = tk.Frame(appearance_page, bg=colors["bg"])
    background_settings.pack(anchor="w", pady=(4, 0))
    tk.Label(background_settings, text="Background", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(0, 8))
    background_actions = tk.Frame(appearance_page, bg=colors["bg"])
    background_actions.pack(anchor="w", pady=(4, 0))
    background_mode = tk.StringVar(value=background_initial)
    applied_background = tk.StringVar(value=background_initial)
    for mode in ("Thumbnail", "Black", "Color", "Image"):
        tk.Radiobutton(background_settings, text=mode, variable=background_mode, value=mode,
                       indicatoron=False, relief="flat", bg=colors["panel"], fg=colors["text"],
                       selectcolor="#253b50", activebackground="#253b50", activeforeground=colors["text"],
                       padx=9, command=lambda mode=mode: apply_background(mode) if mode in backgrounds
                       else (choose_color() if mode == "Color" else choose_image())).pack(side="left", padx=(0, 4))
    for label, command in (("Pick color…", choose_color), ("Pick image…", choose_image)):
        settings_button(background_actions, text=label, command=command,
                        padx=6, pady=2).pack(side="left", padx=(4, 0))
    settings_button(background_actions, text="Video thumbnail", command=choose_video_thumbnail,
                    padx=6, pady=2).pack(side="left", padx=(4, 0))
    brightness_settings = tk.Frame(general, bg=colors["bg"])
    brightness_settings.pack(fill="x", pady=(4, 0))
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
    fit_settings = tk.Frame(appearance_page, bg=colors["bg"])
    fit_settings.pack(anchor="w", pady=(4, 0))
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
    text_settings = tk.Frame(text_page, bg=colors["bg"])
    text_settings.pack(anchor="w", pady=(4, 0))
    layout_choice = tk.StringVar(value=layout)
    alignment_choice = tk.StringVar(value=alignment)
    credit_choice = tk.StringVar(value=credit_mode)
    time_choice = tk.StringVar(value=time_format)
    wall_clock_choice = tk.StringVar(value=idle_clock_format)

    def apply_text_options(_):
        global layout, alignment, credit_mode, time_format, idle_clock_format
        with lock:
            layout = layout_choice.get()
            alignment = alignment_choice.get()
            credit_mode = credit_choice.get()
            time_format = time_choice.get()
            idle_clock_format = wall_clock_choice.get()

    for label, choice, options in (
        ("Title and time", layout_choice, ("Split", "Above", "Below", "Inline above", "Inline below")),
        ("Title alignment", alignment_choice, ("Left", "Center", "Right")),
    ):
        tk.Label(text_settings, text=label, bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=(0, 8))
        menu = tk.OptionMenu(text_settings, choice, *options, command=apply_text_options)
        menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
        menu.pack(side="left", padx=(0, 16))
    credit_settings = tk.Frame(text_page, bg=colors["bg"])
    credit_settings.pack(anchor="w", pady=(4, 0))
    tk.Label(credit_settings, text="Channel / artist", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(0, 8))
    credit_menu = tk.OptionMenu(credit_settings, credit_choice, "Beside title", "Hidden",
                                "Opposite sides", command=apply_text_options)
    credit_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    credit_menu.pack(side="left")
    formats = tk.Frame(general, bg=colors["bg"])
    formats.pack(anchor="w", pady=(4, 0))
    for label, choice, options in (
        ("Playback time", time_choice, ("Minutes", "Hours")),
        ("Clock", wall_clock_choice, ("12-hour", "24-hour")),
    ):
        tk.Label(formats, text=label, bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=(0, 8))
        menu = tk.OptionMenu(formats, choice, *options, command=apply_text_options)
        menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
        menu.pack(side="left", padx=(0, 16))
    font_settings = tk.Frame(general, bg=colors["bg"])
    font_settings.pack(anchor="w", pady=(4, 0))
    font_choice = tk.StringVar(value=display_font)

    def apply_font(value):
        global display_font
        with lock:
            display_font = value
        family = "Consolas" if value == DEFAULT_DISPLAY_FONT and not pixel_font_loaded else value
        for font in (ui_font, heading_font, section_font):
            font.configure(family=family)
        fit_settings_window()
        touch_changed.set()

    tk.Label(font_settings, text="Display font", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(0, 8))
    font_menu = tk.OptionMenu(font_settings, font_choice, *(available_fonts or DISPLAY_FONTS),
                              command=apply_font)
    font_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    font_menu.pack(side="left")
    text_size_choice = tk.StringVar(value=text_size)

    def apply_text_size(value):
        global text_size
        with lock:
            text_size = value
        touch_changed.set()

    tk.Label(font_settings, text="Text size", bg=colors["bg"],
             fg=colors["muted"]).pack(side="left", padx=(12, 6))
    text_size_menu = tk.OptionMenu(font_settings, text_size_choice, *TEXT_SIZES,
                                   command=apply_text_size)
    text_size_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    text_size_menu.pack(side="left")
    ink_settings = tk.Frame(general, bg=colors["bg"])
    ink_settings.pack(fill="x", pady=(4, 0))
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
    outlines = tk.Frame(appearance_page, bg=colors["bg"])
    outlines.pack(anchor="w", pady=(4, 0))
    text_outline_row = tk.Frame(general, bg=colors["bg"])
    text_outline_row.pack(anchor="w", pady=(4, 0))
    scrub_choice = tk.BooleanVar(value=live_scrubbing)
    rapid_tap_choice = tk.BooleanVar(value=combine_rapid_taps)

    def set_scrubbing():
        global live_scrubbing, combine_rapid_taps
        with lock:
            live_scrubbing = scrub_choice.get()
            combine_rapid_taps = rapid_tap_choice.get()
            desktop_seek_changed.set()

    seek_settings = tk.Frame(playback_page, bg=colors["bg"])
    seek_settings.pack(anchor="w", pady=(4, 0))
    for label, choice in (("Seek live while dragging on NEXUS", scrub_choice),
                          ("Combine rapid taps (Windows)", rapid_tap_choice)):
        tk.Checkbutton(seek_settings, text=label, variable=choice, command=set_scrubbing,
                       bg=colors["bg"], fg=colors["text"], selectcolor=colors["panel"],
                       activebackground=colors["bg"], activeforeground=colors["text"]).pack(side="left")
    text_outline = tk.BooleanVar(value=outline_options[0])
    bar_outline = tk.BooleanVar(value=outline_options[1])
    thickness = tk.StringVar(value=outline_options[2])

    def apply_outlines(_=None):
        global outline_options
        with lock:
            outline_options = (text_outline.get(), bar_outline.get(), thickness.get())

    for row, label, choice in ((text_outline_row, "Black text outline", text_outline),
                               (outlines, "Seek bar border", bar_outline)):
        tk.Checkbutton(row, text=label, variable=choice, command=apply_outlines,
                       bg=colors["bg"], fg=colors["text"], selectcolor=colors["panel"],
                       activebackground=colors["bg"], activeforeground=colors["text"]).pack(side="left", padx=(0, 12))
    tk.Label(outlines, text="Bar thickness", bg=colors["bg"], fg=colors["muted"]).pack(side="left")
    size_menu = tk.OptionMenu(outlines, thickness, "Thin", "Default", "Thick", command=apply_outlines)
    size_menu.configure(bg=colors["panel"], fg=colors["text"], highlightthickness=0, relief="flat")
    size_menu.pack(side="left")
    tk.Label(ink_settings, textvariable=ink_level, width=5, bg=colors["bg"], fg=colors["text"]).pack(side="left")
    text = tk.StringVar(value=status)
    tk.Label(window, textvariable=text, anchor="w", justify="left", wraplength=590,
             fg=colors["muted"], bg=colors["bg"]).pack(fill="x", padx=24, pady=(7, 0))

    def refresh():
        global preview_frame, preview_image
        if window.state() in ("withdrawn", "iconic"):
            window.after(250, refresh)
            return
        if editor_page != idle_page_index:
            sync_idle_editor()
        now = time.monotonic()
        with lock:
            amazon_button.configure(
                text="Disconnect Amazon Music" if amazon_connected else "Connect Amazon Music",
                state="disabled" if amazon_status == "Restarting Amazon Music…" or desktop_status else "normal",
            )
            item = active(now)
            if pause_settings_preview(item, now):
                window.after(100, refresh)
                return
            fresh = show_media(display_mode, item, now)
            art = art_cache.get((artwork_source(item), thumbnail_fit)) if fresh else None
            backdrop = background
            if fresh and automatic_art:
                if art and art[0] is not BLACK:
                    backdrop = art[0]
                elif "timeline_anchor" in item:
                    backdrop = DESKTOP_BACKDROP
            color = bar_color(art)
            if fresh:
                position = playback_position(item, now)
                ratio = position / item["duration"] if item["duration"] else 0
                frame = (round(ratio * (WIDTH - 1)), item["seekable"], int(position),
                         int(item["duration"]), color, item["title"], backdrop, layout, alignment,
                         background_brightness, item["credit"], credit_mode, text_color, text_brightness,
                         *outline_options, int((now - item["title_started"]) * 40), time_format, display_font, text_size)
            else:
                frame = dashboard_frame
            if display_mode == "Corsair":
                frame = ()
        message = status
        if message == "Starting...":
            message = f"{startup_stage} ({int(now - stage_started)}s)"
        text.set("\n".join(filter(None, (message, desktop_status, amazon_status, last_tap))))
        if frame != preview_frame:
            rgba = frame if isinstance(frame, bytes) and frame else render(*frame) if frame else BLACK
            rgb = bytearray(WIDTH * HEIGHT * 3)
            for channel in range(3):
                rgb[channel::3] = rgba[channel::4]
            header = f"P6 {WIDTH} {HEIGHT} 255\n".encode("ascii")
            preview_image = tk.PhotoImage(data=header + rgb, format="PPM")
            preview_frame = frame
            preview.itemconfigure(preview_item, image=preview_image)
        window.after(250 if fresh and "timeline_anchor" in item else 33 if held_seek else 100, refresh)

    launch_in_tray = tk.BooleanVar(value=saved.get("start_in_tray") is True)
    launch_with_windows = tk.BooleanVar()
    try:
        launch_with_windows.set(startup.enabled())
    except OSError:
        pass

    def change_startup():
        requested = launch_with_windows.get()
        try:
            startup.set_enabled(requested, __file__)
        except OSError as error:
            launch_with_windows.set(not requested)
            saved_label.set(f"Could not change Windows startup: {error}")
        else:
            saved_label.set("Windows startup enabled" if requested else "Windows startup disabled")

    launch_options = tk.Frame(app_page, bg=colors["bg"])
    launch_options.pack(fill="x", pady=(8, 0))

    def open_local_player_video(filename=None):
        if not filename:
            filename = filedialog.askopenfilename(parent=window, title="Open a local video")
        if not filename:
            return
        path = Path(filename)
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            messagebox.showerror("Open video", "Choose an existing local video file.", parent=window)
            return
        launcher = Path(os.environ["LOCALAPPDATA"]) / "NexusMediaPlayer" / "NexusMediaPlayer.exe"
        try:
            subprocess.Popen([str(launcher), str(path)])
        except OSError as error:
            messagebox.showerror("Open video", "Install NEXUS Media Player to use this shortcut.", parent=window)

    stop_button = settings_button(launch_options, text="Resume drawing" if display_mode == "Corsair" else "Stop drawing",
                                  command=toggle_drawing)
    stop_button.pack(side="right")
    settings_button(modes, text="Open local video", command=open_local_player_video).pack(side="left")
    for label, variable, command in (
        ("Start with Windows", launch_with_windows, change_startup),
        ("Start in tray", launch_in_tray, lambda: save_settings(True)),
    ):
        tk.Checkbutton(launch_options, text=label, variable=variable, command=command,
                       bg=colors["bg"], fg=colors["text"], selectcolor=colors["panel"],
                       activebackground=colors["bg"], activeforeground=colors["text"]).pack(side="left", padx=(0, 16))

    for page in pages:
        for row in page.winfo_children():
            for widget in row.winfo_children():
                if widget.winfo_class() in ("Checkbutton", "Radiobutton", "Menubutton"):
                    animate_button(widget)
                if widget.winfo_class() == "Menubutton":
                    widget.nametowidget(widget.cget("menu")).configure(
                        bg=colors["panel"], fg=colors["text"], activebackground="#253b50",
                        activeforeground=colors["text"], font=ui_font)
    def settings_snapshot():
        return ({**{key: globals()[key] for key in keys}, "start_in_tray": launch_in_tray.get()}, applied_background.get(),
                {name: backgrounds[name] for name in ("Color", "Image") if name in backgrounds})

    last_saved = settings_snapshot() if saved else None
    settings_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="settings")
    saving = None
    save_feedback = False
    quit_requested = False

    def write_settings(snapshot):
        data, mode, images = snapshot
        preferences.save({**data, "background_mode": mode, "backgrounds": {
            name: base64.b64encode(pixels).decode("ascii") for name, pixels in images.items()
        }})

    def finish_save(snapshot):
        global saving, last_saved, save_feedback, quit_requested
        if not saving.done():
            window.after(50, finish_save, snapshot)
            return
        try:
            saving.result()
        except OSError as error:
            saved_label.set(f"Settings could not be saved: {error}")
            saving = None
            save_feedback = quit_requested = False
            return
        saving = None
        last_saved = snapshot
        save_settings()

    def save_settings(show_feedback=False):
        global saving, save_feedback
        save_feedback |= show_feedback
        if saving is not None:
            return
        snapshot = settings_snapshot()
        if snapshot != last_saved:
            saving = settings_writer.submit(write_settings, snapshot)
            window.after(50, finish_save, snapshot)
            return
        if save_feedback:
            saved_label.set("Settings saved")
            save_feedback = False
        if quit_requested:
            if not playback_shutdown.is_set():
                playback_shutdown.set()
                touch_changed.set()
                desktop_seek_changed.set()
            if display_worker.is_alive() or desktop_worker.is_alive():
                window.after(50, save_settings)
                return
            settings_writer.shutdown(wait=False)
            if tray:
                tray.close()
            window.destroy()

    def autosave():
        save_settings()
        window.after(400, autosave)

    def open_settings():
        window.deiconify()
        window.lift()

    def hide():
        save_settings()
        if tray and tray.ready:
            window.withdraw()
        else:
            window.iconify()

    def quit_app():
        global quit_requested
        quit_requested = True
        save_settings()

    saved_label = tk.StringVar()
    footer = tk.Frame(window, bg=colors["bg"])
    footer.pack(fill="x", padx=24, pady=(8, 0))
    settings_button(footer, text="Save settings", command=lambda: save_settings(True)).pack(side="left")
    tk.Label(footer, textvariable=saved_label, bg=colors["bg"], fg=colors["muted"]).pack(side="left", padx=10)
    settings_button(footer, text="Hide", command=hide).pack(side="right", padx=(6, 0))
    settings_button(footer, text="Quit app", command=quit_app).pack(side="right")
    def fit_settings_window():
        window.update_idletasks()
        window.minsize(window.winfo_reqwidth(), window.winfo_reqheight())

    fit_settings_window()
    try:
        tray = Tray(window, open_settings, quit_app, open_local_player_video)
    except OSError:
        tray = None
    if not ((explorer_video or launch_in_tray.get()) and tray and tray.ready):
        window.deiconify()
    window.protocol("WM_DELETE_WINDOW", hide)
    window.after(400, autosave)

    display_worker = threading.Thread(target=update, daemon=True)
    display_worker.start()
    desktop_worker = threading.Thread(target=read_windows_media, daemon=True)
    desktop_worker.start()
    threading.Thread(target=read_local_player, daemon=True).start()
    window.after(200, refresh)
    if explorer_video:
        window.after(0, open_local_player_video, explorer_video)
    window.mainloop()
