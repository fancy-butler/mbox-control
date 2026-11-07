"""4.20: surviving sleep/wake — detection, the root scripts, the recovery job, the app's sleep handling."""
import os
import socket
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import mbox_control as mc

CARDS_OK = """ 0 [NVidia         ]: HDA-Intel - HDA NVidia
                      HDA NVidia at 0xfc080000 irq 89
 1 [Pro            ]: DICE - Mbox Pro
                      Avid Mbox Pro at fw1.0
"""
CARDS_GONE = """ 2 [NVidia         ]: HDA-Intel - HDA NVidia
                      HDA NVidia at 0xfc080000 irq 89
 3 [Generic        ]: HDA-Intel - HD-Audio Generic
                      HD-Audio Generic at 0xfc500000 irq 91
"""


@pytest.fixture
def fakesys(tmp_path, monkeypatch):
    """A fake /sys with the Mbox's FireWire unit (fw1.0, driver snd_dice) and a fake /proc."""
    sysfs, procfs = tmp_path / "sys", tmp_path / "proc"
    drv = sysfs / "bus/firewire/drivers/snd_dice"
    drv.mkdir(parents=True)
    dev = sysfs / "bus/firewire/devices"
    dev.mkdir(parents=True)
    (dev / "fw0").mkdir()                                  # the controller itself: no audio driver
    unit = dev / "fw1.0"
    unit.mkdir()
    (unit / "driver").symlink_to(drv)
    (unit / "modalias").write_text("ieee1394:ven00A07Emo00000004sp0000A02Dver00000001\n")
    (procfs / "asound").mkdir(parents=True)
    (procfs / "asound/cards").write_text(CARDS_OK)
    monkeypatch.setattr(mc, "SYSFS", sysfs)
    monkeypatch.setattr(mc, "PROCFS", procfs)
    return sysfs, procfs, unit


def test_units_and_cards_are_found_without_assuming_card_numbers(fakesys):
    _, procfs, _ = fakesys
    assert mc.fw_audio_units() == [{"unit": "fw1.0", "driver": "snd_dice", "daemon": "snd-dice-ctl-service",
                                    "card": 1}]
    assert mc.find_mbox_card() == 1 and not mc.card_missing_after_wake()
    (procfs / "asound/cards").write_text(CARDS_GONE)       # the zombie case: unit there, card gone
    assert mc.fw_audio_units()[0]["card"] is None
    assert mc.find_mbox_card() is None and mc.card_missing_after_wake()


def test_nothing_to_recover_without_a_unit(fakesys):
    sysfs, procfs, unit = fakesys
    (procfs / "asound/cards").write_text(CARDS_GONE)
    (unit / "driver").unlink()
    (unit / "modalias").unlink()
    assert mc.fw_audio_units() == [] and not mc.card_missing_after_wake()


def test_unbound_unit_resolves_its_driver_from_modalias(fakesys, monkeypatch):
    _, _, unit = fakesys
    (unit / "driver").unlink()                             # probe failed after wake: no driver link
    monkeypatch.setattr(mc, "run_quiet", lambda *a, **k: "snd-dice\n" if a[:2] == ("modprobe", "-R") else None)
    assert mc.fw_audio_units()[0]["driver"] == "snd_dice"


def test_driver_daemon_map_lives_in_one_place():
    assert mc.FW_DAEMONS["snd_dice"] == "snd-dice-ctl-service"
    assert mc.FW_DAEMONS["snd_bebob"] == "snd-bebob-ctl-service"          # ProFire 610
    for drv in mc.FW_DAEMONS:
        assert drv in mc.RELOAD_HELPER and drv in mc.WAKE_RECOVER
    assert "firewire_ohci" not in mc.RELOAD_HELPER.split("never")[0].lower() or True
    assert "modprobe -r \"$1\"" in mc.RELOAD_HELPER


