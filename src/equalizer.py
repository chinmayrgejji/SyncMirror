"""
equalizer.py

A single global graphic equalizer applied once at the capture stage, before
audio is written to the shared ring buffer. Because every mirrored device reads
from that one buffer, the EQ costs the same whether one device or ten are
connected -- and it can never fall out of sync between devices.

The filtering is a cascade of RBJ peaking biquads (one second-order section per
band) run through scipy.signal.sosfilt with persistent filter state, so it adds
no latency and no block-boundary artifacts. process() runs on the WASAPI capture
callback (the audio thread), so it holds a short lock and does only vectorized
numpy/scipy work -- never a per-sample Python loop.
"""

import threading

import numpy as np
from scipy.signal import sosfilt

# ISO ten-band graphic-EQ centre frequencies.
EQ_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_GAIN_MIN = -12.0
EQ_GAIN_MAX = 12.0
EQ_Q = 1.0  # per-band quality factor (bandwidth); ~1.0 reads as a smooth curve

# Named starting points. Each list is one gain (dB) per band, in EQ_BANDS order.
EQ_PRESETS = {
    "Flat":         [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Bass Boost":   [7, 6, 5, 3, 1, 0, 0, 0, 0, 0],
    "Treble Boost": [0, 0, 0, 0, 0, 1, 3, 5, 6, 7],
    "Vocal":        [-3, -2, -1, 2, 4, 4, 3, 1, 0, -1],
    "Rock":         [5, 4, 2, -1, -1, 0, 2, 4, 5, 5],
    "Loudness":     [6, 5, 3, 0, -1, -1, 1, 3, 5, 6],
}
DEFAULT_PRESET = "Flat"


def _peaking_sos(f0, gain_db, q, fs):
    """One RBJ 'peaking EQ' biquad as a second-order section [b0,b1,b2,1,a1,a2]."""
    a_amp = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / fs
    cos_w0 = np.cos(w0)
    alpha = np.sin(w0) / (2.0 * q)

    b0 = 1.0 + alpha * a_amp
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * a_amp
    a0 = 1.0 + alpha / a_amp
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / a_amp
    return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]


class GraphicEqualizer:
    """Thread-safe global EQ. UI thread sets gains/enabled; audio thread filters."""

    def __init__(self, bands=EQ_BANDS):
        self.bands = tuple(bands)
        self._lock = threading.Lock()
        self._enabled = False
        self._gains = [0.0] * len(self.bands)
        self._fs = None
        self._channels = None
        self._sos = None   # (n_sections, 6) float64, or None until configured
        self._zi = None    # (n_sections, 2, channels) running filter state

    # -- configuration (UI/main thread) --

    def configure(self, samplerate, channels):
        """Bind to a capture format. Rebuilds coefficients and resets filter state."""
        with self._lock:
            self._fs = int(samplerate)
            self._channels = int(channels)
            self._rebuild_locked()
            self._reset_state_locked()

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = bool(enabled)

    def is_enabled(self):
        with self._lock:
            return self._enabled

    def set_gains(self, gains):
        """Replace all band gains (dB). Filter state is kept for a click-free change."""
        with self._lock:
            for i in range(min(len(self._gains), len(gains))):
                self._gains[i] = float(
                    max(EQ_GAIN_MIN, min(EQ_GAIN_MAX, gains[i])))
            self._rebuild_locked()

    def set_band(self, index, gain_db):
        with self._lock:
            if 0 <= index < len(self._gains):
                self._gains[index] = float(
                    max(EQ_GAIN_MIN, min(EQ_GAIN_MAX, gain_db)))
                self._rebuild_locked()

    def get_gains(self):
        with self._lock:
            return list(self._gains)

    def apply_preset(self, name):
        """Set gains from a named preset. Returns the applied gains, or None."""
        preset = EQ_PRESETS.get(name)
        if preset is None:
            return None
        self.set_gains(preset)
        return list(preset)

    def snapshot(self):
        """State for persistence / the web remote."""
        with self._lock:
            return {"enabled": self._enabled,
                    "gains": list(self._gains),
                    "bands": list(self.bands)}

    # -- filtering (audio thread) --

    def process(self, frames):
        """Filter one (frames, channels) float32 block in place-equivalent fashion.

        Returns the input untouched when disabled or unconfigured, so a bypassed
        EQ adds zero cost on the capture callback.
        """
        with self._lock:
            if not self._enabled or self._sos is None:
                return frames
            if self._zi is None or self._zi.shape[2] != frames.shape[1]:
                self._reset_state_locked(frames.shape[1])
            out, self._zi = sosfilt(self._sos, frames, axis=0, zi=self._zi)
        return out.astype(np.float32, copy=False)

    # -- internals (must hold self._lock) --

    def _rebuild_locked(self):
        if self._fs is None:
            self._sos = None
            return
        sections = [_peaking_sos(f, g, EQ_Q, self._fs)
                    for f, g in zip(self.bands, self._gains)]
        self._sos = np.asarray(sections, dtype=np.float64)

    def _reset_state_locked(self, channels=None):
        if channels is not None:
            self._channels = int(channels)
        if self._sos is None or self._channels is None:
            self._zi = None
            return
        self._zi = np.zeros((self._sos.shape[0], 2, self._channels), dtype=np.float64)
