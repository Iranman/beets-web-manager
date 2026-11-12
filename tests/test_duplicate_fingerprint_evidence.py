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


def test_different_audio():
    verdict, d = compare_raw_fingerprints(_fp(seed=1), _fp(seed=2))
    assert verdict == DIFFERENT_AUDIO and d["bit_error_rate"] > 0.4


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
