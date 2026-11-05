"""4.16: capture-what-I-played, multitrack, metronome, level check."""
import os
import shutil
import subprocess
import time

import numpy as np
import pytest

import mbox_control as mc

PW = pytest.mark.skipif(not (shutil.which("pw-cli") and os.environ.get("MBOX_TEST_PIPEWIRE")),
                        reason="needs a running PipeWire with fake_mbox / fake_mbox_in (MBOX_TEST_PIPEWIRE=1)")


@pytest.mark.parametrize("peak,clips,kind", [(-0.2, 3, "too_hot"), (-4, 0, "hot"), (-12, 0, "good"),
                                             (-24, 0, "low"), (-50, 0, "silent")])
def test_level_advice(peak, clips, kind):
    assert mc.level_advice(peak, clips)[0] == kind


def test_capture_command_keeps_exact_channels():
    cmd = mc.capture_cmd("mbox_src", ["AUX0", "AUX1"], 48000)
    assert cmd[cmd.index("--channel-map") + 1] == "AUX0,AUX1" and "stream.dont-remix=true" in cmd


def test_retro_buffer_keeps_only_the_last_seconds():
    rb = mc.RetroBuffer("x", ["AUX0"], rate=1000, seconds=2)
    for i in range(50):                                   # 5 s of fake audio in 0.1 s chunks
        chunk = bytes([i]) * 200
        rb.chunks.append(chunk)
        rb.size += len(chunk)
        while rb.size - len(rb.chunks[0]) >= rb.limit:
            rb.size -= len(rb.chunks.popleft())
    assert 2.0 <= rb.held() <= 2.1
    snap = rb.snapshot(1)
    assert len(snap) == 2000 and snap[-1] == 49


def test_input_choice_maps_to_capture_channel(win):
    src_calls = []
    win.set_stream = lambda slot, item: src_calls.append((slot, item))
    import mbox_control
    old = mbox_control.mbox_pw_node
    mbox_control.mbox_pw_node = lambda kind: "mbox_src"
    try:
        src, auxes, name = win.capture_for("mono:Analog-A-3")
    finally:
        mbox_control.mbox_pw_node = old
    assert (src, auxes) == ("mbox_src", ["AUX2"]) and src_calls == [(2, "Analog-A-3")]


def test_tap_tempo(win):
    now = time.time()
    win.taps = [now - 1.5, now - 1.0, now - 0.5]      # three taps half a second apart, then the 4th now
    win.tap_tempo()
    assert 118 <= win.met_bpm.value() <= 122


def test_save_retro_does_nothing_when_off(win, tmp_path):
    win.retro = None
    before = set(win.takes_dir().glob("*")) if win.takes_dir().exists() else set()
    win.save_retro(60)
    after = set(win.takes_dir().glob("*")) if win.takes_dir().exists() else set()
    assert before == after


def _feed(path):
    p = subprocess.Popen(["pw-play", "-P", "node.autoconnect=false", "-P", "node.name=feeder", "--channel-map",
                          "AUX0,AUX1", "-P", "stream.dont-remix=true", str(path)])
    time.sleep(0.3)
    for a in ("AUX0", "AUX1"):
        subprocess.run(["pw-link", f"feeder:output_{a}", f"fake_mbox_in:input_{a}"])
    return p


def _freq(path):
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "48000",
                        "-"], capture_output=True)
    x = np.frombuffer(r.stdout, np.float32)[:48000]
    return np.argmax(np.abs(np.fft.rfft(x * np.hanning(len(x))))) * 48000 / len(x)


@pytest.fixture
def two_tones(tmp_path):
    p = tmp_path / "two.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=880:duration=3", "-filter_complex", "[0][1]amerge=inputs=2",
                    "-ar", "48000", str(p)], check=True)
    return p


@PW
def test_retro_really_captures(two_tones, tmp_path):
    rb = mc.RetroBuffer("fake_mbox_in", ["AUX0"], seconds=2)
    rb.start()
    time.sleep(0.5)
    _feed(two_tones).wait()
    time.sleep(0.3)
    data = rb.snapshot(1.5)
    rb.stop()
    out = tmp_path / "caught.flac"
    mc.save_raw(data, out, 48000, 1, trim=False)
    assert abs(_freq(out) - 440) < 3


@PW
def test_multitrack_really_separates(two_tones, tmp_path):
    wav = tmp_path / "all.wav"
    rec = subprocess.Popen(mc.capture_cmd("fake_mbox_in", ["AUX0", "AUX1"], 48000, out=str(wav), fmt="s24"))
    time.sleep(0.5)
    _feed(two_tones).wait()
    time.sleep(0.3)
    rec.terminate()
    rec.wait()
    a, b = mc.split_multitrack(wav, ["Flute", "Guitar"], tmp_path / "s")
    assert a.name == "01 Flute.flac" and abs(_freq(a) - 440) < 3 and abs(_freq(b) - 880) < 3


@PW
def test_metronome_keeps_time(tmp_path):
    raw = tmp_path / "m.raw"
    rec = subprocess.Popen(["pw-record", "--target", "fake_mbox", "-P", "stream.capture.sink=true", "--channels", "1",
                            "--format", "f32", str(raw)])
    time.sleep(0.4)
    m = mc.Metronome("fake_mbox", bpm=120, beats=4)
    m.start()
    time.sleep(2.6)
    m.stop()
    time.sleep(0.3)
    rec.terminate()
    rec.wait()
    env = np.abs(np.fromfile(raw, np.float32))
    peaks, i, thr = [], 0, env.max() * 0.3
    while i < len(env):
        if env[i] > thr:
            peaks.append(i / 48000)
            i += 4800
        else:
            i += 1
    gaps = np.diff(peaks)
    assert len(gaps) >= 3 and np.allclose(gaps, 0.5, atol=0.01)
