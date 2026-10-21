import json, os, struct, subprocess, sys, tempfile, threading, time, wave, traceback
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

td = tempfile.mkdtemp()
cfg, music, dl = (os.path.join(td, x) for x in ("config", "music", "downloads"))
for d in (cfg, music, dl): os.makedirs(d)
srcs = []
for a in range(4):
    s = os.path.join(dl, f"Artist{a} - Album{a}"); os.makedirs(s); srcs.append(s)
    for n in (1, 2, 3): wav(os.path.join(s, f"0{n}.wav"), f"Artist{a}", f"Album{a}", f"T{n}", n + a * 10)
db = os.path.join(cfg, "musiclibrary.blb")
open(os.path.join(cfg, "config.yaml"), "w").write(
    f"directory: {music}\nlibrary: {db}\nplugins: []\nwebmanager:\n  import_roots: [{dl}]\n  allowed_roots: [{music}, {dl}]\n")
os.environ.update({"BEETSDIR": cfg, "HOME": td,
                   "WEB_MANAGER_DATA_DIR": os.path.join(td, "wm"), "MUSIC_ROOT": music, "DOWNLOADS_ROOT": dl,
                   "BEETS_TRANSACTION_DIR": os.path.join(td, "wm", "tx")})
res = {}
from backend.beets_adapter import BeetsAdapter
from backend import beets_embedded
a = BeetsAdapter()

a.run_import(paths=[srcs[0]], autotag=False, copy=True, move=False)

# 1) Concurrency: readers + writers + imports (sync and async) on many threads.
errors = []; stop = threading.Event(); counts = {"reads": 0, "writes": 0}
lock = threading.Lock()
def reader():
    while not stop.is_set():
        try:
            a.get_items(); a.get_albums(); a.get_stats(); a.get_fields("item", ["title", "path"])
            with lock: counts["reads"] += 1
        except Exception as ex:
            errors.append("read: " + repr(ex)); return
def writer(k):
    i = 0
    while not stop.is_set():
        try:
            ids = [it["id"] for it in a.get_items()]
            a.modify({"comments": f"w{k}-{i}"}, item_ids=ids[:2], write=True); i += 1
            with lock: counts["writes"] += 1
        except Exception as ex:
            errors.append("write: " + repr(ex)); return
def importer(s, asyn):
    try:
        r = a.run_import(paths=[s], autotag=False, copy=True, move=False, is_async=asyn)
        if asyn and isinstance(r, dict) and r.get("operation_id"):
            for _ in range(600):
                op = a.get_operation(r["operation_id"])
                if op.get("status") not in ("running", "pending", "queued"): break
                time.sleep(0.1)
            res.setdefault("async_ops", []).append(op.get("status"))
    except Exception as ex:
        errors.append("import: " + repr(ex) + traceback.format_exc()[-800:])
ts = [threading.Thread(target=reader) for _ in range(12)] + [threading.Thread(target=writer, args=(k,)) for k in range(3)]
for t in ts: t.start()
imps = [threading.Thread(target=importer, args=(srcs[1], False)), threading.Thread(target=importer, args=(srcs[2], True)),
        threading.Thread(target=importer, args=(srcs[3], False))]
for t in imps: t.start()
for t in imps: t.join()
time.sleep(3); stop.set()
for t in ts: t.join()
res["concurrency_errors"] = errors[:5]; res["counts"] = counts
res["albums_after"] = sorted(x["album"] for x in a.get_albums())
res["items_after"] = len(a.get_items())

# 2) Nested inside an outer Flask request context (as a Web Manager route would).
from flask import Flask, g
outer = Flask("outer")
@outer.get("/x")
def x():
    g.marker = "outer"
    items = a.get_items()
    a.modify({"comments": "nested"}, item_ids=[items[0]["id"]], write=False)
    fh = a.open_item_file(items[0]["id"]); n = len(fh.read()); fh.close()
    return {"marker": getattr(g, "marker", None), "n_items": len(items), "bytes": n, "has_lib_on_outer_g": hasattr(g, "lib")}
res["nested"] = outer.test_client().get("/x").get_json()

# 3) Lazy start: importing the backend modules does not load Beets (it starts on first use).
code = ("import sys; from backend import beets_adapter, config_service, beets_embedded;"
        "print(json.dumps({'beets_loaded': any(m=='beets' or m.startswith('beets.') for m in sys.modules)}))")
env = dict(os.environ)
p = subprocess.run([sys.executable, "-c", "import json;" + code], env=env, capture_output=True, text=True, cwd=ROOT)
res["lazy_import"] = p.stdout.strip() or p.stderr[-500:]

# 4) Rollback compatibility: tables in the embedded-written DB vs a DB created by the `beet` CLI.
import sqlite3
def schema(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    s = sorted(r[0] for r in c.execute("select name from sqlite_master where type in ('table','index')")); c.close(); return s
fresh_cfg = os.path.join(td, "fresh"); os.makedirs(fresh_cfg)
open(os.path.join(fresh_cfg, "config.yaml"), "w").write(f"directory: {music}\nlibrary: {fresh_cfg}/lib.blb\n")
subprocess.run(["beet", "stats"], env={**env, "BEETSDIR": fresh_cfg}, capture_output=True)
res["schema_equal"] = schema(db) == schema(os.path.join(fresh_cfg, "lib.blb"))
res["schema_diff"] = sorted(set(schema(db)) ^ set(schema(os.path.join(fresh_cfg, "lib.blb"))))
p = subprocess.run(["beet", "stats"], env={**env, "BEETSDIR": cfg}, capture_output=True, text=True)
res["beet_cli_on_embedded_db"] = (p.returncode, p.stdout.strip().splitlines()[:3], p.stderr[-300:])
print(json.dumps(res, default=str))
