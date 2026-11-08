"""Mbox Probe — records how Avid's Mbox Pro control panel talks to its driver.

Starts Avid's panel (or attaches to it), then walks you through clicking each control once. Everything the
panel sends is written to a log file on your Desktop. Nothing is changed by this program; it only listens.
Send the log back so Mbox Control can learn to drive the Mbox without Avid's panel.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import frida

STEPS = [
    "Just wait — nothing to do (records what the panel reads when it starts)",
    "Hardware Inputs 1: move its FADER down a bit, then back up",
    "Hardware Inputs 1: turn its PAN knob left, then back to the middle",
    "Hardware Inputs 1: click M (mute), then click it again",
    "Hardware Inputs 1: click S (solo), then click it again",
    "Hardware Inputs 1-2: click the LINK button under them (on), then again (off)",
    "Hardware Inputs 8: move its fader down a bit, then back up",
    "Software Returns 1: move its fader down a bit, then back up",
    "Software Returns 8: move its fader down a bit, then back up",
    "Master: move the fader down a bit, then back up",
    "Master: turn Balance a little, then back",
    "Master: turn Width a little, then back",
    "Master: click L mute (M above L), then again; then R mute, then again",
    "Master: click SWAP, then again",
    "Master: click MONO, then again",
    "Click the 'Outputs 3/4' tab, then the 'Outputs 1/2' tab again",
    "Outputs 1/2: change 'Stereo Mix 1' to something else in its dropdown, then back to Stereo Mix 1",
    "Outputs 3/4: change its dropdown to something else, then back",
    "S/PDIF Out L/R: change its dropdown to something else, then back",
    "Headphone A: change its dropdown to something else, then back",
    "Headphone B: change its dropdown to something else, then back",
    "Hardware Input FX Sends: turn knob 1 up a little, then back down",
    "Software Return FX Sends: turn knob 1 up a little, then back down",
    "EFFECT: change 'Room 1' to another effect, then back to Room 1",
    "EFFECT: turn Duration a little, then back; then Volume a little, then back",
    "FX Returns: turn the left knob a little, then back; click its M, then again",
    "Click Tuner, look at it for a few seconds (play a note if an instrument is plugged in), close it",
    "Click Setup: change each setting there once and back (sample rate, clock, buffer, phantom, filters…), then close",
    "Click Flow: look at it for a few seconds, change one thing and back if you can, then close",
    "On the Mbox itself: press the front-panel buttons one after another (Mono, Dim, Speaker select, Multi…)",
    "On the Mbox itself: turn the MONITOR knob a little",
    "PRESETS: click 'Save as…', save a preset called probe, then Load it",
]

PANEL_PATHS = [
    r"C:\Program Files (x86)\Avid\Mbox Pro\Panel.exe",
    r"C:\Program Files\Avid\Mbox Pro\Panel.exe",
]


def desktop():
    d = Path(os.environ.get("USERPROFILE", ".")) / "Desktop"
    return d if d.is_dir() else Path.cwd()


def selftest():
    """CI: inject into Notepad and wait for the hook script to say hello."""
    dev = frida.get_local_device()
    pid = dev.spawn([r"C:\Windows\System32\notepad.exe"])
    session = dev.attach(pid)
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    got = threading.Event()
    script = session.create_script((here / "hook.js").read_text(encoding="utf-8"))
    script.on("message", lambda m, d: got.set() if m.get("type") == "send" and m["payload"].get("kind") == "hello"
              else print(m))
    script.load()
    dev.resume(pid)
    ok = got.wait(15)
    dev.kill(pid)
    print("selftest", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def main():
    if "--selftest" in sys.argv:
        selftest()
    print(__doc__)
    log_path = desktop() / f"mbox-probe-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    log = open(log_path, "w", encoding="utf-8")
    lock = threading.Lock()
    counts = {"call": 0, "pipe-out": 0, "pipe-in": 0}

    def write(rec):
        with lock:
            log.write(json.dumps(rec) + "\n")
            log.flush()

    def on_message(msg, _data):
        if msg.get("type") == "send":
            p = msg["payload"]
            counts[p.get("kind")] = counts.get(p.get("kind"), 0) + 1
            write(p)
            if p.get("kind") in ("hello", "module", "missing", "pipe-open"):
                print("  ·", p.get("kind"), p.get("data"))
        elif msg.get("type") == "error":
            write({"kind": "script-error", "data": msg})
            print("  ! probe error:", msg.get("description"))

    dev = frida.get_local_device()
    running = [p for p in dev.enumerate_processes() if p.name.lower() == "panel.exe"]
    if running:
        print("Avid's panel is already open — please CLOSE it, then press Enter (the probe has to start it,")
        input("so it sees the panel connect to the driver)… ")
    panel = next((p for p in PANEL_PATHS if Path(p).exists()), None)
    if not panel:
        panel = input("Couldn't find Avid's Panel.exe. Paste its full path: ").strip().strip('"')
    print(f"Starting {panel} …")
    pid = dev.spawn([panel], cwd=str(Path(panel).parent))
    session = dev.attach(pid)
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    script = session.create_script((here / "hook.js").read_text(encoding="utf-8"))
    script.on("message", on_message)
    script.load()
    dev.resume(pid)
    write({"kind": "start", "panel": panel, "pid": pid, "t": int(time.time() * 1000)})
    time.sleep(4)

    print("\nNow, one step at a time. Do what it says in Avid's panel, then press Enter.")
    print("Type s + Enter to skip a step you can't find. Type q + Enter to finish early.\n")
    for i, step in enumerate(STEPS, 1):
        write({"kind": "step", "n": i, "text": step, "t": int(time.time() * 1000)})
        ans = input(f"[{i}/{len(STEPS)}] {step}\n    → Enter when done: ").strip().lower()
        write({"kind": "step-done", "n": i, "answer": ans, "t": int(time.time() * 1000)})
        if ans == "q":
            break
        time.sleep(0.5)

    note = input("\nAnything worth telling Claude about this session (optional)? ").strip()
    write({"kind": "note", "text": note})
    try:
        session.detach()
    except frida.InvalidOperationError:
        pass
    log.close()
    print(f"\nDone. {counts.get('call', 0)} calls and {counts.get('pipe-out', 0)} pipe messages recorded.")
    print(f"The log is on your Desktop:\n    {log_path}\nSend that file to Claude. You can close Avid's panel now.")
    input("Press Enter to close this window.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — a window that vanishes with no message helps nobody
        import traceback
        traceback.print_exc()
        input(f"\nThe probe stopped: {e}\nPress Enter to close.")
