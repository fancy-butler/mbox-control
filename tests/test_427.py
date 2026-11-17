"""4.27: the Karaoke scene — mics direct to the speakers, song underneath, Mbox reverb, and back again."""
import mbox_control as mc


def _routing(win):
    out = {n: list(c.values) for n, c in win.ctrls.items() if "meter" not in c.name}
    src = win.ctrl("output-source")
    out[src.numid] = out[src.numid][:15]
    return out


def _setup(win, **kw):
    k = win.karaoke_cfg()
    k.update({"active": False, "mics": ["Analog-A-2", "Analog-A-5", "Analog-A-6"], "levels": {},
              "balance": 0, "condenser": False, "reverb": True, "reverb_type": 3, "reverb_amount": 35})
    k.update(kw)
    return k


def test_levels_and_balance():
    k = dict(mc.KARAOKE_DEFAULT)
    mic, song = mc.karaoke_levels(k, "Analog-A-2")
    assert mic == mc.UNITY and abs(mc.gain_db(song) + 6) < 0.1
    k["balance"] = 12                          # all the way to "more mics": song down, mics stay at 0 dB
    mic, song = mc.karaoke_levels(k, "x")
    assert mic == mc.UNITY and abs(mc.gain_db(song) + 18) < 0.1
    k["balance"] = -12                         # "more song": mics down, song stays
    mic, song = mc.karaoke_levels(k, "x")
    assert abs(mc.gain_db(mic) + 12) < 0.1 and abs(mc.gain_db(song) + 6) < 0.1
    assert mc.karaoke_send({"reverb_amount": 0}) == 0
    assert mc.karaoke_send({"reverb_amount": 100}) == mc.UNITY


def test_karaoke_routes_mics_song_and_reverb_to_outputs_1_2(win):
    win.reload_state()
    win.write(win.ctrl("phantom-powering"), [True])
    _setup(win)
    win.apply_scene("karaoke")
    kind, k, _ = win.pair_source(0)
    assert (kind, k) == ("mix", 1)
    for m in ("Analog-A-2", "Analog-A-5", "Analog-A-6"):
        l, r = win.mix_lr(1, win.slot_of(m))
        assert l == r and l > 0                          # centred, in the speakers' mix
        fx = [win.ctrl("mixer-source-gain", b).values[win.slot_of(m)] for b in mc.FX_BUS]
        assert all(x > 0 for x in fx)                    # and into the reverb
    assert win.mix_lr(1, win.slot_of("Analog-A-1")) == (0, 0)
    pcl = win.mix_lr(1, win.slot_of(mc.PC_LEFT))[0]
    assert abs(mc.gain_db(pcl) + 6) < 0.1                # song 6 dB under
    assert win.ctrl("mixer-source-gain", mc.FX_BUS[0]).values[win.slot_of(mc.PC_LEFT)] == 0   # no reverb on the song
    assert win.mix_lr(1, win.slot_of("Reverb-1"))[0] == mc.UNITY
    assert win.ctrl_item("reverb-type") == "Hall-1"
    assert not win.ctrl("phantom-powering").values[0]    # +48V off for dynamic/wireless mics
    st = win.ctrl("stream-source")
    assert st.items[st.values[4]] == "Analog-A-5"         # the karaoke app can still hear mic 5 on channel 5
    assert win.karaoke_cfg()["active"]


def test_condenser_keeps_phantom(win):
    win.reload_state()
    win.write(win.ctrl("phantom-powering"), [True])
    _setup(win, condenser=True)
    win.apply_scene("karaoke")
    assert win.ctrl("phantom-powering").values[0]
    win.leave_karaoke()


def test_leaving_karaoke_puts_back_the_routing(win):
    win.reload_state()
    win.apply_scene("listen")
    win.reload_state()
    before = _routing(win)
    _setup(win)
    win.apply_scene("karaoke")
    assert _routing(win) != before
    win.apply_scene("listen")                             # switching scenes leaves karaoke first
    win.reload_state()
    assert not win.karaoke_cfg()["active"]
    assert _routing(win) == before


def test_leaving_never_turns_phantom_back_on(win):
    win.reload_state()
    win.write(win.ctrl("phantom-powering"), [True])
    _setup(win)
    win.apply_scene("karaoke")
    win.leave_karaoke()
    win.reload_state()
    assert not win.ctrl("phantom-powering").values[0]


def test_no_reverb_means_no_sends(win):
    win.reload_state()
    _setup(win, reverb=False)
    win.apply_scene("karaoke")
    assert not any(win.ctrl("mixer-source-gain", mc.FX_BUS[0]).values)
    assert win.mix_lr(1, win.slot_of("Reverb-1")) == (0, 0)
    win.leave_karaoke()


def test_live_balance_change_rewrites_the_mix(win):
    win.reload_state()
    _setup(win)
    win.apply_scene("karaoke")
    win.karaoke_cfg()["balance"] = 12
    win.karaoke_write()
    assert abs(mc.gain_db(win.mix_lr(1, win.slot_of(mc.PC_LEFT))[0]) + 18) < 0.1
    win.leave_karaoke()


def test_karaoke_comes_back_after_a_reset(win, spinner):
    win.reload_state()
    _setup(win)
    win.apply_scene("karaoke")
    # the Mbox forgets everything (power cycle): the marker is gone and the mix is empty
    win.route_pair_to_mix(0, 2)
    c = win.ctrl("output-source")
    vals = list(c.values)
    vals[win.MARKER_SLOT] = 0
    win.write(c, vals)
    win.reload_state()
    win.check_reset()
    spinner(0.5)
    win.reload_state()
    assert win.pair_source(0)[:2] == ("mix", 1)
    assert win.mix_lr(1, win.slot_of("Analog-A-2"))[0] > 0
    win.leave_karaoke()


def test_dialog_builds_and_cleans_up_its_meters(win):
    _setup(win)
    n = len(win.meters)
    d = win.karaoke_dialog()
    assert len(win.meters) == n + 6
    d.accept()
    assert len(win.meters) == n
