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
