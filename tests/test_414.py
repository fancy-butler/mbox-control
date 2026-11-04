"""4.14: loopback, the volume pop-up, app rules, settings backup."""
import json

import mbox_control as mc


def test_loopback_follows_the_chosen_output(win):
    win.set_loopback(0)                                    # Outputs 1/2
    ss = win.ctrl("stream-source")
    out = win.ctrl("output-source")
    want = [out.items[out.values[0]], out.items[out.values[1]]]
    assert [ss.items[ss.values[a]] for a in mc.LOOP_AUX] == want
    win.route_pair_to_mix(0, 2)                            # the output changes mix → loopback follows
    win.reload_state()
    win.refresh_loopback()
    win.reload_state()
    ss = win.ctrl("stream-source")
    assert [ss.items[ss.values[a]] for a in mc.LOOP_AUX] == list(mc.mix_items(2))


def test_loopback_off_restores_previous_channels(win):
    ss = win.ctrl("stream-source")
    before = [ss.items[ss.values[a]] for a in mc.LOOP_AUX]
    win.set_loopback(0)
    win.set_loopback(-1)
    ss = win.ctrl("stream-source")
    assert [ss.items[ss.values[a]] for a in mc.LOOP_AUX] == before


def test_stream_mix_is_mic_plus_computer(win, monkeypatch):
    win.set_loopback(4)                                    # Outputs 5/6
    win.make_stream_mix()
    kind, k, _ = win.pair_source(4)
    assert kind == "mix"
    assert win.mix_lr(k, win.slot_of("Analog-A-1"))[0] > 0
    assert win.mix_lr(k, win.slot_of(mc.PC_LEFT))[0] == mc.UNITY


def test_named_inputs_include_loopback():
    assert any(n == "mbox_loopback" and pos == ["AUX6", "AUX7"] for n, _, pos, _ in mc.NAMED_INPUTS)


def test_osd_shows_volume_and_speakers(win, monkeypatch):
    shown = []
    monkeypatch.setattr(win.osd, "volume", lambda p, t: shown.append(("vol", p, t)))
    monkeypatch.setattr(win.osd, "text", lambda i, t: shown.append(("txt", t)))
    win.osd_value("master-knob-value", [128])
    assert shown[-1][0] == "vol" and shown[-1][1] == 50
    win.app_state["osd"] = False
    win.osd_value("master-knob-value", [255])
    assert len(shown) == 1


def test_osd_fallback_window(app):
    o = mc.Osd()
    o.kde = False
    o.volume(40, "Mbox")
    try:
        assert o.win.isVisible() and o.win.bar.value() == 40
    finally:
        o.timer.stop()
        o.win.close()
        o.win.deleteLater()


def test_app_rules_move_new_streams(win, monkeypatch):
    moved = []
    monkeypatch.setattr(mc, "list_sinks", lambda: [{"id": 7, "name": "sa600_sink", "desc": "Speakers"},
                                                   {"id": 8, "name": "mbox_headphones", "desc": "Headphones"}])
    monkeypatch.setattr(mc, "list_app_streams", lambda: [{"id": 41, "sink": 7, "app": "Steam", "what": "", "icon": ""},
                                                         {"id": 42, "sink": 7, "app": "Firefox", "what": "", "icon": ""}])
    monkeypatch.setattr(mc, "run_quiet", lambda *a, **k: moved.append(a) or "")
    win.app_state["app_rules"] = {"Steam": "mbox_headphones"}
    win.apply_app_rules()
    assert moved == [("pactl", "move-sink-input", "41", "mbox_headphones")]


def test_backup_round_trip(win, tmp_path):
    win.set_label("in0", "Microphone")
    win.app_state["discogs_token"] = "SECRET"
    win.app_state["remote_token"] = "PHONEKEY"
    win.app_state["app_rules"] = {"Steam": "mbox_headphones"}
    win.save_app_state()
    path = tmp_path / "b.mboxbackup"
    mc.export_settings(path, include_keys=False)
    import zipfile
    st = json.loads(zipfile.ZipFile(path).read("app-state.json"))
    assert "discogs_token" not in st and "remote_token" not in st and st["app_rules"]
    mc.LABELS_FILE.write_text("{}")
    ok, msg, _ = mc.import_settings(path)
    assert ok and json.loads(mc.LABELS_FILE.read_text())["in0"] == "Microphone"
    assert json.loads(mc.STATE_FILE.read_text())["discogs_token"] == "SECRET"     # your own key is kept


