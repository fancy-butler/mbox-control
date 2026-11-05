"""4.18: mixer undo/redo, A/B compare, compact mixer, colour tags, automatic scenes."""
import mbox_control as mc


def settle(win, spinner):
    win.hist_timer.stop()
    win.record_mix_state()
    spinner(0.05)


def first_slot(win):
    for item in ("Analog-A-1", "Stream-1"):
        s = win.slot_of(item)
        if s is not None:
            return item, s
    raise AssertionError("no mixer slot found")


def test_undo_redo_mixer_gain(win, spinner):
    item, slot = first_slot(win)
    settle(win, spinner)
    before = win.mix_lr(1, slot)
    win.mix_set_lr(1, slot, 1000, 2000)
    assert win.hist_timer.isActive()                 # the change is remembered once it settles
    settle(win, spinner)
    assert win.mix_lr(1, slot) == (1000, 2000)
    win.mix_history_step(-1)
    assert win.mix_lr(1, slot) == before
    assert not win.hist_timer.isActive()             # undoing doesn't record itself as a new change
    win.mix_history_step(+1)
    assert win.mix_lr(1, slot) == (1000, 2000)
    assert win.redo_btn.isEnabled() is False and win.undo_btn.isEnabled()


def test_new_change_after_undo_drops_redo(win, spinner):
    _, slot = first_slot(win)
    settle(win, spinner)
    win.mix_set_lr(1, slot, 500, 500)
    settle(win, spinner)
    win.mix_history_step(-1)
    win.mix_set_lr(1, slot, 700, 700)
    settle(win, spinner)
    assert win.mix_pos == len(win.mix_hist) - 1
    win.mix_history_step(+1)                         # nothing to redo
    assert win.mix_lr(1, slot) == (700, 700)


def test_history_is_capped(win):
    for i in range(130):
        win.mix_hist.append({"ctrls": {}, "mutes": {}, "solo": {"n": i}})
    win.mix_pos = len(win.mix_hist) - 1
    win.record_mix_state()
    assert len(win.mix_hist) <= 100


def test_ab_compare(win, spinner):
    _, slot = first_slot(win)
    settle(win, spinner)
    a = win.mix_lr(1, slot)
    win.ab_switch("B")                               # B starts as a copy
    win.mix_set_lr(1, slot, 1234, 1234)
    settle(win, spinner)
    win.ab_switch("A")
    assert win.mix_lr(1, slot) == a
    win.ab_switch("B")
    assert win.mix_lr(1, slot) == (1234, 1234)
    assert win.ab_btns["B"].isChecked() and not win.ab_btns["A"].isChecked()


def test_compact_mixer_sets_gain_and_follows_changes(win, spinner):
    win.open_compact_mixer()
    cm = win.compact
    assert cm.isVisible() and cm.items
    item = cm.items[0][0]
    slot = win.slot_of(item)
    k = cm.mix()
    cm.set_gain(item, -6.0)
    l_, r_ = win.mix_lr(k, slot)
    assert max(l_, r_) > 0
    win.mix_set_lr(k, slot, 0, 0)                    # changed elsewhere → the compact mixer notices
    cm.tick()
    assert cm.sig == cm.state_sig()
    cm.close()


def test_colour_tags_fall_back_to_partner(win):
    win.set_label("stripcol:Analog-A-1", "#3fb950")
    assert win.item_colour("Analog-A-1") == "#3fb950"
    assert win.item_colour("Analog-A-2") == "#3fb950"
    assert win.item_colour("Analog-A-3") == ""
    win.set_label("in0", "Guitar")
    assert win.item_title("Analog-A-1") == "Guitar"


def test_signal_map_paints_with_colour_tags(win, spinner):
    win.set_label("stripcol:Analog-A-1", "#f85149")
    for m in win.findChildren(mc.SignalMap):
        m.grab()                                     # paints without errors


