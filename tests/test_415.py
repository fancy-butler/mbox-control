"""4.15: EQ profiles (AutoEQ), per-speaker-set EQ, tone, voice chain."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

import mbox_control as mc

FIX = Path(__file__).parent / "fixtures"


def hd280():
    return mc.parse_parametric_eq((FIX / "HD280Pro-ParametricEQ.txt").read_text(), "HD 280 Pro")


def test_parse_autoeq_file():
    p = hd280()
    assert p["preamp"] == -5.8 and len(p["bands"]) == 10
    assert p["bands"][0] == {"type": "LSC", "f": 105.0, "g": -4.2, "q": 0.7}


def test_parse_rejects_junk():
    with pytest.raises(ValueError):
        mc.parse_parametric_eq("hello", "x")


def test_bands_land_in_matching_slots():
    v = mc.eq_params(hd280())
    assert v["ls1"] == (105.0, 0.7, -4.2) and v["hs1"] == (10000.0, 0.7, 3.0)
    assert v["pre"][2] == -5.8
    assert v["pk12"][2] == 0.0                         # unused slots are flat
    assert set(v) == {s for s, _ in mc.EQ_SLOTS}


def test_every_pw_cli_chunk_is_short_enough():
    chunks = mc.eq_pw_params(hd280())
    assert all(len(c) < 480 for c in chunks)            # pw-cli silently drops longer lists
    assert sum(c.count(":Gain") for c in chunks) == len(mc.EQ_SLOTS)


def test_autoeq_search_prefers_oratory():
    idx = """- [Sennheiser HD 280 Pro](./Filk/over-ear/Sennheiser%20HD%20280%20Pro) by Filk
