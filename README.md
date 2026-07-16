# Synced Audio Mirror (prototype)

A from-scratch alternative to Max Paire, aimed directly at the two problems
you ran into: **delay between devices** and **inconsistent sound quality**
across devices.

## How it's different from Max Paire

| Problem in Max Paire | What this prototype does instead |
|---|---|
| Same audio fired at every device with no sync mechanism -> devices drift out of time | Every device reads from a shared, sample-indexed ring buffer at its own **tunable delay offset**, like Snapcast/Sonos-style multi-room sync |
| No drift correction -> devices slip further out of sync over time | Each output cursor is gently nudged (1 sample at a time, inaudible) if it drifts more than ~20ms from its target position |
| Ad hoc per-device resampling in Python/numpy -> different devices sound tonally different | All outputs use **WASAPI shared mode**, so Windows' own audio engine does the resampling/format conversion consistently for every device |
| Fixed, non-adjustable sync | A per-device delay slider (-100ms to +400ms) lets you dial in perfect sync by ear, per earbud, since each earbud's own Bluetooth latency differs |

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

1. Check the boxes for the devices you want to mirror to.
2. Click **Start Sync**.
3. Play any audio (Spotify, YouTube, a game, anything).
4. While it plays, nudge each device's **Delay** slider until it sounds
   in sync with the others. Each earbud has a *different* inherent
   Bluetooth latency, so each will likely need a different value.
5. Use **Volume** to balance loudness differences between devices.

## Known limitations / what to build next

This is a working prototype of the *sync engine*, not a finished app.
Things worth adding as you continue the project:

- **System tray + dark/light theme** (Max Paire had these -- easy to
  port over with `pystray` + `customtkinter`, same as the original).
- **Auto-delay calibration**: instead of manual sliders, play a short
  test click, record it back via a phone/mic, and auto-measure each
  device's actual latency. This is the single biggest quality-of-life
  upgrade you could add.
- **Auto-reconnect** when a Bluetooth device drops and reconnects
  mid-session.
- **Persisted per-device settings** (save each earbud's calibrated
  delay/volume to disk, keyed by device name, so you don't have to
  redo it every session).
- **Exclusive mode option**: WASAPI shared mode (used here) is what
  keeps output quality consistent across devices, but exclusive mode
  can shave off a few more ms of latency for advanced users who want
  to trade consistency for lower delay on one specific device.

## Files

```
src/
  ring_buffer.py   # the core: shared, sample-indexed circular buffer + per-device delay/drift logic
  capture.py       # WASAPI loopback capture -> ring buffer
  output.py         # per-device WASAPI shared-mode output <- ring buffer
  app.py            # Tkinter UI wiring it all together
```

Note: this was written and syntax-checked in a Linux sandbox (no Windows/
WASAPI/audio hardware available there), so it hasn't been runtime-tested
yet. Try it on your Windows machine and let me know what breaks --
happy to iterate from there.
