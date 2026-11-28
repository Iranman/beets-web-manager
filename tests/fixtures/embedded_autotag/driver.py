# Autotag import through Beets inside Web Manager with the real musicbrainz
# plugin, its HTTP served from a recorded MusicBrainz response
# (release_169bf2aa.json: GET /ws/2/release/<id>?inc=... as Beets 2.14 asks
# for it, with the CC BY-NC-SA supplementary keys tags/genres/rating removed).
# argv[1] is the scenario: match | mismatch | provider_down | not_found |
# fp_primary_* | auto_divergent | forced_chroma_noise | auto_chroma_noise |
# forced_chroma_disagree (the *_chroma_* scenarios load the real chroma plugin) |
# partial_human | partial_auto | partial_extra_item (partial-album subset cases).
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
# The *_chroma_* scenarios reproduce the #825 LIVE condition: chroma is actually
# loaded and fingerprints every file at import_task_start, so an in-session
# _matches signal is present at choose_match -- unlike the fp_primary_* / auto_*
# scenarios, which load no chroma (mirroring a run whose chroma lookup returned
# nothing). The chroma network/fpcalc lookup is replaced by a stub that injects
# recordings NOT on the pinned release, exactly as live catalog music does.
CHROMA_SCENARIOS = ("forced_chroma_noise", "auto_chroma_noise", "forced_chroma_disagree")
# The pinned Release's two tracks, cleanly tagged (what the `match` scenario uses).
CLEAN = [("Jóga (Introvert Breaks mix)", 641), ("Jóga (Introvert 4/4 mix)", 534)]
# partial_*: the live "incomplete album" condition. A human-confirmed
# Import-with-ID of a folder holding only a SUBSET of the pinned Release's tracks
# (partial_human: 1 of 2) must import the present track tagged to the Release --
# pre-fix it was rejected by the structural missing-tracks guard before any
# acceptance rung ran (operations._pinned_partial_match), the owner's "a partial
# album can't import" defect. partial_auto is the same subset on the AUTOMATIC
# path (must still refuse). partial_extra_item adds a local file that maps to NO
# Release track (extra_items=1): a wrong-edition/junk-padded folder, refused even
# on the human path (forcing it would orphan/mis-tag that file).
if scenario == "mismatch":
    tracks = [("Something Else", 30), ("Another Song", 45)]
elif scenario in ("fp_primary_divergent", "fp_primary_confirmed", "auto_divergent") + CHROMA_SCENARIOS:
    tracks = DIVERGENT
elif scenario in ("partial_human", "partial_auto"):
    tracks = CLEAN[:1]
elif scenario == "partial_extra_item":
    tracks = CLEAN + [("Totally Unrelated Bonus", 200)]
else:
    tracks = CLEAN
for n, (title, secs) in enumerate(tracks, 1):
    wav(os.path.join(src, f"0{n}.wav"), title, secs, n)
plugin_line = "plugins: [musicbrainz, chroma]" if scenario in CHROMA_SCENARIOS else "plugins: [musicbrainz]"
with open(os.path.join(cfg, "config.yaml"), "w", encoding="utf-8") as f:
    f.write(f"directory: {music}\nlibrary: {cfg}/musiclibrary.blb\n{plugin_line}\n"
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
# Live chroma reproduction: chroma is loaded and its fingerprint_task runs at
# import_task_start. Replace its network/fpcalc lookup with a stub that stores
# recordings which are NOT on the pinned release -- the near-universal live
# result for real catalog music. This populates _matches AFTER Web Manager's
# import_task_created drop (same ordering as live), so choose_match sees a
# non-confirming in-session signal. Pre-fix this blocked the forced pin.
gate_matches = {}  # NB2 (#828): snapshot of chroma's _matches AT the choose_match gate
if scenario in CHROMA_SCENARIOS:
    import beetsplug.chroma as _chroma
    NOISE = (["00000000-0000-4000-8000-000000000001"],  # a recording not on the release
             ["00000000-0000-4000-8000-0000000000a1"])  # a release that is not the pinned one
    _chroma.acoustid_match = lambda log, path, _c=_chroma, _n=NOISE: _c._matches.__setitem__(path, _n)
    # NB2 (#828): prove the chroma signal was actually PRESENT at the gate, not
    # silently empty. Without this, the forced-pin test passes even if _matches
    # were empty at choose_match (a future registration/event-ordering regression
    # where chroma never populates), re-opening the live-vs-CI gap #828 guards.
    # choose_match resolves `_pinned_partial_match` by module-global name at call
    # time, so wrapping the module attribute snapshots _matches at the real gate.
    from beetsplug.webmanager import operations as _ops
    _orig_ppm = _ops._pinned_partial_match
    def _ppm_spy(task, *a, _o=_orig_ppm, _n=NOISE[0][0], **k):
        m = getattr(sys.modules.get("beetsplug.chroma"), "_matches", None)
        if isinstance(m, dict):
            gate_matches["present"] = gate_matches.get("present", False) or bool(m)
            gate_matches["noise"] = gate_matches.get("noise", False) or any(
                hit and _n in (hit[0] or []) for hit in m.values())
        return _o(task, *a, **k)
    _ops._pinned_partial_match = _ppm_spy
# fp_primary_* / auto_divergent: the divergent-title pinned release. Chroma is
# NOT loaded (plugins: [musicbrainz]), mirroring a pinned --search-id run whose
# in-session AcoustID lookup yields nothing, so the threaded per-file evidence
# is the ONLY fingerprint signal. fp_primary_confirmed supplies the release
# recording ids as Web Manager's own evidence (fingerprint-primary accept).
# fp_primary_divergent supplies none on the HUMAN path: it imports via the
# forced-pin rail (the human's pick is the authority; tracklist counts line up).
# auto_divergent is the same folder on the AUTOMATIC path (human_confirmed
# False): it must still be refused -- the force never fires without a human.
human_confirmed = scenario in ("fp_primary_divergent", "fp_primary_confirmed",
                               "forced_chroma_noise", "forced_chroma_disagree",
                               "partial_human", "partial_extra_item")
# One confirmed recording id per file (as production does -- at most one), each
# the recording of the release track that file is length-matched to (641s/534s
# -> track 1/2): 01.wav -> Breaks mix, 02.wav -> 4/4 mix.
REC = ["846b5995-88f2-4d18-ae6d-fdc971615fd0", "06191ccf-303d-4a51-82dd-db04e860576f"]
if scenario == "fp_primary_confirmed":
    fp_evidence = {os.path.join(src, f"0{n}.wav"): [REC[n - 1]] for n in (1, 2)}
elif scenario == "forced_chroma_disagree":
    # Web Manager's OWN reliable cached AcoustID lookup CONFIRMED a recording for
    # 01.wav that is NOT the pinned track it maps to -- a genuine fingerprint
    # disagreement. The human pin must NOT override it: it fails closed to review.
    fp_evidence = {os.path.join(src, "01.wav"): ["99999999-9999-4999-8999-999999999999"]}
else:
    fp_evidence = None
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
if scenario in CHROMA_SCENARIOS:
    # NB2 (#828): what chroma's _matches held at the choose_match gate, so the
    # test can prove the forced-pin decision was made WITH a signal present.
    out["chroma_matches_present"] = gate_matches.get("present", False)
    out["chroma_matches_noise"] = gate_matches.get("noise", False)
print(json.dumps(out, ensure_ascii=False, default=str))