- [Sennheiser HD 280 Pro](./oratory1990/over-ear/Sennheiser%20HD%20280%20Pro) by oratory1990
- [Sennheiser HD 600](./oratory1990/over-ear/Sennheiser%20HD%20600) by oratory1990"""
    rows = mc.autoeq_search(idx, "hd 280")
    assert len(rows) == 2 and rows[0][1] == "oratory1990"


def test_voice_chain_conf_adapts_to_plugins(monkeypatch):
    monkeypatch.setattr(mc, "find_ladspa", lambda *n: None)
    conf, rnn, sc4 = mc.voice_chain_conf("mbox_src", "AUX0")
    assert not rnn and not sc4 and "bq_highpass" in conf and "ladspa" not in conf
    monkeypatch.setattr(mc, "find_ladspa", lambda *n: "/usr/lib/ladspa/" + n[0])
    conf, rnn, sc4 = mc.voice_chain_conf("mbox_src", "AUX0")
    assert rnn and sc4 and "noise_suppressor_mono" in conf and "sc4m" in conf


def test_eq_follows_the_speaker_set_with_tone(win, monkeypatch):
    sent = []
    monkeypatch.setattr(mc, "set_eq_live", lambda node, prof: sent.append(prof) or True)
    monkeypatch.delenv("MBOX_AMIXER", raising=False)
    win.app_state["eq_on"] = True
    win.save_eq_profile(hd280())
    win.assign_eq("B", "HD 280 Pro")
    win.app_state["eq_tone"] = {"bass": 4, "treble": 0}
    monkeypatch.setattr(win, "eq_target", lambda: "B")
    win.apply_eq()
    p = sent[-1]
    assert p["name"] == "HD 280 Pro" and any(b["f"] == 105.0 and b["g"] == 4 for b in p["bands"])
    extra = mc.eq_peak_db(p["bands"]) - mc.eq_peak_db(hd280()["bands"])
    assert -9.8 <= p["preamp"] <= -5.8 and p["preamp"] == pytest.approx(-5.8 - extra)
    monkeypatch.setattr(win, "eq_target", lambda: "A")
    win.apply_eq()
    assert sent[-1]["name"] == "Off (flat)"


@pytest.mark.skipif(not (shutil.which("pw-cli") and os.environ.get("MBOX_TEST_PIPEWIRE")),
                    reason="needs a running PipeWire (set MBOX_TEST_PIPEWIRE=1)")
def test_eq_really_changes_the_sound():
    """Plays a 1 kHz tone through the real EQ chain (already loaded as mbox_main → fake_mbox)."""
    tone = Path("/tmp/mbox-eq-test.wav")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                    "-ac", "2", str(tone)], check=True)

    def level():
        raw = Path("/tmp/mbox-eq-test.raw")
        rec = subprocess.Popen(["pw-record", "--target", "fake_mbox", "-P", "stream.capture.sink=true",
                                "--channels", "8", "--channel-map", "AUX0,AUX1,AUX2,AUX3,AUX4,AUX5,AUX6,AUX7",
                                "-P", "stream.dont-remix=true", "--format", "f32", str(raw)],
                               stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        subprocess.run(["pw-play", "--target", "mbox_main", str(tone)])
        time.sleep(0.3)
        rec.terminate()
        rec.wait()
        x = np.fromfile(raw, dtype=np.float32)
        x = x[:len(x) // 8 * 8].reshape(-1, 8)[:, 6]
        x = x[len(x) // 4: -len(x) // 4]
        return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)
    assert mc.set_eq_live("mbox_main", mc.EQ_FLAT)
    time.sleep(0.3)
    flat = level()
    assert mc.set_eq_live("mbox_main", {"name": "t", "preamp": 0, "bands": [{"type": "PK", "f": 1000, "q": 1, "g": 12}]})
    time.sleep(0.3)
    assert level() - flat == pytest.approx(12, abs=0.5)
    mc.set_eq_live("mbox_main", mc.EQ_FLAT)


def test_autoeq_url_includes_the_model_folder():
    url = mc.autoeq_url("oratory1990/over-ear/Sennheiser%20HD%20280%20Pro", "Sennheiser HD 280 Pro")
    assert url.endswith("/oratory1990/over-ear/Sennheiser%20HD%20280%20Pro/Sennheiser%20HD%20280%20Pro%20ParametricEQ.txt")


@pytest.mark.skipif(not (shutil.which("pw-cli") and os.environ.get("MBOX_TEST_PIPEWIRE")),
                    reason="needs a running PipeWire (set MBOX_TEST_PIPEWIRE=1)")
def test_subsonic_filter_cuts_thumps_but_keeps_the_low_e():
    """15 Hz (a pluck thump) is cut hard; 41 Hz (a bass guitar's low E) barely; off = untouched."""
    def level(freq):
        tone = Path(f"/tmp/mbox-sub-{freq}.wav")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency={freq}:duration=3", "-ac", "2", str(tone)], check=True)
        raw = Path("/tmp/mbox-sub-test.raw")
        rec = subprocess.Popen(["pw-record", "--target", "fake_mbox", "-P", "stream.capture.sink=true",
                                "--channels", "8", "--channel-map", "AUX0,AUX1,AUX2,AUX3,AUX4,AUX5,AUX6,AUX7",
                                "-P", "stream.dont-remix=true", "--format", "f32", str(raw)],
                               stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        subprocess.run(["pw-play", "--target", "mbox_main", str(tone)])
        time.sleep(0.3)
        rec.terminate()
        rec.wait()
        x = np.fromfile(raw, dtype=np.float32)
        x = x[:len(x) // 8 * 8].reshape(-1, 8)[:, 6]
        x = x[len(x) // 4: -len(x) // 4]
        return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)
    off = dict(mc.EQ_FLAT, subsonic=False)
    on = dict(mc.EQ_FLAT, subsonic=True)
    assert mc.set_eq_live("mbox_main", off)
    time.sleep(0.3)
    t15, t41 = level(15), level(41)
    assert mc.set_eq_live("mbox_main", on)
    time.sleep(0.3)
    s15, s41 = level(15), level(41)
    mc.set_eq_live("mbox_main", off)
    assert t15 - s15 > 20                     # the thump (one octave below 30 Hz): about −24 dB
    assert t41 - s41 < 4                      # the low E: within a few dB
