"""Synthetic, throwaway library for the Playwright E2E suite.

Everything lives under the temp root handed in by serve.py. Audio is one
second of silence in a tagged WAV: stdlib plus mutagen, so no ffmpeg needed.
The database is written through Beets' own Library API, never sqlite3.
"""
import struct
import wave
from pathlib import Path

import mutagen.id3
import mutagen.wave

# (artist, album, year, mb_releasegroupid, mb_albumid, tracks). Fixed fake MBIDs.
ALBUMS = [
    ("Synthetic Aurora", "Glass Horizons", 2019,
     "11111111-1111-4111-8111-111111111111", "aaaaaaaa-1111-4111-8111-111111111111",
     ["First Light", "Paper Moons", "Long Exposure"]),
    ("Synthetic Aurora", "Static Bloom", 2021,
     "22222222-2222-4222-8222-222222222222", "aaaaaaaa-2222-4222-8222-222222222222",
     ["Slow Bloom", "Radio Garden"]),
    ("The Test Pilots", "Flight Recorder", 2015,
     "33333333-3333-4333-8333-333333333333", "aaaaaaaa-3333-4333-8333-333333333333",
     ["Black Box", "Holding Pattern"]),
    # No MusicBrainz IDs: exercises the missing-identity paths.
    ("Unknown Quantity", "Untagged Sessions", 2001, "", "", ["Take One"]),
]
#: Shown in the review queue; a suggestion that has to be approved by a person.
REVIEW_FOLDER = "Review Artist - Review Album"
REVIEW_RELEASE_ID = "bbbbbbbb-1111-4111-8111-111111111111"
REVIEW_GROUP_ID = "cccccccc-1111-4111-8111-111111111111"
#: A plain folder in the downloads root for the import page.
IMPORT_FOLDER = "Import Artist - Import Album"


def write_wav(path: Path, artist: str, album: str, title: str, track: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 8000)
    tagged = mutagen.wave.WAVE(str(path))
    tagged.add_tags()
    for frame in (mutagen.id3.TIT2(encoding=3, text=[title]), mutagen.id3.TPE1(encoding=3, text=[artist]),
                  mutagen.id3.TPE2(encoding=3, text=[artist]), mutagen.id3.TALB(encoding=3, text=[album]),
                  mutagen.id3.TRCK(encoding=3, text=[str(track)])):
        tagged.tags.add(frame)
    tagged.save()


def build(music: Path, downloads: Path, db_path: Path) -> None:
    from beets.library import Item, Library  # imported after BEETSDIR is set

    lib = Library(str(db_path), directory=str(music))
    try:
        for artist, album, year, rgid, mbid, titles in ALBUMS:
            items = []
            for n, title in enumerate(titles, 1):
                path = music / artist / album / f"{n:02d} {title}.wav"
                write_wav(path, artist, album, title, n)
                item = Item.from_path(str(path))
                item.update({"track": n, "year": year, "mb_albumid": mbid, "mb_releasegroupid": rgid,
                             "mb_trackid": f"{rgid[:-4]}{n:04d}" if rgid else "", "albumartist": artist})
                items.append(item)
            added = lib.add_album(items)
            added.update({"year": year, "mb_albumid": mbid, "mb_releasegroupid": rgid})
            added.store()
    finally:
        lib._close()

    for n, title in enumerate(["Review One", "Review Two"], 1):
        write_wav(downloads / REVIEW_FOLDER / f"{n:02d} {title}.wav", "Review Artist", "Review Album", title, n)
    write_wav(downloads / IMPORT_FOLDER / "01 Import One.wav", "Import Artist", "Import Album", "Import One", 1)
