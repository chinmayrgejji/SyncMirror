# """
# app.py

# Prototype UI for the multi-device synced audio mirror.

# Run with:  python app.py
# (Windows only -- requires pyaudiowpatch, which wraps WASAPI.)
# """

# import threading
# import tkinter as tk
# from tkinter import ttk, messagebox

# import pyaudiowpatch as pyaudio

# from ring_buffer import SyncRingBuffer
# from capture import LoopbackCapture
# from output import DeviceOutput

# SAMPLERATE = 48000
# CHANNELS = 2
# RING_BUFFER_SECONDS = 10.0
# BASE_SAFETY_MS = 150.0  # headroom so every device has room to be delayed further if needed


# class App:
#     def __init__(self, root: tk.Tk):
#         self.root = root
#         self.root.title("Synced Audio Mirror (prototype)")
#         self.root.geometry("560x480")

#         self.pa = pyaudio.PyAudio()
#         self.ring_buffer = None
#         self.capture = LoopbackCapture(samplerate=None, channels=CHANNELS)

#         self.device_outputs = {}   # device_index -> DeviceOutput
#         self.device_vars = {}      # device_index -> {"enabled": BoolVar, "delay": DoubleVar, "delay_label": StringVar}
#         self.running = False
#         self.source_delay = tk.DoubleVar(value=0.0)
#         self.source_delay_label = tk.StringVar(value="0 ms")
#         self._updating_buffers = False

#         self._build_ui()
#         self._populate_devices()

#     # ------------------------------------------------------------- UI --

#     def _build_ui(self):
#         top = ttk.Frame(self.root, padding=10)
#         top.pack(fill="x")

#         self.start_btn = ttk.Button(top, text="Start Sync", command=self.start_sync)
#         self.start_btn.pack(side="left")

#         self.stop_btn = ttk.Button(top, text="Stop Sync", command=self.stop_sync, state="disabled")
#         self.stop_btn.pack(side="left", padx=8)

#         self.status_label = ttk.Label(top, text="Stopped")
#         self.status_label.pack(side="left", padx=12)

#         # Source delay controls (applies a global offset to all outputs)
#         src = ttk.Frame(self.root, padding=(10, 6))
#         src.pack(fill="x")
#         ttk.Label(src, text="Source Delay:").pack(side="left")
#         ttk.Button(src, text="-20", width=3, command=lambda: self._change_source_delay(-20)).pack(side="left", padx=(8,2))
#         ttk.Button(src, text="-10", width=3, command=lambda: self._change_source_delay(-10)).pack(side="left")
#         ttk.Label(src, textvariable=self.source_delay_label, width=8, anchor="center").pack(side="left", padx=(6,6))
#         ttk.Button(src, text="+10", width=3, command=lambda: self._change_source_delay(10)).pack(side="left")
#         ttk.Button(src, text="+20", width=3, command=lambda: self._change_source_delay(20)).pack(side="left", padx=(2,8))

#         ttk.Label(
#             self.root,
#             text="Tip: after starting, nudge each device's delay slider until they sound in sync.\n"
#                  "This compensates for that specific earbud's own Bluetooth latency.",
#             wraplength=520, justify="left"
#         ).pack(fill="x", padx=10, pady=(0, 6))

#         self.devices_frame = ttk.Frame(self.root, padding=10)
#         self.devices_frame.pack(fill="both", expand=True)

#     def _populate_devices(self):
#         devices = self.capture.list_output_devices()
#         for info in devices:
#             row = ttk.Frame(self.devices_frame, padding=4, relief="groove")
#             row.pack(fill="x", pady=4)

#             enabled_var = tk.BooleanVar(value=False)
#             delay_var = tk.DoubleVar(value=0.0)
#             delay_display = tk.StringVar(value="0 ms")
#             buffer_display = tk.StringVar(value="buf: --")

#             ttk.Checkbutton(row, text=info["name"], variable=enabled_var).pack(side="left")

