"""4.19: save takes in other formats, convert audio files."""
import subprocess

import pytest

import mbox_control as mc


def probe(path, entries):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "default=nw=1", str(path)],
                       capture_output=True, text=True)
    return r.stdout


@pytest.fixture
def tagged(tmp_path):
    """A 96 kHz 24-bit FLAC with tags and cover art, like a real take or rip."""
    cover = tmp_path / "cover.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v",
                    "1", str(cover)], check=True)
    src = tmp_path / "song.flac"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic",
                    "-ar", "96000", "-sample_fmt", "s32", "-c:a", "flac", "-metadata", "title=Reel Tune",
                    "-metadata", "artist=Stan", str(src)], check=True)
    return src


@pytest.mark.parametrize("key", [f[0] for f in mc.AUDIO_FORMATS])
def test_every_format_converts_with_tags(tagged, tmp_path, key):
    _, _, ext, _, art = mc.audio_format(key)
    dst = mc.convert_target(tagged, key, tmp_path / "out")
    mc.transcode(tagged, dst, key)
    assert dst.exists() and dst.suffix == "." + ext
    info = probe(dst, "stream=codec_type,sample_rate:format_tags=title,artist:stream_tags=title")
    assert "Reel Tune" in info, info
    if ext in ("mp3", "ogg", "opus") or key == "aac":
        assert "sample_rate=48000" in info                  # resampled from 96 kHz
    if art and ext != "ogg":
        assert "codec_type=video" in info                   # cover art kept
    assert not list((tmp_path / "out").glob("*.part.*"))


def test_converted_copies_go_beside_in_a_folder(tagged):
    dst = mc.convert_target(tagged, "mp3_v0")
    assert dst.parent.name == "Converted (MP3)" and dst.name == "song.mp3"
    same = mc.convert_target(tagged, "flac", tagged.parent)
    assert same.name == "song (converted).flac"


def test_takes_are_saved_in_the_chosen_format(win, tagged):
    win.app_state["save_fmt"] = "mp3_192"
    out = win.finish_format(tagged)
    assert out.suffix == ".mp3" and out.exists() and not tagged.exists()
    win.app_state["save_fmt"] = "flac"
    assert win.finish_format(out) == out


def test_convert_card_runs_a_batch(win, tagged, tmp_path, spinner):
    (tmp_path / "junk.txt").write_text("not audio")
    win.convert_add([str(tmp_path)])
    assert win.conv_list.count() == 1                     # only the audio file
    win.convert_add([str(tagged)])
    assert win.conv_list.count() == 1                     # no duplicates
    win.conv_fmt.setCurrentIndex(win.conv_fmt.findData("opus"))
    win.convert_start()
    for _ in range(100):
        spinner(0.1)
        if win.conv_job is None:
            break
    assert (tmp_path / "Converted (Opus 160 kbps)" / "song.opus").exists() or \
        list(tmp_path.glob("Converted*/song.opus"))
    assert win.conv_note.text().startswith("Done: 1 converted")


def test_capture_offers_plain_pairs_not_the_rip_setting(win):
    data = [win.retro_src.itemData(i) for i in range(win.retro_src.count())]
    assert "rip" not in data and "pair:Analog-A-1" in data and "pair:Analog-A-5" in data
    src, auxes, name = win.capture_for("pair:Analog-A-5")
    assert auxes == ["AUX4", "AUX5"] and "Aux In 5/6" in name
    ss = win.ctrl("stream-source")
    assert ss.items[ss.values[4]] == "Analog-A-5" and ss.items[ss.values[5]] == "Analog-A-6"


def test_old_rip_choice_is_migrated(win):
    win.app_state["retro_src"] = "rip"
    box = mc.QtWidgets.QComboBox()
    win.input_choices(box, "retro_src")
    assert box.currentData() == f"pair:{win.app_state['setup']['rip']}"


