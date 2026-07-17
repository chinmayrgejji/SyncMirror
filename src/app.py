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

import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import pyaudiowpatch as pyaudio

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
        self.on_toggle(self.info["index"], self.enabled_var.get())

    def _delay_changed(self, value):
        value = float(value)
        self.delay_label.configure(text=f"{int(value)} ms")
        self.on_delay_change(self.info["index"], value)

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


class App:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("SyncMirror")
        self.root.geometry("620x680")
        self.root.configure(fg_color=BG)

        self.pa = pyaudio.PyAudio()
        self.ring_buffer = None
        self.capture = LoopbackCapture(samplerate=None, channels=CHANNELS)

        self.cards = {}            # device_index -> DeviceCard
        self.device_outputs = {}   # device_index -> DeviceOutput
        self.running = False
        self.source_delay = tk.DoubleVar(value=0.0)
        self._updating = False

        self._build_ui()
        self._populate_devices()

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

        # -- controls bar --
        controls = ctk.CTkFrame(self.root, fg_color=CARD_BG, corner_radius=10)
        controls.pack(fill="x", padx=20, pady=8)

        btn_row = ctk.CTkFrame(controls, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(14, 10))

        self.start_btn = ctk.CTkButton(btn_row, text="Start Sync", command=self.start_sync,
                                        fg_color=GOOD, hover_color="#33b35f", width=120)
        self.start_btn.pack(side="left")

        self.stop_btn = ctk.CTkButton(btn_row, text="Stop", command=self.stop_sync,
                                       fg_color="#3a4152", hover_color="#4a5162", width=90,
                                       state="disabled")
        self.stop_btn.pack(side="left", padx=8)

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

        ctk.CTkLabel(
            self.root,
            text="Tip: start sync, play audio, then nudge each device's delay until they line up.\n"
                 "Each earbud has its own Bluetooth latency, so each needs its own value.",
            text_color=TEXT_DIM, font=ctk.CTkFont(size=11), justify="left",
        ).pack(anchor="w", padx=22, pady=(4, 8))

        # -- scrollable device list --
        self.devices_frame = ctk.CTkScrollableFrame(self.root, fg_color="transparent")
        self.devices_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))

    def _populate_devices(self):
        devices = self.capture.list_output_devices()
        for info in devices:
            card = DeviceCard(self.devices_frame, info,
                               on_toggle=self._on_toggle, on_delay_change=self._on_delay_change)
            card.pack(fill="x", pady=6)
            self.cards[info["index"]] = card

    # --------------------------------------------------------- live loop --

    def _update_live_stats(self):
        if not self._updating:
            return
        for idx, card in self.cards.items():
            out = self.device_outputs.get(idx)
            if out and self.ring_buffer:
                avail = self.ring_buffer.frames_available(out.reader)
                ms = (avail / float(self.ring_buffer.samplerate)) * 1000.0
                stutters = self.ring_buffer.get_stutter_count(out.reader.name)
                card.update_live_stats(ms, stutters, active=True)
            else:
                card.update_live_stats(0, 0, active=False)
        self.root.after(200, self._update_live_stats)

    # --------------------------------------------------------- actions --

    def start_sync(self):
        selected = [idx for idx, card in self.cards.items() if card.enabled_var.get()]
        if not selected:
            messagebox.showwarning("No devices selected", "Turn on at least one output device to mirror to.")
            return

        try:
            samplerate = self.capture.prepare()
            self.ring_buffer = SyncRingBuffer(RING_BUFFER_SECONDS, CHANNELS, samplerate)
            self.capture.start(self.ring_buffer)
        except Exception as e:
            messagebox.showerror("Capture error", str(e))
            return

        for idx in selected:
            card = self.cards[idx]
            out = DeviceOutput(
                self.pa, card.info, self.ring_buffer,
                base_safety_ms=BASE_SAFETY_MS,
                user_delay_ms=card.delay_var.get() + self.source_delay.get(),
                volume=1.0,
            )
            out.start()
            self.device_outputs[idx] = out

        self.running = True
        self.status_dot.configure(text_color=GOOD)
        self.status_label.configure(text=f"Running — {len(self.device_outputs)} device(s)",
                                     text_color="white")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        self._updating = True
        self._update_live_stats()

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
            out = DeviceOutput(
                self.pa, card.info, self.ring_buffer,
                base_safety_ms=BASE_SAFETY_MS,
                user_delay_ms=card.delay_var.get() + self.source_delay.get(),
                volume=1.0,
            )
            out.start()
            self.device_outputs[device_index] = out
        else:
            out = self.device_outputs.pop(device_index, None)
            if out:
                out.stop()

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

    def on_close(self):
        if self.running:
            self.stop_sync()
        self.capture.close()
        self.pa.terminate()
        self.root.destroy()


def main():
    root = ctk.CTk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()