#             ttk.Label(row, text="Delay:").pack(side="left", padx=(20, 2))
#             ttk.Button(row, text="-20", width=3, command=lambda idx=info["index"]: self._change_delay(idx, -20)).pack(side="left")
#             ttk.Button(row, text="-10", width=3, command=lambda idx=info["index"]: self._change_delay(idx, -10)).pack(side="left")
#             ttk.Label(row, textvariable=delay_display, width=8, anchor="center").pack(side="left", padx=(4, 4))
#             ttk.Button(row, text="+10", width=3, command=lambda idx=info["index"]: self._change_delay(idx, 10)).pack(side="left")
#             ttk.Button(row, text="+20", width=3, command=lambda idx=info["index"]: self._change_delay(idx, 20)).pack(side="left")

#             self.device_vars[info["index"]] = {
#                 "enabled": enabled_var,
#                 "delay": delay_var,
#                 "delay_label": delay_display,
#                 "buffer_label": buffer_display,
#                 "info": info,
#             }

#     def _update_buffer_displays(self):
#         # periodic UI update showing frames available for each active reader
#         if not self._updating_buffers:
#             return
#         for idx, dev in list(self.device_vars.items()):
#             buf_var = dev.get("buffer_label")
#             if not buf_var:
#                 continue
#             out = self.device_outputs.get(idx)
#             if out:
#                 try:
#                     avail = self.ring_buffer.frames_available(out.reader)
#                     ms = int((avail / float(self.ring_buffer.samplerate)) * 1000.0)
#                     buf_var.set(f"buf: {ms}ms")
#                 except Exception:
#                     buf_var.set("buf: err")
#             else:
#                 buf_var.set("buf: --")

#         # schedule next update
#         self.root.after(250, self._update_buffer_displays)

#     # --------------------------------------------------------- actions --

#     def start_sync(self):
#         selected = [d for idx, d in self.device_vars.items() if d["enabled"].get()]
#         if not selected:
#             messagebox.showwarning("No devices selected", "Check at least one output device to mirror to.")
#             return

#         try:
#             samplerate = self.capture.prepare()
#             self.ring_buffer = SyncRingBuffer(RING_BUFFER_SECONDS, CHANNELS, samplerate)
#             self.capture.start(self.ring_buffer)
#         except Exception as e:
#             messagebox.showerror("Capture error", str(e))
#             return

#         for d in selected:
#             info = d["info"]
#             out = DeviceOutput(
#                 self.pa, info, self.ring_buffer,
#                 base_safety_ms=BASE_SAFETY_MS,
#                 user_delay_ms=d["delay"].get() + self.source_delay.get(),
#                 volume=1.0,
#             )
#             out.start()
#             self.device_outputs[info["index"]] = out

#         self.running = True
#         self.status_label.config(text=f"Running -> {len(self.device_outputs)} device(s)")
#         self.start_btn.config(state="disabled")
#         self.stop_btn.config(state="normal")
#         # start periodic buffer display updates
#         self._updating_buffers = True
#         self._update_buffer_displays()

#     def stop_sync(self):
#         for out in self.device_outputs.values():
#             out.stop()
#         self.device_outputs.clear()
#         self.capture.stop()

#         self.running = False
#         self.status_label.config(text="Stopped")
#         self.start_btn.config(state="normal")
#         self.stop_btn.config(state="disabled")
#         self._updating_buffers = False

#     def _change_delay(self, device_index, delta_ms):
#         device = self.device_vars.get(device_index)
#         if not device:
#             return

#         current_delay = device["delay"].get()
#         new_delay = max(-500.0, min(500.0, current_delay + delta_ms))
#         device["delay"].set(new_delay)
#         device["delay_label"].set(f"{int(new_delay)} ms")

#         out = self.device_outputs.get(device_index)
#         if out:
#             combined = new_delay + self.source_delay.get()
#             out.set_user_delay_ms(combined)

#     def _change_source_delay(self, delta_ms):
#         current = self.source_delay.get()
#         new = max(-500.0, min(500.0, current + delta_ms))
#         self.source_delay.set(new)
#         self.source_delay_label.set(f"{int(new)} ms")