def test_hearing_a_stereo_pair_and_putting_it_back(win):
    l_slot, r_slot = win.slot_of("Analog-A-5"), win.slot_of("Analog-A-6")
    win.mix_set_lr(1, l_slot, 0, 0)
    win.mix_set_lr(1, r_slot, 0, 0)
    win.retro_choice = "pair:Analog-A-5"
    win.retro_monitor(True)
    assert win.mix_lr(1, l_slot) == (mc.UNITY, 0) and win.mix_lr(1, r_slot) == (0, mc.UNITY)
    win.retro_monitor(False)
    assert win.mix_lr(1, l_slot) == (0, 0) and win.mix_lr(1, r_slot) == (0, 0)


def test_a_crash_during_a_quick_take_is_undone_at_start(app, tmp_path, monkeypatch):
    import json
    import shutil
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state.json"))
    shutil.rmtree(mc.CONF_DIR, ignore_errors=True)
    mc.CONF_DIR.mkdir(parents=True, exist_ok=True)
    mc.STATE_FILE.write_text(json.dumps({"rec_src": "mono:Analog-A-4", "take_restore": "pair:Analog-A-5"}))
    monkeypatch.setattr(mc.MainWindow, "run_setup_wizard", lambda self: None)
    w = mc.MainWindow(mc.Backend(2))
    try:
        assert w.app_state["rec_src"] == "pair:Analog-A-5" and "take_restore" not in w.app_state
    finally:
        w.quitting = True
        w.close()
        w.deleteLater()


def test_quick_take_and_capture_can_both_hear_the_same_input(win):
    slot = win.slot_of("Analog-A-1")
    win.mix_set_lr(1, slot, 0, 0)
    win.input_monitor("take", "mono:Analog-A-1", True)
    assert win.mix_lr(1, slot) == (mc.UNITY, mc.UNITY)
    win.input_monitor("retro", "mono:Analog-A-1", True)       # already audible: nothing to undo later
    win.input_monitor("retro", "", False)
    assert win.mix_lr(1, slot) == (mc.UNITY, mc.UNITY)        # still on while the take runs
    win.input_monitor("take", "", False)
    assert win.mix_lr(1, slot) == (0, 0)
    assert win.take_hear.isChecked()


@pytest.fixture
def uneven(tmp_path):
    """A take with a quiet first half (−30 dB) and a loud second half (−8 dB)."""
    import numpy as np
    t = np.arange(96000) / 48000
    x = np.sin(2 * np.pi * 82 * t).astype(np.float32)
    x[:48000] *= 0.0316
    x[48000:] *= 0.398
    p = tmp_path / "take.flac"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "f32le", "-ar", "48000", "-ac", "1", "-i", "-",
                    "-c:a", "flac", str(p)], input=x.tobytes(), check=True)
    return p


def _halves_db(path):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-"],
                         capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    rms = lambda v: 20 * np.log10(np.sqrt(np.mean(v ** 2)))     # noqa: E731
    return rms(x[12000:36000]), rms(x[60000:84000])


def test_normalize_puts_the_peak_at_minus_one(uneven):
    q0, l0 = _halves_db(uneven)
    mc.polish_take(uneven, "normalize")
    assert abs(mc.peak_db(uneven) - (-1.0)) < 0.3
    q1, l1 = _halves_db(uneven)
    assert abs((l1 - q1) - (l0 - q0)) < 0.5           # loud/quiet balance unchanged


def test_even_out_brings_quiet_and_loud_closer(uneven):
    q0, l0 = _halves_db(uneven)
    mc.polish_take(uneven, "compress")
    q1, l1 = _halves_db(uneven)
    assert (l1 - q1) < (l0 - q0) - 6                  # the gap shrinks by several dB
    assert abs(mc.peak_db(uneven) - (-1.0)) < 0.3
    assert not list(uneven.parent.glob("*.polish.*")) and not list(uneven.parent.glob("*.norm.*"))


def test_takes_are_polished_before_converting(win, uneven):
    win.app_state["take_polish"] = "normalize"
    win.app_state["save_fmt"] = "wav24"
    out = win.finish_format(uneven, polish=True)
    assert out.suffix == ".wav" and abs(mc.peak_db(out) - (-1.0)) < 0.3
    assert win.take_polish.count() == 3


