"""Local raw-fingerprint evidence for duplicate grouping (supporting only)."""

import random
import subprocess

import backend.acoustid_service as acs
from backend.matching.duplicate_identity import (
    DIFFERENT_AUDIO, INCONCLUSIVE, SAME_AUDIO, compare_raw_fingerprints,
)
from backend.provider_boundary import ProviderOutcome


def _fp(n=400, seed=1):
    rnd = random.Random(seed)
    return [rnd.getrandbits(32) for _ in range(n)]


def _noisy(fp, seed=9):  # flip ~2 of 32 bits per item, like a lossy re-encode
    rnd = random.Random(seed)
    return [v ^ (1 << rnd.randrange(32)) ^ (1 << rnd.randrange(32)) for v in fp]


def test_identical_fingerprints_are_same_audio():
    fp = _fp()
    verdict, d = compare_raw_fingerprints(fp, list(fp))
    assert verdict == SAME_AUDIO and d["offset"] == 0 and d["bit_error_rate"] == 0


def test_reencode_noise_is_still_same_audio():
    fp = _fp()
    assert compare_raw_fingerprints(fp, _noisy(fp))[0] == SAME_AUDIO


def test_offset_shifted_fingerprints_are_same_audio():
    fp = _fp(500)
    verdict, d = compare_raw_fingerprints(fp[37:], fp)  # e.g. a trimmed intro
    assert verdict == SAME_AUDIO and d["offset"] == -37
    verdict, d = compare_raw_fingerprints(fp, fp[25:])
    assert verdict == SAME_AUDIO and d["offset"] == 25


def test_different_audio_after_alignment():
    # Same 30-item intro, then unrelated audio (e.g. another song or mix).
    intro = _fp(30, seed=5)
    verdict, d = compare_raw_fingerprints(intro + _fp(seed=1), intro + _fp(seed=2))
    assert verdict == DIFFERENT_AUDIO and d["bit_error_rate"] > 0.4


def test_unaligned_unrelated_audio_is_inconclusive_not_different():
    verdict, d = compare_raw_fingerprints(_fp(seed=1), _fp(seed=2))
    assert verdict == INCONCLUSIVE and d["reason"] == "no_alignment"


def test_offset_window_is_symmetric():
    fp = _fp(600)
    v1, d1 = compare_raw_fingerprints(fp[120:], fp)
    v2, d2 = compare_raw_fingerprints(fp, fp[120:])
    assert (v1, d1["offset"]) == (SAME_AUDIO, -120) and (v2, d2["offset"]) == (SAME_AUDIO, 120)


def test_offset_beyond_window_is_inconclusive_not_different():
    fp = _fp(600)
    for a, b in ((fp[130:], fp), (fp, fp[130:])):
        verdict, d = compare_raw_fingerprints(a, b)
        assert verdict == INCONCLUSIVE and d["reason"] == "no_alignment"


def test_short_clip_from_inside_long_track_is_inconclusive():
    fp = _fp(960)
    verdict, d = compare_raw_fingerprints(fp[600:700], fp)
    assert verdict == INCONCLUSIVE and d["reason"] == "no_alignment"


def test_silence_is_inconclusive_not_same_audio():
    for silent in ([0] * 400, [0x0F0F0F0F] * 400, [0, 1] * 200):
        verdict, d = compare_raw_fingerprints(silent, list(silent))
        assert verdict == INCONCLUSIVE and d["reason"] == "low_entropy"


def test_malformed_or_too_short_is_inconclusive():
    assert compare_raw_fingerprints([], _fp())[0] == INCONCLUSIVE
    assert compare_raw_fingerprints(None, _fp())[0] == INCONCLUSIVE
    assert compare_raw_fingerprints(["x"], _fp())[0] == INCONCLUSIVE
    assert compare_raw_fingerprints(_fp(10), _fp(10))[0] == INCONCLUSIVE


def _audio(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"not really audio " + name.encode())
    return str(p)


