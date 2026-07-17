"""
capture.py

Captures the PC's current audio output via WASAPI loopback and writes it
into the shared SyncRingBuffer, untouched (no resampling here) so every
downstream device works from identical source data.
"""

import numpy as np
import pyaudiowpatch as pyaudio

from ring_buffer import SyncRingBuffer


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
        """List all WASAPI output devices (candidates for mirrored playback)."""
        if self._source_output_device_index is None:
            try:
                self.get_default_loopback_device()
            except Exception:
                pass

        devices = []
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get("hostApi") != wasapi_info["index"] or info["maxOutputChannels"] <= 0:
                continue
            if self._source_output_device_index is not None and info["index"] == self._source_output_device_index:
                continue
            devices.append(info)
        return devices