def test_dead_input_warns_about_phantom_power(win, monkeypatch):
    import time as _t
    ph = win.ctrl("phantom-powering")
    ph.values[0] = False
    monkeypatch.setattr(win, "item_db", lambda item: -90.0)
    win.take_mode = True
    win.app_state["take_src"] = "mono:Analog-A-2"
    win.watch_dead_input()
    assert not win.hint.isVisible()                    # not straight away
    win.dead_watch["take:Analog-A-2"]["t"] = _t.time() - 5
    win.watch_dead_input()
    assert win.hint.isVisible() and "+48V" in win.hint.text.text() and "Input 2" in win.hint.text.text()
    win.hint.hide()
    win.watch_dead_input()
    assert not win.hint.isVisible()                    # once per take
    win.take_mode = False
    win.watch_dead_input()
    assert not win.dead_watch


def test_no_warning_when_sound_arrives(win, monkeypatch):
    import time as _t
    monkeypatch.setattr(win, "item_db", lambda item: -20.0)
    win.take_mode = True
    win.watch_dead_input()
    for st in win.dead_watch.values():
        st["t"] = _t.time() - 7
    win.watch_dead_input()
    assert not win.hint.isVisible()
    win.take_mode = False


def test_lossy_takes_are_normalized_a_little_lower(win, uneven):
    win.app_state["take_polish"] = "normalize"
    win.app_state["save_fmt"] = "mp3_v0"
    out = win.finish_format(uneven, polish=True)
    assert out.suffix == ".mp3" and mc.peak_db(out) < -1.2



def test_a_live_bass_does_not_hide_a_dead_mic_in_multitrack(win, monkeypatch):
    import time as _t
    win.ctrl("phantom-powering").values[0] = False
    monkeypatch.setattr(win, "item_db", lambda item: -12.0 if item == "Analog-A-1" else -90.0)
    win.app_state["mt_inputs"] = ["Analog-A-1", "Analog-A-2"]
    win.mt_proc = object()
    try:
        win.watch_dead_input()
        for st in win.dead_watch.values():
            st["t"] = _t.time() - 5
        win.watch_dead_input()
        assert win.hint.isVisible() and "Input 2" in win.hint.text.text() and "+48V" in win.hint.text.text()
    finally:
        win.mt_proc = None


def test_too_quiet_mic_is_called_out(win, monkeypatch):
    import time as _t
    win.ctrl("phantom-powering").values[0] = True
    monkeypatch.setattr(win, "item_db", lambda item: -48.0)
    win.take_mode = True
    win.app_state["take_src"] = "mono:Analog-A-2"
    win.watch_dead_input()
    win.dead_watch["take:Analog-A-2"]["t"] = _t.time() - 7
    win.watch_dead_input()
    assert win.hint.isVisible() and "very quiet" in win.hint.text.text() and "+48V" not in win.hint.text.text()
    win.take_mode = False


def test_multitrack_shows_a_strip_per_ticked_input(win):
    win.mt_boxes["Analog-A-3"].setChecked(True)
    win.mt_boxes["Analog-A-4"].setChecked(False)
    assert not win.mt_strips["Analog-A-4"].isVisibleTo(win.mt_btn.parentWidget())
    assert win.mt_strips["Analog-A-3"].isVisibleTo(win.mt_btn.parentWidget())
    assert win.mt_strips["Analog-A-1"].spectrum is False


