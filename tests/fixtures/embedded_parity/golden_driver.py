# Golden comparison: same library, HTTP transport (real `beet web` process) vs in-process transport.
import json, os, struct, subprocess, sys, tempfile, time, wave, urllib.request
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
key = "ab" * 32
open(os.path.join(cfg, "config.yaml"), "w").write(
    f"directory: {music}\nlibrary: {cfg}/musiclibrary.blb\npluginpath: [{ROOT}/beetsplug]\nplugins: [web, webmanager]\n"
    f"web:\n  host: 127.0.0.1\n  port: 18337\n  include_paths: yes\nwebmanager:\n  import_roots: [{dl}]\n  allowed_roots: [{music}, {dl}]\n")
env = {**os.environ, "BEETSDIR": cfg, "HOME": td, "BEETS_WEBMANAGER_API_KEY": key, "PYTHONPATH": ROOT}
os.environ.update({"BEETSDIR": cfg, "HOME": td, "BEETS_WEBMANAGER_API_KEY": key, "BEETS_WEB_URL": "http://127.0.0.1:18337",
                   "BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:18337", "WEB_MANAGER_DATA_DIR": os.path.join(td, "wm"),
                   "MUSIC_ROOT": music, "DOWNLOADS_ROOT": dl})
web = subprocess.Popen(["beet", "web"], env=env, stdout=subprocess.DEVNULL, stderr=open(os.path.join(td, "web.log"), "w"))
for _ in range(100):
    try: urllib.request.urlopen("http://127.0.0.1:18337/stats", timeout=1); break
    except Exception: time.sleep(0.3)
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError
a = BeetsAdapter()
os.environ["BEETS_ENGINE"] = "http"
for s in srcs: a.run_import(paths=[s], autotag=False, copy=True, move=False)
def run(fn):
    try: return ["ok", fn()]
    except BeetsAdapterError as ex: return ["err", type(ex).__name__, ex.status_code, ex.error_code]
    except Exception as ex: return ["exc", type(ex).__name__, str(ex)[:200]]
items_http = a.get_items(); iid = items_http[0]["id"]; aid = a.get_albums()[0]["id"]
calls = {
 "stats": lambda: a.get_stats(), "artists": lambda: a.get_artists(), "items": lambda: a.get_items(),
 "albums": lambda: a.get_albums(), "item": lambda: a.get_item(iid), "item_missing": lambda: a.get_item(999999),
 "album": lambda: a.get_album(aid), "album_missing": lambda: a.get_album(999999),
 "fields": lambda: a.get_fields("item", ["title", "path", "artist"]),
 "values": lambda: a.get_unique_field_values("item", "artist"),
 "q_unicode": lambda: a.find_items_by_query("artist:Björk"), "q_slash": lambda: a.find_items_by_query("artist:AC/DC"),
 "q_pct": lambda: a.find_items_by_query("artist:100%"), "q_hash": lambda: a.find_albums_by_query("album:Q? & #A"),
 "q_path": lambda: a.find_items_by_query(f"path:{music}"), "by_album": lambda: a.find_all_items_by_album_id(aid),
 "item_paths": lambda: a.list_item_paths() if hasattr(a, "list_item_paths") else None,
 "page": lambda: a.get_items_page(),
 "plugin_status_keys": lambda: sorted(a.get_plugin_status().keys()),
 "bad_modify": lambda: a.modify({"no_such_field": "x"}, item_ids=[iid]),
 "bad_op": lambda: a.get_operation("does-not-exist"),
 "bad_relocate": lambda: a.relocate_album(999999, "rename"),
}
out = {}
for name, fn in calls.items():
    os.environ["BEETS_ENGINE"] = "http"; h = run(fn)
    os.environ["BEETS_ENGINE"] = "embedded"; e = run(fn)
    out[name] = ("SAME", len(h[1]) if h[0]=="ok" and hasattr(h[1],"__len__") else h[:3]) if h == e else {"http": h, "embedded": e}
f_http = (os.environ.__setitem__("BEETS_ENGINE", "http"), a.open_item_file(iid))[1]
f_emb = (os.environ.__setitem__("BEETS_ENGINE", "embedded"), a.open_item_file(iid))[1]
hdr = lambda r: {k: r.headers.get(k) for k in ("Content-Type", "Content-Length", "Content-Disposition", "Accept-Ranges")}
out["file_stream"] = "SAME" if (f_http.read() == f_emb.read() and hdr(f_http) == hdr(f_emb) and f_http.status == f_emb.status) else {"http": [hdr(f_http), f_http.status], "emb": [hdr(f_emb), f_emb.status]}
web.terminate()
print(json.dumps(out, default=str, ensure_ascii=False))
