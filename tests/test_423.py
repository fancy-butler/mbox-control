"""4.23: headphones get their own EQ; tone boosts only lower the level as far as the curve really rises."""
import pytest
import mbox_control as mc


def test_band_db_matches_the_textbook_points():
    pk = {"type": "PK", "f": 1000.0, "q": 1.0, "g": 6.0}
    assert mc.band_db(pk, 1000.0) == pytest.approx(6.0, abs=0.05)
    assert abs(mc.band_db(pk, 50.0)) < 0.2
    ls = {"type": "LSC", "f": 105.0, "q": 0.7, "g": 12.0}
    assert mc.band_db(ls, 20.0) == pytest.approx(12.0, abs=0.3) and abs(mc.band_db(ls, 5000.0)) < 0.1
    hs = {"type": "HSC", "f": 8000.0, "q": 0.7, "g": -6.0}
    assert mc.band_db(hs, 20000.0) == pytest.approx(-6.0, abs=0.6) and abs(mc.band_db(hs, 100.0)) < 0.1


def test_peak_is_never_negative_and_stacks():
    assert mc.eq_peak_db([]) == 0.0
    assert mc.eq_peak_db([{"type": "PK", "f": 1000.0, "q": 1.0, "g": -6.0}]) == 0.0
    two = [{"type": "LSC", "f": 105.0, "q": 0.7, "g": 6.0}] * 2
    assert mc.eq_peak_db(two) == pytest.approx(12.0, abs=0.4)


def setup_eq(win, monkeypatch):
    sent = []
    monkeypatch.setattr(mc, "set_eq_live", lambda node, prof: sent.append(prof) or True)
    monkeypatch.delenv("MBOX_AMIXER", raising=False)
    win.app_state["eq_on"] = True
    win.save_eq_profile({"name": "Cans", "preamp": -3.0, "bands": [{"type": "PK", "f": 3000.0, "q": 1.0, "g": 3.0}]})
    win.assign_eq("H", "Cans")
    return sent


def test_headphones_eq_when_ticked_or_speakers_off(win, monkeypatch):
    sent = setup_eq(win, monkeypatch)
    item = {"v": "Green"}
    monkeypatch.setattr(win, "ctrl_item", lambda n: item["v"] if n == "spkr-button" else None)
    win.app_state["hp_listen"] = False
    assert win.eq_target() == "A"
    win.apply_eq()
    assert sent[-1]["name"] == "Off (flat)"
    win.set_hp_listen(True)
    assert win.eq_target() == "H" and sent[-1]["name"] == "Cans"
    win.set_hp_listen(False)
    item["v"] = "Off"
    assert win.eq_target() == "H"
    item["v"] = "Red-Blink"
    assert win.eq_target() == "B"


def test_bass_boost_only_drops_what_it_adds(win, monkeypatch):
    sent = setup_eq(win, monkeypatch)
    monkeypatch.setattr(win, "eq_target", lambda: "H")
    win.app_state["eq_tone"] = {"bass": 12, "treble": 0}
    win.apply_eq()
    p = sent[-1]
    assert p["preamp"] == pytest.approx(-12.0, abs=0.3)      # curve now peaks at +12: −12 total, not −3−12
    win.app_state["eq_tone"] = {"bass": -6, "treble": 0}
    win.apply_eq()
    assert sent[-1]["preamp"] == pytest.approx(-3.0)                # cuts never lower the level


def test_old_headphone_profile_on_b_also_lands_on_headphones(tmp_path):
    state = {"eq_sets": {"B": "HD 280"}}
    sets = state["eq_sets"]
    if "H" not in sets and sets.get("B", "Off (flat)") != "Off (flat)":
        sets["H"] = sets["B"]
    assert sets == {"B": "HD 280", "H": "HD 280"}


def test_subsonic_follows_the_headphones_row(win, monkeypatch):
    sent = setup_eq(win, monkeypatch)
    monkeypatch.setattr(win, "eq_target", lambda: "H")
    win.app_state["subsonic_sets"] = {"A": True, "H": False}
    win.apply_eq()
    assert sent[-1]["subsonic"] is False