def test_scrolling_the_page_never_changes_a_setting(win, spinner):
    from PySide6 import QtCore, QtGui, QtWidgets
    box = win.save_fmt
    before = box.currentIndex()
    sa = box.parentWidget()
    while sa and not isinstance(sa, QtWidgets.QScrollArea):
        sa = sa.parentWidget()
    win.nav.setCurrentRow(win.page_index("Record"))
    spinner(0.2)
    win.setFocus()
    pos = QtCore.QPointF(5, 5)
    ev = QtGui.QWheelEvent(pos, box.mapToGlobal(pos), QtCore.QPoint(0, 0), QtCore.QPoint(0, -120),
                           QtCore.Qt.NoButton, QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(box, ev)
    assert box.currentIndex() == before                 # not changed by a passing scroll
    assert box.focusPolicy() == QtCore.Qt.StrongFocus
    box.setFocus()
    spinner(0.05)
    if box.hasFocus():                                  # clicked into: the wheel works as usual
        QtWidgets.QApplication.sendEvent(box, QtGui.QWheelEvent(
            pos, box.mapToGlobal(pos), QtCore.QPoint(0, 0), QtCore.QPoint(0, -120), QtCore.Qt.NoButton,
            QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False))
        assert box.currentIndex() != before


def _route(win, first, mix):
    c = win.ctrl("output-source")
    c.values[first] = c.items.index(f"Mixer-{2 * mix - 1}")
    c.values[first + 1] = c.items.index(f"Mixer-{2 * mix}")


def test_hearing_reaches_the_headphones_mix_too(win):
    _route(win, 0, 1)
    _route(win, 6, 2)                                   # Headphone A plays Stereo Mix 2
    s1, s2 = win.slot_of("Analog-A-1"), win.slot_of("Analog-A-2")
    for k in (1, 2):
        for sl in (s1, s2):
            win.mix_set_lr(k, sl, 0, 0)
    win.input_monitor("multitrack", "multi:Analog-A-1,Analog-A-2", True)
    for k in (1, 2):
        assert win.mix_lr(k, s1) == (mc.UNITY, mc.UNITY) and win.mix_lr(k, s2) == (mc.UNITY, mc.UNITY)
    win.input_monitor("multitrack", "", False)
    for k in (1, 2):
        assert win.mix_lr(k, s1) == (0, 0) and win.mix_lr(k, s2) == (0, 0)


def test_headphones_playing_the_computer_directly_are_explained_once(win):
    c = win.ctrl("output-source")
    c.values[6] = c.items.index("Stream-1")
    c.values[7] = c.items.index("Stream-2")
    win.app_state.pop("hp_direct_told", None)
    win.input_monitor("take", "mono:Analog-A-1", True)
    assert win.hint.isVisible() and "Headphone A" in win.hint.text.text()
    win.input_monitor("take", "", False)
    win.hint.hide()
    win.input_monitor("take", "mono:Analog-A-1", True)
    assert not win.hint.isVisible()
    win.input_monitor("take", "", False)


def test_lowcut_switches_follow_the_mbox_and_names(win, spinner):
    c = win.ctrl("input-hp-filter")
    cbs = [w for w in win.findChildren(mc.QtWidgets.QCheckBox) if w.text().startswith("Input 2")
           and "rumble" in w.toolTip()]
    assert len(cbs) == 2                                # Quick take and Multitrack
    cbs[0].setChecked(True)
    assert c.values[1] is True or c.values[1] == 1
    win.run_refreshers()
    assert cbs[1].isChecked()                           # both reflect the one Mbox setting
    win.set_label("in1", "AT2020")
    assert all("AT2020" in cb.text() for cb in cbs)


def test_show_folder_reveals_the_last_take(win, tmp_path, monkeypatch):
    got = []
    monkeypatch.setattr(mc, "reveal_in_files", lambda p: got.append(p))
    take = win.takes_dir() / "take-x.flac"
    take.parent.mkdir(parents=True, exist_ok=True)
    take.write_bytes(b"x")
    win.app_state["last_take"] = str(take)
    win.take_dir_btn.click()
    assert got == [take]                                # the file itself, so it's selected


def test_reveal_falls_back_to_opening_the_folder(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(mc.QtGui.QDesktopServices, "openUrl", lambda u: opened.append(u.toLocalFile()) or True)
    f = tmp_path / "a.flac"
    f.write_bytes(b"x")
    mc.reveal_in_files(f)                               # no file-manager service in the test session
    assert opened in ([], [str(tmp_path)])
