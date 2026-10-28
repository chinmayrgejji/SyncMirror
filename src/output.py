"""
output.py

One DeviceOutput per mirrored earbud/speaker. Each opens its own WASAPI
*shared mode* stream (letting Windows' audio engine handle any final
sample-rate/format conversion consistently across all devices), and pulls
audio from the shared SyncRingBuffer at its own, independently-tunable
delay offset.
"""

import time

import numpy as np
import pyaudiowpatch as pyaudio

from ring_buffer import SyncRingBuffer, ReaderState


# How long an output callback may go silent before we treat the stream as dead.
# A healthy shared-mode stream fires its callback every few ms; a dropped device
# (e.g. a Bluetooth earbud) stalls it, so this catches disconnects fast without
# waiting for the periodic hardware rescan.
CALLBACK_STALL_TIMEOUT_S = 0.5

# When a Bluetooth device physically drops, Windows keeps its endpoint "Active"
# for several seconds. During that grace window there's no real device left to
# rate-limit rendering, so PortAudio calls the callback in a tight loop far above
# its normal cadence. If the measured callback rate exceeds this multiple of the
# expected (healthy) rate, we treat the stream as dead. This is the signal that
# catches a dropped BT earbud before the ~10s hardware-enumeration timeout, which
# matters because the spin starves the *other*, still-connected device.
CALLBACK_SPIN_RATE_MULTIPLIER = 3.0


