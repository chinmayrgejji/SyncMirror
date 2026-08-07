"""Regenerate SyncMirror.ico from the transparent 256px master.

Downscaling a detailed 256px logo straight to 16/24/32px looks blurry, so each
small size is resampled with LANCZOS and given a light unsharp pass. The 256px
frame is kept pristine. Source: SyncMirror_original.ico (transparent 256 frame).
"""
from PIL import Image, ImageFilter

SRC = "SyncMirror_original.ico"
OUT = "SyncMirror.ico"
SIZES = [16, 24, 32, 40, 48, 64, 128, 256]

master = Image.open(SRC)
master.size = (256, 256)
master = master.convert("RGBA")

frames = []
for s in SIZES:
    if s == 256:
        frames.append(master.copy())
        continue
    img = master.resize((s, s), Image.LANCZOS)
    # Counter the softness introduced by shrinking; stronger on the tiniest sizes.
    percent = 160 if s <= 32 else 110
    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=percent, threshold=1))
    frames.append(img)

frames[0].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES],
               append_images=frames[1:])
print(f"Wrote {OUT} with sizes {SIZES}")