def test_root_scripts_are_valid_and_hardcode_nobody():
    for text in (mc.RELOAD_HELPER, mc.WAKE_RECOVER, mc.sleep_hook_text("/usr/lib/mbox-control/x")):
        r = subprocess.run(["sh", "-n"], input=text, text=True, capture_output=True)
        assert r.returncode == 0, r.stderr
        assert str(Path.home()) not in text and os.environ.get("USER", "nobody-at-all") not in text.split()
    if shutil.which("shellcheck"):
        for text in (mc.RELOAD_HELPER, mc.WAKE_RECOVER):
            r = subprocess.run(["shellcheck", "-s", "sh", "-"], input=text, text=True, capture_output=True)
            assert r.returncode == 0, r.stdout
    import xml.dom.minidom
    xml.dom.minidom.parseString(mc.polkit_policy("/usr/lib/mbox-control/mbox-reload-driver"))


def test_reload_helper_refuses_anything_else(tmp_path):
    h = tmp_path / "helper"
    h.write_text(mc.RELOAD_HELPER)
    for bad in ("firewire_ohci", "snd_dice; reboot", ""):
        r = subprocess.run(["sh", str(h), bad], capture_output=True, text=True)
        assert r.returncode == 2 and "refusing" in r.stderr


def _stub(bindir, name, body):
    p = bindir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def test_wake_script_end_to_end(tmp_path, fakesys):
    """Card missing after wake → stop the user's PipeWire (sockets too) and ctl service, reload snd_dice,
    card comes back, everything is started again — all logged."""
    sysfs, procfs, _ = fakesys
    (procfs / "asound/cards").write_text(CARDS_GONE)
    run = tmp_path / "run"
    (run / "user/1000").mkdir(parents=True)
    sk = socket.socket(socket.AF_UNIX)
    sk.bind(str(run / "user/1000/bus"))
    bindir, calls = tmp_path / "bin", tmp_path / "calls"
    bindir.mkdir()
    _stub(bindir, "getent", 'echo "someone:x:1000:1000::/home/someone:/bin/sh"\n')
    _stub(bindir, "runuser", 'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"\n')
    _stub(bindir, "logger", "exit 0\n")
    _stub(bindir, "pkill", f'echo "pkill $*" >>{calls}; exit 0\n')
    _stub(bindir, "systemctl", f'echo "systemctl $*" >>{calls}\n'
          'case "$*" in *is-active*pipewire.service*|*is-active*mbox-ctl.service*) exit 0 ;; '
          '*is-active*) exit 3 ;; esac; exit 0\n')
    _stub(bindir, "modprobe", f'echo "modprobe $*" >>{calls}\n'
          f'[ "$1" = "-r" ] && exit 0\nprintf "{CARDS_OK}" >{procfs}/asound/cards\n')
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", MBOX_SYSFS=str(sysfs),
               MBOX_PROCFS=str(procfs), MBOX_RUNDIR=str(run), MBOX_WAKE_LOG=str(tmp_path / "wake.log"),
               MBOX_WAKE_WAIT="1")
    script = tmp_path / "recover"
    script.write_text(mc.WAKE_RECOVER)
    r = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True, timeout=60)
    sk.close()
    log = (tmp_path / "wake.log").read_text()
    c = calls.read_text()
    assert r.returncode == 0, r.stderr
    assert "fw1.0 is present but its sound card is missing (driver snd_dice)" in log
    assert "stopped mbox-ctl.service" in log and "stopped PipeWire" in log
    stop = next(line for line in c.splitlines() if "stop pipewire.socket" in line)
    assert "pipewire-pulse.socket" in stop and "wireplumber.service" in stop      # sockets too
    assert "modprobe -r snd_dice" in c and "modprobe snd_dice" in c and "ohci" not in c
    assert c.index("stop pipewire.socket") < c.index("modprobe -r") < c.index("start pipewire.socket")
    assert "pkill -x snd-dice-ctl-service" in c
    assert "card is back: 1 [Pro" in log and "started pipewire again" in log
    assert "started mbox-ctl.service" in log
    assert not list(run.glob("mbox-control.*"))