class DeviceOutput:
    def __init__(self, device_name: str, device_info: dict, ring_buffer: SyncRingBuffer,
                 base_safety_ms: float = 150.0, user_delay_ms: float = 0.0, volume: float = 1.0):
        # Each DeviceOutput owns its own PyAudio instance (created in start(), below)
        # rather than sharing one across all devices. PortAudio snapshots the device
        # list once per PyAudio() instance, so a private, freshly-created instance
        # guarantees this stream resolves the device as it currently exists -- and
        # means plugging/unplugging *other* devices can never disturb a stream that's
        # already running.
        self.device_name = device_name
        self.pa = None
        self.device_info = device_info
        self.ring_buffer = ring_buffer
        self.reader = ReaderState(name=device_info["name"])
        self._stream = None
        self.output_rate = int(device_info.get("defaultSampleRate", ring_buffer.samplerate))
        self.input_rate = ring_buffer.samplerate
        self.frames_per_buffer = 1024
        self._last_callback_ts = 0.0
        self._cb_count = 0
        self._last_frame_count = self.frames_per_buffer
        self._health_last_ts = 0.0
        self._health_last_count = 0
        # Carries the sub-frame remainder between callbacks so that, on average, we
        # advance the reader by exactly output_frame_count * (input/output) source
        # frames -- never the rounded-up amount, which would slowly drain the buffer.
        self._read_frac = 0.0
        # Set True the instant the callback aborts itself (dropped device).
        self._dead = False
        # Rolling window used *inside* the callback to detect a spin and self-abort.
        self._spin_window_start = 0.0
        self._spin_window_count = 0

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

    def is_alive(self) -> bool:
        """True while the output stream is still genuinely playing to a real device.

        A dropped Bluetooth device does NOT flip is_active() to False, nor does its
        callback simply stall -- during Windows' multi-second endpoint grace period
        PortAudio keeps the stream 'active' and calls the callback in a tight spin
        loop (nothing physical left to pace it). That spin is what stutters the
        other, still-connected device. We detect the death three ways:
          1. is_active() went False (device cleanly invalidated), or
          2. the callback stalled (no calls for CALLBACK_STALL_TIMEOUT_S), or
          3. the callback is spinning far above its healthy rate.
        """
        stream = self._stream
        if stream is None:
            return False
        if self._dead:
            return False
        try:
            if not stream.is_active():
                return False
        except Exception:
            return False

        now = time.monotonic()
        if now - self._last_callback_ts > CALLBACK_STALL_TIMEOUT_S:
            return False

        dt = now - self._health_last_ts
        if dt >= 0.15:
            dcount = self._cb_count - self._health_last_count
            self._health_last_ts = now
            self._health_last_count = self._cb_count
            frame_count = max(1, self._last_frame_count)
            expected_rate = self.output_rate / frame_count  # callbacks/sec when healthy
            actual_rate = dcount / dt
            if actual_rate > expected_rate * CALLBACK_SPIN_RATE_MULTIPLIER:
                return False
        return True

    def _needed_source_frames(self, output_frame_count: int) -> int:
        ratio = self.input_rate / self.output_rate
        return max(1, int(np.ceil(output_frame_count * ratio)) + 2)

    def _plan_read(self, output_frame_count: int) -> tuple[int, int]:
        """Return (frames_to_read, frames_to_consume) for one output callback.

        `frames_to_consume` is the *exact* number of source frames this callback is
        entitled to advance the reader by, accumulated fractionally so that over time
        we advance by precisely output_frame_count * (input/output) -- the physically
        correct rate the output hardware demands. `frames_to_read` is a hair larger so
        the resampler has an endpoint sample to interpolate against; those extra frames
        are peeked but NOT consumed, so they never drain the safety buffer.
        """
        ratio = self.input_rate / self.output_rate
        exact = output_frame_count * ratio + self._read_frac
        consume = int(exact)
        self._read_frac = exact - consume
        consume = max(1, consume)
        read = consume + 2
        return read, consume

    def _resample(self, frames: np.ndarray, output_frame_count: int, phase: float = 0.0) -> np.ndarray:
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

        # Fixed step that continues from the previous block's fractional phase.
        ratio = self.input_rate / self.output_rate
        pos = phase + np.arange(output_frame_count, dtype=np.float64) * ratio
        i0 = np.minimum(pos.astype(np.int64), src_len - 2)
        f = np.clip(pos - i0, 0.0, 1.0).astype(np.float32)[:, None]
        return (frames[i0] * (1.0 - f) + frames[i0 + 1] * f).astype(np.float32, copy=False)

    # -------------------------------------------------------------------

    def _resolve_device_index(self):
        """Look up this device's *current* PortAudio index by name.

        The index cached on self.device_info may be stale (e.g. it was read from a
        device list scan taken a moment ago, and another device has since been
        plugged in or unplugged, shifting indices around). Resolving fresh, against
        the PyAudio instance we're about to open the stream on, guarantees we open
        the right hardware.
        """
        wasapi_info = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if (info.get("hostApi") == wasapi_info["index"]
                    and info["maxOutputChannels"] > 0
                    and info["name"] == self.device_name):
                return i
        raise RuntimeError(f"Device '{self.device_name}' is no longer available.")

    def start(self):
        channels = self.ring_buffer.channels
        self.pa = pyaudio.PyAudio()
        now = time.monotonic()
        self._last_callback_ts = now
        self._health_last_ts = now
        self._health_last_count = 0
        self._cb_count = 0
        self._dead = False
        self._spin_window_start = now
        self._spin_window_count = 0
        self._read_frac = 0.0

        def callback(in_data, frame_count, time_info, status):
            now = time.monotonic()
            self._last_callback_ts = now
            self._cb_count += 1
            self._last_frame_count = frame_count

            # --- self-abort on spin --------------------------------------
            # A dropped Bluetooth device isn't paced by any hardware anymore, so
            # PortAudio calls this callback back-to-back in a tight loop, pegging a
            # CPU core and (because every output stream shares one in-process
            # PortAudio host) starving the other, still-connected device. Detect
            # that spin here and return paAbort, which makes PortAudio stop calling
            # us *immediately* -- far faster, and without the blocking drain, than
            # tearing the stream down from the UI thread.
            self._spin_window_count += 1
            win = now - self._spin_window_start
            if win >= 0.05:
                expected_rate = self.output_rate / max(1, frame_count)  # cb/sec healthy
                actual_rate = self._spin_window_count / win
                self._spin_window_start = now
                self._spin_window_count = 0
                if actual_rate > expected_rate * CALLBACK_SPIN_RATE_MULTIPLIER:
                    self._dead = True
                    silence = np.zeros((frame_count, channels), dtype=np.float32)
                    return (silence.tobytes(), pyaudio.paAbort)
            # -------------------------------------------------------------

            phase = self._read_frac  # must be captured before _plan_read updates it
            read_frames, consume_frames = self._plan_read(frame_count)
            source = self.ring_buffer.read(self.reader, read_frames, consume=consume_frames)
            out = self._resample(source, frame_count, phase)
            with self.reader.lock:
                volume = self.reader.volume
            if volume != 1.0:
                out *= volume
            return (out.tobytes(), pyaudio.paContinue)

        try:
            device_index = self._resolve_device_index()
            self._stream = self.pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=self.output_rate,
                output=True,
                output_device_index=device_index,
                frames_per_buffer=self.frames_per_buffer,
                stream_callback=callback,
            )
            self._stream.start_stream()
        except Exception:
            # Opening failed (e.g. the device isn't visible to this process's
            # PortAudio yet). Tear the PyAudio instance back down so it doesn't pin
            # PortAudio's init refcount above 0 -- otherwise a later engine rebuild
            # could never reach 0 and re-scan hardware to pick this device up.
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self.pa.terminate()
            self.pa = None
            raise

    def stop(self):
        if self._stream is not None:
            # The stream may already be stopped (the callback self-aborted on a
            # dropped device), in which case stop_stream() raises -- ignore it and
            # go straight to close().
            try:
                self._stream.stop_stream()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None