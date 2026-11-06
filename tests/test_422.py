"""4.22: J-card maker — real dimensions, true-size PDF, wrapping track list, the dialog."""
import shutil
import subprocess

import numpy as np
import pytest

import mbox_control as mc

SIDES = {"A": [("Running Up That Hill — Kate Bush", 299), ("Smalltown Boy — Bronski Beat", 302)],
         "B": [("The Killing Moon — Echo & the Bunnymen", 345)]}


def test_panel_sizes_are_the_standard_ones():
    assert [round(w, 2) for _, _, w in mc.jcard_panels({"panels": 3})] == [26.99, 12.7, 65.09]
    assert mc.jcard_size({"panels": 3}) == (pytest.approx(104.78), 101.6)
    assert mc.jcard_size({"panels": 4})[0] == pytest.approx(168.28)
    assert mc.jcard_size({"panels": 5})[0] == pytest.approx(230.19)


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="needs poppler's pdftoppm")
def test_pdf_prints_at_actual_size(tmp_path, app):
    spec = {"title": "T", "artist": "A", "sides": SIDES, "bg": "#1d2b44", "panels": 3, "paper": "Letter"}
    pdf = mc.save_jcard_pdf(spec, tmp_path / "card.pdf")
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    assert "612 x 792 pts" in info                                   # Letter
    subprocess.run(["pdftoppm", "-r", "300", "-png", "-singlefile", str(pdf), str(tmp_path / "page")], check=True)
    from PySide6 import QtGui
    img = QtGui.QImage(str(tmp_path / "page.png")).convertToFormat(QtGui.QImage.Format_RGB888)
    a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width() * 3]
    a = a.reshape(img.height(), img.width(), 3).astype(int)
    dark = (abs(a - np.array([0x1d, 0x2b, 0x44])).sum(axis=2) < 40)    # the card's background
    r = int(np.median(np.where(dark)[0]))                            # a row through the middle of the card
    cols = np.where(dark[r])[0]
    c = cols.min() + 20                                              # a column through the rear flap
    rows = np.where(dark[:, c])[0]
    w_mm = (cols.max() - cols.min() + 1) / 300 * 25.4
    h_mm = (rows.max() - rows.min() + 1) / 300 * 25.4
    assert w_mm == pytest.approx(104.78, abs=0.4) and h_mm == pytest.approx(101.6, abs=0.4)


def test_png_is_600_dpi_flat_card(tmp_path, app):
    out = mc.save_jcard_png({"title": "T", "sides": SIDES, "panels": 4}, tmp_path / "c.png")
    from PySide6 import QtGui
    img = QtGui.QImage(str(out))
    assert img.width() == round(168.28 / 25.4 * 600) and img.height() == round(101.6 / 25.4 * 600)


def test_side_text_parses_times():
    assert mc.JCardDialog.parse_side("Song One  3:45\n\nSong Two\nTen   12:01") == \
        [("Song One", 225), ("Song Two", 0), ("Ten", 721)]


def test_paper_follows_the_country():
    from PySide6 import QtCore
    old = QtCore.QLocale()
    try:
        QtCore.QLocale.setDefault(QtCore.QLocale("en_CA"))
        assert mc.default_paper() == "Letter"
        QtCore.QLocale.setDefault(QtCore.QLocale("de_DE"))
        assert mc.default_paper() == "A4"
    finally:
        QtCore.QLocale.setDefault(old)


def test_dialog_uses_the_planned_sides_and_remembers_the_look(win, tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "extract_cover", lambda files, dest: None)
    win.tape_sides, win.tape_files = SIDES, []
    d = mc.JCardDialog(win, win.tape_sides, win.tape_files)
    assert "Running Up That Hill — Kate Bush  4:59" in d.side_edit["A"].toPlainText()
    d.title.setText("Night Drive")
    d.panels.setCurrentIndex(1)
    d.render_preview()
    assert not d.preview.pixmap().isNull()
    spec = d.collect()
    assert spec["sides"]["B"] == [("The Killing Moon — Echo & the Bunnymen", 345)] and spec["panels"] == 4
    assert win.app_state["jcard"]["title"] == "Night Drive"
    pdf = mc.save_jcard_pdf(spec, tmp_path / "x.pdf")
    assert pdf.exists() and pdf.stat().st_size > 1000
    d.reject()


def test_cover_art_comes_from_the_songs(tmp_path):
    cover = tmp_path / "art.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1",
                    str(cover)], check=True)
    song = tmp_path / "s.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", "-i", str(cover),
                    "-map", "0:a", "-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic", str(song)],
                   check=True)
    got = mc.extract_cover([song], tmp_path / "out.jpg")
    assert got and got.exists() and got.stat().st_size > 0
    plain = tmp_path / "p.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", str(plain)], check=True)
    (tmp_path / "folder.jpg").write_bytes(cover.read_bytes())
    assert mc.extract_cover([plain], tmp_path / "out2.jpg") == tmp_path / "folder.jpg"


def _fake_pw_play(tmp_path, monkeypatch, accepts_raw=True):
    """A pw-play like PipeWire 1.4+: piped samples need --raw, otherwise it quits with an error."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    log = tmp_path / "pw-play.log"
    (b / "pw-play").write_text(
        "#!/bin/sh\n"
        f'echo "$@" >>{log}\n'
        'case " $* " in *" --raw "*) cat >/dev/null; exit 0 ;; esac\n'
        'echo "error: sf_open failed: File contains data in an unknown format." >&2; exit 1\n')
    (b / "pw-play").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{__import__('os').environ['PATH']}")
    monkeypatch.setattr(mc, "_PW_RAW", accepts_raw)
    return log


def test_metronome_passes_raw_to_newer_pw_play(tmp_path, monkeypatch, app):
    log = _fake_pw_play(tmp_path, monkeypatch, accepts_raw=True)
    m = mc.Metronome(None, 120, 4, 0.5, rate=48000)
    errs = []
    m.failed.connect(errs.append)
    m.start()
    import time
    time.sleep(0.6)
    m.stop()
    app.processEvents()
    assert "--raw" in log.read_text() and not errs


def test_metronome_reports_when_pw_play_refuses(tmp_path, monkeypatch, app, win):
    _fake_pw_play(tmp_path, monkeypatch, accepts_raw=False)        # an old flag check: no --raw sent
    monkeypatch.setattr(mc, "pw_node_exists", lambda n, k: False)
    win.toggle_metronome()
    import time
    end = time.time() + 3
    while win.metro is not None and time.time() < end:
        app.processEvents()
        time.sleep(0.05)
    assert win.metro is None and win.met_btn.text() == "▶  Start"
    assert "couldn't play" in win.hint.text.text() and "unknown format" in win.hint.text.text()