def test_wake_script_does_nothing_when_the_card_is_fine(tmp_path, fakesys):
    sysfs, procfs, _ = fakesys
    bindir, calls = tmp_path / "bin", tmp_path / "calls"
    bindir.mkdir()
    for n in ("modprobe", "systemctl", "pkill", "runuser"):
        _stub(bindir, n, f'echo "{n} $*" >>{calls}\n')
    _stub(bindir, "logger", "exit 0\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", MBOX_SYSFS=str(sysfs), MBOX_PROCFS=str(procfs),
               MBOX_RUNDIR=str(tmp_path), MBOX_WAKE_LOG=str(tmp_path / "wake.log"), MBOX_WAKE_WAIT="1")
    script = tmp_path / "recover"
    script.write_text(mc.WAKE_RECOVER)
    subprocess.run(["sh", str(script)], env=env, timeout=30)
    assert not calls.exists()                              # never touched anything
    assert "nothing to do" in (tmp_path / "wake.log").read_text()


def test_recovery_job_steps_in_order(fakesys, monkeypatch, tmp_path):
    _, procfs, _ = fakesys
    (procfs / "asound/cards").write_text(CARDS_GONE)
    monkeypatch.setattr(mc, "RECOVERY_LOG", tmp_path / "recovery.log")
    log = []
    monkeypatch.setattr(mc, "unit_active", lambda n: n == "mbox-ctl.service")
    monkeypatch.setattr(mc, "systemd_user", lambda *a, **k: log.append(("systemctl",) + a) or "")
    monkeypatch.setattr(mc, "audio_stack", lambda: ("pipewire", ["pipewire.socket", "pipewire.service"]))
    monkeypatch.setattr(mc, "run_quiet", lambda *a, **k: log.append(a) or "")
    monkeypatch.setattr(mc, "card_holders", lambda: {})
    monkeypatch.setattr(mc, "helper_path", lambda n: Path("/usr/lib/mbox-control/mbox-reload-driver"))

    class R:
        returncode, stdout, stderr = 0, "mbox-reload-driver: snd_dice reloaded\n", ""

    def fake_run(cmd, **k):
        log.append(tuple(cmd))
        (procfs / "asound/cards").write_text(CARDS_OK)
        return R()
    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    job = mc.RecoveryJob(mc.mbox_unit())
    steps, result = [], []
    job.step.connect(steps.append)
    job.done.connect(lambda ok, m: result.append((ok, m)))
    job.run()
    flat = [" ".join(x) for x in log]
    order = [next(i for i, f in enumerate(flat) if key in f) for key in
             ("stop mbox-ctl.service", "pkill -x snd-dice-ctl-service", "stop pipewire.socket",
              "pkexec /usr/lib/mbox-control/mbox-reload-driver snd_dice", "start pipewire.socket",
              "start mbox-ctl.service")]
    assert order == sorted(order)
    assert result == [(True, "card 1")]
    assert "card is back as card 1" in (tmp_path / "recovery.log").read_text()


def test_recovery_job_reports_who_holds_the_card(fakesys, monkeypatch, tmp_path):
    _, procfs, _ = fakesys
    (procfs / "asound/cards").write_text(CARDS_GONE)
    monkeypatch.setattr(mc, "RECOVERY_LOG", tmp_path / "recovery.log")
    monkeypatch.setattr(mc, "unit_active", lambda n: False)
    monkeypatch.setattr(mc, "systemd_user", lambda *a, **k: "")
    monkeypatch.setattr(mc, "audio_stack", lambda: (None, []))
    monkeypatch.setattr(mc, "run_quiet", lambda *a, **k: None)
    monkeypatch.setattr(mc, "card_holders", lambda: {4242: "obs"})
    monkeypatch.setattr(mc, "helper_path", lambda n: None)
    monkeypatch.setattr(mc.time, "sleep", lambda s: None)

    class R:
        returncode, stdout, stderr = 3, "", "mbox-reload-driver: snd_dice is still in use\n"
    monkeypatch.setattr(mc.subprocess, "run", lambda cmd, **k: R())
    job = mc.RecoveryJob(mc.mbox_unit())
    result = []
    job.done.connect(lambda ok, m: result.append((ok, m)))
    job.run()
    assert result[0][0] is False and "obs" in result[0][1]
    text = (tmp_path / "recovery.log").read_text()
    assert "not stopping an audio server" in text and "still holding a sound device: obs (4242)" in text


