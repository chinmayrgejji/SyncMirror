"""
ring_buffer.py

A single-writer / multi-reader circular audio buffer, indexed by an
ever-increasing sample counter (not wall-clock time). This is the core
piece that makes synchronized multi-device playback possible.

Design:
- The capture thread writes newly-captured frames and advances `write_pos`.
- Every output device has its own `ReaderState`, which tracks its own
  `read_pos`. Each device can be reading from a *different* point in the
  timeline (i.e. a different delay), completely independently.
- Delay is expressed simply as "how many frames behind the write head
  should this reader stay" -- turning delay compensation into pointer
  arithmetic instead of resampling or timestamp math.
- Drift correction: if a reader's actual position drifts too far from its
  *target* position (because its output device's clock runs slightly
  faster/slower than the capture clock), we nudge it by silently
  duplicating or dropping a single frame every so often, instead of
  jumping -- this avoids audible clicks.
"""

import threading
import numpy as np
import logging
import os
from datetime import datetime

logger = logging.getLogger("synced_audio_ring")


class ReaderState:
    """Per-output-device read cursor into the shared ring buffer."""

    def __init__(self, name: str):
        self.name = name
        self.read_pos = None          # int, absolute sample index; None until primed
        self.target_delay_frames = 0  # base safety buffer + user-tunable slider, in frames
        self.volume = 1.0             # 0.0 - 1.0+, applied on read
        self.lock = threading.Lock()

    def set_delay_frames(self, frames: int):
        with self.lock:
            self.target_delay_frames = max(0, int(frames))

    def set_volume(self, volume: float):
        with self.lock:
            self.volume = max(0.0, float(volume))


class SyncRingBuffer:
    """
    Shared circular buffer. `channels` and `samplerate` must match the
    capture format. All output devices read from this same buffer via
    WASAPI *shared mode*, so Windows' own audio engine performs any
    final resampling to each device's mix format -- consistently, for
    every device, using the same code path. This avoids the "each
    earbud sounds different" problem caused by ad hoc per-device
    resampling.
    """

    def __init__(self, capacity_seconds: float, channels: int, samplerate: int):
        self.channels = channels
        self.samplerate = samplerate
        self.capacity_frames = int(capacity_seconds * samplerate)
        self._buf = np.zeros((self.capacity_frames, channels), dtype=np.float32)
        self.write_pos = 0  # absolute sample index of the next frame to be written
        self._write_lock = threading.Lock()

        # Stutter logging: per-device counters and on-disk append logs
        self._stutter_counters: dict[str, int] = {}
        logs_root = os.path.join(os.path.dirname(__file__), '..', 'stutter_logs')
        os.makedirs(logs_root, exist_ok=True)
        self._stutter_logs_root = os.path.abspath(logs_root)

        # Drift-correction tuning
        self.max_drift_frames = int(0.020 * samplerate)   # allow +/-20ms before correcting
        self.correction_step = 1                          # frames nudged per correction event

    # ---------------------------------------------------------- writer --

    def write(self, frames: np.ndarray):
        """Called from the capture callback. frames shape: (n, channels)."""
        n = frames.shape[0]
        if n == 0:
            return
        with self._write_lock:
            start = self.write_pos % self.capacity_frames
            end = start + n
            if end <= self.capacity_frames:
                self._buf[start:end] = frames
            else:
                first = self.capacity_frames - start
                self._buf[start:] = frames[:first]
                self._buf[: end - self.capacity_frames] = frames[first:]
            self.write_pos += n

    # ---------------------------------------------------------- reader --

    def set_reader_delay(self, reader: ReaderState, frames: int):
        with reader.lock:
            reader.target_delay_frames = max(0, int(frames))
            if reader.read_pos is not None:
                current_write = self.write_pos
                reader.read_pos = max(0, current_write - reader.target_delay_frames)

    def read(self, reader: ReaderState, n_frames: int) -> np.ndarray:
        """
        Called from an output device's callback. Returns exactly
        n_frames of audio (silence-padded if the buffer hasn't caught up
        yet), applying that device's volume and delay/drift compensation.
        """
        with reader.lock:
            target_delay = reader.target_delay_frames
            volume = reader.volume

        current_write = self.write_pos  # snapshot; writer may keep moving, that's fine

        if reader.read_pos is None:
            # First read: prime this device's cursor safely behind the write head.
            reader.read_pos = max(0, current_write - target_delay)

        target_pos = current_write - target_delay
        drift = reader.read_pos - target_pos  # positive = reading ahead of where it should be

        out = np.zeros((n_frames, self.channels), dtype=np.float32)

        # Not enough data written yet at all -> return silence, hold position.
        if current_write - reader.read_pos < 0:
            reader.read_pos = max(0, current_write - target_delay)

        available = current_write - reader.read_pos
        to_copy = min(n_frames, max(0, available))

        # Log underruns / stutters: when we can't supply the requested frames
        if to_copy < n_frames:
            missing = n_frames - to_copy
            logger.warning(
                "STUTTER: reader=%s requested=%d available=%d missing=%d write_pos=%d read_pos=%s",
                reader.name, n_frames, available, missing, current_write, str(reader.read_pos),
            )

            # Increment in-memory counter and append a timestamped line to a per-device log file
            cnt = self._stutter_counters.get(reader.name, 0) + 1
            self._stutter_counters[reader.name] = cnt

            safe_name = (
                reader.name.replace(' ', '_')
                .replace('/', '_')
                .replace('\\', '_')
                .replace('(', '')
                .replace(')', '')
            )
            log_path = os.path.join(self._stutter_logs_root, f"{safe_name}.log")
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(
                        f"{datetime.utcnow().isoformat()}Z stutter#{cnt} reader={reader.name} requested={n_frames} "
                        f"available={available} missing={missing} write_pos={current_write} read_pos={reader.read_pos}\n"
                    )
            except Exception:
                logger.exception("Failed to write stutter log for %s", reader.name)

        if to_copy > 0:
            start = reader.read_pos % self.capacity_frames
            end = start + to_copy
            if end <= self.capacity_frames:
                out[:to_copy] = self._buf[start:end]
            else:
                first = self.capacity_frames - start
                out[:first] = self._buf[start:]
                out[first:to_copy] = self._buf[: end - self.capacity_frames]

        reader.read_pos += to_copy

        # --- gentle drift correction: nudge, don't jump ---
        if abs(drift) > self.max_drift_frames:
            step = self.correction_step if drift < 0 else -self.correction_step
            reader.read_pos += step

        if volume != 1.0:
            out *= volume

        return out

    def frames_available(self, reader: ReaderState) -> int:
        if reader.read_pos is None:
            return 0
        return max(0, self.write_pos - reader.read_pos)
    
    def get_stutter_count(self, name: str) -> int:
        return self._stutter_counters.get(name, 0)