def test_import_rejects_other_files(tmp_path):
    bad = tmp_path / "x.mboxbackup"
    bad.write_text("not a zip")
    assert mc.import_settings(bad)[0] is False


def test_freeze_watch_logs_a_stuck_window(app, tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(mc, "CRASH_LOG", tmp_path / "crash.log")
    monkeypatch.setattr(mc, "CONF_DIR", tmp_path)
    fw = mc.FreezeWatch()
    fw.beat -= 10                                    # as if the window had been frozen for 10 s
    time.sleep(1.5)
    text = (tmp_path / "crash.log").read_text()
    assert "not responding" in text and "Thread" in text
    fw.timer.stop()


def test_fast_meters_can_be_switched_off(monkeypatch):
    monkeypatch.delenv("MBOX_AMIXER", raising=False)
    monkeypatch.setenv("MBOX_NO_DIRECT", "1")
    b = mc.Backend(2)
    assert b.enable_direct() is False


def test_write_storm_is_blocked_and_logged(win, tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "CRASH_LOG", tmp_path / "crash.log")
    c = win.ctrl("metering")
    win.backend._hist = []                           # forget the app's own start-up writes
    sent = []
    monkeypatch.setattr(win.backend, "_run", lambda *a: sent.append(a) or "")
    for _ in range(mc.Backend.REPEAT_LIMIT):
        win.backend.write(c, [True])
    import pytest
    with pytest.raises(RuntimeError, match="write loop"):
        win.backend.write(c, [True])
    assert len(sent) == mc.Backend.REPEAT_LIMIT
    assert "write storm blocked" in (tmp_path / "crash.log").read_text()
    win.backend.write(c, [False])                    # a different value still goes through


def test_scenes_never_trip_the_brake(win):
    for sid in ("listen", "rip", "tape", "record", "guitar", "listen"):
        win.apply_scene(sid)
    assert "Write failed" not in win.statusBar().currentMessage()


def test_loopback_retries_only_once(win, monkeypatch):
    win.set_loopback(0)
    win.refresh_loopback()                           # settled: what we asked for is what the Mbox has
    calls = []
    monkeypatch.setattr(win, "set_stream", lambda a, item: calls.append((a, item)))
    ss = win.ctrl("stream-source")
    ss.values[6] = 0                                # the Mbox "refused" the change
    for _ in range(5):
        win.refresh_loopback()
    assert len(calls) == 2                           # one attempt (two channels), not a loop


def test_a_hung_helper_is_not_waited_on_again(monkeypatch):
    import subprocess
    import time
    calls = []

    def slow(cmd, **k):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, k.get("timeout", 1))
    monkeypatch.setattr(mc.subprocess, "run", slow)
    monkeypatch.setattr(mc.shutil, "which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr(mc, "HUNG", {})
    assert mc.run_quiet("pactl", "info") is None
    t = time.time()
    assert mc.run_quiet("pactl", "list") is None
    assert len(calls) == 1 and time.time() - t < 0.1
    assert mc.mbox_playing() is False                # doesn't wait either


def test_amixer_timeout_fails_fast_afterwards(monkeypatch):
    import subprocess
    import pytest
    monkeypatch.setattr(mc, "HUNG", {})
    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(a, 3)))
    b = mc.Backend(2)
    with pytest.raises(mc.MboxNotAnswering):
        b.read_all()
    assert mc.is_hung(mc.AMIXER)


def test_not_answering_banner(win, tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "CRASH_LOG", tmp_path / "crash.log")
    win.show_not_answering()
    assert win.hint.isVisible() and "stopped answering" in win.hint.text.text()
    assert "not answering" in (tmp_path / "crash.log").read_text()