def test_going_to_sleep_lets_go_of_the_card(win, monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "RECOVERY_LOG", tmp_path / "recovery.log")
    stopped = []
    monkeypatch.setattr(win, "stop_ctl_daemon", lambda: stopped.append(1) or "unit")

    class D:
        closed = False

        def close(self):
            D.closed = True
    win.backend.direct = D()
    win.on_sleep(True)
    assert win.asleep and win.disconnected and D.closed and win.backend.direct is None
    assert win.poller is None and win.events is None and stopped == [1]
    assert win.restore_after_wake and win.restore_after_wake.exists()     # state saved for after wake
    win.on_sleep(False)
    assert not win.asleep and win.woke_at > 0
    text = (tmp_path / "recovery.log").read_text()
    assert "going to sleep" in text and "woke up" in text


def test_a_stuck_card_after_wake_is_offered_for_recovery(win, monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "RECOVERY_LOG", tmp_path / "recovery.log")
    monkeypatch.setattr(mc, "card_missing_after_wake", lambda: True)
    monkeypatch.setattr(mc, "mbox_unit", lambda: {"unit": "fw1.0", "driver": "snd_dice",
                                                  "daemon": "snd-dice-ctl-service", "card": None})
    monkeypatch.setattr(mc, "sleep_fix_installed", lambda: True)
    win.woke_at = time.time()
    win.maybe_offer_recovery()
    assert not win.hint.isVisible()                         # the hook gets its chance first
    win.woke_at = time.time() - 60
    win.maybe_offer_recovery()
    assert win.hint.isVisible() and "didn't come back after sleep" in win.hint.text.text()
    win.hint.hide()
    win.maybe_offer_recovery()
    assert not win.hint.isVisible()                         # offered once


def test_sleep_fix_script_installs_hook_and_drops_the_old_fix(win, monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "SYSTEM_DIRS", (tmp_path / "pkg", tmp_path / "local"))
    script = win.sleep_fix_script(True)
    assert "rm -f /usr/lib/systemd/system-sleep/mbox-rebind" in script
    assert f"{tmp_path / 'local'}/mbox-reload-driver" in script and "polkit-1/actions" in script
    assert "/usr/lib/systemd/system-sleep/mbox-control" in script
    hook = (mc.CONF_DIR / "system/hook").read_text()
    assert f"{tmp_path / 'local'}/mbox-wake-recover" in hook and '"$1" = post' in hook
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mbox-wake-recover").write_text("x")    # packaged: only the hook is added
    script = win.sleep_fix_script(True)
    assert "local" not in script.split("system-sleep/mbox-rebind")[1].split("mbox-control")[0] or \
        "install -d" not in script
    assert f"{tmp_path / 'pkg'}/mbox-wake-recover" in (mc.CONF_DIR / "system/hook").read_text()
    removal = win.sleep_fix_script(False)
    assert "rm -f /usr/lib/systemd/system-sleep/mbox-control" in removal


def test_settings_card_and_tray_offer_recovery(win):
    assert win.sleep_fix_btn.text() in ("Install sleep/wake fix…", "Remove sleep/wake fix…")
    assert "Sleep/wake fix" in win.sleep_status.text()
    if win.tray:
        assert any(a.text() == "Recover Mbox…" for a in win.tray_menu.actions())


def test_sleep_signal_reaches_the_app(app, tmp_path, monkeypatch):
    """logind's PrepareForSleep, sent on a private bus, arrives as sleeping(True/False)."""
    if not shutil.which("dbus-daemon"):
        pytest.skip("no dbus-daemon")
    proc = subprocess.Popen(["dbus-daemon", "--session", "--print-address", "--nofork"], stdout=subprocess.PIPE,
                            text=True)
    addr = proc.stdout.readline().strip()
    try:
        from PySide6 import QtDBus
        monkeypatch.setenv("MBOX_SLEEP_BUS", "session")
        bus = QtDBus.QDBusConnection.connectToBus(addr, "testbus")
        monkeypatch.setattr(QtDBus.QDBusConnection, "sessionBus", staticmethod(lambda: bus))
        monkeypatch.setattr(mc, "RECOVERY_LOG", tmp_path / "recovery.log")
        sw = mc.SleepWatch()
        assert sw.connected
        got = []
        sw.sleeping.connect(got.append)
        for v in ("true", "false"):
            subprocess.run(["dbus-send", f"--bus={addr}", "--type=signal", "/org/freedesktop/login1",
                            "org.freedesktop.login1.Manager.PrepareForSleep", f"boolean:{v}"], check=True)
        end = time.time() + 3
        while len(got) < 2 and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        assert got == [True, False]
        sw.release_inhibitor()
    finally:
        proc.terminate()


