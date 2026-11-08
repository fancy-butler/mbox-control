"""Windows backend: the pipe protocol and the Linux-style view of Avid's driver, against a fake driver that
starts from a real parameter dump (tests/fixtures/win-driver-dump.json, read from an Mbox Pro on 2026-11-08)."""
import json
import queue
import time
from pathlib import Path

import pytest

import mbox_win as mw

DUMP = {int(k): v for k, v in json.loads((Path(__file__).parent / "fixtures/win-driver-dump.json").read_text()).items()}


FakeDriver = mw.FakeDriver


@pytest.fixture
def drv():
    return FakeDriver()


@pytest.fixture
def be(drv):
    link = mw.DriverLink(drv.req, drv.note).connect(timeout=2)
    return mw.WinBackend(link)


def by_name(ctrls, name, index=0):
    return next(c for c in ctrls.values() if c.name == name and c.index == index)


def test_encoding_is_byte_identical_to_avids_panel():
    """Messages captured from Avid's panel in the probe log."""
    assert mw.encode(2, -3).hex() == "1800000002000000fdffffff0c0000000000000000000000"
    assert mw.encode(1, -5, [[0, 1177]]).hex() == (
        "4400000001000000fbffffff38000000000000000a0000002c000000000000000a000000100000000000000002000000"
        "0000000010000000000000000200000099040000")
    assert mw.encode(0, -6, [[False, 11, None]]).hex() == (
        "4d00000000000000faffffff41000000000000000a00000035000000000000000a0000000d000000000000000100000000"
        "100000000000000002000000" + "0b000000" + "0c0000000000000000000000")


def test_decode_round_trip_of_every_type():
    body = [[True, 1, None], [-3, 2, None], [0.25, 3, None], ["Headphone A", 4, None], [[1.0, -200.0], 5, None]]
    kind, cmd, back = mw.decode(mw.encode(0, -6, body))
    assert (kind, cmd, back) == (0, -6, body)


def test_connect_reads_every_setting(be):
    assert len(be.link.params) == len(DUMP)
    assert be.link.get(0) == "5.10.0.6019" and be.link.get(4) == 48000.0


def test_layout_matches_the_dump():
    for m in range(6):
        assert DUMP[mw.mix_base(m) + 155] == "L/R"                       # every mix block ends the same way
    assert [DUMP[mw.out_param(p, 0)] for p in range(6)] == mw.OUT_ORDER
    assert DUMP[mw.strip(0, 0) + 2] == "1" and DUMP[mw.strip(0, 8) + 2] == "1"     # HW in 1, SW return 1


def test_controls_look_like_the_linux_driver(be):
    c = be.read_all()
    names = {x.name for x in c.values()}
    for n in ("clock-rate", "clock-source", "mixer-source-gain", "output-source", "output-source-meter",
              "spkr-button", "mono-button", "mute-button", "input-hp-filter", "reverb-type", "master-knob-assign"):
        assert n in names
    assert len([x for x in c.values() if x.name == "mixer-source-gain"]) == 16
    assert by_name(c, "clock-rate").items[by_name(c, "clock-rate").values[0]] == "48000"
    assert by_name(c, "clock-source").items[by_name(c, "clock-source").values[0]] == "Internal"


def test_output_pairs_read_as_mixes_returns_and_inputs(be):
    o = by_name(be.read_all(), "output-source")
    it = [o.items[v] for v in o.values]
    assert it[0:2] == ["Mixer-1", "Mixer-2"]                     # Outputs 1/2 ← Stereo Mix 1 (dump: 0)
    assert it[2:4] == ["Mixer-3", "Mixer-4"]                     # Outputs 3/4 ← Stereo Mix 2
    assert it[12:14] == ["Stream-3", "Stream-4"]                 # S/PDIF ← Software Returns 3/4 (dump: 7)
    assert it[6:8] == ["Mixer-9", "Mixer-10"]                    # Headphone A ← Stereo Mix 5 (dump: 4)
    assert it[8:10] == ["Analog-A-5", "Analog-A-6"]              # Headphone B ← Hardware Inputs 5/6 (dump: 12)