def test_speaker_chip_switches_sets_and_off(win, monkeypatch):
    from PySide6 import QtCore
    win.chip_spk.clicked.emit()
    shown = [[x.text() for x in win.spk_menu.actions() if x.text()]]
    win.spk_menu.close()
    assert shown and shown[0][:3] == [t for t in shown[0][:3] if t.startswith("Speakers ")]
    assert "Speakers off (headphones only)" in shown[0] and "Use the headphone EQ" in shown[0]
    win.backend.__dict__.pop("_hist", None)            # earlier tests' writes would trip the storm guard
    toasts = []
    monkeypatch.setattr(win, "toast", lambda *a, **k: toasts.append(a))
    win.switch_speakers("off")
    assert not toasts
    assert win.ctrl_item("spkr-button") == "Off" and win.eq_target() == "H"
    assert win.chip_spk.text().startswith("Speakers off")
    win.switch_speakers("B")
    assert win.ctrl_item("spkr-button") == "Red" and "Speakers B" in win.chip_spk.text()
    win.set_hp_listen(True)
    assert "🎧" in win.chip_spk.text() and win.eq_target() == "H"


def test_eq_on_off_switches_live_when_the_chain_stays(win, monkeypatch):
    sent = setup_eq(win, monkeypatch)
    monkeypatch.setattr(mc, "mbox_pw_node", lambda kind: "mbox")
    monkeypatch.setattr(mc, "restart_pipewire", lambda: pytest.fail("no restart needed"))
    monkeypatch.setattr(win, "eq_target", lambda: "H")
    win.app_state["subsonic_sets"] = {"H": True}
    win.toggle_eq()                                     # on → off: the subsonic filter keeps the chain
    assert sent[-1]["name"] == "Off (flat)" and sent[-1]["subsonic"] is True
    win.toggle_eq()
    assert sent[-1]["name"] == "Cans"


def test_old_main_output_is_renamed_once(monkeypatch, tmp_path):
    old, new = tmp_path / "mbox-sa600.conf", tmp_path / "mbox-main.conf"
    monkeypatch.setattr(mc, "OLD_MAIN_CONF", old)
    monkeypatch.setattr(mc, "MAIN_CONF", new)
    monkeypatch.setattr(mc, "run_quiet", lambda *a, **k: "sa600_sink\n" if "get-default-sink" in a else "")
    monkeypatch.setattr(mc, "rlog", lambda m: None)
    old.write_text('capture.props = { node.name = "sa600_sink" }\nplayback.props = { node.name = "sa600_sink_out" '
                   'target.object = "alsa_output.firewire-x" }\n  { type = builtin name = sub1 label = bq_highpass }\n')
    state = {"pr_out": "sa600_sink", "mirror": {"B": "sink:sa600_sink"}, "eq_on": True}
    assert mc.migrate_main_output(state) == (True, True)
    text = new.read_text()
    assert '"mbox_main"' in text and '"mbox_main_out"' in text and "sa600" not in text and "sub1" in text
    assert not old.exists()
    assert state == {"pr_out": "mbox_main", "mirror": {"B": "sink:mbox_main"}, "eq_on": True}
    assert mc.migrate_main_output(state) == (False, False)       # second start: nothing to do


def test_no_personal_gear_in_user_facing_text():
    src = open(mc.__file__, encoding="utf-8").read()
    for word in ("SA-600", "Missions", "the TV on"):
        assert word not in src, word


def test_eq_curve_shape():
    f = [30.0, 105.0, 1000.0, 10000.0]
    flat = mc.eq_curve({"preamp": 0, "bands": []}, f)
    assert all(abs(v) < 1e-6 for v in flat)
    bass = mc.eq_curve({"preamp": -12, "bands": [{"type": "LSC", "f": 105.0, "q": 0.7, "g": 12.0}]}, f)
    assert bass[0] == pytest.approx(0, abs=0.6) and bass[3] == pytest.approx(-12, abs=0.1)
    sub = mc.eq_curve({"preamp": 0, "bands": [], "subsonic": True}, [10.0, 30.0, 1000.0])
    assert sub[0] < -15 and sub[1] == pytest.approx(-3, abs=0.6) and abs(sub[2]) < 0.1


def test_graph_follows_the_tone_slider(win, monkeypatch):
    setup_eq(win, monkeypatch)
    monkeypatch.setattr(win, "eq_target", lambda: "H")
    win.app_state["eq_tone"] = {"bass": 0, "treble": 0}
    win.apply_eq()
    before = list(win.eq_graph.cur)
    win.tone["bass"].setValue(10)
    win.tone_changed()
    g = win.eq_graph
    assert g.cur != before and g.base == before and "Headphones: Cans" in g.title
    assert g.cur[0] - g.cur[-1] > 8                     # bass now well above treble
    g.resize(600, 200)
    g.grab()                                            # paints without errors
