"""4.17: practice player — tempo/pitch, key detection, instrument keys, looping."""
import subprocess

import numpy as np
import pytest

import mbox_control as mc


def tones(notes, rate=22050, dur=0.5):
    out = []
    for n in notes:
        f = 440 * 2 ** ((n - 69) / 12)
        t = np.arange(int(rate * dur)) / rate
        out.append(sum(np.sin(2 * np.pi * f * k * t) / k for k in (1, 2, 3)) * np.exp(-t * 1.5))
    return np.concatenate(out).astype(np.float32)


G_MAJOR = [67, 71, 74, 79, 72, 76, 79, 84, 74, 78, 81, 86, 67, 71, 74, 79] * 4 + [67, 69, 71, 72, 74, 76, 78, 79] * 3
E_MINOR = [64, 67, 71, 76, 60, 64, 67, 72, 62, 66, 69, 74, 59, 63, 66, 71, 64, 67, 71, 76] * 4 + \
    [64, 66, 67, 69, 71, 72, 74, 76] * 3


@pytest.mark.parametrize("notes,tonic,mode", [(G_MAJOR, 7, "major"), (E_MINOR, 4, "minor")])
def test_detect_key(notes, tonic, mode):
    t, m, conf = mc.detect_key(tones(notes), 22050)
    assert (t, m) == (tonic, mode) and conf > 0.8


@pytest.mark.parametrize("tonic,mode,inst,shift", [
    (7, "major", "D", -5),        # G major tune on a D whistle → D major
    (4, "minor", "D", -5),        # E minor → B minor (the D whistle's relative minor)
    (2, "major", "D", 0),
    (0, "major", "G", -5),        # C → G
    (9, "major", "C", 3),         # A → C
])
def test_shift_to_instrument_key(tonic, mode, inst, shift):
    assert mc.shift_to_key(tonic, mode, inst) == shift


def test_no_filter_when_nothing_changes():
    assert mc.practice_filter(1.0, 0) == ""


def test_fallback_filter_chain(monkeypatch):
    monkeypatch.setattr(mc, "_RUBBERBAND", False)
    f = mc.practice_filter(0.3, 0)
    assert f.count("atempo=0.5") == 1 and f.endswith("atempo=0.60000")      # atempo only goes down to 0.5


@pytest.mark.parametrize("rubberband", [True, False])
@pytest.mark.parametrize("speed,semis,freq,dur", [(1.0, 2, 493.9, 2.0), (0.5, 0, 440.0, 4.0), (0.75, -3, 370.0, 2.67)])
def test_rendered_pitch_and_length(monkeypatch, tmp_path, rubberband, speed, semis, freq, dur):
    monkeypatch.setattr(mc, "_RUBBERBAND", rubberband)
    if rubberband and " rubberband " not in subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                                           capture_output=True, text=True).stdout:
        pytest.skip("this ffmpeg has no rubberband")
    src = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-ac", "2", "-ar", "48000", str(src)], check=True)
    job = mc.PracticeRender(src, 0, None, speed, semis, 48000)
    got = {}
    job.ready.connect(lambda d, e: got.update(d=d))
    job.run()                                         # synchronously
    x = np.frombuffer(got["d"], np.int16).reshape(-1, 2)[:, 0].astype(np.float32)
    assert abs(len(x) / 48000 - dur) < 0.05
    seg = x[len(x) // 4: 3 * len(x) // 4]               # the middle half, away from start/end effects
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 8 * len(seg)))
    f = np.argmax(spec) * 48000 / (8 * len(seg))
    cents = 1200 * np.log2(f / freq)
    assert abs(cents) < 10, f"{f:.1f} Hz is {cents:+.0f} cents off {freq} Hz"   # 10 cents: inaudible


def test_practice_wave_drag_sets_loop(win, spinner):
    w = win.pr_wave
    w.resize(800, 110)
    w.set_data(np.ones(200), np.ones(200), 10.0)
    got = []
    w.loopChosen.connect(lambda a, b: got.append((round(a, 1), round(b, 1))))
    from PySide6 import QtCore, QtGui
    from PySide6.QtCore import QPointF, Qt

    def ev(kind, x):
        return QtGui.QMouseEvent(kind, QPointF(x, 50), QPointF(x, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    w.mousePressEvent(ev(QtCore.QEvent.MouseButtonPress, 8 + 784 * 0.2))
    w.mouseMoveEvent(ev(QtCore.QEvent.MouseMove, 8 + 784 * 0.5))
    w.mouseReleaseEvent(ev(QtCore.QEvent.MouseButtonRelease, 8 + 784 * 0.5))
    assert got == [(2.0, 5.0)]


@pytest.mark.parametrize("src", [82.4, 220.0, 440.0])      # low E on a bass, A3, A4
@pytest.mark.parametrize("speed,semis", [(0.75, 0), (1.25, 0), (0.75, -3), (0.5, 2)])
def test_practice_filter_stays_in_tune(src, speed, semis):
    """Every slow-down/transpose must land within 10 cents (inaudible) of the right note."""
    filt = mc.practice_filter(speed, semis)
    a = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={src}:duration=3:sample_rate=44100",   # like most songs
                        "-af", filt, "-ac", "1", "-ar", "48000", "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(a, np.int16).astype(np.float32)
    assert abs(len(x) / 48000 - 3 / speed) < 0.06
    seg = x[len(x) // 4: 3 * len(x) // 4]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 8 * len(seg)))
    want = src * 2 ** (semis / 12)
    cents = 1200 * np.log2(np.argmax(spec) * 48000 / (8 * len(seg)) / want)
    assert abs(cents) < 10, f"{filt}: {cents:+.0f} cents"