#         # update all active outputs to include new global offset
#         for idx, out in self.device_outputs.items():
#             dev = self.device_vars.get(idx)
#             if not dev:
#                 continue
#             user = dev["delay"].get()
#             out.set_user_delay_ms(user + new)

#     def on_close(self):
#         if self.running:
#             self.stop_sync()
#         self.capture.close()
#         self.pa.terminate()
#         self.root.destroy()


# def main():
#     root = tk.Tk()
#     app = App(root)
#     root.protocol("WM_DELETE_WINDOW", app.on_close)
#     root.mainloop()


# if __name__ == "__main__":
#     main()



"""
app.py

UI for the multi-device synced audio mirror, built on customtkinter.

Run with:  python app.py
(Windows only -- requires pyaudiowpatch, which wraps WASAPI.)
"""

import asyncio
import ctypes
from datetime import datetime, timezone
from io import BytesIO
import queue
import tkinter as tk
import threading
import time
from tkinter import messagebox

import customtkinter as ctk
import pyaudiowpatch as pyaudio

try:
    from PIL import Image, ImageFilter, ImageOps, ImageTk
except ImportError:
    Image = None
    ImageFilter = None
    ImageOps = None
    ImageTk = None

from ring_buffer import SyncRingBuffer
from capture import LoopbackCapture
from output import DeviceOutput

SAMPLERATE = 48000  # fallback only; capture.prepare() detects the real one
CHANNELS = 2
RING_BUFFER_SECONDS = 10.0
BASE_SAFETY_MS = 150.0

