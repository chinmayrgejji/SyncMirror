"""
app.py

UI for the multi-device synced audio mirror, built on customtkinter.

Run with:  python app.py
(Windows only -- requires pyaudiowpatch, which wraps WASAPI.)
"""
import os
import sys

# When the frozen exe relaunches itself purely to scan audio devices (see
# capture.enumerate_output_devices), do that and exit before loading the GUI --
# otherwise every device scan would open another app window.
if getattr(sys, "frozen", False) and "--enumerate-devices" in sys.argv:
    from capture import emit_enumeration
    _args = sys.argv[sys.argv.index("--enumerate-devices") + 1:]
    emit_enumeration(_args[0] if _args else None)
    sys.exit(0)

import asyncio
import ctypes
from datetime import datetime, timezone
from io import BytesIO
import json
import queue
import tkinter as tk
import threading
import time
from tkinter import messagebox

import customtkinter as ctk
import pyaudiowpatch as pyaudio

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk
except ImportError:
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageOps = None
    ImageTk = None

try:
    import pystray
except ImportError:
    pystray = None

try:
    from webremote import WebRemote
except ImportError:
    WebRemote = None

from ring_buffer import SyncRingBuffer
from capture import LoopbackCapture
from output import DeviceOutput

SAMPLERATE = 48000  # fallback only; capture.prepare() detects the real one
CHANNELS = 2
RING_BUFFER_SECONDS = 10.0
BASE_SAFETY_MS = 150.0

DELAY_MIN, DELAY_MAX = -500.0, 500.0
BUFFER_METER_MAX_MS = 300.0  # meter is "full" at this many ms of headroom
BUFFER_TARGET_MS = 90.0      # start of the "healthy" (green) zone; drives the tick

# Default to the OS theme; the user can override it from the header switcher.
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

# ---- colour system -------------------------------------------------------
# A real elevation ladder: page < card < nested row, plus a hairline border so
# surfaces read as surfaces instead of blobs of near-identical grey.
# Every colour is a (light, dark) pair so CustomTkinter widgets follow the
# active appearance mode automatically; raw tk/PIL surfaces resolve via _c().
BG = ("#f2f4f8", "#101319")          # page
CARD_BG = ("#ffffff", "#1a1e27")     # card surface
CARD_HI = ("#e8ebf1", "#232936")     # nested rows inside a card
BORDER = ("#d4d9e2", "#2a3040")      # hairline card border
GHOST_BORDER = ("#c3cad6", "#39415420")
ACCENT = ("#2b7fff", "#4da3ff")
ACCENT_HOVER = ("#1f6fe6", "#3688dc")
GOOD = ("#1ca94c", "#3ecf6e")
WARN = ("#c8901a", "#e8b93b")
BAD = ("#d63a20", "#e8543b")
TEXT = ("#1a1e27", "#e8ecf4")
TEXT_DIM = ("#2b303d", "#8b93a3")
TEXT_FAINT = ("#454b5a", "#5d6577")

# Artwork stage / placeholder colours for the raw tk.Label + PIL backdrop,
# which don't auto-follow the appearance mode. Resolved through _c().
ARTWORK_BG = ("#e4e8f0", "#0c0f16")
ARTWORK_PLACEHOLDER_FG = ("#c0c6d2", "#2a3040")
ARTWORK_EMPTY_RGB = ((228, 232, 240), (22, 25, 35))


def _c(color):
    """Resolve a (light, dark) colour pair to the active appearance mode.

    CustomTkinter widgets accept the pair directly, but raw tk widgets and
    PIL rendering need a single concrete value for the current theme."""
    if isinstance(color, (tuple, list)) and len(color) == 2:
        return color[0] if ctk.get_appearance_mode() == "Light" else color[1]
    return color

# ---- spacing scale -------------------------------------------------------
# Only these four values are allowed anywhere in the layout.
PAD_XS, PAD_S, PAD_M, PAD_L = 4, 8, 14, 20

# ---- type scale ----------------------------------------------------------
# Four steps. Hierarchy comes from weight + colour, not from a fifth size.
FS_LABEL, FS_BODY, FS_TITLE, FS_DISPLAY = 11, 13, 20, 24
ARTWORK_ASPECT_RATIO = 16 / 9
ARTWORK_WIDTH = 440
ARTWORK_HEIGHT = round(ARTWORK_WIDTH / ARTWORK_ASPECT_RATIO)

VIS_BARS = 40           # number of synth/equalizer bars
VIS_INTERVAL_MS = 33    # ~30 fps redraw
VIS_ATTACK = 0.6        # how fast a bar rises toward a new peak
VIS_DECAY = 0.15        # how slowly it falls back

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


def resource_path(relative):
    """Resolve a bundled resource path, working from source and a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, relative)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), relative)


ICON_PATH = resource_path("SyncMirror.ico")


def _register_bundled_fonts():
    """Register any TTF/OTF fonts under assets/fonts for this process (private,
    so nothing is installed system-wide) and return the preferred UI family.

    Tk renders bundled fonts far more crisply at small sizes than the default;
    if no Inter files are present we fall back to Segoe UI on Windows."""
    family = "Segoe UI"
    fonts_dir = resource_path(os.path.join("assets", "fonts"))
    if not os.path.isdir(fonts_dir):
        return family
    try:
        gdi32 = ctypes.windll.gdi32
    except Exception:
        return family
    FR_PRIVATE = 0x10
    registered_inter = False
    for fname in os.listdir(fonts_dir):
        if not fname.lower().endswith((".ttf", ".otf")):
            continue
        path = os.path.join(fonts_dir, fname)
        try:
            if gdi32.AddFontResourceExW(ctypes.c_wchar_p(path), FR_PRIVATE, 0):
                if "inter" in fname.lower():
                    registered_inter = True
        except Exception:
            pass
    return "Inter" if registered_inter else family


UI_FONT_FAMILY = _register_bundled_fonts()
try:
    # Applies to every CTkFont() that doesn't pass an explicit family.
    ctk.ThemeManager.theme["CTkFont"]["family"] = UI_FONT_FAMILY
except Exception:
    pass


def settings_path():
    """Per-user settings file, kept out of the (read-only when installed) app dir."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "SyncMirror")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    return os.path.join(folder, "settings.json")


class SettingsStore:
    """Persists per-device delay/volume/mute/enabled across launches, keyed by the
    device's friendly name (stable across reconnects)."""

    def __init__(self):
        self.path = settings_path()
        self.devices = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("devices"), dict):
                self.devices = data["devices"]
        except (OSError, ValueError):
            self.devices = {}

    def get(self, name):
        entry = self.devices.get(name)
        return entry if isinstance(entry, dict) else None

    def update(self, name, **fields):
        self.devices.setdefault(name, {}).update(fields)

    def save(self):
        # Atomic write so a crash mid-save can't corrupt the settings file.
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"devices": self.devices}, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass


class MediaController:
    @staticmethod
    def _wait_for(operation):
        """Synchronously wait for a PyWinRT async operation from Tk's UI thread."""
        async def wait_for_operation():
            return await operation

        return asyncio.run(wait_for_operation())

    def __init__(self):
        self.winrt_available = False
        self.session_manager = None
        self.last_error = ""
        self._thumbnail_key = None
        self._thumbnail_bytes = None
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
            # PyWinRT exposes WinRT async operations as awaitables; they do
            # not provide the .get() method used by the old projection.
            self.session_manager = self._wait_for(
                GlobalSystemMediaTransportControlsSessionManager.request_async()
            )
            self.winrt_available = True
        except Exception as exc:
            self.session_manager = None
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _send_media_key(self, vk_code: int):
        ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
        ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

    def play_pause(self):
        self._send_media_key(VK_MEDIA_PLAY_PAUSE)

    def next_track(self):
        self._send_media_key(VK_MEDIA_NEXT_TRACK)

    def previous_track(self):
        self._send_media_key(VK_MEDIA_PREV_TRACK)

    def stop(self):
        self._send_media_key(VK_MEDIA_STOP)

    def _get_thumbnail_bytes(self, thumbnail):
        """Read the WinRT thumbnail stream into bytes for Pillow to decode."""
        if thumbnail is None:
            return None
        from winrt.windows.storage.streams import DataReader

        stream = self._wait_for(thumbnail.open_read_async())
        reader = DataReader(stream.get_input_stream_at(0))
        self._wait_for(reader.load_async(stream.size))
        data = bytearray(reader.unconsumed_buffer_length)
        reader.read_bytes(data)
        return bytes(data)

    def get_current_track(self, include_thumbnail=False):
        if not self.winrt_available or self.session_manager is None:
            return None
        try:
            session = self.session_manager.get_current_session()
            if session is None:
                return None
            props = self._wait_for(session.try_get_media_properties_async())
            title = getattr(props, "title", None) or "Unknown title"
            # GSMTC exposes this as the singular `artist` property.
            artist = getattr(props, "artist", None) or "Unknown artist"
            album = getattr(props, "album_title", None) or ""
            source = getattr(session, "source_app_user_model_id", None) or ""
            playback = session.get_playback_info()
            status = str(getattr(playback, "playback_status", "")).split(".")[-1]
            timeline = session.get_timeline_properties()
            art_key = (source, title, artist, album)
            last_updated = getattr(timeline, "last_updated_time", None)
            if include_thumbnail and art_key != self._thumbnail_key:
                self._thumbnail_key = art_key
                try:
                    self._thumbnail_bytes = self._get_thumbnail_bytes(getattr(props, "thumbnail", None))
                except Exception:
                    self._thumbnail_bytes = None
            return {
                "title": title,
                "artist": artist,
                "album": album,
                "source": source,
                "status": status,
                "position": getattr(timeline, "position", None),
                "duration": getattr(timeline, "end_time", None),
                "last_updated": last_updated,
                "art_key": art_key,
                "thumbnail": self._thumbnail_bytes if include_thumbnail else None,
            }
        except Exception as exc:
            self.last_error = str(exc)
            return None


