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
        self.read_pos = None                      # int, absolute sample index; None until primed
        self.user_base_delay_frames = 0           # delay requested by the app (base+sources)
        self.adaptive_delay_frames = 0            # extra safety added automatically on underruns
        self.target_delay_frames = 0              # actual total delay used for this reader
        self.volume = 1.0                         # 0.0 - 1.0+, applied on read
        self.lock = threading.Lock()
        self.underrun_count = 0
        self._consecutive_underruns = 0

    def _update_target_delay(self):
        self.target_delay_frames = max(0, self.user_base_delay_frames + self.adaptive_delay_frames)

    def set_delay_frames(self, frames: int):
        with self.lock:
            self.user_base_delay_frames = max(0, int(frames))
            self._update_target_delay()

    def add_adaptive_delay_frames(self, frames: int):
        with self.lock:
            self.adaptive_delay_frames = max(0, self.adaptive_delay_frames + int(frames))
            self._update_target_delay()

    def clear_adaptive_delay(self):
        with self.lock:
            self.adaptive_delay_frames = 0
            self._update_target_delay()

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
        self.max_adaptive_delay_frames = int(0.500 * samplerate)  # cap auto safety at 500ms
        self.min_adaptive_bump_frames = int(0.010 * samplerate)  # at least 10ms on underrun
        # If a reader experiences this many consecutive underruns, perform a fast resync
        self.underrun_resync_threshold = 3

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

    def peek_latest(self, n_frames: int) -> np.ndarray:
        """Non-destructively return the most recent n_frames as a mono float32
        array, for visualization. Does not touch any reader cursor."""
        with self._write_lock:
            write_pos = self.write_pos
            if write_pos <= 0 or n_frames <= 0:
                return np.zeros(0, dtype=np.float32)
            n = min(n_frames, self.capacity_frames, write_pos)
            start = (write_pos - n) % self.capacity_frames
            end = start + n
            if end <= self.capacity_frames:
                chunk = self._buf[start:end].copy()
            else:
                first = self.capacity_frames - start
                chunk = np.empty((n, self.channels), dtype=np.float32)
                chunk[:first] = self._buf[start:]
                chunk[first:] = self._buf[: end - self.capacity_frames]
        if chunk.ndim == 2 and chunk.shape[1] > 1:
            return chunk.mean(axis=1)
        return chunk.reshape(-1)

    # ---------------------------------------------------------- reader --

    def set_reader_delay(self, reader: ReaderState, frames: int):
        with reader.lock:
            reader.user_base_delay_frames = max(0, int(frames))
            reader._update_target_delay()
            # Do not reposition the read cursor immediately on live delay changes.
            # The output callback will gently drift the reader to the new target.

    def read(self, reader: ReaderState, n_frames: int) -> np.ndarray:
        """
        Called from an output device's callback. Returns exactly
        n_frames of audio (silence-padded if the buffer hasn't caught up
        yet), applying that device's volume and delay/drift compensation.
        """
        with reader.lock:
            reader._update_target_delay()
            target_delay = reader.target_delay_frames
            volume = reader.volume
            read_pos = reader.read_pos

        current_write = self.write_pos  # snapshot; writer may keep moving, that's fine

        if read_pos is None:
            read_pos = max(0, current_write - target_delay)

        out = np.zeros((n_frames, self.channels), dtype=np.float32)

        if current_write - read_pos < 0:
            read_pos = max(0, current_write - target_delay)

        available = current_write - read_pos
        to_copy = min(n_frames, max(0, available))
        target_pos = current_write - target_delay
        drift = read_pos - target_pos  # positive = reading ahead of where it should be

        if to_copy < n_frames:
            missing = n_frames - to_copy
            logger.warning(
                "STUTTER: reader=%s requested=%d available=%d missing=%d write_pos=%d read_pos=%s",
                reader.name, n_frames, available, missing, current_write, str(read_pos),
            )

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
                        f"available={available} missing={missing} write_pos={current_write} read_pos={read_pos}\n"
                    )
            except Exception:
                logger.exception("Failed to write stutter log for %s", reader.name)

            with reader.lock:
                reader.underrun_count += 1
                reader._consecutive_underruns = getattr(reader, '_consecutive_underruns', 0) + 1
                bump = max(missing, self.min_adaptive_bump_frames)
                reader.adaptive_delay_frames = min(self.max_adaptive_delay_frames,
                                                  reader.adaptive_delay_frames + bump)
                reader._update_target_delay()

            # If underruns keep happening across several consecutive callbacks,
            # perform a fast resync which mimics reconnecting the device (what helps
            # in the UI) to avoid very long audible gaps.
            if getattr(reader, '_consecutive_underruns', 0) >= self.underrun_resync_threshold:
                logger.info(
                    "RESYNC: reader=%s consecutive_underruns=%d performing fast resync",
                    reader.name, getattr(reader, '_consecutive_underruns', 0),
                )
                # aggressive reposition to the current target and clear adaptive safety
                read_pos = max(0, current_write - target_delay)
                with reader.lock:
                    reader.adaptive_delay_frames = 0
                    reader._update_target_delay()
                    reader._consecutive_underruns = 0
            else:
                rewind = min(bump, read_pos)
                read_pos = max(0, read_pos - rewind)

        if to_copy > 0:
            with self._write_lock:
                start = read_pos % self.capacity_frames
                end = start + to_copy
                if end <= self.capacity_frames:
                    out[:to_copy] = self._buf[start:end]
                else:
                    first = self.capacity_frames - start
                    out[:first] = self._buf[start:]
                    out[first:to_copy] = self._buf[: end - self.capacity_frames]

        # successful read resets consecutive underrun counter
        if to_copy > 0:
            if hasattr(reader, '_consecutive_underruns'):
                with reader.lock:
                    reader._consecutive_underruns = 0

        read_pos += to_copy

        # --- drift correction: move toward the target delay more aggressively when needed ---
        if abs(drift) > self.max_drift_frames:
            max_step = int(self.samplerate * 0.010)
            desired_step = max(1, abs(drift) // 8)
            step = min(desired_step, max_step)
            if drift > 0:
                read_pos -= step
            else:
                read_pos += step

        with reader.lock:
            reader.read_pos = read_pos
            if reader.adaptive_delay_frames > 0 and available > target_delay + int(self.samplerate * 0.030):
                reader.adaptive_delay_frames = max(0, reader.adaptive_delay_frames - 1)
                reader._update_target_delay()

        if volume != 1.0:
            out *= volume

        return out

    def frames_available(self, reader: ReaderState) -> int:
        if reader.read_pos is None:
            return 0
        return max(0, self.write_pos - reader.read_pos)
    
    def get_stutter_count(self, name: str) -> int:
        return self._stutter_counters.get(name, 0)
