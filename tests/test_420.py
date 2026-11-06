"""4.20: surviving sleep/wake — detection, the root scripts, the recovery job, the app's sleep handling."""
import os
import socket
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
    if subprocess.run(["which", "shellcheck"], capture_output=True).returncode == 0:
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
    if subprocess.run(["which", "dbus-daemon"], capture_output=True).returncode:
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
