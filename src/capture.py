"""
capture.py

Captures the PC's current audio output via WASAPI loopback and writes it
into the shared SyncRingBuffer, untouched (no resampling here) so every
downstream device works from identical source data.
"""

import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import pyaudiowpatch as pyaudio

from ring_buffer import SyncRingBuffer

ENUMERATE_FLAG = "--enumerate-devices"


# Enumeration must run in a *separate process*. PortAudio reference-counts
# Pa_Initialize()/Pa_Terminate() and only scans the system's audio hardware on
# the transition from 0 -> 1. Because this app keeps a persistent PyAudio
# instance alive (the live loopback capture), that count never returns to 0, so
# any PyAudio() created *within this process* just replays the snapshot taken at
# startup -- it can never see devices plugged in or removed afterwards. A brand
# new process starts its own count at 0, so its first Pa_Initialize() performs a
# genuine, fresh hardware scan.
def _scan_output_devices():
    """Fresh WASAPI output-device scan. Must run in its own process (see above)."""
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_name = None
        try:
            default_name = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        except Exception:
            pass
        out = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("hostApi") != wasapi["index"] or info["maxOutputChannels"] <= 0:
                continue
            if info.get("isLoopbackDevice", False):
                continue
            if default_name is not None and info["name"] == default_name:
                continue
            out.append(dict(info))
        return out
    finally:
        pa.terminate()


def emit_enumeration(path=None):
    """Child-process entry: serialize the device scan to `path` (or stdout)."""
    data = json.dumps(_scan_output_devices())
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
    else:
        sys.stdout.write(data)


# In development the child is a real python.exe, so we hand it the scan via `-c`
# and read stdout. A frozen build has no python.exe (sys.executable is our own
# windowed exe with no usable stdout), so we relaunch the exe with a flag that
# runs emit_enumeration() and writes the result to a temp file we then read.
_ENUMERATE_SCRIPT = (
    "import sys; sys.path.insert(0, r'{src}'); "
    "from capture import emit_enumeration; emit_enumeration()"
)


def enumerate_output_devices(timeout: float = 6.0):
    """Return the WASAPI output devices *currently* attached to the system.

    Runs the scan in a short-lived subprocess so PortAudio re-enumerates real
    hardware every call, instead of replaying a stale snapshot.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if getattr(sys, "frozen", False):
        fd, out_path = tempfile.mkstemp(suffix=".json", prefix="syncmirror_devs_")
        os.close(fd)
        try:
            proc = subprocess.run(
                [sys.executable, ENUMERATE_FLAG, out_path],
                capture_output=True, text=True, timeout=timeout,
                creationflags=creationflags,
            )
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    data = f.read()
            except OSError:
                data = ""
            if not data.strip():
                raise RuntimeError(proc.stderr.strip() or "device enumeration subprocess failed")
            return json.loads(data)
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    src_dir = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run(
        [sys.executable, "-c", _ENUMERATE_SCRIPT.format(src=src_dir)],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or "device enumeration subprocess failed")
    return json.loads(proc.stdout)


class LoopbackCapture:
    def __init__(self, samplerate: int | None = None, channels: int = 2, frames_per_buffer: int = 512):
        self.samplerate = samplerate
        self.channels = channels
        self.frames_per_buffer = frames_per_buffer
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self.device_info = None
        self._source_output_device_index = None

    def prepare(self):
        self.device_info = self.get_default_loopback_device()
        default_rate = self.device_info.get("defaultSampleRate")
        if default_rate is not None:
            self.samplerate = int(default_rate)
        elif self.samplerate is None:
            self.samplerate = 48000
        return self.samplerate

    def get_default_loopback_device(self):
        """Find the WASAPI loopback device that mirrors the current default output."""
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        self._source_output_device_index = default_speakers["index"]

        if not default_speakers.get("isLoopbackDevice", False):
            for loopback in self._pa.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    return loopback
            raise RuntimeError(
                "Could not find a loopback device matching the current default output. "
                "Make sure 'Stereo Mix' style loopback is available (pyaudiowpatch provides "
                "this automatically on Windows 10/11 via WASAPI)."
            )
        return default_speakers

    def start(self, ring_buffer: SyncRingBuffer):
        if self.device_info is None:
            self.device_info = self.get_default_loopback_device()
        if self.samplerate is None:
            default_rate = self.device_info.get("defaultSampleRate")
            self.samplerate = int(default_rate) if default_rate is not None else 48000

        def callback(in_data, frame_count, time_info, status):
            # in_data is raw interleaved float32 PCM from WASAPI loopback
            samples = np.frombuffer(in_data, dtype=np.float32)
            frames = samples.reshape(-1, self.channels)
            ring_buffer.write(frames)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self.channels,
            rate=self.samplerate,
            frames_per_buffer=self.frames_per_buffer,
            input=True,
            input_device_index=self.device_info["index"],
            stream_callback=callback,
        )
        self._stream.start_stream()

    def stop(self):
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None

    def close(self):
        self.stop()
        self._pa.terminate()

    def list_output_devices(self):
        """List all WASAPI output devices (candidates for mirrored playback).

        Delegates to enumerate_output_devices(), which runs the scan in a
        short-lived subprocess. That is the only way to reflect what's *currently*
        attached: PortAudio only scans hardware on a 0 -> 1 Pa_Initialize()
        transition, and this process keeps a PyAudio instance alive for the live
        capture, pinning the count above 0. A fresh subprocess starts at 0, so it
        always sees newly connected / disconnected devices. Safe to call at any
        time, including while capture is running.
        """
        return enumerate_output_devices()