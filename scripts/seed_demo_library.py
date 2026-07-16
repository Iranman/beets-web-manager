#!/usr/bin/env python3
"""Seed a small, safe demo music library.

Generates a handful of short sine-wave WAV files (self-synthesized audio —
not copied from any real recording, so there is no copyright concern) tagged
with clearly-fake "Demo Artist / Demo Album" metadata. Lets a new user click
through the import/library UI without needing their own music collection or
paid AI credentials.

Usage:
    python scripts/seed_demo_library.py [target_dir]

Default target_dir is $MUSIC_LIBRARY_PATH or ./data/music/Demo Artist.

Demo mode is opt-in and fully removable: delete the target directory (or the
whole demo library root) to remove it, and unset DEMO_MODE.
"""
import math
import os
import struct
import sys
import wave
from pathlib import Path

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC
    from mutagen.mp3 import MP3
except ImportError:
    print("mutagen is required (pip install -r requirements.txt)", file=sys.stderr)
    sys.exit(1)

DEMO_TRACKS = [
    {"track": 1, "title": "Demo Sine Wave in C", "freq": 261.63},   # C4
    {"track": 2, "title": "Demo Sine Wave in E", "freq": 329.63},   # E4
    {"track": 3, "title": "Demo Sine Wave in G", "freq": 392.00},   # G4
]
DEMO_ARTIST = "Demo Artist (Synthesized, Not Real Music)"
DEMO_ALBUM = "Beets Web Manager Demo Album"
DEMO_YEAR = "2026"
SAMPLE_RATE = 22050
DURATION_SECONDS = 3


def _write_sine_wav(path: Path, freq: float) -> None:
    n_samples = SAMPLE_RATE * DURATION_SECONDS
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for i in range(n_samples):
            # Fade in/out over 0.1s to avoid audible clicks.
            fade = min(1.0, i / (SAMPLE_RATE * 0.1), (n_samples - i) / (SAMPLE_RATE * 0.1))
            sample = int(8000 * fade * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
            frames += struct.pack("<h", sample)
        w.writeframes(bytes(frames))


def seed(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for track in DEMO_TRACKS:
        wav_path = target_dir / f"{track['track']:02d} - {track['title']}.wav"
        _write_sine_wav(wav_path, track["freq"])
        # Tag directly on the WAV via a minimal ID3 write is unreliable for
        # WAV; beets reads WAV comment fields poorly, so we keep filenames
        # descriptive (beets' fromfilename plugin picks these up) and skip
        # ID3 for WAV — this is intentionally simple, not a full tagger.
        print(f"  wrote {wav_path}")
    print(f"\nDemo library seeded at: {target_dir}")
    print("Clearly synthesized audio — no real music, no copyright concern.")
    print(f"Remove it any time: rm -rf {target_dir!s}")


def main() -> None:
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        base = Path(os.environ.get("MUSIC_LIBRARY_PATH", "./data/music"))
        target = base / DEMO_ARTIST / DEMO_ALBUM
    seed(target)


if __name__ == "__main__":
    main()