def test_fpcalc_missing_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(acs, "_RAW_FP_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr(acs.Path, "exists", lambda self: False)
    out = acs.local_audio_match(_audio(tmp_path, "a.flac"), _audio(tmp_path, "b.flac"))
    assert out["verdict"] == INCONCLUSIVE and out["reason"] == "fingerprint_unavailable"


def test_fpcalc_failing_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(acs, "_RAW_FP_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("shutil.which", lambda _: str(tmp_path / "a.flac"))

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 30)
    monkeypatch.setattr(subprocess, "run", boom)
    out = acs.local_audio_match(_audio(tmp_path, "a.flac"), _audio(tmp_path, "b.flac"))
    assert out["verdict"] == INCONCLUSIVE

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 3, "garbage", "err"))
    assert acs.local_audio_match(_audio(tmp_path, "a.flac"), _audio(tmp_path, "b.flac"))["verdict"] == INCONCLUSIVE
    assert not (tmp_path / "cache").exists()  # failures are never cached


def test_raw_fingerprint_is_cached_per_file(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(acs, "_RAW_FP_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("shutil.which", lambda _: str(tmp_path / "a.flac"))
    fp = _fp()
    calls = []

    def run(cmd, **k):
        calls.append(cmd)
        assert "-raw" in cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"duration": 60, "fingerprint": fp}), "")
    monkeypatch.setattr(subprocess, "run", run)
    a, b = _audio(tmp_path, "a.flac"), _audio(tmp_path, "b.flac")
    assert acs.local_audio_match(a, b)["verdict"] == SAME_AUDIO
    assert acs.local_audio_match(a, b)["verdict"] == SAME_AUDIO
    assert len(calls) == 2  # second pair comparison served from cache
    r = acs.raw_fingerprint_outcome(a)
    assert r.outcome == ProviderOutcome.CONFIRMED and r.from_cache


def _fake_fpcalc(monkeypatch, tmp_path, fp):
    import json
    monkeypatch.setattr(acs, "_RAW_FP_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("shutil.which", lambda _: str(tmp_path / "a.flac"))
    calls = []

    def run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"duration": 60, "fingerprint": fp}), "")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _cache_file(tmp_path):
    files = list((tmp_path / "cache").rglob("*.json"))
    assert len(files) == 1
    return files[0]


def test_corrupt_cache_entry_is_recomputed(tmp_path, monkeypatch):
    fp = _fp()
    calls = _fake_fpcalc(monkeypatch, tmp_path, fp)
    a = _audio(tmp_path, "a.flac")
    assert acs.raw_fingerprint_outcome(a).outcome == ProviderOutcome.CONFIRMED
    for corrupt in ('["x", 1]', "[true, false]", '{"fingerprint": [1]}', "[]", "not json", "[1.5]"):
        _cache_file(tmp_path).write_text(corrupt, encoding="utf-8")
        r = acs.raw_fingerprint_outcome(a)
        assert r.outcome == ProviderOutcome.CONFIRMED and not r.from_cache and r.data == fp
    assert len(calls) == 7


def test_cache_invalidated_when_mtime_or_size_changes(tmp_path, monkeypatch):
    import os
    calls = _fake_fpcalc(monkeypatch, tmp_path, _fp())
    a = _audio(tmp_path, "a.flac")
    acs.raw_fingerprint_outcome(a)
    assert acs.raw_fingerprint_outcome(a).from_cache and len(calls) == 1
    st = os.stat(a)
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert not acs.raw_fingerprint_outcome(a).from_cache and len(calls) == 2
    mtime = os.stat(a).st_mtime_ns
    with open(a, "ab") as f:
        f.write(b"more")
    os.utime(a, ns=(mtime, mtime))  # same mtime, different size
    assert not acs.raw_fingerprint_outcome(a).from_cache and len(calls) == 3
    assert acs.raw_fingerprint_outcome(a).from_cache and len(calls) == 3
