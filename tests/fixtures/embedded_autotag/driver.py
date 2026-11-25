# Autotag import through Beets inside Web Manager with the real musicbrainz
# plugin, its HTTP served from a recorded MusicBrainz response
# (release_169bf2aa.json: GET /ws/2/release/<id>?inc=... as Beets 2.14 asks
# for it, with the CC BY-NC-SA supplementary keys tags/genres/rating removed).
# argv[1] is the scenario: match | mismatch | provider_down | not_found.
import json, os, socket, struct, sys, tempfile, wave
ROOT = os.environ["BWM_ROOT"]; sys.path.insert(0, ROOT); os.chdir(ROOT)
import mutagen.id3, mutagen.wave
import requests
import requests.adapters

scenario = sys.argv[1]
REL = "169bf2aa-1b2a-4506-b872-3aebfa52a5dc"
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "embedded_autotag", "release_169bf2aa.json")
requests_seen = []


def send(self, request, **kwargs):
    requests_seen.append(request.url.split("?")[0])
    if scenario == "provider_down":
        raise requests.exceptions.ConnectionError("recorded: MusicBrainz unreachable")
    resp = requests.Response()
    resp.request, resp.url = request, request.url
    resp.headers["Content-Type"] = "application/json"
    if request.url.startswith(f"https://musicbrainz.org/ws/2/release/{REL}?") and scenario != "not_found":
        resp.status_code = 200
        with open(FIXTURE, "rb") as f:
            resp._content = f.read()
    else:
        resp.status_code = 404
        resp._content = b'{"error": "Not Found"}'
    return resp


requests.adapters.HTTPAdapter.send = send


def no_network(*a, **k):
    raise AssertionError("opened a real socket")


socket.socket.connect = socket.socket.connect_ex = no_network


def wav(path, title, seconds, n):
    # 1 kHz mono keeps a 10-minute track small; Beets reads the length.
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(1000); w.writeframes(b"\0\0" * 1000 * seconds)
    mw = mutagen.wave.WAVE(path); mw.add_tags()
    for f in (mutagen.id3.TIT2(encoding=3, text=[title]), mutagen.id3.TPE1(encoding=3, text=["Björk"]),
              mutagen.id3.TPE2(encoding=3, text=["Björk"]), mutagen.id3.TALB(encoding=3, text=["Jóga"]),
              mutagen.id3.TRCK(encoding=3, text=[str(n)])):
        mw.tags.add(f)
    mw.save()


td = tempfile.mkdtemp()
cfg, music, dl = (os.path.join(td, x) for x in ("config", "music", "downloads"))
for d in (cfg, music, dl):
    os.makedirs(d)
src = os.path.join(dl, "Björk - Jóga")
os.makedirs(src)
# The fp_primary_* scenarios keep the real artist/album but give the files
# divergent (classical-style) titles, so Beets' album text distance exceeds
# its strong threshold exactly as the live John Williams import did -- the
# fingerprint-primary override is the only thing that can accept them.
DIVERGENT = [("Violin Concerto no. 1 I. Prologue", 641),
             ("Serenade after Plato II. Aristophanes", 534)]
if scenario == "mismatch":
    tracks = [("Something Else", 30), ("Another Song", 45)]
elif scenario in ("fp_primary_divergent", "fp_primary_confirmed", "auto_divergent"):
    tracks = DIVERGENT
else:
    tracks = [("Jóga (Introvert Breaks mix)", 641), ("Jóga (Introvert 4/4 mix)", 534)]
for n, (title, secs) in enumerate(tracks, 1):
    wav(os.path.join(src, f"0{n}.wav"), title, secs, n)
with open(os.path.join(cfg, "config.yaml"), "w", encoding="utf-8") as f:
    f.write(f"directory: {music}\nlibrary: {cfg}/musiclibrary.blb\nplugins: [musicbrainz]\n"
            f"musicbrainz:\n  ratelimit: 100\n"
            f"webmanager:\n  import_roots: [{dl}]\n  allowed_roots: [{music}, {dl}]\n")
os.environ.update({"BEETSDIR": cfg, "HOME": td, "WEB_MANAGER_DATA_DIR": os.path.join(td, "wm"),
                   "MUSIC_ROOT": music, "DOWNLOADS_ROOT": dl})

from backend import beets_embedded
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError
beets_embedded._separate_beets_address = lambda: ""
beets_embedded._beet_commands = lambda: []
a = BeetsAdapter()
out = {"status_ok": beets_embedded.status()["ok"]}
# fp_primary_* / auto_divergent: the divergent-title pinned release. Chroma is
# NOT loaded (plugins: [musicbrainz]), mirroring a pinned --search-id run whose
# in-session AcoustID lookup yields nothing, so the threaded per-file evidence
# is the ONLY fingerprint signal. fp_primary_confirmed supplies the release
# recording ids as Web Manager's own evidence (fingerprint-primary accept).
# fp_primary_divergent supplies none on the HUMAN path: it imports via the
# forced-pin rail (the human's pick is the authority; tracklist counts line up).
# auto_divergent is the same folder on the AUTOMATIC path (human_confirmed
# False): it must still be refused -- the force never fires without a human.
human_confirmed = scenario in ("fp_primary_divergent", "fp_primary_confirmed")
# One confirmed recording id per file (as production does -- at most one), each
# the recording of the release track that file is length-matched to (641s/534s
# -> track 1/2): 01.wav -> Breaks mix, 02.wav -> 4/4 mix.
REC = ["846b5995-88f2-4d18-ae6d-fdc971615fd0", "06191ccf-303d-4a51-82dd-db04e860576f"]
fp_evidence = ({os.path.join(src, f"0{n}.wav"): [REC[n - 1]] for n in (1, 2)}
               if scenario == "fp_primary_confirmed" else None)
try:
    res = a.run_import(paths=[src], autotag=True, search_ids=[REL], quiet_fallback="skip",
                       copy=True, move=False, write=False,
                       human_confirmed=human_confirmed, fingerprint_confirmed=fp_evidence)
    out["import"] = {k: res.get(k) for k in ("ok", "skipped", "imported_album_ids", "no_strong_match",
                                             "confirmed_fingerprint_primary", "forced_pin")}
except BeetsAdapterError as ex:
    out["import_error"] = [type(ex).__name__, ex.status_code, ex.error_code, str(ex)]
albums = a.get_albums()
out["albums"] = [{k: al.get(k) for k in ("album", "albumartist", "mb_albumid", "mb_releasegroupid")} for al in albums]
out["items"] = sorted(i["title"] for i in a.get_items())
out["source_left"] = sorted(os.listdir(src)) if os.path.isdir(src) else None
out["status_ok_after"] = beets_embedded.status()["ok"]
out["stats_after"] = a.get_stats()
out["requests"] = sorted(set(requests_seen))
print(json.dumps(out, ensure_ascii=False, default=str))