DELAY_MIN, DELAY_MAX = -500.0, 500.0
BUFFER_METER_MAX_MS = 300.0  # meter is "full" at this many ms of headroom

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG = "#1a1d23"
CARD_BG = "#232732"
ACCENT = "#4da3ff"
GOOD = "#3ecf6e"
WARN = "#e8b93b"
BAD = "#e8543b"
TEXT_DIM = "#8b93a3"
ARTWORK_ASPECT_RATIO = 16 / 9
ARTWORK_WIDTH = 440
ARTWORK_HEIGHT = round(ARTWORK_WIDTH / ARTWORK_ASPECT_RATIO)

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


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

    def __init__(self, master, info, on_toggle, on_delay_change):
        super().__init__(master, fg_color=CARD_BG, corner_radius=10)
        self.info = info
        self.on_toggle = on_toggle
        self.on_delay_change = on_delay_change

        self.enabled_var = tk.BooleanVar(value=False)
        self.delay_var = tk.DoubleVar(value=0.0)

        self.grid_columnconfigure(1, weight=1)

        # -- top row: name + enable switch --
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 4))
        top.grid_columnconfigure(0, weight=1)

        name = info["name"]
        display_name = name if len(name) <= 42 else name[:39] + "..."
        ctk.CTkLabel(top, text=display_name, font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").grid(row=0, column=0, sticky="w")

        self.switch = ctk.CTkSwitch(
            top, text="", variable=self.enabled_var, command=self._toggled,
            progress_color=ACCENT, width=40,
        )
        self.switch.grid(row=0, column=1, sticky="e")

        # -- delay row --
        delay_row = ctk.CTkFrame(self, fg_color="transparent")
        delay_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 4))
        delay_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(delay_row, text="Delay", text_color=TEXT_DIM, width=44,
                     anchor="w").grid(row=0, column=0, sticky="w")

        self.delay_slider = ctk.CTkSlider(
            delay_row, from_=DELAY_MIN, to=DELAY_MAX, variable=self.delay_var,
            command=self._delay_changed, progress_color=ACCENT, button_color=ACCENT,
        )
        self.delay_slider.grid(row=0, column=1, sticky="ew", padx=10)

        self.delay_label = ctk.CTkLabel(delay_row, text="0 ms", width=56, anchor="e")
        self.delay_label.grid(row=0, column=2, sticky="e")

        # -- nudge buttons for fine control --
        nudge_row = ctk.CTkFrame(self, fg_color="transparent")
        nudge_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 4))
        for label, delta in (("-20", -20), ("-5", -5), ("+5", 5), ("+20", 20)):
            ctk.CTkButton(
                nudge_row, text=label, width=44, height=22, fg_color="#2d3340",
                hover_color="#3a4152", font=ctk.CTkFont(size=11),
                command=lambda d=delta: self._nudge(d),
            ).pack(side="left", padx=(0, 6))

        # -- buffer meter + stutter badge --
        meter_row = ctk.CTkFrame(self, fg_color="transparent")
        meter_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 12))
        meter_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(meter_row, text="Buffer", text_color=TEXT_DIM, width=44,
                     anchor="w").grid(row=0, column=0, sticky="w")

        self.buffer_bar = ctk.CTkProgressBar(meter_row, progress_color=GOOD, height=8)
        self.buffer_bar.set(0)
        self.buffer_bar.grid(row=0, column=1, sticky="ew", padx=10)

        self.buffer_label = ctk.CTkLabel(meter_row, text="-- ms", width=56, anchor="e",
                                          text_color=TEXT_DIM, font=ctk.CTkFont(size=11))
        self.buffer_label.grid(row=0, column=2, sticky="e")

        self.stutter_label = ctk.CTkLabel(self, text="", text_color=TEXT_DIM,
                                           font=ctk.CTkFont(size=11), anchor="w")
        self.stutter_label.grid(row=4, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 10))

    def _toggled(self):
        self.on_toggle(self.info["name"], self.enabled_var.get())

    def _delay_changed(self, value):
        value = float(value)
        self.delay_label.configure(text=f"{int(value)} ms")
        self.on_delay_change(self.info["name"], value)

    def _nudge(self, delta):
        new_val = max(DELAY_MIN, min(DELAY_MAX, self.delay_var.get() + delta))
        self.delay_var.set(new_val)
        self._delay_changed(new_val)

    def update_live_stats(self, buffer_ms, stutter_count, active: bool):
        if not active:
            self.buffer_bar.set(0)
            self.buffer_bar.configure(progress_color="#3a4152")
            self.buffer_label.configure(text="--")
            self.stutter_label.configure(text="")
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

        if stutter_count > 0:
            self.stutter_label.configure(
                text=f"⚠ {stutter_count} stutter{'s' if stutter_count != 1 else ''} this session",
                text_color=WARN,
            )
        else:
            self.stutter_label.configure(text="✓ clean", text_color=GOOD)


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
        self._artwork_results = queue.Queue()
        self._metadata_results = queue.Queue()
        self._metadata_request_running = False
        self._timeline_position_seconds = None
        self._timeline_duration_seconds = None
        self._timeline_anchor = time.monotonic()
        self._timeline_playing = False
        self._timeline_track_key = None
        self._last_track_seen_at = 0.0

        self._build_ui()
        self._populate_devices()
        self.root.after(500, self._poll_device_scan_results)
        self.root.after(1500, self._auto_rescan)
        self._update_track_metadata()
        self._poll_artwork_results()
        self._poll_metadata_results()
        self._tick_playback_clock()

    # ------------------------------------------------------------- UI --

    def _build_ui(self):
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 8))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")

        ctk.CTkLabel(title_row, text="SyncMirror", font=ctk.CTkFont(size=22, weight="bold")
                     ).pack(side="left")

        status_frame = ctk.CTkFrame(title_row, fg_color="transparent")
        status_frame.pack(side="right")
        self.status_dot = ctk.CTkLabel(status_frame, text="●", text_color=BAD,
                                        font=ctk.CTkFont(size=16))
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = ctk.CTkLabel(status_frame, text="Stopped", text_color=TEXT_DIM)
        self.status_label.pack(side="left")

        ctk.CTkLabel(header, text="Synchronized audio mirroring across Bluetooth devices",
                     text_color=TEXT_DIM, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(2, 0))

        # -- split workspace: playback on the left, sync/devices on the right --
        workspace = ctk.CTkFrame(self.root, fg_color="transparent")
        workspace.pack(fill="both", expand=True, padx=20, pady=(4, 20))
        workspace.grid_columnconfigure(0, weight=1, uniform="pane")
        workspace.grid_columnconfigure(1, weight=1, uniform="pane")
        workspace.grid_rowconfigure(0, weight=1)

        playback = ctk.CTkFrame(workspace, fg_color=CARD_BG, corner_radius=10)
        playback.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        playback.grid_columnconfigure(0, weight=1)
        playback.bind("<Configure>", self._schedule_artwork_resize)

        devices_pane = ctk.CTkFrame(workspace, fg_color="transparent")
        devices_pane.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # -- sync controls and devices --
        controls = ctk.CTkFrame(devices_pane, fg_color=CARD_BG, corner_radius=10)
        controls.pack(fill="x", pady=(0, 8))

        btn_row = ctk.CTkFrame(controls, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(14, 10))

        self.start_btn = ctk.CTkButton(btn_row, text="Start Sync", command=self.start_sync,
                                        fg_color=GOOD, hover_color="#33b35f", width=120)
        self.start_btn.pack(side="left")

        self.stop_btn = ctk.CTkButton(btn_row, text="Stop", command=self.stop_sync,
                                       fg_color="#3a4152", hover_color="#4a5162", width=90,
                                       state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        self.reset_btn = ctk.CTkButton(btn_row, text="Reset Sync", command=self._reset_adaptive_delays,
                                        fg_color="#3a4152", hover_color="#4a5162", width=90,
                                        state="disabled")
        self.reset_btn.pack(side="left", padx=(0, 8))

        self.refresh_btn = ctk.CTkButton(btn_row, text="Refresh Devices", command=self._populate_devices,
                                         fg_color="#3a4152", hover_color="#3a4152", width=100)
        self.refresh_btn.pack(side="left", padx=(0, 8))

        src_row = ctk.CTkFrame(controls, fg_color="transparent")
        src_row.pack(fill="x", padx=14, pady=(0, 14))
        ctk.CTkLabel(src_row, text="Global source delay", text_color=TEXT_DIM
                     ).pack(side="left")
        self.source_delay_label = ctk.CTkLabel(src_row, text="0 ms", width=56)
        for label, delta in (("-20", -20), ("-10", -10)):
            ctk.CTkButton(src_row, text=label, width=44, height=24, fg_color="#2d3340",
                          hover_color="#3a4152", font=ctk.CTkFont(size=11),
                          command=lambda d=delta: self._change_source_delay(d)
                          ).pack(side="left", padx=(14, 4))
        self.source_delay_label.pack(side="left", padx=6)
        for label, delta in (("+10", 10), ("+20", 20)):
            ctk.CTkButton(src_row, text=label, width=44, height=24, fg_color="#2d3340",
                          hover_color="#3a4152", font=ctk.CTkFont(size=11),
                          command=lambda d=delta: self._change_source_delay(d)
                          ).pack(side="left", padx=(4, 0))

        # -- playback area --
        artwork_frame = ctk.CTkFrame(
            playback, fg_color="#161923", corner_radius=8,
            width=ARTWORK_WIDTH, height=ARTWORK_HEIGHT,
        )
        artwork_frame.grid(row=0, column=0, padx=14, pady=(14, 10))
        artwork_frame.grid_propagate(False)
        self.artwork_frame = artwork_frame
        self.artwork_label = tk.Label(
            artwork_frame, text="No artwork", bg="#161923", fg=TEXT_DIM,
            borderwidth=0, highlightthickness=0,
        )
        self.artwork_label.pack(fill="both", expand=True)

        playback_header = ctk.CTkFrame(playback, fg_color="transparent")
        playback_header.grid(row=1, column=0, sticky="ew", padx=14)
        playback_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(playback_header, text="NOW PLAYING", text_color=TEXT_DIM,
                     font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="w")
        self.playback_status_label = ctk.CTkLabel(playback_header, text="NO SESSION",
                                                   text_color=TEXT_DIM, font=ctk.CTkFont(size=11))
        self.playback_status_label.grid(row=0, column=1, sticky="e")

        self.track_name_label = ctk.CTkLabel(
            playback, text="Song: No active track detected", anchor="w", justify="left",
            font=ctk.CTkFont(size=15, weight="bold"), wraplength=1200,
        )
        self.track_name_label.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 0))
        self.track_artist_label = ctk.CTkLabel(
            playback, text="Artist: Start playback in another app.", anchor="w", justify="left",
            font=ctk.CTkFont(size=13), wraplength=1200,
        )
        self.track_artist_label.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 0))
        self.track_album_label = ctk.CTkLabel(
            playback, text="Album: —", anchor="w", justify="left", text_color=TEXT_DIM,
            font=ctk.CTkFont(size=12), wraplength=1200,
        )
        self.track_album_label.grid(row=4, column=0, sticky="ew", padx=14, pady=(2, 0))
        self.track_source_label = ctk.CTkLabel(
            playback, text="Source: —", anchor="w", justify="left", text_color=TEXT_DIM,
            font=ctk.CTkFont(size=12), wraplength=1200,
        )
        self.track_source_label.grid(row=5, column=0, sticky="ew", padx=14, pady=(2, 0))
        self.track_time_label = ctk.CTkLabel(
            playback, text="Time: --:-- / --:--", anchor="w", justify="left", text_color=TEXT_DIM,
            font=ctk.CTkFont(size=12), wraplength=1200,
        )
        self.track_time_label.grid(row=6, column=0, sticky="ew", padx=14, pady=(2, 0))
        self.track_progress = ctk.CTkProgressBar(playback, progress_color=ACCENT, height=7)
        self.track_progress.set(0)
        self.track_progress.grid(row=7, column=0, sticky="ew", padx=14, pady=(7, 0))

        media_row = ctk.CTkFrame(playback, fg_color="#161923", corner_radius=8)
        media_row.grid(row=8, column=0, sticky="ew", padx=14, pady=(10, 14))
        media_row.configure(fg_color="transparent")
        transport = ctk.CTkFrame(media_row, fg_color="transparent")
        transport.pack(pady=(4, 10))
        self.prev_btn = ctk.CTkButton(
            transport, text="⏮", width=44, height=44, corner_radius=22,
            command=self._media_previous, fg_color="transparent", hover_color="#2d3340",
            font=ctk.CTkFont(size=18),
        )
        self.prev_btn.pack(side="left", padx=12)
        self.play_btn = ctk.CTkButton(
            transport, text="▶", width=54, height=54, corner_radius=27,
            command=self._media_play_pause, fg_color=ACCENT, hover_color="#3688dc",
            font=ctk.CTkFont(size=21, weight="bold"),
        )
        self.play_btn.pack(side="left", padx=12)
        self.next_btn = ctk.CTkButton(
            transport, text="⏭", width=44, height=44, corner_radius=22,
            command=self._media_next, fg_color="transparent", hover_color="#2d3340",
            font=ctk.CTkFont(size=18),
        )
        self.next_btn.pack(side="left", padx=12)

        ctk.CTkLabel(
            devices_pane,
            text="Tip: start sync, play audio, then nudge each device's delay until they line up.\n"
                 "Each earbud has its own Bluetooth latency, so each needs its own value.",
            text_color=TEXT_DIM, font=ctk.CTkFont(size=11), justify="left",
        ).pack(anchor="w", padx=22, pady=(4, 8))

        # -- scrollable device list --
        self.devices_frame = ctk.CTkScrollableFrame(devices_pane, fg_color="transparent")
        self.devices_frame.pack(fill="both", expand=True)

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
                                 on_toggle=self._on_toggle, on_delay_change=self._on_delay_change)
                card.pack(fill="x", pady=6)
                self.cards[name] = card

        for name in list(self.cards.keys()):
            if name not in current_names:
                out = self.device_outputs.pop(name, None)
                if out:
                    out.stop()
                self.cards[name].destroy()
                del self.cards[name]

        if self.running:
            self.status_label.configure(
                text=f"Running — {len(self.device_outputs)} device(s)", text_color="white"
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
                    text=f"Running — {len(self.device_outputs)} device(s)", text_color="white"
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
                    volume=1.0,
                )
                out.start()
                self.device_outputs[idx] = out
            except Exception:
                failed.append(card.info["name"])
                card.enabled_var.set(False)

        self.running = True
        self.status_dot.configure(text_color=GOOD)
        self.status_label.configure(text=f"Running — {len(self.device_outputs)} device(s)",
                                     text_color="white")
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
                    volume=1.0,
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
                    volume=1.0,
                )
                out.start()
                self.device_outputs[name] = out
            except Exception:
                failed.append(name)
                card.enabled_var.set(False)

        self.status_label.configure(text=f"Running — {len(self.device_outputs)} device(s)",
                                     text_color="white")
        if failed:
            messagebox.showwarning(
                "Device unavailable",
                "Could not start these device(s):\n" + "\n".join(failed),
            )

    def _on_delay_change(self, device_index, value):
        out = self.device_outputs.get(device_index)
        if out:
            out.set_user_delay_ms(value + self.source_delay.get())

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
        self.status_label.configure(text=f"Running — {len(self.device_outputs)} device(s)", text_color="white")

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
        if not thumbnail_bytes or Image is None or ImageOps is None or ImageTk is None:
            # Clear the Tk label before releasing the old PhotoImage.  Releasing
            # it first deletes its Tcl image while the label still refers to it.
            old_image = self._artwork_image
            self.artwork_label.configure(image="", text="No artwork")
            self._artwork_image = None
            return
        try:
            with Image.open(BytesIO(thumbnail_bytes)) as raw_image:
                artwork = raw_image.convert("RGBA")
                resampling = getattr(Image, "Resampling", Image).LANCZOS
                canvas = ImageOps.fit(
                    artwork,
                    (self._artwork_width, self._artwork_height),
                    method=resampling,
                )
                if ImageFilter is not None:
                    canvas = canvas.filter(ImageFilter.UnsharpMask(radius=1.2, percent=110, threshold=3))
            # This native Tk label and its image are both created on Tk's main thread.
            old_image = self._artwork_image
            new_image = ImageTk.PhotoImage(canvas, master=self.root)
            self.artwork_label.configure(image=new_image, text="")
            self._artwork_image = new_image
        except Exception:
            old_image = self._artwork_image
            self.artwork_label.configure(image="", text="No artwork")
            self._artwork_image = None

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
        current = controller.get_current_track(include_thumbnail=True)
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
            self.track_time_label.configure(text="Time: --:-- / --:--")
            self.track_progress.set(0)
            return
        if self._timeline_playing:
            position += time.monotonic() - self._timeline_anchor
        position = min(position, duration) if duration else position
        self.track_time_label.configure(
            text=f"Time: {self._format_media_time_seconds(position)} / {self._format_media_time_seconds(duration)}"
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
                self.track_name_label.configure(text=f"Song: {title}")
                self.track_artist_label.configure(text=f"Artist: {artist}")
                self.track_album_label.configure(text=f"Album: {album or 'No album information'}")
                self.track_source_label.configure(text=f"Source: {source}")
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
                self.track_name_label.configure(text="Song: No active playback session")
                self.track_artist_label.configure(text="Artist: Start playback in another app.")
                self.track_album_label.configure(text="Album: —")
                self.track_source_label.configure(text="Source: —")
                self.track_time_label.configure(text="Time: --:-- / --:--")
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
            self.track_name_label.configure(text="Song: Media metadata unavailable")
            error = error or "WinRT media control could not be initialized."
            self.track_artist_label.configure(text=f"Artist: {error}")
            self.track_album_label.configure(text="Album: —")
            self.track_source_label.configure(text="Source: —")
            self.track_time_label.configure(text="Time: --:-- / --:--")
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

    def on_close(self):
        if self.running:
            self.stop_sync()
        self.endpoint_watcher.stop()
        self.capture.close()
        self.root.destroy()


def main():
    root = ctk.CTk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()