def test_mixer_gains_follow_the_db_pairs(be):
    c = be.read_all()
    left, right = by_name(c, "mixer-source-gain", 0), by_name(c, "mixer-source-gain", 1)
    assert left.values[0] == 16384 and right.values[0] == 0      # HW in 1: [0 dB, −∞] = hard left
    assert left.values[1] == right.values[1] == mw.db_to_lin(-13.018688617787966)
    assert left.values[8] == right.values[8] == mw.db_to_lin(-3.0102999566398125)     # SW return 1


def test_writing_a_fader_sends_one_param_and_reads_back(be, drv):
    c = be.read_all()
    left = by_name(c, "mixer-source-gain", 0)
    vals = list(left.values)
    vals[0] = 8192                                               # HW in 1, left side, −6 dB
    be.write(left, vals)
    assert drv.sets[-1] == {mw.strip(0, 0): [pytest.approx(-6.0206, abs=1e-3), -200.0]}
    assert be.read(left.numid).values[0] == 8192


def test_a_level_on_a_muted_channel_unmutes_it(be, drv):
    drv.params[125] = True
    be.link.params[125] = True
    c = be.read_all()
    left = by_name(c, "mixer-source-gain", 0)
    assert left.values[0] == 0                                   # muted reads as silent
    vals = list(left.values)
    vals[0] = 16384
    be.write(left, vals)
    assert drv.sets[-1][125] is False and drv.sets[-1][120][0] == pytest.approx(0.0)


def test_output_source_write_maps_pairs_and_refuses_split_pairs(be, drv):
    o = by_name(be.read_all(), "output-source")
    vals = list(o.values)
    vals[0], vals[1] = o.items.index("Stream-1"), o.items.index("Stream-2")
    be.write(o, vals)
    assert drv.sets[-1] == {57: 6}                               # Software Returns 1/2
    vals[0], vals[1] = o.items.index("Stream-2"), o.items.index("Stream-1")
    with pytest.raises(RuntimeError, match="whole pairs"):
        be.write(o, vals)


def test_buttons_and_filters(be, drv):
    c = be.read_all()
    be.write(by_name(c, "mono-button"), [1])
    be.write(by_name(c, "mute-button"), [2])
    be.write(by_name(c, "spkr-button"), [3])                     # Red = speakers B
    hpf = by_name(c, "input-hp-filter")
    be.write(hpf, [True, False, False, True])
    assert drv.params[mw.P_MONO] is True and drv.params[mw.P_DIM] is True and drv.params[mw.P_SPKR] == 2
    assert drv.params[46] is True and drv.params[49] is True and drv.params[47] is False


def test_front_panel_changes_arrive_as_control_changes(drv):
    got = queue.Queue()
    link = mw.DriverLink(drv.req, drv.note, on_change=lambda ids: got.put(ids)).connect(timeout=2)
    be = mw.WinBackend(link)
    link.meter_ids = []
    link.start()
    try:
        drv.press(mw.P_SPKR, 3)                                  # someone pressed Speaker select on the box
        ids = got.get(timeout=2)
        assert be.changed_controls(ids) == {be.view.id_spkr}
        assert be.read(be.view.id_spkr).values == [5]            # Orange = speakers C
    finally:
        link.stop()


def test_meters_are_polled_and_scaled(drv):
    link = mw.DriverLink(drv.req, drv.note).connect(timeout=2)
    be = mw.WinBackend(link)
    drv.params[mw.out_param(0, 6)] = [0.5, 999]
    link.start(meter_interval=0.01)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and be.read(be.view.id_out_m).values[0] != 2048:
            time.sleep(0.02)
        assert be.read(be.view.id_out_m).values[0] == 2048
    finally:
        link.stop()


def test_things_windows_cannot_do_say_so(be):
    c = be.read_all()
    with pytest.raises(RuntimeError, match="word clock"):
        be.write(by_name(c, "clock-source"), [1])
    with pytest.raises(RuntimeError):
        be.write(by_name(c, "stream-source"), [0] * 8)
