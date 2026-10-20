# Golden comparison: every stock read the adapter makes in-process gets the same
# status and body from a real `beet web` process on the same library.
import json, os, struct, subprocess, sys, tempfile, time, wave, urllib.error, urllib.parse, urllib.request
ROOT = os.environ["BWM_ROOT"]; sys.path.insert(0, ROOT); os.chdir(ROOT)
import mutagen.id3, mutagen.wave
def wav(path, artist, album, title, n):
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(struct.pack("<h", n) * 8000)
    mw = mutagen.wave.WAVE(path); mw.add_tags()
    for f in (mutagen.id3.TIT2(encoding=3, text=[title]), mutagen.id3.TPE1(encoding=3, text=[artist]),
              mutagen.id3.TPE2(encoding=3, text=[artist]), mutagen.id3.TALB(encoding=3, text=[album]),
              mutagen.id3.TRCK(encoding=3, text=[str(n)])):
        mw.tags.add(f)
    mw.save()
td = tempfile.mkdtemp(); cfg, music, dl = (os.path.join(td, x) for x in ("config", "music", "downloads"))
for d in (cfg, music, dl): os.makedirs(d)
names = [("Björk", "Homogenic", "Jóga"), ("AC/DC", "Back in Black", "Hells Bells"), ("100% Pure", "Q? & #A", "Track #1")]
srcs = []
for i, (ar, al, ti) in enumerate(names):
    s = os.path.join(dl, f"src{i}"); os.makedirs(s); srcs.append(s)
    wav(os.path.join(s, "01.wav"), ar, al, ti, i + 1); wav(os.path.join(s, "02.wav"), ar, al, ti + " II", i + 2)
open(os.path.join(cfg, "config.yaml"), "w").write(
    f"directory: {music}\nlibrary: {cfg}/musiclibrary.blb\nplugins: [web, webmanager]\n"
    f"web:\n  host: 127.0.0.1\n  port: 18337\n  include_paths: yes\nwebmanager:\n  import_roots: [{dl}]\n  allowed_roots: [{music}, {dl}]\n")
os.environ.update({"BEETSDIR": cfg, "HOME": td, "WEB_MANAGER_DATA_DIR": os.path.join(td, "wm"),
                   "MUSIC_ROOT": music, "DOWNLOADS_ROOT": dl})
from backend import beets_embedded
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError
beets_embedded._separate_beets_address = lambda: ""  # no DNS lookups from a test
a = BeetsAdapter()
for s in srcs: a.run_import(paths=[s], autotag=False, copy=True, move=False)
eng = beets_embedded.engine(); real_call = eng.call; reads = []
def spy(method, path, **kw):
    resp = real_call(method, path, **kw)
    if method == "GET" and not path.startswith("/webmanager") and not kw.get("stream"):
        reads.append((path, {k: v for k, v in (kw.get("params") or {}).items() if v is not None},
                      resp.status_code, resp.get_data()))
    return resp
eng.call = spy
env = {**os.environ, "PYTHONPATH": ROOT}
web = subprocess.Popen(["beet", "web"], env=env, stdout=subprocess.DEVNULL, stderr=open(os.path.join(td, "web.log"), "w"))
for _ in range(100):
    try: urllib.request.urlopen("http://127.0.0.1:18337/stats", timeout=1); break
    except Exception: time.sleep(0.3)
def run(fn):
    try: r = fn(); return ["ok", len(r) if hasattr(r, "__len__") else r]
    except BeetsAdapterError as ex: return ["err", type(ex).__name__, ex.status_code, ex.error_code]
items = a.get_items(); iid = items[0]["id"]; aid = a.get_albums()[0]["id"]
calls = {
 "stats": lambda: a.get_stats(), "artists": lambda: a.get_artists(), "items": lambda: a.get_items(),
 "albums": lambda: a.get_albums(), "item": lambda: a.get_item(iid), "item_missing": lambda: a.get_item(999999),
 "album": lambda: a.get_album(aid), "album_missing": lambda: a.get_album(999999),
 "values": lambda: a.get_unique_field_values("item", "artist"),
 "q_unicode": lambda: a.find_items_by_query("artist:Björk"), "q_slash": lambda: a.find_items_by_query("artist:AC/DC"),
 "q_pct": lambda: a.find_items_by_query("artist:100%"), "q_hash": lambda: a.find_albums_by_query("album:Q? & #A"),
 "q_path": lambda: a.find_items_by_query(f"path:{music}"), "by_album": lambda: a.find_all_items_by_album_id(aid),
 "page": lambda: a.get_items_page(),
}
def over_http(path, params):
    url = "http://127.0.0.1:18337" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url, timeout=10) as r: return r.status, r.read()
    except urllib.error.HTTPError as ex: return ex.code, ex.read()
def body(raw):
    try: return json.loads(raw)
    except ValueError: return raw.decode("utf-8", "replace")[:200]
out = {}
for name, fn in calls.items():
    del reads[:]; result = run(fn)
    diffs = [[p, q, s, h[0]] for p, q, s, raw in reads for h in [over_http(p, q)]
             if (s, body(raw)) != (h[0], body(h[1]))]
    out[name] = ["SAME", result[1]] if reads and not diffs else {"result": result, "reads": len(reads), "diffs": diffs}
emb = a.open_item_file(iid)
st, data = over_http(f"/item/{iid}/file", {})
with urllib.request.urlopen(f"http://127.0.0.1:18337/item/{iid}/file", timeout=10) as h:
    hdr = lambda r: {k: r.headers.get(k) for k in ("Content-Type", "Content-Length", "Content-Disposition")}
    same = emb.read() == data and hdr(emb) == hdr(h) and emb.status == st
    out["file_stream"] = "SAME" if same else {"http": [hdr(h), st], "emb": [hdr(emb), emb.status]}
web.terminate()
print(json.dumps(out, default=str, ensure_ascii=False))
