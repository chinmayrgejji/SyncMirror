"""
app.py

Prototype UI for the multi-device synced audio mirror.

Run with:  python app.py
(Windows only -- requires pyaudiowpatch, which wraps WASAPI.)
"""

import threading
import tkinter as tk
from tkinter import ttk, messagebox

import pyaudiowpatch as pyaudio

from ring_buffer import SyncRingBuffer
from capture import LoopbackCapture
from output import DeviceOutput

SAMPLERATE = 48000
CHANNELS = 2
RING_BUFFER_SECONDS = 10.0
BASE_SAFETY_MS = 150.0  # headroom so every device has room to be delayed further if needed


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Synced Audio Mirror (prototype)")
        self.root.geometry("560x480")

        self.pa = pyaudio.PyAudio()
        self.ring_buffer = None
        self.capture = LoopbackCapture(samplerate=None, channels=CHANNELS)

        self.device_outputs = {}   # device_index -> DeviceOutput
        self.device_vars = {}      # device_index -> {"enabled": BoolVar, "delay": DoubleVar, "delay_label": StringVar}
        self.running = False
        self.source_delay = tk.DoubleVar(value=0.0)
        self.source_delay_label = tk.StringVar(value="0 ms")
        self._updating_buffers = False

        self._build_ui()
        self._populate_devices()

    # ------------------------------------------------------------- UI --

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        self.start_btn = ttk.Button(top, text="Start Sync", command=self.start_sync)
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(top, text="Stop Sync", command=self.stop_sync, state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        self.status_label = ttk.Label(top, text="Stopped")
        self.status_label.pack(side="left", padx=12)

        # Source delay controls (applies a global offset to all outputs)
        src = ttk.Frame(self.root, padding=(10, 6))
        src.pack(fill="x")
        ttk.Label(src, text="Source Delay:").pack(side="left")
        ttk.Button(src, text="-20", width=3, command=lambda: self._change_source_delay(-20)).pack(side="left", padx=(8,2))
        ttk.Button(src, text="-10", width=3, command=lambda: self._change_source_delay(-10)).pack(side="left")
        ttk.Label(src, textvariable=self.source_delay_label, width=8, anchor="center").pack(side="left", padx=(6,6))
        ttk.Button(src, text="+10", width=3, command=lambda: self._change_source_delay(10)).pack(side="left")
        ttk.Button(src, text="+20", width=3, command=lambda: self._change_source_delay(20)).pack(side="left", padx=(2,8))

        ttk.Label(
            self.root,
            text="Tip: after starting, nudge each device's delay slider until they sound in sync.\n"
                 "This compensates for that specific earbud's own Bluetooth latency.",
            wraplength=520, justify="left"
        ).pack(fill="x", padx=10, pady=(0, 6))

        self.devices_frame = ttk.Frame(self.root, padding=10)
        self.devices_frame.pack(fill="both", expand=True)

    def _populate_devices(self):
        devices = self.capture.list_output_devices()
        for info in devices:
            row = ttk.Frame(self.devices_frame, padding=4, relief="groove")
            row.pack(fill="x", pady=4)

            enabled_var = tk.BooleanVar(value=False)
            delay_var = tk.DoubleVar(value=0.0)
            delay_display = tk.StringVar(value="0 ms")
            buffer_display = tk.StringVar(value="buf: --")

            ttk.Checkbutton(row, text=info["name"], variable=enabled_var).pack(side="left")

            ttk.Label(row, text="Delay:").pack(side="left", padx=(20, 2))
            ttk.Button(row, text="-20", width=3, command=lambda idx=info["index"]: self._change_delay(idx, -20)).pack(side="left")
            ttk.Button(row, text="-10", width=3, command=lambda idx=info["index"]: self._change_delay(idx, -10)).pack(side="left")
            ttk.Label(row, textvariable=delay_display, width=8, anchor="center").pack(side="left", padx=(4, 4))
            ttk.Button(row, text="+10", width=3, command=lambda idx=info["index"]: self._change_delay(idx, 10)).pack(side="left")
            ttk.Button(row, text="+20", width=3, command=lambda idx=info["index"]: self._change_delay(idx, 20)).pack(side="left")

            self.device_vars[info["index"]] = {
                "enabled": enabled_var,
                "delay": delay_var,
                "delay_label": delay_display,
                "buffer_label": buffer_display,
                "info": info,
            }

    def _update_buffer_displays(self):
        # periodic UI update showing frames available for each active reader
        if not self._updating_buffers:
            return
        for idx, dev in list(self.device_vars.items()):
            buf_var = dev.get("buffer_label")
            if not buf_var:
                continue
            out = self.device_outputs.get(idx)
            if out:
                try:
                    avail = self.ring_buffer.frames_available(out.reader)
                    ms = int((avail / float(self.ring_buffer.samplerate)) * 1000.0)
                    buf_var.set(f"buf: {ms}ms")
                except Exception:
                    buf_var.set("buf: err")
            else:
                buf_var.set("buf: --")

        # schedule next update
        self.root.after(250, self._update_buffer_displays)

    # --------------------------------------------------------- actions --

    def start_sync(self):
        selected = [d for idx, d in self.device_vars.items() if d["enabled"].get()]
        if not selected:
            messagebox.showwarning("No devices selected", "Check at least one output device to mirror to.")
            return

        try:
            samplerate = self.capture.prepare()
            self.ring_buffer = SyncRingBuffer(RING_BUFFER_SECONDS, CHANNELS, samplerate)
            self.capture.start(self.ring_buffer)
        except Exception as e:
            messagebox.showerror("Capture error", str(e))
            return

        for d in selected:
            info = d["info"]
            out = DeviceOutput(
                self.pa, info, self.ring_buffer,
                base_safety_ms=BASE_SAFETY_MS,
                user_delay_ms=d["delay"].get() + self.source_delay.get(),
                volume=1.0,
            )
            out.start()
            self.device_outputs[info["index"]] = out

        self.running = True
        self.status_label.config(text=f"Running -> {len(self.device_outputs)} device(s)")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        # start periodic buffer display updates
        self._updating_buffers = True
        self._update_buffer_displays()

    def stop_sync(self):
        for out in self.device_outputs.values():
            out.stop()
        self.device_outputs.clear()
        self.capture.stop()

        self.running = False
        self.status_label.config(text="Stopped")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._updating_buffers = False

    def _change_delay(self, device_index, delta_ms):
        device = self.device_vars.get(device_index)
        if not device:
            return

        current_delay = device["delay"].get()
        new_delay = max(-500.0, min(500.0, current_delay + delta_ms))
        device["delay"].set(new_delay)
        device["delay_label"].set(f"{int(new_delay)} ms")

        out = self.device_outputs.get(device_index)
        if out:
            combined = new_delay + self.source_delay.get()
            out.set_user_delay_ms(combined)

    def _change_source_delay(self, delta_ms):
        current = self.source_delay.get()
        new = max(-500.0, min(500.0, current + delta_ms))
        self.source_delay.set(new)
        self.source_delay_label.set(f"{int(new)} ms")

        # update all active outputs to include new global offset
        for idx, out in self.device_outputs.items():
            dev = self.device_vars.get(idx)
            if not dev:
                continue
            user = dev["delay"].get()
            out.set_user_delay_ms(user + new)

    def on_close(self):
        if self.running:
            self.stop_sync()
        self.capture.close()
        self.pa.terminate()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