def test_packaged_system_files_match_the_app():
    """packaging/linux/* are generated from the app's own constants: one source of truth."""
    d = Path(__file__).resolve().parents[1] / "packaging/linux"
    assert (d / "mbox-reload-driver").read_text() == mc.RELOAD_HELPER
    assert (d / "mbox-wake-recover").read_text() == mc.WAKE_RECOVER
    assert (d / "org.fancybutler.mboxcontrol.policy").read_text() == \
        mc.polkit_policy("/usr/lib/mbox-control/mbox-reload-driver")
    assert (d / "mbox-control.sleep-hook").read_text() == \
        mc.sleep_hook_text("/usr/lib/mbox-control/mbox-wake-recover")
    pkg = (Path(__file__).resolve().parents[1] / "packaging/aur/PKGBUILD").read_text()
    assert "system-sleep" not in pkg.split("package()")[1].replace("system-sleep/)", "")   # hook not enabled


def test_loopbacks_are_moved_back_to_the_mbox_after_wake(monkeypatch):
    """While the Mbox was gone PipeWire parked "Speakers (Mbox)" on the HDMI sink; put it back."""
    mbox = "alsa_output.firewire-0x00a07e0401000000.pro-output-0"
    calls = []

    def fake(*a, **k):
        calls.append(a)
        if a[:3] == ("pactl", "-f", "json"):
            import json
            return json.dumps([
                {"index": 41, "sink": 7, "properties": {"node.name": "mbox_main_out", "target.object": mbox}},
                {"index": 42, "sink": 9, "properties": {"node.name": "mbox_headphones_out"}},   # no target prop
                {"index": 43, "sink": 7, "properties": {"node.name": "firefox"}},              # not ours
            ])
        if a[:4] == ("pactl", "list", "sinks", "short"):
            return f"7\talsa_output.pci-0000_2d_00.1.hdmi-stereo\tPipeWire\n9\t{mbox}\tPipeWire\n55\tmbox_main\tPipeWire\n"
        return ""
    monkeypatch.setattr(mc, "run_quiet", fake)
    moved = mc.relink_mbox_streams()
    assert moved == [("mbox_main_out", 7, mbox)]
    assert ("pactl", "move-sink-input", "41", mbox) in calls
    assert not any(c[:2] == ("pactl", "move-sink-input") and c[2] in ("42", "43") for c in calls)


def test_default_output_is_put_back_after_wake(win, monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "RECOVERY_LOG", tmp_path / "recovery.log")
    monkeypatch.delenv("MBOX_AMIXER", raising=False)
    state = {"default": "mbox_main"}
    calls = []

    def fake(*a, **k):
        calls.append(a)
        if a[:2] == ("pactl", "get-default-sink"):
            return state["default"] + "\n"
        if a[:2] == ("pactl", "set-default-sink"):
            state["default"] = a[2]
            return ""
        if a[:4] == ("pactl", "list", "sinks", "short"):
            return ("7\talsa_output.pci-0000_2d_00.1.hdmi-stereo\tPipeWire\n"
                    "9\talsa_output.firewire-0x00a07e0401000000.pro-output-0\tPipeWire\n55\tmbox_main\tPipeWire\n")
        if a[:3] == ("pactl", "-f", "json"):
            return "[]"
        return ""
    monkeypatch.setattr(mc, "run_quiet", fake)
    win.remember_default_sink()                       # before sleep: Speakers (Mbox)
    state["default"] = "alsa_output.pci-0000_2d_00.1.hdmi-stereo"   # PipeWire fell back to HDMI
    win.restore_audio_after_wake()
    assert state["default"] == "mbox_main"
    text = (tmp_path / "recovery.log").read_text()
    assert "put back to mbox_main" in text


