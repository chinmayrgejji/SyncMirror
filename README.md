# SyncMirror

Mirror your PC's audio to **multiple output devices at once**, kept in sync —
built to solve the two problems that plague naive multi-output setups:
**delay between devices** and **inconsistent sound quality** across devices.

Capture the system's default output via WASAPI loopback, feed it into a single
shared, sample-indexed ring buffer, and let each output device read from that
buffer at its own tunable delay — Snapcast/Sonos-style multi-room sync, on
Windows.

## How it works

| Common problem | What SyncMirror does instead |
|---|---|
| Same audio fired at every device with no sync mechanism -> devices drift out of time | Every device reads from a shared, sample-indexed ring buffer at its own **tunable delay offset** |
| No drift correction -> devices slip further out of sync over time | Each output cursor is gently nudged (1 sample at a time, inaudible) if it drifts more than ~20ms from its target position |
| Ad hoc per-device resampling in Python/numpy -> different devices sound tonally different | All outputs use **WASAPI shared mode**, so Windows' own audio engine does the resampling/format conversion consistently for every device |
| Fixed, non-adjustable sync | A per-device delay slider (-500ms to +500ms) lets you dial in perfect sync by ear, per earbud, since each earbud's own Bluetooth latency differs |
| Static device list; a dropped device stalls everything | The device list **updates live** as devices connect/disconnect, and a dropped device is detected and torn down instantly so it can't stutter the others |

## Features

- **Multi-device mirroring** with independent per-device delay and volume.
- **Live device list** — plug in or disconnect a device mid-session and its
  card appears/disappears automatically; newly connected devices can be added
  to the mirror without restarting.
- **Instant disconnect handling** — a dropped device (e.g. a Bluetooth earbud)
  is caught immediately via a WinRT audio-endpoint watcher, with a callback
  self-abort and stream health-check as fallbacks, and is torn down on a
  background thread so it never freezes the UI or stutters the other devices.
- **Drift correction + adaptive safety buffer** to keep devices locked in sync
  and recover gracefully from underruns.
- **Now-playing panel** — track title/artist, album artwork, a live progress
  bar, and media transport controls (play/pause, next, previous) via the
  Windows GlobalSystemMediaTransportControls API.
- **Live audio visualizer** — an FFT-driven equalizer rendered behind the album
  art (blurred artwork backdrop → reactive spectrum bars → sharp thumbnail), with
  the bars automatically tinted to match the current cover's color palette.
- **Consistent output quality** across all devices through WASAPI shared mode.
- Per-device stutter logging to `stutter_logs/` for diagnosing sync issues.

## Requirements

- Windows 10/11 (WASAPI-only, like the original)
- Python 3.10+

```
pip install -r requirements.txt
```

## Run

```
cd src
python app.py
```

1. Check the boxes for the devices you want to mirror to (the list updates
   live as you connect/disconnect devices).
2. Click **Start Sync**.
3. Play any audio (Spotify, YouTube, a game, anything).
4. While it plays, nudge each device's **Delay** slider until it sounds
   in sync with the others. Each earbud has a *different* inherent
   Bluetooth latency, so each will likely need a different value.
5. Use **Volume** to balance loudness differences between devices.
6. Use **Reset Sync** to clear accumulated adaptive delay if a device drifts
   after a track change.

## What to build next

- **System tray + light theme** (`pystray` + `customtkinter`).
- **Auto-delay calibration**: play a short test click, record it back via a
  phone/mic, and auto-measure each device's actual latency instead of tuning
  the sliders by ear. Biggest quality-of-life upgrade left.
- **Auto-resume** a device when it reconnects mid-session (disconnect handling
  is already instant; the missing half is re-adding it automatically once it
  comes back).
- **Persisted per-device settings** (save each device's calibrated
  delay/volume to disk, keyed by device name).
- **Exclusive-mode option** to trade cross-device consistency for a few ms less
  latency on a specific device.
- Offload resampling to a per-device worker to further reduce CPU-driven
  stutter under heavy load.

## Files

```
src/
  ring_buffer.py            # the core: shared, sample-indexed circular buffer + per-device delay/drift logic
  capture.py                # WASAPI loopback capture -> ring buffer; subprocess-based device enumeration
  output.py                 # per-device WASAPI shared-mode output <- ring buffer; disconnect self-detection
  app.py                    # customtkinter UI, live device list, now-playing panel, disconnect watcher
  check_media_metadata.py   # helper for inspecting GlobalSystemMediaTransportControls metadata
stutter_logs/               # per-device stutter logs written at runtime
tests/
```