class DeviceCard(ctk.CTkFrame):
    """One row per audio output device: enable toggle, delay control, live buffer meter."""

    def __init__(self, master, info, on_toggle, on_delay_change, on_volume_change):
        super().__init__(master, fg_color=CARD_BG, corner_radius=10,
                         border_width=1, border_color=BORDER)
        self.info = info
        self.on_toggle = on_toggle
        self.on_delay_change = on_delay_change
        self.on_volume_change = on_volume_change

        self.enabled_var = tk.BooleanVar(value=False)
        self.delay_var = tk.DoubleVar(value=0.0)
        self.volume_var = tk.DoubleVar(value=100.0)
        self.muted_var = tk.BooleanVar(value=False)

        # Stutter history: remember when the count last climbed so we can show
        # "2m ago" instead of a bare counter with no time context.
        self._last_stutter_mono = None
        self._prev_stutter_count = 0

        self.grid_columnconfigure(0, weight=1)

        # -- top row: name + enable switch --
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=PAD_M, pady=(PAD_M, PAD_S))
        top.grid_columnconfigure(0, weight=1)

        name = info["name"]
        display_name = name if len(name) <= 42 else name[:39] + "..."
        ctk.CTkLabel(top, text=display_name, text_color=TEXT,
                     font=ctk.CTkFont(size=FS_BODY, weight="bold"),
                     anchor="w").grid(row=0, column=0, sticky="w")

        self.switch = ctk.CTkSwitch(
            top, text="", variable=self.enabled_var, command=self._toggled,
            progress_color=ACCENT, width=40,
        )
        self.switch.grid(row=0, column=1, sticky="e")

        # -- delay row: nudges folded in beside the slider (was its own row) --
        delay_row = ctk.CTkFrame(self, fg_color="transparent")
        delay_row.grid(row=1, column=0, sticky="ew", padx=PAD_M, pady=(0, PAD_S))
        delay_row.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(delay_row, text="DELAY", text_color=TEXT_FAINT, width=44, anchor="w",
                     font=ctk.CTkFont(size=FS_LABEL, weight="bold")
                     ).grid(row=0, column=0, sticky="w")

        self._nudge_btn(delay_row, "\u2212", -5).grid(row=0, column=1, padx=(0, PAD_S))

        self.delay_slider = ctk.CTkSlider(
            delay_row, from_=DELAY_MIN, to=DELAY_MAX, variable=self.delay_var,
            command=self._delay_changed, progress_color=ACCENT, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER, height=14,
        )
        self.delay_slider.grid(row=0, column=2, sticky="ew")

        self._nudge_btn(delay_row, "+", 5).grid(row=0, column=3, padx=(PAD_S, PAD_S))

        self.delay_label = ctk.CTkLabel(delay_row, text="0 ms", width=56, anchor="e",
                                        text_color=TEXT,
                                        font=ctk.CTkFont(size=FS_LABEL, weight="bold"))
        self.delay_label.grid(row=0, column=4, sticky="e")

        # -- volume row: level slider + value + mute --
        vol_row = ctk.CTkFrame(self, fg_color="transparent")
        vol_row.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=PAD_M,
            pady=(0, PAD_S),
        )

        # Layout:
        # VOLUME | slider ---------------- | 100% | mute
        vol_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            vol_row,
            text="VOLUME",
            text_color=TEXT_FAINT,
            width=52,
            anchor="w",
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, PAD_M),
        )

        self.volume_slider = ctk.CTkSlider(
            vol_row,
            from_=0,
            to=100,
            variable=self.volume_var,
            command=self._volume_changed,
            progress_color=ACCENT,
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            height=14,
        )
        self.volume_slider.grid(
            row=0,
            column=1,
            sticky="ew",
        )

        # Floating value bubble while dragging either slider.
        self.delay_slider.bind(
            "<B1-Motion>",
            lambda _e: self._show_slider_tip(f"{int(self.delay_var.get())} ms"))
        self.delay_slider.bind("<ButtonRelease-1>", self._hide_slider_tip)
        self.volume_slider.bind(
            "<B1-Motion>",
            lambda _e: self._show_slider_tip(f"{int(self.volume_var.get())}%"))
        self.volume_slider.bind("<ButtonRelease-1>", self._hide_slider_tip)

        self.volume_label = ctk.CTkLabel(
            vol_row,
            text="100%",
            width=48,
            anchor="e",
            text_color=TEXT,
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
        )
        self.volume_label.grid(
            row=0,
            column=2,
            sticky="e",
            padx=(PAD_M, PAD_S),
        )

        self.mute_btn = ctk.CTkButton(
            vol_row,
            text="\U0001F50A",
            width=32,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=CARD_HI,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_DIM,
            font=ctk.CTkFont(size=FS_BODY),
            command=self._toggle_mute,
        )
        self.mute_btn.grid(
            row=0,
            column=3,
            sticky="e",
        )


        # -- buffer meter with the stutter state folded in as a badge --
        meter_row = ctk.CTkFrame(self, fg_color="transparent")
        meter_row.grid(row=3, column=0, sticky="ew", padx=PAD_M, pady=(0, PAD_M))
        meter_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(meter_row, text="BUFFER", text_color=TEXT_FAINT, width=44, anchor="w",
                     font=ctk.CTkFont(size=FS_LABEL, weight="bold")
                     ).grid(row=0, column=0, sticky="w")

        self.buffer_bar = ctk.CTkProgressBar(meter_row, progress_color=GOOD, height=6,
                                             fg_color=CARD_HI, corner_radius=3)
        self.buffer_bar.set(0)
        self.buffer_bar.grid(row=0, column=1, sticky="ew", padx=PAD_S)

        # A faint tick where the "healthy" (green) zone begins, so users learn what
        # a good buffer looks like at a glance instead of decoding colours.
        self.buffer_tick = ctk.CTkFrame(self.buffer_bar, width=2, height=10,
                                        corner_radius=1, fg_color=TEXT_FAINT)
        self.buffer_tick.place(relx=BUFFER_TARGET_MS / BUFFER_METER_MAX_MS,
                               rely=0.5, anchor="center")

        self.buffer_label = ctk.CTkLabel(meter_row, text="--", width=52, anchor="e",
                                         text_color=TEXT_DIM,
                                         font=ctk.CTkFont(size=FS_LABEL))
        self.buffer_label.grid(row=0, column=2, sticky="e")

        self.stutter_label = ctk.CTkLabel(meter_row, text="", width=96, anchor="e",
                                          text_color=TEXT_FAINT,
                                          font=ctk.CTkFont(size=FS_LABEL))
        self.stutter_label.grid(row=0, column=3, sticky="e", padx=(PAD_S, 0))
        self._refresh_enabled_style()

    def _refresh_enabled_style(self):
        """Make inactive device controls read as inactive without hiding settings."""
        active = self.enabled_var.get()
        control_text = TEXT if active else TEXT_FAINT
        slider_color = ACCENT if active else CARD_HI
        self.delay_slider.configure(progress_color=slider_color, button_color=slider_color)
        self.volume_slider.configure(progress_color=slider_color, button_color=slider_color)
        self.delay_label.configure(text_color=control_text)
        self.volume_label.configure(text_color=control_text)
        self.buffer_label.configure(text_color=control_text)
        self.stutter_label.configure(text_color=TEXT_DIM if active else TEXT_FAINT)

    def _nudge_btn(self, master, label, delta):
        button = ctk.CTkButton(
            master, text=label, width=24, height=24, corner_radius=12,
            fg_color="transparent", hover_color=CARD_HI, border_width=1,
            border_color=BORDER, text_color=TEXT_DIM,
            font=ctk.CTkFont(size=FS_BODY, weight="bold"),
            command=lambda d=delta: self._nudge(d),
        )
        # Right-click provides a finer one-millisecond adjustment.
        button.bind("<Button-3>", lambda _event, d=delta: self._nudge(1 if d > 0 else -1))
        return button

    def _toggled(self):
        self._refresh_enabled_style()
        self.on_toggle(self.info["name"], self.enabled_var.get())

    def _delay_changed(self, value):
        value = float(value)
        self.delay_label.configure(text=f"{int(value)} ms")
        self.on_delay_change(self.info["name"], value)

    def _nudge(self, delta):
        new_val = max(DELAY_MIN, min(DELAY_MAX, self.delay_var.get() + delta))
        self.delay_var.set(new_val)
        self._delay_changed(new_val)

    def effective_volume(self):
        """Linear gain (0.0-1.0) the output should apply: 0 while muted."""
        if self.muted_var.get():
            return 0.0
        return self.volume_var.get() / 100.0

    def _volume_changed(self, value):
        # Dragging the slider up while muted is treated as an intent to unmute.
        if self.muted_var.get() and float(value) > 0:
            self.muted_var.set(False)
            self._refresh_mute_button()
        self.volume_label.configure(text=f"{int(float(value))}%")
        self.on_volume_change(self.info["name"], self.effective_volume())

    def _toggle_mute(self):
        self.muted_var.set(not self.muted_var.get())
        self._refresh_mute_button()
        self.on_volume_change(self.info["name"], self.effective_volume())

    def set_muted(self, muted):
        """Set mute to an explicit state (used by the web remote)."""
        if bool(muted) != self.muted_var.get():
            self._toggle_mute()

    def _refresh_mute_button(self):
        if self.muted_var.get():
            self.mute_btn.configure(text="\U0001F507", text_color=BAD)
        else:
            self.mute_btn.configure(text="\U0001F50A", text_color=TEXT_DIM)

    def _show_slider_tip(self, text):
        """Show a small value bubble that follows the pointer while dragging."""
        tip = getattr(self, "_slider_tip", None)
        if tip is None or not tip.winfo_exists():
            tip = tk.Toplevel(self)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            self._slider_tip_lbl = tk.Label(
                tip, bd=0, padx=8, pady=3, fg="#06121f",
                font=("Segoe UI", 9, "bold"))
            self._slider_tip_lbl.pack()
            self._slider_tip = tip
        self._slider_tip_lbl.config(text=text, bg=_c(ACCENT))
        x, y = self.winfo_pointerxy()
        self._slider_tip.geometry(f"+{x - 18}+{y - 40}")
        self._slider_tip.deiconify()

    def _hide_slider_tip(self, _event=None):
        tip = getattr(self, "_slider_tip", None)
        if tip is not None and tip.winfo_exists():
            tip.withdraw()

    def apply_saved(self, delay=None, volume=None, muted=None, enabled=None):
        """Restore persisted settings onto the widgets WITHOUT firing the change
        callbacks (setting a var directly doesn't trigger a slider/switch command),
        so restoring doesn't cause spurious writes back to the store."""
        if delay is not None:
            self.delay_var.set(float(delay))
            self.delay_label.configure(text=f"{int(float(delay))} ms")
        if volume is not None:
            self.volume_var.set(float(volume))
            self.volume_label.configure(text=f"{int(float(volume))}%")
        if muted is not None:
            self.muted_var.set(bool(muted))
            self._refresh_mute_button()
        if enabled is not None:
            self.enabled_var.set(bool(enabled))
        self._refresh_enabled_style()

    def update_live_stats(self, buffer_ms, stutter_count, active: bool):
        if not active:
            self.buffer_bar.set(0)
            self.buffer_bar.configure(progress_color=CARD_HI)
            self.buffer_label.configure(text="--")
            self.stutter_label.configure(text="")
            self._prev_stutter_count = 0
            return

        frac = max(0.0, min(1.0, buffer_ms / BUFFER_METER_MAX_MS))
        self.buffer_bar.set(frac)
        if buffer_ms < 40:
            color = BAD
        elif buffer_ms < 90:
            color = WARN
        else:
            color = GOOD
        self.buffer_bar.configure(progress_color=color)
        self.buffer_label.configure(text=f"{int(buffer_ms)} ms")

        if stutter_count > self._prev_stutter_count:
            self._last_stutter_mono = time.monotonic()
        elif stutter_count < self._prev_stutter_count:
            self._last_stutter_mono = None  # counter reset (device restarted)
        self._prev_stutter_count = stutter_count

        if stutter_count > 0:
            self.stutter_label.configure(
                text=f"⚠ {stutter_count} · {self._format_ago(self._last_stutter_mono)}",
                text_color=WARN)
        else:
            self.stutter_label.configure(text="✓ clean", text_color=GOOD)

    @staticmethod
    def _format_ago(mono):
        """Compact 'time since' label for the last stutter."""
        if mono is None:
            return "now"
        delta = time.monotonic() - mono
        if delta < 60:
            return f"{int(delta)}s ago"
        if delta < 3600:
            return f"{int(delta // 60)}m ago"
        return f"{int(delta // 3600)}h ago"


