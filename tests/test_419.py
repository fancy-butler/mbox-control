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
