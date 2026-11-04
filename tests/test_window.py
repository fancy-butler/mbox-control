"""The whole app against the fake Mbox: scenes, routing, speakers, recording, the remote, setup."""
import json
import threading
import urllib.error
import urllib.request

import pytest
from PySide6 import QtWidgets

import mbox_control as mc


def scene(win, sid):
    win.apply_scene(sid)


def test_window_builds_every_page(win):
    names = [win.nav.item(i).text().split(None, 1)[-1] for i in range(win.nav.count())]
    assert names == ["Home", "Hardware", "Mixer", "Speakers", "Record", "Effects", "Remote", "Settings", "Advanced"]
    for i in range(win.nav.count()):
        win.nav.setCurrentRow(i)


def test_listen_scene_routes_computer_to_outputs_1_2(win):
    scene(win, "listen")
    kind, k, _ = win.pair_source(0)
    assert kind == "mix"
    assert win.mix_lr(k, win.slot_of(mc.PC_LEFT))[0] == mc.UNITY
    assert win.mix_lr(k, win.slot_of(mc.PC_RIGHT))[1] == mc.UNITY


def _routing(win):
    out = {n: list(c.values) for n, c in win.ctrls.items() if "meter" not in c.name}
    src = win.ctrl("output-source")
    out[src.numid] = out[src.numid][:15]       # slot 15 is the app's own "was the Mbox reset?" marker
    return out


def test_undo_puts_everything_back(win):
    win.reload_state()
    before = _routing(win)
    scene(win, "tape")
    assert _routing(win) != before
    win.undo_scene()
    win.reload_state()
    assert _routing(win) == before


def test_guitar_scene_mutes_every_chosen_input(win):
    win.app_state["guitar_inputs"] = ["Analog-A-1", "Analog-A-2"]
    scene(win, "guitar")
    for item in ("Analog-A-1", "Analog-A-2"):
        slot = win.slot_of(item)
        assert all(win.mix_lr(k, slot) == (0, 0) for k in mc.STEREO_MIX)
    assert win.gx_name("Analog-A-1") == "gx_head" and win.gx_name("Analog-A-2") == "gx_in2"


def test_mono_record_source_feeds_both_sides(win):
    win.set_rec_source("mono:Analog-A-1")
    left, right, label = win.rec_source()
    assert left == right == "Analog-A-1" and "centred" in label


def test_hint_offers_and_hear_it_routes(win, spinner):
    k = win.pair_source(0)[1]
    slot = win.slot_of("Analog-A-1")
    win.mix_set_lr(k, slot, 0, 0)
    win.computer_listening = lambda: False
    win.input_level = lambda item: 900 if item == "Analog-A-1" else 0
    win.sniff_inputs()
    win.sniff_since["Analog-A-1"] -= 3            # pretend it's been playing for 3 s
    win.sniff_inputs()
    assert win.hint.isVisible() and "Input 1" in win.hint.text.text()
    win.hear_input("Analog-A-1", mono=True)
    assert win.input_heard("Analog-A-1")


def test_hint_stays_quiet_while_an_app_records(win):
    win.computer_listening = lambda: True
    win.input_level = lambda item: 900 if item == "Analog-A-2" else 0
    win.mix_set_lr(win.pair_source(0)[1], win.slot_of("Analog-A-2"), 0, 0)
    win.sniff_inputs()
    win.sniff_since["Analog-A-2"] -= 3
    win.sniff_inputs()
    assert not win.hint.isVisible()


def test_setup_wizard_saves_names(win):
    renamed = []
    win.rename_main_output = renamed.append
    d = mc.SetupWizard(win)
    d.out_edit.setText("Living-room amp")
    d.in_edits[0].setText("Microphone")
    d.always["Analog-A-5"].setChecked(True)
    d.save()
    assert win.label("out0") == "Living-room amp" and win.label("in0") == "Microphone"
    assert win.app_state["setup"]["always_on"] == ["Analog-A-5"]
    assert renamed == ["Living-room amp (Mbox)"]