class AudioEndpointWatcher:
    """Fires the instant an audio render endpoint is disabled/removed by Windows.

    Waiting for a dropped device's audio *callback* to spin or stall (our previous
    approach) is slow: Windows keeps the endpoint nominally alive for a while after
    a Bluetooth device physically disconnects, so there's a multi-second lag before
    we notice. The OS itself knows far sooner -- a WinRT DeviceWatcher built on the
    audio-render selector (which only matches *enabled* interfaces) fires `Removed`
    almost immediately when the endpoint is disabled. We record the friendly name of
    anything removed; the UI loop drains those names and tears the matching output
    down at once.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._id_to_name = {}        # device interface id -> friendly name
        self._removed_names = set()  # friendly names removed since last drain
        self._watcher = None

    def start(self):
        try:
            from winrt.windows.devices.enumeration import DeviceWatcher, DeviceInformation
            from winrt.windows.media.devices import MediaDevice

            selector = MediaDevice.get_audio_render_selector()
            watcher = DeviceInformation.create_watcher_aqs_filter(selector)
            watcher.add_added(self._on_added)
            watcher.add_updated(self._on_updated)
            watcher.add_removed(self._on_removed)
            watcher.start()
            self._watcher = watcher
        except Exception as exc:
            # If the watcher can't be created we simply fall back to the callback
            # spin/stall detection -- slower, but still functional.
            print(f"[SyncMirror] AudioEndpointWatcher unavailable: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            self._watcher = None

    def stop(self):
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                pass
            self._watcher = None

    def _on_added(self, sender, info):
        try:
            with self._lock:
                self._id_to_name[info.id] = info.name
        except Exception:
            pass

    def _on_updated(self, sender, update):
        # Keep the id->name map warm; updates don't carry the name reliably.
        pass

    def _on_removed(self, sender, update):
        try:
            with self._lock:
                name = self._id_to_name.pop(update.id, None)
                if name:
                    self._removed_names.add(name)
                    print(f"[SyncMirror] Endpoint watcher: '{name}' removed by OS.",
                          flush=True)
        except Exception:
            pass

    def drain_removed(self):
        """Return (and clear) the set of friendly names removed since last call."""
        with self._lock:
            if not self._removed_names:
                return frozenset()
            names = frozenset(self._removed_names)
            self._removed_names.clear()
            return names


class App:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("SyncMirror")
        self.root.geometry("1280x780")
        self.root.minsize(1080, 650)
        self.root.configure(fg_color=BG)
        try:
            self.root.iconbitmap(ICON_PATH)
        except Exception:
            pass

        self.ring_buffer = None
        self.capture = LoopbackCapture(samplerate=None, channels=CHANNELS)
        self.media = MediaController()

        self.endpoint_watcher = AudioEndpointWatcher()
        self.endpoint_watcher.start()

        self.cards = {}            # device name -> DeviceCard
        self.device_outputs = {}   # device name -> DeviceOutput
        self.running = False
        self._device_scan_results = queue.Queue()
        self._device_scan_running = False
        self.source_delay = tk.DoubleVar(value=0.0)
        self._updating = False
        self._buffer_low_seen = False
        self._auto_reset_pending = False
        self._auto_reset_handle = None
        self._artwork_bytes = None
        self._artwork_image = None
        self._artwork_key = None
        self._artwork_fetching_key = None
        self._artwork_width = ARTWORK_WIDTH
        self._artwork_height = ARTWORK_HEIGHT
        self._artwork_resize_handle = None
        self._art_bg = None            # cached blurred backdrop (RGB) at current size
        self._art_fg = None            # cached small sharp thumbnail (RGBA)
        self._art_fg_offset = (0, 0)
        self._bar_colors = None        # per-bar RGB colors sampled from the artwork
        self._vis_levels = [0.0] * VIS_BARS
        self._artwork_results = queue.Queue()
        self._metadata_results = queue.Queue()
        self._metadata_request_running = False
        self._timeline_position_seconds = None
        self._timeline_duration_seconds = None
        self._timeline_anchor = time.monotonic()
        self._timeline_playing = False
        self._timeline_track_key = None
        self._last_track_seen_at = 0.0
        self.tray_icon = None
        self._tray_state_sig = None
        self.settings = SettingsStore()
        self._settings_save_handle = None
        self.web_remote = None
        self.web_command_queue = queue.Queue()
        self._web_dialog = None          # persistent panel; kept open while server runs
        self._web_enabled_var = None

        self._build_ui()
        self._populate_devices()
        self.root.after(500, self._poll_device_scan_results)
        self.root.after(1500, self._auto_rescan)
        self._update_track_metadata()
        self._poll_artwork_results()
        self._poll_metadata_results()
        self._tick_playback_clock()
        if Image is not None:
            self._tick_visualizer()
        self._build_tray()
        self._poll_web_commands()
        self._publish_web_state()

    # ------------------------------------------------------------- UI --

    def _build_ui(self):
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=PAD_L, pady=(PAD_L, PAD_S))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")

        ctk.CTkLabel(title_row, text="SyncMirror", text_color=TEXT,
                     font=ctk.CTkFont(size=FS_DISPLAY, weight="bold")).pack(side="left")

        status_frame = ctk.CTkFrame(title_row, fg_color=CARD_BG, corner_radius=14,
                                    border_width=1, border_color=BORDER)
        status_frame.pack(side="right")
        self.status_dot = ctk.CTkLabel(status_frame, text="\u25cf", text_color=BAD,
                                       font=ctk.CTkFont(size=FS_BODY))
        self.status_dot.pack(side="left", padx=(PAD_M, PAD_S), pady=PAD_XS)
        self.status_label = ctk.CTkLabel(status_frame, text="Stopped", text_color=TEXT_DIM,
                                         font=ctk.CTkFont(size=FS_LABEL, weight="bold"))
        self.status_label.pack(side="left", padx=(0, PAD_M))
        self._pulse_phase = 0
        self._pulse_status_dot()

        # -- theme switcher: defaults to the OS theme, user can pin Light/Dark --
        self.theme_var = tk.StringVar(value="System")
        self.theme_switcher = ctk.CTkSegmentedButton(
            title_row, values=["System", "Light", "Dark"], variable=self.theme_var,
            command=self._on_theme_change, fg_color=CARD_BG, selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER, unselected_color=CARD_BG,
            unselected_hover_color=CARD_HI, text_color=TEXT_DIM,
            border_width=1, corner_radius=14,
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
        )
        self.theme_switcher.pack(side="right", padx=(0, PAD_S))

        ctk.CTkLabel(header, text="Synchronized audio mirroring across Bluetooth devices",
                     text_color=TEXT_FAINT, font=ctk.CTkFont(size=FS_LABEL)
                     ).pack(anchor="w", pady=(PAD_XS, 0))

        # -- split workspace: playback on the left, sync/devices on the right --
        workspace = ctk.CTkFrame(self.root, fg_color="transparent")
        workspace.pack(fill="both", expand=True, padx=PAD_L, pady=(PAD_S, PAD_L))
        workspace.grid_columnconfigure(0, weight=1, uniform="pane")
        workspace.grid_columnconfigure(1, weight=1, uniform="pane")
        workspace.grid_rowconfigure(0, weight=1)

        playback = ctk.CTkFrame(workspace, fg_color=CARD_BG, corner_radius=12,
                                border_width=1, border_color=BORDER)
        playback.grid(row=0, column=0, sticky="nsew", padx=(0, PAD_S))
        playback.grid_columnconfigure(0, weight=1)
        playback.bind("<Configure>", self._schedule_artwork_resize)
        self.playback_pane = playback

        devices_pane = ctk.CTkFrame(workspace, fg_color="transparent")
        devices_pane.grid(row=0, column=1, sticky="nsew", padx=(PAD_S, 0))

        # -- sync controls --
        controls = ctk.CTkFrame(devices_pane, fg_color=CARD_BG, corner_radius=12,
                                border_width=1, border_color=BORDER)
        controls.pack(fill="x", pady=(0, PAD_S))

        btn_row = ctk.CTkFrame(controls, fg_color="transparent")
        btn_row.pack(fill="x", padx=PAD_M, pady=(PAD_M, PAD_S))

        # Only the primary action is filled; everything else is a ghost button.
        self.start_btn = ctk.CTkButton(btn_row, text="Start Sync", command=self.start_sync,
                                       fg_color=GOOD, hover_color="#33b35f", width=118,
                                       height=34, corner_radius=8,
                                       font=ctk.CTkFont(size=FS_BODY, weight="bold"))
        self.start_btn.pack(side="left")

        self.stop_btn = self._ghost_button(btn_row, "Stop", self.stop_sync, width=80)
        self.stop_btn.configure(state="disabled")
        self.stop_btn.pack(side="left", padx=(PAD_S, 0))

        self.reset_btn = self._ghost_button(btn_row, "Reset Sync", self._reset_adaptive_delays,
                                            width=98)
        self.reset_btn.configure(state="disabled")
        self.reset_btn.pack(side="left", padx=(PAD_S, 0))

        self.web_btn = self._ghost_button(btn_row, "Web Remote", self._toggle_web_remote,
                                          width=110)
        if WebRemote is None:
            self.web_btn.configure(state="disabled")
        self.web_btn.pack(side="right")

        src_row = ctk.CTkFrame(controls, fg_color=CARD_HI, corner_radius=8)
        src_row.pack(fill="x", padx=PAD_M, pady=(0, PAD_M))
        ctk.CTkLabel(src_row, text="GLOBAL SOURCE DELAY", text_color=TEXT_FAINT,
                     font=ctk.CTkFont(size=FS_LABEL, weight="bold")
                     ).pack(side="left", padx=PAD_M, pady=PAD_S)

        self.source_delay_label = ctk.CTkLabel(src_row, text="0 ms", width=56,
                                               text_color=TEXT,
                                               font=ctk.CTkFont(size=FS_BODY, weight="bold"))
        for label, delta in (("-20", -20), ("-10", -10)):
            self._ghost_button(src_row, label, lambda d=delta: self._change_source_delay(d),
                               width=42, height=24, small=True
                               ).pack(side="left", padx=(0, PAD_XS))
        self.source_delay_label.pack(side="left", padx=PAD_S)
        for label, delta in (("+10", 10), ("+20", 20)):
            self._ghost_button(src_row, label, lambda d=delta: self._change_source_delay(d),
                               width=42, height=24, small=True
                               ).pack(side="left", padx=(0, PAD_XS))

        # -- playback area --
        artwork_frame = ctk.CTkFrame(
            playback, fg_color=ARTWORK_BG, corner_radius=10,
            width=ARTWORK_WIDTH, height=ARTWORK_HEIGHT,
        )
        artwork_frame.grid(row=0, column=0, padx=PAD_M, pady=(PAD_M, PAD_M))
        artwork_frame.grid_propagate(False)
        self.artwork_frame = artwork_frame
        self.artwork_label = tk.Label(
            artwork_frame, text="\u266a", bg=_c(ARTWORK_BG), fg=_c(ARTWORK_PLACEHOLDER_FG),
            font=("Segoe UI", 44), borderwidth=0, highlightthickness=0,
        )
        self.artwork_label.pack(fill="both", expand=True)

        playback_header = ctk.CTkFrame(playback, fg_color="transparent")
        playback_header.grid(row=1, column=0, sticky="ew", padx=PAD_M)
        playback_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(playback_header, text="NOW PLAYING", text_color=TEXT_FAINT,
                     font=ctk.CTkFont(size=FS_LABEL, weight="bold")
                     ).grid(row=0, column=0, sticky="w")
        self.playback_status_label = ctk.CTkLabel(playback_header, text="NO SESSION",
                                                  text_color=TEXT_FAINT,
                                                  font=ctk.CTkFont(size=FS_LABEL, weight="bold"))
        self.playback_status_label.grid(row=0, column=1, sticky="e")

        # Title / artist carry the hierarchy; album + source collapse to one dim line.
        self.track_name_label = ctk.CTkLabel(
            playback, text="No active track detected", anchor="w", justify="left",
            text_color=TEXT, font=ctk.CTkFont(size=FS_TITLE, weight="bold"),
        )
        self.track_name_label.grid(row=2, column=0, sticky="ew", padx=PAD_M, pady=(PAD_S, 0))
        self.track_artist_label = ctk.CTkLabel(
            playback, text="Start playback in another app", anchor="w", justify="left",
            text_color=TEXT_DIM, font=ctk.CTkFont(size=FS_BODY),
        )
        self.track_artist_label.grid(row=3, column=0, sticky="ew", padx=PAD_M, pady=(PAD_XS, 0))
        self.track_meta_label = ctk.CTkLabel(
            playback, text="", anchor="w", justify="left", text_color=TEXT_FAINT,
            font=ctk.CTkFont(size=FS_LABEL),
        )
        self.track_meta_label.grid(row=4, column=0, sticky="ew", padx=PAD_M, pady=(PAD_XS, 0))

        self.track_progress = ctk.CTkProgressBar(playback, progress_color=ACCENT, height=5,
                                                 fg_color=CARD_HI, corner_radius=3)
        self.track_progress.set(0)
        self.track_progress.grid(row=5, column=0, sticky="ew", padx=PAD_M, pady=(PAD_M, PAD_XS))

        # Elapsed / remaining sit at the ends of the bar instead of on their own line.
        time_row = ctk.CTkFrame(playback, fg_color="transparent")
        time_row.grid(row=6, column=0, sticky="ew", padx=PAD_M)
        time_row.grid_columnconfigure(0, weight=1)
        self.track_time_label = ctk.CTkLabel(time_row, text="--:--", text_color=TEXT_FAINT,
                                             font=ctk.CTkFont(size=FS_LABEL))
        self.track_time_label.grid(row=0, column=0, sticky="w")
        self.track_remaining_label = ctk.CTkLabel(time_row, text="--:--", text_color=TEXT_FAINT,
                                                  font=ctk.CTkFont(size=FS_LABEL))
        self.track_remaining_label.grid(row=0, column=1, sticky="e")

        transport = ctk.CTkFrame(playback, fg_color="transparent")
        transport.grid(row=7, column=0, pady=(PAD_M, PAD_M))
        self.prev_btn = ctk.CTkButton(
            transport, text="\u23ee", width=40, height=40, corner_radius=20,
            command=self._media_previous, fg_color="transparent", hover_color=CARD_HI,
            text_color=TEXT_DIM, font=ctk.CTkFont(size=16),
        )
        self.prev_btn.pack(side="left", padx=PAD_M)
        self.play_btn = ctk.CTkButton(
            transport, text="\u25b6", width=52, height=52, corner_radius=26,
            command=self._media_play_pause, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.play_btn.pack(side="left", padx=PAD_M)
        self.next_btn = ctk.CTkButton(
            transport, text="\u23ed", width=40, height=40, corner_radius=20,
            command=self._media_next, fg_color="transparent", hover_color=CARD_HI,
            text_color=TEXT_DIM, font=ctk.CTkFont(size=16),
        )
        self.next_btn.pack(side="left", padx=PAD_M)

        # -- devices list header: refresh demoted to an icon-tier action --
        devices_header = ctk.CTkFrame(devices_pane, fg_color="transparent")
        devices_header.pack(fill="x", padx=PAD_XS, pady=(PAD_S, PAD_XS))
        ctk.CTkLabel(devices_header, text="OUTPUT DEVICES", text_color=TEXT_FAINT,
                     font=ctk.CTkFont(size=FS_LABEL, weight="bold")).pack(side="left")
        self.refresh_btn = ctk.CTkButton(
            devices_header, text="\u27f3", width=26, height=26, corner_radius=13,
            command=self._populate_devices, fg_color="transparent", hover_color=CARD_HI,
            text_color=TEXT_DIM, font=ctk.CTkFont(size=FS_BODY),
        )
        self.refresh_btn.pack(side="right")

        # -- scrollable device list --
        self.devices_frame = ctk.CTkScrollableFrame(devices_pane, fg_color="transparent")
        self.devices_frame.pack(fill="both", expand=True)
        self.empty_devices_label = ctk.CTkLabel(
            self.devices_frame,
            text="No output devices detected\nConnect a speaker or headset, then refresh.",
            text_color=TEXT_FAINT,
            font=ctk.CTkFont(size=FS_BODY),
            justify="center",
        )
        self.empty_devices_label.pack(fill="x", pady=PAD_L)

        ctk.CTkLabel(
            devices_pane,
            text="Tip: start sync, play audio, then nudge each device's delay until they line up.",
            text_color=TEXT_FAINT, font=ctk.CTkFont(size=FS_LABEL), justify="left",
        ).pack(anchor="w", padx=PAD_XS, pady=(PAD_S, 0))

        self.root.bind("<space>", self._keyboard_play_pause)
        self.root.bind("<Control-m>", self._keyboard_mute)

    def _keyboard_play_pause(self, _event=None):
        self._media_play_pause()
        return "break"

    def _keyboard_mute(self, _event=None):
        widget = self.root.focus_get()
        while widget is not None:
            if isinstance(widget, DeviceCard):
                widget._toggle_mute()
                return "break"
            widget = getattr(widget, "master", None)
        return "break"

    def _ghost_button(self, master, text, command, width=90, height=34, small=False):
        """Secondary action: outlined, transparent, never competing with Start."""
        return ctk.CTkButton(
            master, text=text, command=command, width=width, height=height,
            corner_radius=8 if not small else 6, fg_color="transparent",
            hover_color=CARD_HI, border_width=1, border_color=BORDER,
            text_color=TEXT_DIM,
            font=ctk.CTkFont(size=FS_LABEL if small else FS_BODY,
                             weight="normal" if small else "bold"),
        )

    def _pulse_status_dot(self):
        """Soft breathing pulse on the status dot while sync is running."""
        if getattr(self, "running", False):
            self._pulse_phase = (self._pulse_phase + 1) % 20
            t = abs(10 - self._pulse_phase) / 10.0
            shades = ("#2b8f4f", "#33a95d", "#3ecf6e", "#63e08c")
            self.status_dot.configure(text_color=shades[int(t * (len(shades) - 1))])
        self.root.after(90, self._pulse_status_dot)

    def _on_theme_change(self, choice):
        """Switch appearance mode. CustomTkinter widgets follow the (light, dark)
        colour pairs automatically; the raw tk.Label + PIL backdrop are refreshed
        by hand since they don't observe the appearance-mode change."""
        ctk.set_appearance_mode(choice)  # "System" / "Light" / "Dark"
        self._apply_manual_theme()

    def _apply_manual_theme(self):
        """Re-resolve theme-dependent colours on surfaces CTk doesn't manage."""
        if getattr(self, "artwork_label", None) is not None:
            self.artwork_label.configure(
                bg=_c(ARTWORK_BG), fg=_c(ARTWORK_PLACEHOLDER_FG)
            )
        # Rebuild the cached backdrop so the empty-art colour matches the theme.
        self._set_artwork(self._artwork_bytes, force=True)

    def _populate_devices(self):
        """Trigger a device scan. Runs off the UI thread since re-enumerating WASAPI
        hardware (needed to see newly plugged/unplugged devices) can take a beat."""
        if self._device_scan_running:
            return
        self._device_scan_running = True
        threading.Thread(target=self._scan_devices_worker, daemon=True).start()

    def _auto_rescan(self):
        """Periodically re-scan for hardware changes so devices appear/disappear on
        their own, without the user pressing 'Refresh Devices'. Kept short so a
        dropped device (and its dead output stream) is torn down quickly."""
        self._populate_devices()
        self.root.after(1500, self._auto_rescan)

    def _scan_devices_worker(self):
        try:
            devices = self.capture.list_output_devices()
        except Exception:
            devices = None
        self._device_scan_results.put(devices)

    def _poll_device_scan_results(self):
        try:
            while True:
                devices = self._device_scan_results.get_nowait()
                self._device_scan_running = False
                if devices is not None:
                    self._apply_device_scan(devices)
        except queue.Empty:
            pass
        self.root.after(500, self._poll_device_scan_results)

    def _apply_device_scan(self, devices):
        """Reconcile the on-screen device cards with what's currently attached:
        add cards for newly-seen devices, and remove cards (stopping any active
        output first) for devices that were unplugged/disabled."""
        current_names = set()
        for info in devices:
            name = info["name"]
            current_names.add(name)
            if name in self.cards:
                # device is still here -- keep the existing widget/settings, just
                # refresh the cached info (its numeric index may have shifted)
                self.cards[name].info = info
            else:
                card = DeviceCard(self.devices_frame, info,
                                 on_toggle=self._on_toggle, on_delay_change=self._on_delay_change,
                                 on_volume_change=self._on_volume_change)
                saved = self.settings.get(name)
                if saved:
                    card.apply_saved(
                        delay=saved.get("delay"), volume=saved.get("volume"),
                        muted=saved.get("muted"), enabled=saved.get("enabled"),
                    )
                card.pack(fill="x", pady=6)
                self.cards[name] = card

        for name in list(self.cards.keys()):
            if name not in current_names:
                out = self.device_outputs.pop(name, None)
                if out:
                    out.stop()
                self.cards[name].destroy()
                del self.cards[name]

        if current_names:
            self.empty_devices_label.pack_forget()
        elif not self.empty_devices_label.winfo_ismapped():
            self.empty_devices_label.pack(fill="x", pady=PAD_L)

        if self.running:
            self.status_label.configure(
                text=f"Running — {len(self.device_outputs)} device(s)", text_color=TEXT
            )

    # --------------------------------------------------------- live loop --

    def _update_live_stats(self):
        if not self._updating:
            return

        # Fast-path removal of dead outputs: if a device dropped (e.g. a Bluetooth
        # earbud disconnected), its stream is invalidated by WASAPI. Leaving it in
        # place lets the dead stream keep pulling on the shared audio engine, which
        # stutters the still-connected devices, so we tear it down immediately.
        #
        # Two detectors feed this:
        #   1. The OS endpoint watcher (fastest) -- Windows tells us the moment an
        #      audio endpoint is disabled, well before the stream misbehaves.
        #   2. is_alive() -- a fallback that notices the callback spinning/stalling
        #      if the watcher ever misses an event.
        removed_by_os = self.endpoint_watcher.drain_removed()

        def _matches_removed(out_name: str) -> bool:
            for r in removed_by_os:
                if r == out_name or r in out_name or out_name in r:
                    return True
            return False

        dropped = [
            name for name, out in self.device_outputs.items()
            if _matches_removed(name) or not out.is_alive()
        ]
        for name in dropped:
            print(f"[SyncMirror] Output '{name}' looks dead (disconnected) -> stopping it.", flush=True)
            out = self.device_outputs.pop(name, None)
            if out:
                # Tear the dead stream down on a background thread. Closing a dropped
                # Bluetooth stream can block for seconds while WASAPI/PortAudio drains
                # or times out the invalid endpoint -- and it holds PortAudio's global
                # lock while it does. Running it inline here would freeze the UI *and*
                # starve the still-connected device for exactly as long as the drop was
                # already stuttering it. The callback has already self-aborted, so the
                # stream is silent; we just need close()/terminate() off the UI thread.
                threading.Thread(target=out.stop, name=f"teardown-{name}", daemon=True).start()
            card = self.cards.get(name)
            if card is not None:
                card.enabled_var.set(False)
                card.update_live_stats(0, 0, active=False)
        if dropped:
            # Kick a rescan so the card for the removed device disappears promptly too.
            self._populate_devices()
            if self.running:
                self.status_label.configure(
                    text=f"Running — {len(self.device_outputs)} device(s)", text_color=TEXT
                )

        lowest_buffer_ms = None
        for idx, card in list(self.cards.items()):
            out = self.device_outputs.get(idx)
            if out and self.ring_buffer:
                avail = self.ring_buffer.frames_available(out.reader)
                ms = (avail / float(self.ring_buffer.samplerate)) * 1000.0
                stutters = self.ring_buffer.get_stutter_count(out.reader.name)
                card.update_live_stats(ms, stutters, active=True)
                lowest_buffer_ms = ms if lowest_buffer_ms is None else min(lowest_buffer_ms, ms)
            else:
                card.update_live_stats(0, 0, active=False)

        if lowest_buffer_ms is not None:
            self._check_auto_reset_condition(lowest_buffer_ms)

        self.root.after(100, self._update_live_stats)

    # --------------------------------------------------------- actions --

    def start_sync(self):
        selected = [idx for idx, card in self.cards.items() if card.enabled_var.get()]
        if not selected:
            messagebox.showwarning("No devices selected", "Turn on at least one output device to mirror to.")
            return

        # Recreate capture from scratch so this process's PortAudio is torn down to
        # a 0 refcount and re-initialised. That re-enumeration is what lets us open
        # streams on devices that were plugged in *after* the app launched.
        try:
            self.capture.close()
        except Exception:
            pass
        self.capture = LoopbackCapture(samplerate=None, channels=CHANNELS)

        try:
            samplerate = self.capture.prepare()
            self.ring_buffer = SyncRingBuffer(RING_BUFFER_SECONDS, CHANNELS, samplerate)
            self.capture.start(self.ring_buffer)
        except Exception as e:
            messagebox.showerror("Capture error", str(e))
            return

        failed = []
        for idx in selected:
            card = self.cards[idx]
            try:
                out = DeviceOutput(
                    card.info["name"], card.info, self.ring_buffer,
                    base_safety_ms=BASE_SAFETY_MS,
                    user_delay_ms=card.delay_var.get() + self.source_delay.get(),
                    volume=card.effective_volume(),
                )
                out.start()
                self.device_outputs[idx] = out
            except Exception:
                failed.append(card.info["name"])
                card.enabled_var.set(False)

        self.running = True
        self.status_dot.configure(text_color=GOOD)
        self.status_label.configure(text=f"Running — {len(self.device_outputs)} device(s)",
                                     text_color=TEXT)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.reset_btn.configure(state="normal")

        self._updating = True
        self._update_live_stats()

        if failed:
            messagebox.showwarning(
                "Device unavailable",
                "Could not start these device(s):\n" + "\n".join(failed),
            )

    def stop_sync(self):
        for out in self.device_outputs.values():
            out.stop()
        self.device_outputs.clear()
        self.capture.stop()

        self.running = False
        self._updating = False
        self.status_dot.configure(text_color=BAD)
        self.status_label.configure(text="Stopped", text_color=TEXT_DIM)
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.reset_btn.configure(state="disabled")

        for card in self.cards.values():
            card.update_live_stats(0, 0, active=False)

    def _on_toggle(self, device_index, enabled):
        self._record_device_settings(device_index)
        if not self.running:
            return

        card = self.cards.get(device_index)
        if card is None:
            return

        if enabled:
            if device_index in self.device_outputs:
                return
            try:
                out = DeviceOutput(
                    card.info["name"], card.info, self.ring_buffer,
                    base_safety_ms=BASE_SAFETY_MS,
                    user_delay_ms=card.delay_var.get() + self.source_delay.get(),
                    volume=card.effective_volume(),
                )
                out.start()
                self.device_outputs[device_index] = out
            except Exception:
                # Device was almost certainly connected after the app launched, so
                # this process's PortAudio doesn't know about it yet. Rebuild the
                # whole engine (forcing a fresh hardware scan) so it becomes usable.
                self._rebuild_audio_engine()
        else:
            out = self.device_outputs.pop(device_index, None)
            if out:
                out.stop()

    def _rebuild_audio_engine(self):
        """Tear PortAudio fully down (every PyAudio instance in this process) so the
        next Pa_Initialize() re-enumerates hardware, then restart capture and every
        enabled output. This is what allows a device connected *after* app launch to
        be mirrored. Causes a brief (sub-second) audio gap on the other devices."""
        if not self.running:
            return

        delays = {name: card.delay_var.get() for name, card in self.cards.items()}
        enabled = [name for name, card in self.cards.items() if card.enabled_var.get()]

        # tear everything down -> PortAudio refcount reaches 0
        for out in self.device_outputs.values():
            out.stop()
        self.device_outputs.clear()
        try:
            self.capture.close()
        except Exception:
            pass

        # fresh instance -> next Pa_Initialize() is a genuine 0 -> 1 hardware scan
        self.capture = LoopbackCapture(samplerate=None, channels=CHANNELS)
        try:
            samplerate = self.capture.prepare()
            self.ring_buffer = SyncRingBuffer(RING_BUFFER_SECONDS, CHANNELS, samplerate)
            self.capture.start(self.ring_buffer)
        except Exception as e:
            messagebox.showerror("Capture error", str(e))
            self.stop_sync()
            return

        failed = []
        for name in enabled:
            card = self.cards.get(name)
            if card is None:
                continue
            try:
                out = DeviceOutput(
                    name, card.info, self.ring_buffer,
                    base_safety_ms=BASE_SAFETY_MS,
                    user_delay_ms=delays.get(name, 0.0) + self.source_delay.get(),
                    volume=card.effective_volume(),
                )
                out.start()
                self.device_outputs[name] = out
            except Exception:
                failed.append(name)
                card.enabled_var.set(False)

        self.status_label.configure(text=f"Running — {len(self.device_outputs)} device(s)",
                                     text_color=TEXT)
        if failed:
            messagebox.showwarning(
                "Device unavailable",
                "Could not start these device(s):\n" + "\n".join(failed),
            )

    def _on_delay_change(self, device_index, value):
        out = self.device_outputs.get(device_index)
        if out:
            out.set_user_delay_ms(value + self.source_delay.get())
        self._record_device_settings(device_index)

    def _on_volume_change(self, device_index, volume):
        out = self.device_outputs.get(device_index)
        if out:
            out.set_volume(volume)
        self._record_device_settings(device_index)

    def _record_device_settings(self, name):
        card = self.cards.get(name)
        if card is None:
            return
        self.settings.update(
            name,
            delay=card.delay_var.get(),
            volume=card.volume_var.get(),
            muted=card.muted_var.get(),
            enabled=card.enabled_var.get(),
        )
        self._schedule_settings_save()

    def _schedule_settings_save(self):
        # Debounce: dragging a slider fires rapidly, so coalesce into one disk write.
        if self._settings_save_handle is not None:
            self.root.after_cancel(self._settings_save_handle)
        self._settings_save_handle = self.root.after(600, self._save_settings_now)

    def _save_settings_now(self):
        self._settings_save_handle = None
        self.settings.save()

    def _change_source_delay(self, delta_ms):
        new_val = max(DELAY_MIN, min(DELAY_MAX, self.source_delay.get() + delta_ms))
        self.source_delay.set(new_val)
        self.source_delay_label.configure(text=f"{int(new_val)} ms")

        for idx, out in self.device_outputs.items():
            card = self.cards.get(idx)
            if card:
                out.set_user_delay_ms(card.delay_var.get() + new_val)

    def _media_play_pause(self):
        self.media.play_pause()
        self.root.after(350, lambda: self._update_track_metadata(schedule_next=False))

    def _media_next(self):
        self.media.next_track()
        self.root.after(500, lambda: self._update_track_metadata(schedule_next=False))

    def _media_previous(self):
        self.media.previous_track()
        self.root.after(500, lambda: self._update_track_metadata(schedule_next=False))

    def _check_auto_reset_condition(self, lowest_buffer_ms: float):
        # If playback drops very low and then spikes back up, that often means a track change
        # has just completed. Reset the adaptive sync state shortly after the spike.
        if self._auto_reset_pending:
            return

        if lowest_buffer_ms < 25.0:
            self._buffer_low_seen = True
            return

        if self._buffer_low_seen and lowest_buffer_ms > 120.0:
            self._buffer_low_seen = False
            self._auto_reset_pending = True
            self.status_label.configure(text="Running — auto-reset pending...", text_color=WARN)
            self._auto_reset_handle = self.root.after(1200, self._perform_auto_reset)

    def _perform_auto_reset(self):
        self._auto_reset_handle = None
        self._auto_reset_pending = False
        self._reset_adaptive_delays()
        self.status_label.configure(text=f"Running — {len(self.device_outputs)} device(s)", text_color=TEXT)

    @staticmethod
    def _format_media_time(value):
        if value is None:
            return "--:--"
        try:
            total_seconds = max(0, int(value.total_seconds()))
            minutes, seconds = divmod(total_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"
        except (AttributeError, TypeError, ValueError):
            return "--:--"

    def _schedule_artwork_resize(self, event):
        width = max(280, event.width - 28)
        height = round(width / ARTWORK_ASPECT_RATIO)
        if width == self._artwork_width and height == self._artwork_height:
            return
        self._artwork_width = width
        self._artwork_height = height
        self.artwork_frame.configure(width=width, height=height)
        # Text wraps to the real pane width instead of a fixed 1200px that never wrapped.
        for lbl in (self.track_name_label, self.track_artist_label, self.track_meta_label):
            lbl.configure(wraplength=width)
        if self._artwork_resize_handle is not None:
            self.root.after_cancel(self._artwork_resize_handle)
        self._artwork_resize_handle = self.root.after(100, self._redraw_artwork)

    def _redraw_artwork(self):
        self._artwork_resize_handle = None
        self._set_artwork(self._artwork_bytes, force=True)

    def _set_artwork(self, thumbnail_bytes, force=False):
        if thumbnail_bytes == self._artwork_bytes and not force:
            return
        self._artwork_bytes = thumbnail_bytes
        if self.web_remote is not None:
            self.web_remote.set_artwork(thumbnail_bytes)
        target = (self._artwork_width, self._artwork_height)

        if Image is None or ImageTk is None:
            return

        # No artwork: use a flat themed backdrop so the bars still have a stage.
        if not thumbnail_bytes:
            self._art_bg = Image.new("RGB", target, _c(ARTWORK_EMPTY_RGB))
            self._art_fg = None
            self._art_fg_offset = (0, 0)
            self._bar_colors = None
            self._composite_artwork()
            return

        try:
            with Image.open(BytesIO(thumbnail_bytes)) as raw_image:
                artwork = raw_image.convert("RGBA")
                resampling = getattr(Image, "Resampling", Image).LANCZOS

                # Back layer: the album art fitted to the banner, heavily blurred
                # and dimmed so the synth bars in front stand out.
                background = ImageOps.fit(artwork, target, method=resampling).convert("RGB")
                if ImageFilter is not None:
                    background = background.filter(ImageFilter.GaussianBlur(radius=18))
                    background = background.point(lambda p: int(p * 0.7))
                self._art_bg = background

                # Front layer: the thumbnail kept SMALL (native size, never upscaled).
                foreground = artwork.copy()
                foreground.thumbnail(target, resampling)
                if ImageFilter is not None:
                    foreground = foreground.filter(
                        ImageFilter.UnsharpMask(radius=1.2, percent=110, threshold=3)
                    )
                self._art_fg = foreground
                self._art_fg_offset = (
                    (target[0] - foreground.width) // 2,
                    (target[1] - foreground.height) // 2,
                )
                # Bars take their colors from the album art so the visualizer
                # matches the thumbnail's palette.
                self._bar_colors = self._make_bar_colors(self._palette_from_image(artwork))
            self._composite_artwork()
        except Exception:
            self._art_bg = Image.new("RGB", target, _c(ARTWORK_EMPTY_RGB))
            self._art_fg = None
            self._art_fg_offset = (0, 0)
            self._bar_colors = None
            self._composite_artwork()

    def _composite_artwork(self):
        """Draw the three layers: blurred backdrop -> synth bars -> thumbnail."""
        if self._art_bg is None or Image is None or ImageTk is None:
            return
        w, h = self._art_bg.size
        frame = self._art_bg.convert("RGBA")

        if ImageDraw is not None:
            overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            n = len(self._vis_levels)
            gap = 3
            bar_w = max(2.0, (w - gap * (n + 1)) / n)
            max_h = h * 0.92
            for i, level in enumerate(self._vis_levels):
                bar_h = max(2.0, level * max_h)
                x0 = gap + i * (bar_w + gap)
                x1 = x0 + bar_w
                y1 = h
                y0 = y1 - bar_h
                alpha = int(90 + 110 * min(1.0, level))  # taller = more opaque
                if self._bar_colors is not None:
                    r, g, b = self._bar_colors[i % len(self._bar_colors)]
                else:
                    r, g, b = (77, 163, 255)
                draw.rounded_rectangle([x0, y0, x1, y1], radius=bar_w / 2,
                                       fill=(r, g, b, alpha))
            frame = Image.alpha_composite(frame, overlay)

        if self._art_fg is not None:
            frame.paste(self._art_fg, self._art_fg_offset, self._art_fg)

        new_image = ImageTk.PhotoImage(frame, master=self.root)
        self.artwork_label.configure(image=new_image, text="")
        self._artwork_image = new_image

    def _palette_from_image(self, img, n=6):
        """Extract up to n vibrant RGB colors from the artwork for the bars."""
        try:
            rgb = img.convert("RGB")
            quant = getattr(Image, "Quantize", None)
            method = quant.MEDIANCUT if quant is not None else None
            pal_img = rgb.quantize(colors=max(2, n), method=method) if method is not None \
                else rgb.quantize(colors=max(2, n))
            palette = pal_img.getpalette() or []
            counts = pal_img.getcolors() or []  # list of (count, palette_index)
            colors = []
            for _cnt, idx in sorted(counts, key=lambda c: c[0], reverse=True):
                base = idx * 3
                r, g, b = palette[base:base + 3]
                # skip near-black and near-white so bars stay colorful
                if max(r, g, b) < 45 or min(r, g, b) > 235:
                    continue
                colors.append((r, g, b))
            if not colors:
                # fall back to whatever we have, else accent blue
                for _cnt, idx in sorted(counts, key=lambda c: c[0], reverse=True):
                    base = idx * 3
                    colors.append(tuple(palette[base:base + 3]))
            return colors or [(77, 163, 255)]
        except Exception:
            return [(77, 163, 255)]

    def _make_bar_colors(self, palette):
        """Spread a palette across VIS_BARS as a smooth left-to-right gradient."""
        import numpy as np
        if not palette:
            return None
        pal = np.array(palette, dtype=np.float32)
        pal = np.clip(pal * 1.15, 0, 255)  # brighten a touch so bars pop
        if len(pal) == 1:
            row = tuple(int(c) for c in pal[0])
            return [row] * VIS_BARS
        xs = np.linspace(0, len(pal) - 1, VIS_BARS)
        lo = np.floor(xs).astype(int)
        hi = np.minimum(lo + 1, len(pal) - 1)
        frac = (xs - lo)[:, None]
        cols = pal[lo] * (1 - frac) + pal[hi] * frac
        return [tuple(int(c) for c in row) for row in cols]

    def _spectrum_bands(self, samples):
        """Turn a mono sample window into VIS_BARS normalized (0..1) band levels."""
        import numpy as np
        samples = samples[-2048:]
        windowed = samples * np.hanning(samples.size)
        spec = np.abs(np.fft.rfft(windowed))
        sr = self.ring_buffer.samplerate if self.ring_buffer else SAMPLERATE
        freqs = np.fft.rfftfreq(samples.size, 1.0 / sr)
        fmin, fmax = 40.0, min(16000.0, sr / 2)
        edges = np.logspace(np.log10(fmin), np.log10(fmax), VIS_BARS + 1)
        # Sample the spectrum at each band's *center* frequency by interpolation.
        # Averaging only the bins that fall inside a band leaves the lowest bands
        # empty (at a 2048-sample window the bins are ~23 Hz apart, wider than the
        # first few log-spaced bands), which made the leftmost bars always flat.
        centers = np.sqrt(edges[:-1] * edges[1:])
        bands = np.interp(centers, freqs, spec)
        bands = np.log1p(bands * 8.0)
        peak = bands.max()
        if peak > 1e-6:
            bands /= peak
        return np.clip(bands, 0.0, 1.0)

    def _tick_visualizer(self):
        import numpy as np
        new = None
        if self.running and self.ring_buffer is not None:
            samples = self.ring_buffer.peek_latest(2048)
            if samples.size >= 128 and float(np.max(np.abs(samples))) > 1e-4:
                new = self._spectrum_bands(samples)
        if new is None:
            # Gentle idle shimmer when nothing is playing.
            t = time.monotonic()
            idx = np.arange(VIS_BARS)
            new = 0.05 + 0.04 * (0.5 + 0.5 * np.sin(t * 2.0 + idx * 0.5))

        for i in range(VIS_BARS):
            cur = self._vis_levels[i]
            k = VIS_ATTACK if new[i] > cur else VIS_DECAY
            self._vis_levels[i] = cur + (float(new[i]) - cur) * k

        self._composite_artwork()
        self.root.after(VIS_INTERVAL_MS, self._tick_visualizer)

    def _request_artwork(self, art_key):
        """Fetch artwork outside Tk's event loop, as some media apps delay the stream."""
        if art_key == self._artwork_key or art_key == self._artwork_fetching_key:
            return
        self._artwork_key = art_key
        self._artwork_fetching_key = art_key
        self._set_artwork(None)
        threading.Thread(target=self._fetch_artwork, args=(art_key,), daemon=True).start()

    def _fetch_artwork(self, expected_key):
        controller = MediaController()
        current = controller.get_current_track(include_thumbnail=True) if controller.winrt_available else None
        thumbnail = current.get("thumbnail") if current and current.get("art_key") == expected_key else None
        self._artwork_results.put((expected_key, thumbnail))

    def _poll_artwork_results(self):
        try:
            while True:
                expected_key, thumbnail = self._artwork_results.get_nowait()
                self._apply_artwork(expected_key, thumbnail)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_artwork_results)

    def _apply_artwork(self, expected_key, thumbnail_bytes):
        if expected_key == self._artwork_key and thumbnail_bytes:
            self._set_artwork(thumbnail_bytes)
        elif expected_key == self._artwork_key:
            # A track transition can briefly expose no session. Retry on the
            # next metadata refresh instead of permanently caching the miss.
            self._artwork_key = None
        if expected_key == self._artwork_fetching_key:
            self._artwork_fetching_key = None

    @staticmethod
    def _media_seconds(value):
        try:
            return max(0.0, value.total_seconds()) if value is not None else None
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _timeline_age(last_updated):
        """Seconds elapsed since GSMTC last stamped the timeline position.

        `last_updated` is a WinRT DateTime projected as a timezone-aware datetime.
        Returns 0.0 if it's missing, malformed, or in the future (clock skew)."""
        if last_updated is None:
            return 0.0
        try:
            now = datetime.now(timezone.utc)
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
            age = (now - last_updated).total_seconds()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 0.0
        # Ignore absurd values (e.g. epoch-zero sentinels) that would fling the
        # clock far past the track; only trust a plausible recent timestamp.
        if age < 0.0 or age > 24 * 3600:
            return 0.0
        return age

    def _render_playback_clock(self):
        position = self._timeline_position_seconds
        duration = self._timeline_duration_seconds
        if position is None or duration is None:
            self.track_time_label.configure(text="--:--")
            self.track_remaining_label.configure(text="--:--")
            self.track_progress.set(0)
            return
        if self._timeline_playing:
            position += time.monotonic() - self._timeline_anchor
        position = min(position, duration) if duration else position
        self.track_time_label.configure(text=self._format_media_time_seconds(position))
        remaining = max(0, (duration or 0) - position)
        self.track_remaining_label.configure(
            text=f"-{self._format_media_time_seconds(remaining)}" if duration else "--:--"
        )
        self.track_progress.set(min(1.0, position / duration) if duration else 0)

    @staticmethod
    def _format_media_time_seconds(seconds):
        if seconds is None:
            return "--:--"
        minutes, seconds = divmod(max(0, int(seconds)), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

    def _tick_playback_clock(self):
        self._render_playback_clock()
        self.root.after(250, self._tick_playback_clock)

    def _update_track_metadata(self, schedule_next=True):
        if not self._metadata_request_running:
            self._metadata_request_running = True
            threading.Thread(target=self._fetch_metadata, daemon=True).start()
        if schedule_next:
            self.root.after(1000, self._update_track_metadata)

    def _fetch_metadata(self):
        metadata_media = MediaController()
        current = metadata_media.get_current_track() if metadata_media.winrt_available else None
        self._metadata_results.put((current, metadata_media.winrt_available, metadata_media.last_error))

    def _poll_metadata_results(self):
        try:
            while True:
                current, winrt_available, error = self._metadata_results.get_nowait()
                self._metadata_request_running = False
                self._apply_track_metadata(current, winrt_available, error)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_metadata_results)

    def _apply_track_metadata(self, current, winrt_available, error):
        if winrt_available:
            if current:
                self._last_track_seen_at = time.monotonic()
                title = current.get("title", "Unknown title")
                artist = current.get("artist", "Unknown artist")
                album = current.get("album", "")
                source = current.get("source", "") or "System media session"
                raw_status = str(current.get("status", "")).upper()
                status = {"4": "PLAYING", "5": "PAUSED", "1": "CLOSED", "2": "OPENED", "3": "CHANGING"}.get(
                    raw_status, raw_status.replace("_", " ") or "ACTIVE"
                )
                position_seconds = self._media_seconds(current.get("position"))
                duration_seconds = self._media_seconds(current.get("duration"))
                track_key = current.get("art_key")
                same_playing_track = (
                    status == "PLAYING"
                    and self._timeline_playing
                    and track_key == self._timeline_track_key
                    and self._timeline_position_seconds is not None
                )
                # GSMTC reports `position` as of `last_updated`, not "now". If the
                # track is playing, the real current position is that value plus the
                # wall-clock time elapsed since. Without this, sessions that were
                # already playing before the app read them (or apps like Chrome that
                # stamp position at track start) would wrongly begin the clock at 0.
                age = self._timeline_age(current.get("last_updated")) if status == "PLAYING" else 0.0
                # Chrome often returns 0 for timeline.position while playing.
                # Keep our monotonic local clock for the same track instead of
                # resetting it to that stale value on every metadata poll.
                if not same_playing_track:
                    if position_seconds is not None:
                        self._timeline_position_seconds = position_seconds
                        self._timeline_anchor = time.monotonic() - age
                    else:
                        self._timeline_position_seconds = position_seconds
                        self._timeline_anchor = time.monotonic()
                self._timeline_duration_seconds = duration_seconds or self._timeline_duration_seconds
                self._timeline_track_key = track_key
                self._timeline_playing = status == "PLAYING"
                self.track_name_label.configure(text=title)
                self.track_artist_label.configure(text=artist)
                meta = "  \u00b7  ".join(x for x in (album or "", source or "") if x)
                self.track_meta_label.configure(text=meta)
                self._render_playback_clock()
                self.playback_status_label.configure(text=status, text_color=GOOD if status == "PLAYING" else TEXT_DIM)
                self.play_btn.configure(text="⏸" if status == "PLAYING" else "▶")
                self._request_artwork(current.get("art_key"))
            else:
                # Keep the previous track visible during Chrome's short session
                # hand-off instead of flashing "No active playback session".
                if time.monotonic() - self._last_track_seen_at < 5.0:
                    self.playback_status_label.configure(text="CHANGING", text_color=TEXT_DIM)
                    return
                self.track_name_label.configure(text="No active playback session")
                self.track_artist_label.configure(text="Start playback in another app")
                self.track_meta_label.configure(text="")
                self.track_time_label.configure(text="--:--")
                self.track_remaining_label.configure(text="--:--")
                self._timeline_position_seconds = None
                self._timeline_duration_seconds = None
                self._timeline_playing = False
                self._timeline_track_key = None
                self.track_progress.set(0)
                self.playback_status_label.configure(text="NO SESSION", text_color=TEXT_DIM)
                self.play_btn.configure(text="▶")
                self._artwork_key = None
                self._set_artwork(None)
        else:
            self.track_name_label.configure(text="Media metadata unavailable")
            error = error or "WinRT media control could not be initialized."
            self.track_artist_label.configure(text=error)
            self.track_meta_label.configure(text="")
            self.track_time_label.configure(text="--:--")
            self.track_remaining_label.configure(text="--:--")
            self._timeline_position_seconds = None
            self._timeline_duration_seconds = None
            self._timeline_playing = False
            self._timeline_track_key = None
            self.track_progress.set(0)
            self.playback_status_label.configure(text="UNAVAILABLE", text_color=BAD)
            self.play_btn.configure(text="▶")
            self._artwork_key = None
            self._set_artwork(None)

    def _reset_adaptive_delays(self):
        """Clear accumulated adaptive safety on all active devices (useful after track changes)."""
        for idx, out in self.device_outputs.items():
            out.reader.clear_adaptive_delay()

    # --------------------------------------------------------- tray --

    def _build_tray(self):
        """Run a Windows system-tray icon in a background thread. Its menu callbacks
        fire on pystray's thread, so each one hops back onto Tk via root.after."""
        if pystray is None or Image is None:
            return
        try:
            image = Image.open(ICON_PATH)
        except Exception:
            return
        menu = pystray.Menu(
            pystray.MenuItem("SyncMirror", None, enabled=False),
            pystray.MenuItem(self._tray_status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show SyncMirror", self._tray_show, default=True),
            pystray.MenuItem(
                lambda item: "Stop Sync" if self.running else "Start Sync",
                self._tray_toggle_sync,
            ),
            pystray.MenuItem("Reset Sync", self._tray_reset,
                             enabled=lambda item: self.running),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit SyncMirror", self._tray_quit),
        )
        self.tray_icon = pystray.Icon("SyncMirror", image, "SyncMirror", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self._tick_tray()

    def _tick_tray(self):
        # pystray caches the menu; the dynamic labels only re-read their callables
        # when update_menu() is called, so poll for state changes and refresh then.
        if self.tray_icon is not None:
            sig = (self.running, len(self.device_outputs))
            if sig != self._tray_state_sig:
                self._tray_state_sig = sig
                try:
                    self.tray_icon.update_menu()
                except Exception:
                    pass
        self.root.after(500, self._tick_tray)

    def _tray_status_text(self, item=None):
        # Re-evaluated each time the menu opens (pystray calls the text callable).
        if self.running:
            n = len(self.device_outputs)
            return f"\u25cf  {n} device(s) connected"
        return "\u25cb  Stopped"

    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self._show_window)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_toggle_sync(self, icon=None, item=None):
        self.root.after(0, self._toggle_sync)

    def _toggle_sync(self):
        if self.running:
            self.stop_sync()
        else:
            self.start_sync()

    def _tray_reset(self, icon=None, item=None):
        self.root.after(0, self._reset_adaptive_delays)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self._shutdown)

    # --------------------------------------------------------- web remote --

    def _toggle_web_remote(self):
        # The header button just opens the panel now; the panel's own switch is
        # what actually turns the server on and off.
        if WebRemote is None:
            return
        if self.web_remote is None and not self._start_web_remote():
            return
        self._show_web_dialog()

    def _start_web_remote(self):
        """Start a fresh server (new PIN/QR each time). Returns True on success."""
        if WebRemote is None:
            return False
        if self.web_remote is not None:
            return True
        try:
            self.web_remote = WebRemote(self.web_command_queue)
            self.web_remote.start()
            self.web_remote.set_artwork(self._artwork_bytes)
        except Exception as exc:
            self.web_remote = None
            messagebox.showerror("Web remote", f"Could not start server:\n{exc}")
            return False
        self._update_web_btn()
        return True

    def _stop_web_remote(self):
        if self.web_remote is not None:
            self.web_remote.stop()
            self.web_remote = None
        self._update_web_btn()

    def _update_web_btn(self):
        self.web_btn.configure(
            text="Web Remote: ON" if self.web_remote is not None else "Web Remote")

    def _show_web_dialog(self):
        # Reuse the panel if it's already open instead of spawning another.
        if self._web_dialog is not None and self._web_dialog.winfo_exists():
            self._refresh_web_dialog()
            self._web_dialog.deiconify()
            self._web_dialog.lift()
            self._web_dialog.focus_force()
            return

        dlg = ctk.CTkToplevel(self.root)
        self._web_dialog = dlg
        dlg.title("Web Remote")
        dlg.configure(fg_color=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        # Closing the panel leaves the server running; only the switch stops it.
        dlg.protocol("WM_DELETE_WINDOW", self._close_web_dialog)

        # CustomTkinter needs the icon set after the window is realised.
        dlg.after(220, lambda: self._safe_iconbitmap(dlg))

        card = ctk.CTkFrame(
            dlg, fg_color=CARD_BG, corner_radius=16,
            border_width=1, border_color=BORDER,
        )
        card.pack(padx=PAD_M, pady=PAD_M, fill="both", expand=True)

        # ---- Header: status dot + title + on/off switch ----
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=PAD_L, pady=(PAD_L, PAD_S))

        self._web_status_dot = ctk.CTkLabel(
            header, text="●", text_color=GOOD,
            font=ctk.CTkFont(size=16), width=18)
        self._web_status_dot.pack(side="left", padx=(0, PAD_XS))

        header_text = ctk.CTkFrame(header, fg_color="transparent")
        header_text.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            header_text, text="Web Remote", text_color=TEXT, anchor="w",
            font=ctk.CTkFont(size=FS_TITLE, weight="bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            header_text, text="Control SyncMirror from your phone",
            text_color=TEXT_DIM, anchor="w",
            font=ctk.CTkFont(size=FS_LABEL),
        ).pack(anchor="w", pady=(1, 0))

        self._web_enabled_var = tk.BooleanVar(value=self.web_remote is not None)
        self._web_toggle_switch = ctk.CTkSwitch(
            header, text="", variable=self._web_enabled_var,
            command=self._on_web_toggle, progress_color=ACCENT, width=44)
        self._web_toggle_switch.pack(side="right", padx=(PAD_S, 0))

        # ---- Content: QR (left) + details (right) ----
        content = ctk.CTkFrame(card, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=PAD_L, pady=PAD_S)
        content.grid_columnconfigure(0, weight=0)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(0, weight=1)

        qr_section = ctk.CTkFrame(
            content, fg_color=CARD_HI, corner_radius=12,
            border_width=1, border_color=BORDER)
        qr_section.grid(row=0, column=0, sticky="nsew", padx=(0, PAD_M))

        ctk.CTkLabel(
            qr_section, text="SCAN TO CONNECT", text_color=TEXT_FAINT,
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
        ).pack(pady=(PAD_M, PAD_S))

        qr_panel = ctk.CTkFrame(qr_section, fg_color="#ffffff", corner_radius=10)
        qr_panel.pack(padx=PAD_M, pady=(0, PAD_S))
        self._web_qr_label = ctk.CTkLabel(qr_panel, text="", width=190, height=190)
        self._web_qr_label.pack(padx=8, pady=8)

        self._web_qr_hint = ctk.CTkLabel(
            qr_section, text="Scan with your phone camera",
            text_color=TEXT_DIM, font=ctk.CTkFont(size=FS_LABEL))
        self._web_qr_hint.pack(pady=(0, PAD_M))

        details = ctk.CTkFrame(content, fg_color="transparent")
        details.grid(row=0, column=1, sticky="nsew")

        # -- Status --
        status_card = ctk.CTkFrame(details, fg_color=CARD_HI, corner_radius=10)
        status_card.pack(fill="x", pady=(0, PAD_S))
        ctk.CTkLabel(
            status_card, text="STATUS", text_color=TEXT_FAINT,
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
        ).pack(anchor="w", padx=PAD_M, pady=(PAD_M, 2))
        self._web_status_label = ctk.CTkLabel(
            status_card, text="●  Remote server active", text_color=GOOD,
            anchor="w", font=ctk.CTkFont(size=FS_BODY, weight="bold"))
        self._web_status_label.pack(anchor="w", padx=PAD_M, pady=(0, PAD_M))

        # -- Address --
        address_card = ctk.CTkFrame(details, fg_color=CARD_HI, corner_radius=10)
        address_card.pack(fill="x", pady=(0, PAD_S))
        ctk.CTkLabel(
            address_card, text="ADDRESS", text_color=TEXT_FAINT,
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
        ).pack(anchor="w", padx=PAD_M, pady=(PAD_M, 2))
        address_row = ctk.CTkFrame(address_card, fg_color="transparent")
        address_row.pack(fill="x", padx=PAD_M, pady=(0, PAD_M))
        self._web_address_label = ctk.CTkLabel(
            address_row, text="", text_color=ACCENT, anchor="w",
            font=ctk.CTkFont(size=FS_BODY, weight="bold"))
        self._web_address_label.pack(side="left", fill="x", expand=True)
        self._web_copy_btn = ctk.CTkButton(
            address_row, text="Copy", width=58, height=28, corner_radius=6,
            fg_color="transparent", hover_color=CARD_BG, border_width=1,
            border_color=BORDER, text_color=TEXT_DIM,
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
            command=lambda: self._copy_to_clipboard(self._web_url))
        self._web_copy_btn.pack(side="right", padx=(PAD_S, 0))

        # -- PIN --
        pin_card = ctk.CTkFrame(details, fg_color=CARD_HI, corner_radius=10)
        pin_card.pack(fill="x")
        ctk.CTkLabel(
            pin_card, text="ACCESS PIN", text_color=TEXT_FAINT,
            font=ctk.CTkFont(size=FS_LABEL, weight="bold"),
        ).pack(anchor="w", padx=PAD_M, pady=(PAD_M, 0))
        self._web_pin_label = ctk.CTkLabel(
            pin_card, text="", text_color=TEXT,
            font=ctk.CTkFont(size=FS_DISPLAY, weight="bold"))
        self._web_pin_label.pack(anchor="w", padx=PAD_M, pady=(0, PAD_M))

        # ---- Footer ----
        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.pack(fill="x", padx=PAD_L, pady=(PAD_S, PAD_L))
        ctk.CTkLabel(
            footer, text="Phone and computer must be on the same Wi-Fi network.",
            text_color=TEXT_DIM, font=ctk.CTkFont(size=FS_LABEL),
        ).pack(side="left")
        ctk.CTkButton(
            footer, text="Done", width=90, height=34, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=FS_BODY, weight="bold"),
            command=self._close_web_dialog,
        ).pack(side="right")

        self._web_url = ""
        self._refresh_web_dialog()
        dlg.update_idletasks()
        self._center_over_root(dlg)

    def _refresh_web_dialog(self):
        """Repaint the panel's dynamic parts (QR, PIN, address, status) for the
        current server state. Turning the switch off then on lands here with a
        brand-new server, so the QR and PIN refresh automatically."""
        dlg = self._web_dialog
        if dlg is None or not dlg.winfo_exists():
            return
        running = self.web_remote is not None
        if self._web_enabled_var is not None:
            self._web_enabled_var.set(running)

        if running:
            url = f"http://{self.web_remote.local_ip()}:{self.web_remote.port}"
            pin = self.web_remote.token
            self._web_url = url
            qr = self._make_qr_image(f"{url}/?token={pin}", size=190)
            self._web_qr_image = qr
            if qr is not None:
                self._web_qr_label.configure(image=qr, text="")
            else:
                self._web_qr_label.configure(
                    image=None, text="QR unavailable", text_color=TEXT_DIM,
                    font=ctk.CTkFont(size=FS_BODY))
            self._web_qr_hint.configure(text="Scan with your phone camera")
            self._web_status_dot.configure(text_color=GOOD)
            self._web_status_label.configure(
                text="●  Remote server active", text_color=GOOD)
            self._web_address_label.configure(text=url, text_color=ACCENT)
            self._web_copy_btn.configure(state="normal")
            self._web_pin_label.configure(text=str(pin), text_color=TEXT)
        else:
            self._web_url = ""
            self._web_qr_image = None
            self._web_qr_label.configure(
                image=None, text="Server off", text_color=TEXT_DIM,
                font=ctk.CTkFont(size=FS_BODY, weight="bold"))
            self._web_qr_hint.configure(text="Turn the switch on to connect")
            self._web_status_dot.configure(text_color=TEXT_FAINT)
            self._web_status_label.configure(
                text="○  Remote server stopped", text_color=TEXT_DIM)
            self._web_address_label.configure(text="—", text_color=TEXT_DIM)
            self._web_copy_btn.configure(state="disabled")
            self._web_pin_label.configure(text="— — — — — —", text_color=TEXT_FAINT)

    def _on_web_toggle(self):
        if self._web_enabled_var.get():
            if not self._start_web_remote():
                self._web_enabled_var.set(False)
                return
        else:
            self._stop_web_remote()
        self._refresh_web_dialog()

    def _close_web_dialog(self):
        if self._web_dialog is not None and self._web_dialog.winfo_exists():
            self._web_dialog.destroy()
        self._web_dialog = None

    def _make_qr_image(self, data, size=190):
        try:
            import qrcode
        except Exception:
            return None
        try:
            qr = qrcode.QRCode(border=2, box_size=10,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(data)
            qr.make(fit=True)
            pil = qr.make_image(fill_color="#101319", back_color="#ffffff").convert("RGB")
            return ctk.CTkImage(light_image=pil, dark_image=pil, size=(size, size))
        except Exception:
            return None

    def _safe_iconbitmap(self, window):
        try:
            window.iconbitmap(ICON_PATH)
        except Exception:
            pass

    def _copy_to_clipboard(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            pass

    def _center_over_root(self, window):
        try:
            self.root.update_idletasks()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            ww, wh = window.winfo_width(), window.winfo_height()
            x = rx + (rw - ww) // 2
            y = ry + (rh - wh) // 3
            window.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass

    def _poll_web_commands(self):
        try:
            while True:
                self._handle_web_command(self.web_command_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self._poll_web_commands)

    def _handle_web_command(self, cmd):
        action = cmd.get("action")
        try:
            if action == "toggle_sync":
                self._toggle_sync()
            elif action == "start":
                self.start_sync()
            elif action == "stop":
                self.stop_sync()
            elif action == "set_source_delay":
                self._set_source_delay(float(cmd.get("value", 0)))
            elif action == "media":
                which = cmd.get("which")
                if which == "play_pause":
                    self._media_play_pause()
                elif which == "next":
                    self._media_next()
                elif which == "previous":
                    self._media_previous()
            elif action == "set_delay":
                card = self.cards.get(cmd.get("name"))
                if card:
                    d = max(DELAY_MIN, min(DELAY_MAX, float(cmd.get("delay", 0))))
                    card.delay_var.set(d)
                    card._delay_changed(d)
            elif action == "set_volume":
                card = self.cards.get(cmd.get("name"))
                if card:
                    v = max(0.0, min(100.0, float(cmd.get("volume", 100))))
                    card.volume_var.set(v)
                    card._volume_changed(v)
            elif action == "set_muted":
                card = self.cards.get(cmd.get("name"))
                if card:
                    card.set_muted(bool(cmd.get("muted")))
            elif action == "toggle_device":
                card = self.cards.get(cmd.get("name"))
                if card:
                    card.enabled_var.set(bool(cmd.get("enabled")))
                    card._toggled()
        except Exception as exc:
            print(f"[SyncMirror] web command error: {exc}", flush=True)

    def _publish_web_state(self):
        # Built on the Tk thread (safe to read widgets) then handed to the server.
        if self.web_remote is not None:
            devices = []
            for name, card in self.cards.items():
                devices.append({
                    "name": name,
                    "enabled": bool(card.enabled_var.get()),
                    "active": name in self.device_outputs,
                    "delay": round(card.delay_var.get(), 1),
                    "volume": round(card.volume_var.get()),
                    "muted": bool(card.muted_var.get()),
                })
            position = self._timeline_position_seconds
            if position is not None and self._timeline_playing:
                position += time.monotonic() - self._timeline_anchor
            now_playing = {
                "title": self.track_name_label.cget("text"),
                "artist": self.track_artist_label.cget("text"),
                "meta": self.track_meta_label.cget("text"),
                "position": position,
                "duration": self._timeline_duration_seconds,
                "playing": bool(self._timeline_playing),
            }
            self.web_remote.set_state({
                "running": self.running,
                "source_delay": self.source_delay.get(),
                "now_playing": now_playing,
                "devices": devices,
            })
        self.root.after(700, self._publish_web_state)

    def _set_source_delay(self, value):
        new_val = max(DELAY_MIN, min(DELAY_MAX, value))
        self.source_delay.set(new_val)
        self.source_delay_label.configure(text=f"{int(new_val)} ms")
        for idx, out in self.device_outputs.items():
            card = self.cards.get(idx)
            if card:
                out.set_user_delay_ms(card.delay_var.get() + new_val)

    def on_close(self):
        # With a tray icon present, the window's close button just hides to the
        # tray; the tray's "Quit" is the real exit. Without a tray, close exits.
        if self.tray_icon is not None:
            self.root.withdraw()
            return
        self._shutdown()

    def _shutdown(self):
        if self.running:
            self.stop_sync()
        if self.web_remote is not None:
            self.web_remote.stop()
            self.web_remote = None
        if self._settings_save_handle is not None:
            self.root.after_cancel(self._settings_save_handle)
            self._settings_save_handle = None
        self.settings.save()
        self.endpoint_watcher.stop()
        self.capture.close()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.root.destroy()


def main():
    # Frozen exes re-run this entry point in every spawned child; without this a
    # multiprocessing child would relaunch the whole UI, opening windows in a loop.
    import multiprocessing
    multiprocessing.freeze_support()

    # Give Windows an explicit app id so the taskbar uses our icon, not Python's.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SyncMirror.App")
    except Exception:
        pass

    root = ctk.CTk()
    try:
        root.iconbitmap(ICON_PATH)
    except Exception:
        pass
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()