def test_auto_scene_applies_and_reverts(win, spinner, monkeypatch):
    running = set()
    monkeypatch.setattr(mc, "running_programs", lambda: running)
    win.app_state["auto_scenes"] = [{"proc": "obs", "label": "OBS", "scene": "record", "revert": True}]
    applied = []
    monkeypatch.setattr(win, "apply_scene", lambda sid: applied.append(sid))
    restored = []
    monkeypatch.setattr(win, "apply_snapshot", lambda snap: restored.append(snap))
    win.check_auto_scenes()
    assert applied == []
    running.add("obs")
    win.check_auto_scenes()
    win.check_auto_scenes()                          # still running: not applied twice
    assert applied == ["record"]
    running.clear()
    win.check_auto_scenes()
    assert len(restored) == 1


def test_auto_scene_card_adds_rules(win):
    win.auto_app.setCurrentIndex(0)
    win.auto_scene.setCurrentIndex(0)
    win.add_auto_scene()
    win.add_auto_scene()                             # same app twice → one rule
    assert len(win.app_state["auto_scenes"]) == 1
    assert "Already set" in win.statusBar().currentMessage()
    assert win.auto_rows.count() == 4
    win.auto_scene.setCurrentIndex(1)
    win.add_auto_scene()                             # different scene → the rule changes, still one row
    assert len(win.app_state["auto_scenes"]) == 1
    assert win.app_state["auto_scenes"][0]["scene"] == win.auto_scene.itemData(1)
    assert "Changed" in win.statusBar().currentMessage()


def test_auto_scene_shows_on_screen_when_app_is_behind(win, monkeypatch):
    shown = []
    monkeypatch.setattr(win.osd, "text", lambda icon, t: shown.append(t))
    monkeypatch.setattr(win, "isActiveWindow", lambda: False)
    win.announce("x", "Guitarix opened → Guitar")
    assert shown == ["Guitarix opened → Guitar"]
    monkeypatch.setattr(win, "isActiveWindow", lambda: True)
    win.announce("x", "again")
    assert len(shown) == 1


def test_running_programs_lists_this_process():
    progs = mc.running_programs()
    assert any(p.startswith("python") for p in progs)


def test_front_and_rear_jacks_keep_their_own_names(win):
    hv = win.hw_view
    assert hv.jack_tag_key("front1") == "in0:a" and hv.jack_tag_key("line1") == "in0:b"
    win.set_label("in0:a", "Jazz Bass")
    assert win.label("in0") == "Jazz Bass"           # front is in use → that's the input's name
    win.set_label("in0:b", "Keyboard")
    assert win.label("in0") == "Jazz Bass"           # naming the rear jack doesn't rename the input
    win.toggle_input_side(0)                         # pressed F/R on the Mbox
    assert win.input_side(0) == "b" and win.label("in0") == "Keyboard"
    assert win.item_title("Analog-A-1") == "Keyboard"
    win.set_label("in0", "Synth")                    # renaming the input renames the jack in use
    assert win.label("in0:b") == "Synth" and win.label("in0:a") == "Jazz Bass"
    win.toggle_input_side(0)
    assert win.label("in0") == "Jazz Bass"


def test_old_single_names_move_to_the_jack_in_use(win):
    win.labels = {"in1": "Mic"}
    win.migrate_input_names()
    assert win.labels["in1:a"] == "Mic" and "in1:b" not in win.labels


def test_switch_buttons_on_the_box_toggle(win, monkeypatch):
    called = []
    monkeypatch.setattr(win, "toggle_input_side", lambda r: called.append(r))
    from PySide6 import QtCore, QtGui
    hv = win.hw_view
    for jid, r, _, _ in hv.shapes:
        if jid == "sw3":
            s, ox = hv._scale()
            pos = QtCore.QPointF(r.center().x() * s + ox, r.center().y() * s)
            ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, pos, pos, QtCore.Qt.LeftButton,
                                   QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
            hv.mousePressEvent(ev)
    assert called == [2]
    hv.grab()                                        # paints with the new button labels
