"""Logic that needs no window: parsing, maths, file naming, QR codes, the fast meter reader."""
import struct

import numpy as np
import pytest

import mbox_control as mc


def test_parse_real_dump():
    ctrls = mc.parse_amixer(open(mc.os.environ["FAKE_DUMP"]).read())
    names = {c.name for c in ctrls.values()}
    for needed in ("output-source", "mixer-source", "mixer-source-gain", "metering", "spkr-button"):
        assert needed in names
    src = next(c for c in ctrls.values() if c.name == "output-source")
    assert src.type == "ENUMERATED" and len(src.values) == 16 and src.items


def test_version_ordering():
    assert mc.version_tuple("4.10") > mc.version_tuple("4.9")
    assert mc.version_tuple("4.12.1") > mc.version_tuple("4.12")


def test_pan_law_and_gain():
    l, r = mc.from_pan(mc.UNITY, 0)
    assert l == r and abs(l - mc.UNITY * 0.7071) < 5          # −3 dB centre
    assert mc.gain_db(mc.UNITY) == 0


def test_file_names_and_layout(tmp_path):
    d = mc.album_dir(tmp_path, "Shuli/Gad", "Songs: Vol 1", "letter")
    assert d.parent.parent.name == "S" and "/" not in d.name
    assert mc.track_filename(3, "Osseh Shalom", "letter") == "03 Osseh Shalom.flac"
    assert mc.track_filename(3, "Osseh Shalom", "artist") == "03 - Osseh Shalom.flac"


def test_tracklist_text_sides():
    rows = [(0, 125, "One", ""), (125, 250, "Two", "שתיים")]
    text = mc.tracklist_text(rows, 1)
    assert text.splitlines()[0].startswith("A1") and "2:05" in text


def test_deepest_dips_finds_the_gaps():
    env = np.ones(2000) * 0.5
    env[600:620] = 0.001
    env[1400:1420] = 0.001
    cuts = mc.deepest_dips(env, 0.05, 2, min_gap=5)
    assert len(cuts) == 2
    assert abs(cuts[0] - 30.5) < 1.5 and abs(cuts[1] - 70.5) < 1.5


@pytest.mark.parametrize("offset", [25, -30, 0, 45])
def test_tuning_offset(offset):
    rate, rng = 11025, np.random.default_rng(1)
    parts = []
    for _ in range(60):
        f = 440 * 2 ** ((rng.integers(48, 80) - 69) / 12) * 2 ** (offset / 1200)
        t = np.arange(int(rate * 0.6)) / rate
        parts.append(sum((0.6 / h) * np.sin(2 * np.pi * f * h * t) for h in range(1, 5)) * np.exp(-t * 2))
    x = np.concatenate(parts).astype(np.float32)
    cents, conf = mc.tuning_offset(x, rate)
    assert abs(cents - offset) < 2 and conf > 0.9


def test_speed_filter(monkeypatch):
    monkeypatch.setattr(mc, "audio_rate", lambda p: 48000)
    assert mc.speed_filter("x.flac", 0) == ""
    assert mc.speed_filter("x.flac", 2.0) == "asetrate=48960,aresample=48000"


def test_qr_code_shape():
    m = mc.qr_matrix("http://192.168.200.200:8765/?k=" + "A" * 12)
    assert len(m) in (21, 25, 29, 33) and all(len(r) == len(m) for r in m)
    for y, x in ((0, 0), (0, len(m) - 7), (len(m) - 7, 0)):     # three finder squares
        assert m[y][x] and m[y + 6][x + 6] and not m[y + 1][x + 1]


def test_qr_code_decodes():
    cv2 = pytest.importorskip("cv2")
    url = "http://192.168.2.38:8765/?k=Zx9-QqPw3_ab"
    m = mc.qr_matrix(url)
    s, q = 8, 4
    img = np.full(((len(m) + 2 * q) * s,) * 2, 255, np.uint8)
    for y, row in enumerate(m):
        for x, v in enumerate(row):
            if v:
                img[(y + q) * s:(y + q + 1) * s, (x + q) * s:(x + q + 1) * s] = 0
    assert cv2.QRCodeDetector().detectAndDecode(img)[0] == url


def test_install_commands_per_distro(monkeypatch):
    for pm, pkg in (("pacman", "chromaprint"), ("apt-get", "libchromaprint-tools"), ("dnf", "chromaprint-tools")):
        monkeypatch.setattr(mc, "package_manager", lambda pm=pm: pm)
        assert mc.install_argv("chromaprint")[-1] == pkg
        assert mc.install_hint("chromaprint").startswith("sudo ")


def test_direct_reader_layout(monkeypatch):
    """Byte layout of struct snd_ctl_elem_value on 64-bit Linux (checked against <sound/asound.h>)."""
    kernel = {30: ("output-source-meter", "INTEGER", [100, 4095] + [0] * 14),
              27: ("stream-source", "ENUMERATED", [19, 20, 21, 22, 23, 24, 27, 28]),
              49: ("metering", "BOOLEAN", [1])}

    def ioctl(fd, req, buf):
        assert req == 0xC4C85512 and len(buf) == 1224
        name, kind, vals = kernel[struct.unpack_from("I", buf, 0)[0]]
        buf[16:60] = name.encode().ljust(44, b"\0")
        struct.pack_into(f"{len(vals)}{'I' if kind == 'ENUMERATED' else 'q'}", buf, 72, *vals)
    monkeypatch.setattr("fcntl.ioctl", ioctl)
    r = mc.DirectReader.__new__(mc.DirectReader)
    r.fd = -1
    assert r.read(30, "INTEGER", 16) == ("output-source-meter", [100, 4095] + [0] * 14)
    assert r.read(27, "ENUMERATED", 8)[1] == [19, 20, 21, 22, 23, 24, 27, 28]
    assert r.read(49, "BOOLEAN", 1)[1] == [True]


def test_backend_falls_back_when_renumbered():
    b = mc.Backend(2)

    class Gone:
        def read(self, *a):
            return "something-else", [0] * 16
    b.direct = Gone()
    shape = mc.Control(30, "output-source-meter")
    shape.type, shape.values = "INTEGER", [0] * 16
    b.shapes = {30: shape}
    called = []
    b._run = lambda *a: called.append(a) or "numid=30,iface=MIXER,name='x'\n  ; type=INTEGER\n  : values=7\n"
    assert b.read(30).name == "x" and called            # asked amixer instead of trusting the fast read