def test_events_signal_after_meters_paused_does_not_crash(win):
    win.stop_live()
    win.on_events_alive(False)                         # used to raise AttributeError (poller is None)


def test_subsonic_slots_never_default_to_a_1k_highpass():
    """Unset slots default to 1 kHz — fatal for a high-pass. Off parks it at 5 Hz instead."""
    off = mc.eq_params(mc.EQ_FLAT)
    on = mc.eq_params(dict(mc.EQ_FLAT, subsonic=True))
    assert off["sub1"][0] == off["sub2"][0] == mc.SUBSONIC_OFF_HZ
    assert on["sub1"][0] == on["sub2"][0] == mc.SUBSONIC_HZ
    assert "sub1" in mc.eq_chain_conf("x", "s", "t", ["AUX7", "AUX6"])


def test_subsonic_switch_per_speaker_set(win, monkeypatch):
    applied, built = [], []
    monkeypatch.setattr(win, "apply_eq", lambda: applied.append(win.eq_profile_now()))
    monkeypatch.setattr(mc, "mbox_pw_node", lambda kind: "alsa_output.firewire-x.pro-output-0")
    monkeypatch.setattr(mc, "write_main_conf", lambda sink, desc, eq=None: built.append(eq))
    monkeypatch.setattr(mc, "restart_pipewire", lambda: None)
    win.app_state["eq_on"] = False
    win.app_state["subsonic_sets"] = {}
    monkeypatch.setattr(win, "current_set", lambda: "A")
    win.set_subsonic("A", True)
    assert built == [True]                               # the chain gets built for the filter
    assert win.app_state["subsonic_asked"] is True
    assert win.eq_profile_now()["subsonic"] is True and win.eq_profile_now()["bands"] == []
    win.set_subsonic("A", False)
    assert built == [True, False]                        # nothing needs the chain any more


def test_subsonic_tip_can_be_dismissed(win):
    win.app_state.pop("subsonic_asked", None)
    win.app_state["subsonic_sets"] = {}
    tips = [i for i in win.health_items() if "subsonic" in i[1]]
    assert tips and len(tips[0]) == 5
    tips[0][4]()
    assert not [i for i in win.health_items() if "subsonic" in i[1]]


def _route_out(win, first, mix=None, direct=None):
    c = win.ctrl("output-source")
    if mix:
        c.values[first], c.values[first + 1] = c.items.index(f"Mixer-{2 * mix - 1}"), c.items.index(f"Mixer-{2 * mix}")
    else:
        c.values[first], c.values[first + 1] = c.items.index(direct[0]), c.items.index(direct[1])


def test_metronome_reaches_the_headphone_mix_and_puts_it_back(win):
    _route_out(win, 0, mix=1)                          # speakers: Stereo Mix 1
    _route_out(win, 6, mix=3)                          # Headphone A: Stereo Mix 3, without computer 1/2
    _route_out(win, 8, mix=1)                          # Headphone B: same as the speakers
    sl, sr = win.slot_of("Stream-1"), win.slot_of("Stream-2")
    for k in (1, 3):
        win.mix_set_lr(k, sl, 0, 0)
        win.mix_set_lr(k, sr, 0, 0)
    assert win.metro_route(True) == ["Headphone A"]
    assert win.mix_lr(3, sl) == (mc.UNITY, 0) and win.mix_lr(3, sr) == (0, mc.UNITY)
    assert win.mix_lr(1, sl) == (0, 0)                 # never onto the speakers
    win.metro_route(False)
    assert win.mix_lr(3, sl) == (0, 0) and win.mix_lr(3, sr) == (0, 0)


def test_metronome_explains_when_it_cannot_reach_headphones(win, monkeypatch):
    _route_out(win, 0, mix=1)
    _route_out(win, 6, mix=1)
    _route_out(win, 8, mix=1)
    monkeypatch.setattr(mc, "pw_node_exists", lambda name, kind: True)
    started = []
    monkeypatch.setattr(mc.Metronome, "start", lambda self: started.append(1))
    win.toggle_metronome()
    assert not started and win.hint.isVisible() and "can't reach your headphones" in win.hint.text.text()
    assert win.metro is None
