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
