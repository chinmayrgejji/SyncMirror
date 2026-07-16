"""
output.py

One DeviceOutput per mirrored earbud/speaker. Each opens its own WASAPI
*shared mode* stream (letting Windows' audio engine handle any final
sample-rate/format conversion consistently across all devices), and pulls
audio from the shared SyncRingBuffer at its own, independently-tunable
delay offset.
"""

import numpy as np
import pyaudiowpatch as pyaudio

from ring_buffer import SyncRingBuffer, ReaderState


class DeviceOutput:
    def __init__(self, pa: pyaudio.PyAudio, device_info: dict, ring_buffer: SyncRingBuffer,
                 base_safety_ms: float = 150.0, user_delay_ms: float = 0.0, volume: float = 1.0):
        self.pa = pa
        self.device_info = device_info
        self.ring_buffer = ring_buffer
        self.reader = ReaderState(name=device_info["name"])
        self._stream = None
        self.output_rate = int(device_info.get("defaultSampleRate", ring_buffer.samplerate))
        self.input_rate = ring_buffer.samplerate
        self.frames_per_buffer = 2048

        base_frames = int((base_safety_ms / 1000.0) * ring_buffer.samplerate)
        user_frames = int((user_delay_ms / 1000.0) * ring_buffer.samplerate)
        self.reader.set_delay_frames(base_frames + user_frames)
        self.reader.set_volume(volume)

        self._base_frames = base_frames

    # -- live tuning, callable while playing (e.g. from a UI slider) --

    def set_user_delay_ms(self, ms: float):
        user_frames = int((ms / 1000.0) * self.ring_buffer.samplerate)
        self.ring_buffer.set_reader_delay(self.reader, self._base_frames + user_frames)

    def set_volume(self, volume: float):
        self.reader.set_volume(volume)

    def _needed_source_frames(self, output_frame_count: int) -> int:
        ratio = self.input_rate / self.output_rate
        return max(1, int(np.ceil(output_frame_count * ratio)) + 2)

    def _resample(self, frames: np.ndarray, output_frame_count: int) -> np.ndarray:
        if self.input_rate == self.output_rate:
            if frames.shape[0] == output_frame_count:
                return frames
            if frames.shape[0] < output_frame_count:
                out = np.zeros((output_frame_count, frames.shape[1]), dtype=np.float32)
                out[:frames.shape[0]] = frames
                return out
            return frames[:output_frame_count]

        if frames.shape[0] == 0:
            return np.zeros((output_frame_count, self.ring_buffer.channels), dtype=np.float32)

        src_len = frames.shape[0]
        if src_len == 1:
            return np.repeat(frames, output_frame_count, axis=0)

        target_positions = np.linspace(0, src_len - 1, num=output_frame_count, dtype=np.float32)
        src_positions = np.arange(src_len, dtype=np.float32)
        out = np.empty((output_frame_count, frames.shape[1]), dtype=np.float32)
        for ch in range(frames.shape[1]):
            out[:, ch] = np.interp(target_positions, src_positions, frames[:, ch])
        return out

    # -------------------------------------------------------------------

    def start(self):
        channels = self.ring_buffer.channels

        def callback(in_data, frame_count, time_info, status):
            source_frames = self._needed_source_frames(frame_count)
            source = self.ring_buffer.read(self.reader, source_frames)
            out = self._resample(source, frame_count)
            with self.reader.lock:
                volume = self.reader.volume
            if volume != 1.0:
                out *= volume
            return (out.tobytes(), pyaudio.paContinue)

        self._stream = self.pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=self.output_rate,
            output=True,
            output_device_index=self.device_info["index"],
            frames_per_buffer=self.frames_per_buffer,
            stream_callback=callback,
        )
        self._stream.start_stream()

    def stop(self):
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