def test_signal_map_draws(win, spinner):
    win.nav.setCurrentRow(win.page_index("Hardware"))
    spinner(0.3)
    srcs, mixes, outs, *_ = win.sig_map.model()
    assert len(outs) == 6 and srcs[0][0] == "pc"
    assert not win.sig_map.grab().isNull()


def test_titles_follow_their_recording(win, tmp_path, spinner):
    a, b = tmp_path / "sideA.flac", tmp_path / "sideB.flac"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    win.split_src.setText(str(a))
    spinner(0.3)
    win.add_track_row(1, "Osseh Shalom", 0, 100)
    win.split_src.setText(str(b))
    spinner(0.3)
    assert win.track_table.rowCount() == 0                 # side B doesn't inherit side A's titles
    win.split_src.setText(str(a))
    spinner(0.3)
    assert win.track_table.item(0, 1).text() == "Osseh Shalom"


def _http(port, path, method="GET", headers=None, app=None):
    out = {}

    def run():
        try:
            r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                                              headers=headers or {}), timeout=5)
            out["code"] = r.status
        except urllib.error.HTTPError as e:
            out["code"] = e.code
    t = threading.Thread(target=run)
    t.start()
    while t.is_alive():
        app.processEvents()
    app.processEvents()
    return out["code"]


def test_remote_needs_the_paired_key(win, app, monkeypatch, spinner):
    port = 18765
    orig = win.remote.start
    monkeypatch.setattr(win.remote, "start", lambda p=port: orig(port))
    win.start_remote(quiet=True)
    key = win.remote.token
    assert len(key) >= 12
    assert _http(port, "/state", app=app) == 403
    assert _http(port, "/state?k=" + key, app=app) == 200
    assert _http(port, "/do/mute/1", "POST", {"X-Mbox-Key": "wrong"}, app=app) == 403
    assert not win.app_state.get("spk_mute")
    assert _http(port, "/do/mute/1", "POST", {"X-Mbox-Key": key}, app=app) == 204
    spinner(0.2)
    assert win.app_state.get("spk_mute")
    win.unpair_phones()
    assert _http(port, "/state?k=" + key, app=app) == 403


def test_updates_from_downloads_only_in_tester_mode(win):
    dl = mc.HOME / "Downloads"
    dl.mkdir(exist_ok=True)
    (dl / "mbox_control.py").write_text('VERSION = "99.0"\n')
    try:
        win.app_state["tester_updates"] = False
        win.check_update()
        assert win.update_offer is None
        win.app_state["tester_updates"] = True
        win.check_update()
        assert win.update_offer["ver"] == "99.0"
    finally:
        (dl / "mbox_control.py").unlink()


def test_broken_download_is_never_installed(win, monkeypatch, spinner):
    import io
    monkeypatch.setattr(mc, "UPDATE_REPO", "someone/mbox-control")

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(mc.urllib.request, "urlopen", lambda req, timeout=0: R(b'VERSION = "99.0"\nbroken(\n'))
    installed, warned = [], []
    monkeypatch.setattr(win, "apply_update", installed.append)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a: warned.append(a))
    win.update_offer = {"kind": "github", "ver": "99.0"}
    win.install_offer()
    for _ in range(50):
        spinner(0.05)
        if warned:
            break
    assert not installed and warned


def test_diagnostics_has_no_home_path():
    text = mc.diagnostics()
    assert "Mbox Control: " + mc.VERSION in text
    assert str(mc.HOME) not in text


@pytest.mark.parametrize("page", ["Record", "Settings", "Speakers"])
def test_tabs_have_names(win, page):
    win.nav.setCurrentRow(win.page_index(page))
    t = win.stack.currentWidget().findChild(QtWidgets.QTabWidget)
    assert t.count() >= 2


def test_state_file_is_valid_json(win):
    win.save_app_state()
    json.loads(mc.STATE_FILE.read_text())
