#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Fancy Butler
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY; see the
# GNU General Public License (the LICENSE file) for details.
"""
Mbox Control 4 — a modern control panel for the Avid Mbox 3 Pro on Linux.

Works through the controls that snd-dice-ctl-service adds to the ALSA card
(via `amixer` / `alsactl monitor`), and through PipeWire for sample rate,
latency, named inputs and recording. Nothing is written to the Mbox unless
you change something.

Hardware model (confirmed by decoding Avid's driver state):
  * 6 stereo mixes = internal buses (0,1) (2,3) … (10,11); 16384 = 0 dB, linear
    amplitude, constant-power pan (−3 dB centre). Avid's faders top out at 0 dB.
  * FX (reverb) send = buses 12/13, fed to output-source slots 10/11.
  * output-source slots: 0-5 Outputs 1-6, 6/7 Headphone A, 8/9 Headphone B,
    10/11 effect input, 12/13 S/PDIF out, 14/15 unused.
"""
import itertools
import json
import math
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QPointF, QRectF

APP_NAME = "Mbox Control"
VERSION = "4.24"
UPDATE_REPO = ""          # "owner/mbox-control" once the project is on GitHub; empty = no online checks
HOME = Path.home()
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
CONF_DIR = XDG_CONFIG / "mbox-control"
SCENES_DIR = CONF_DIR / "scenes"
PW_CONF_DIR = XDG_CONFIG / "pipewire/pipewire.conf.d"
LABELS_FILE = CONF_DIR / "labels.json"
STATE_FILE = CONF_DIR / "app-state.json"
ORIGINAL_SNAPSHOT = CONF_DIR / "original-state.json"
UNDO_SNAPSHOT = CONF_DIR / "before-last-scene.json"
SERVICE_PATH = Path(os.environ.get(
    "MBOX_CTL_SERVICE", HOME / "snd-firewire-ctl-services/target/release/snd-dice-ctl-service"))
if not SERVICE_PATH.exists():
    SERVICE_PATH = Path(shutil.which("snd-dice-ctl-service") or
                        next((str(p) for p in (Path("/usr/local/bin/snd-dice-ctl-service"),
                                               HOME / ".cargo/bin/snd-dice-ctl-service") if p.exists()),
                             str(SERVICE_PATH)))
AMIXER = os.environ.get("MBOX_AMIXER", "amixer")
ALSACTL = os.environ.get("MBOX_ALSACTL", "alsactl")

UNITY = 16384
GAIN_MAX = 65535
METER_MAX = 4095
METER_FLOOR_DB = -60.0
PC_LEFT, PC_RIGHT = "Stream-7", "Stream-8"        # the main "… (Mbox)" PipeWire output
REMOTE_PORT = 8765

STEREO_MIX = {k: (2 * (k - 1), 2 * (k - 1) + 1) for k in range(1, 7)}
FX_BUS = (12, 13)
OUTPUT_PAIRS = [  # name, first output-source slot, analog outputs (for trims) or None
    ("Outputs 1/2", 0, (1, 2)),
    ("Outputs 3/4", 2, (3, 4)),
    ("Outputs 5/6", 4, (5, 6)),
    ("Headphone A", 6, None),
    ("Headphone B", 8, None),
    ("S/PDIF out", 12, None),
]
SLOT_NAMES = {0: "Output 1", 1: "Output 2", 2: "Output 3", 3: "Output 4", 4: "Output 5",
              5: "Output 6", 6: "Headphone A L", 7: "Headphone A R", 8: "Headphone B L",
              9: "Headphone B R", 10: "Effect input L", 11: "Effect input R",
              12: "S/PDIF out L", 13: "S/PDIF out R", 14: "Unused", 15: "Unused"}
SPEAKERS = {"Green": ("A", "#3fb950"), "Red": ("B", "#f85149"), "Orange": ("C", "#e3a52a")}
LETTER_COLOUR = {"A": ("Green", "#3fb950"), "B": ("Red", "#f85149"), "C": ("Orange", "#e3a52a")}
RATES = [44100, 48000, 88200, 96000, 176400, 192000]
QUANTA = [32, 64, 128, 256, 512, 1024, 2048]
METER_CTRLS = ("mixer-source-meter", "output-source-meter", "stream-source-meter")
FAST_CTRLS = ("master-knob-value", "spkr-button", "mono-button", "mute-button")

VOLATILE = {"metering", "mixer-source-meter", "output-source-meter", "stream-source-meter",
            "mixer-out-saturation", "locked-clock-source", "slipped-clock-source",
            "master-knob-value", "nickname", "clock-rate"}

# ------------------------------------------------------------------ theme
ACCENT = "#5aa9ff"
BG0, BG1, BG2 = "#14161a", "#1b1e23", "#23272e"
TEXT, SUBTLE = "#e8eaed", "#8b93a1"
DARK = True
THEMES = {"dark": ("#14161a", "#1b1e23", "#23272e", "#e8eaed", "#8b93a1"),
          "light": ("#eef0f3", "#f8f9fb", "#ffffff", "#1b1e23", "#5f6673")}


def kde_colours():
    """(dark?, accent) from KDE's settings, or (None, None) if not on KDE."""
    path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kdeglobals"
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None, None
    sect, vals = "", {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            sect = line
        elif "=" in line:
            k, v = line.split("=", 1)
            vals[(sect, k.strip())] = v.strip()

    def rgb(v):
        try:
            r, g, b = (int(x) for x in v.split(",")[:3])
            return r, g, b
        except (ValueError, AttributeError):
            return None
    bg = rgb(vals.get(("[Colors:Window]", "BackgroundNormal")))
    acc = rgb(vals.get(("[General]", "AccentColor"))) or rgb(vals.get(("[Colors:Selection]", "BackgroundNormal")))
    dark = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) < 128 if bg else None
    return dark, ("#%02x%02x%02x" % acc) if acc else None


def set_theme(mode="system", accent="system"):
    global BG0, BG1, BG2, TEXT, SUBTLE, ACCENT, DARK
    kdark, kacc = kde_colours()
    dark = (kdark if kdark is not None else True) if mode == "system" else mode == "dark"
    BG0, BG1, BG2, TEXT, SUBTLE = THEMES["dark" if dark else "light"]
    DARK = dark
    ACCENT = kacc if accent == "system" and kacc else "#5aa9ff" if dark else "#1f6fd1"

def make_style():
    ar, ag, ab = (QtGui.QColor(ACCENT).red(), QtGui.QColor(ACCENT).green(), QtGui.QColor(ACCENT).blue())
    e = "255,255,255" if DARK else "0,0,0"
    strong = "white" if DARK else "#000000"
    hover = "#2c313a" if DARK else "#dfe3ea"
    scrollh = "{scrollh}" if DARK else "#c3c8d0"
    return f"""
QWidget {{ font-size: 10.5pt; color: {TEXT}; }}
QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG0}; }}
QListWidget#nav {{ background: {BG1}; border: none; padding-top: 12px; font-size: 11.5pt; outline: 0; }}
QListWidget#nav::item {{ padding: 11px 16px; border-radius: 10px; margin: 2px 10px; color: {SUBTLE}; }}
QListWidget#nav::item:selected {{ background: rgba({ar},{ag},{ab},0.18); color: {strong}; }}
QListWidget#nav::item:hover:!selected {{ background: rgba({e},0.06); }}
QFrame#card {{ background: {BG1}; border: 1px solid rgba({e},0.08); border-radius: 14px; }}
QFrame#strip {{ background: {BG2}; border-radius: 10px; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 20pt; font-weight: 700; }}
QLabel#title {{ font-size: 12.5pt; font-weight: 600; }}
QLabel#muted {{ color: {SUBTLE}; }}
QLabel#big {{ font-size: 26pt; font-weight: 700; }}
QPushButton {{ background: {BG2}; border: 1px solid rgba({e},0.12); border-radius: 8px;
               padding: 6px 12px; }}
QPushButton:hover {{ background: {hover}; }}
QPushButton:checked {{ background: {ACCENT}; color: #0b0d10; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #555; }}
QPushButton#primary {{ background: {ACCENT}; color: #0b0d10; font-weight: 600; border: none; }}
QPushButton#danger {{ background: #c2413b; color: white; font-weight: 600; border: none; }}
QPushButton#scene {{ text-align: left; padding: 12px 16px; border-radius: 12px;
                     background: rgba({ar},{ag},{ab},0.08); border: 1px solid rgba({ar},{ag},{ab},0.25); }}
QPushButton#scene:hover {{ background: rgba({ar},{ag},{ab},0.18); }}
QPushButton#mute, QPushButton#solo, QPushButton#link {{ padding: 3px; border-radius: 6px; font-weight: 600; }}
QPushButton#mute:checked {{ background: #c2413b; color: white; border-color: #c2413b; }}
QPushButton#solo:checked {{ background: #e3a52a; color: #111; border-color: #e3a52a; }}
QPushButton#phantom:checked {{ background: #c2413b; color: white; border-color: #c2413b; font-weight: 700; }}
QComboBox, QLineEdit, QSpinBox {{ background: {BG2}; border: 1px solid rgba({e},0.12);
                                  border-radius: 8px; padding: 5px 8px; }}
QComboBox QAbstractItemView {{ background: {BG2}; selection-background-color: {ACCENT};
                               selection-color: #0b0d10; }}
QTabBar::tab {{ background: transparent; padding: 8px 16px; color: {SUBTLE};
                border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {strong}; border-bottom: 2px solid {ACCENT}; }}
QTabWidget::pane {{ border: none; }}
QCheckBox::indicator {{ width: 18px; height: 18px; }}
QSlider::groove:horizontal {{ height: 6px; background: {BG2}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: white; width: 16px; margin: -6px 0; border-radius: 8px; }}
QStatusBar {{ background: {BG1}; color: {SUBTLE}; }}
QMenuBar {{ background: {BG1}; }}
QToolTip {{ background: {BG2}; color: {TEXT}; border: 1px solid rgba({e},0.15); padding: 6px; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background: transparent; width: 10px; height: 10px; }}
QScrollBar::handle {{ background: {scrollh}; border-radius: 5px; min-height: 30px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
"""


def apply_theme(app):
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, colour in ((QtGui.QPalette.Window, BG0), (QtGui.QPalette.Base, BG2),
                         (QtGui.QPalette.AlternateBase, BG1), (QtGui.QPalette.Button, BG2),
                         (QtGui.QPalette.WindowText, TEXT), (QtGui.QPalette.Text, TEXT),
                         (QtGui.QPalette.ButtonText, TEXT), (QtGui.QPalette.Highlight, ACCENT),
                         (QtGui.QPalette.HighlightedText, "#0b0d10"), (QtGui.QPalette.ToolTipBase, BG2),
                         (QtGui.QPalette.ToolTipText, TEXT), (QtGui.QPalette.PlaceholderText, SUBTLE)):
        pal.setColor(role, QtGui.QColor(colour))
    app.setPalette(pal)
    app.setStyleSheet(make_style())


# ================================================================== ALSA layer

class Control:
    def __init__(self, numid, name, index=0):
        self.numid, self.name, self.index = numid, name, index
        self.type, self.access, self.items, self.values = None, "", [], []
        self.min = self.max = 0

    @property
    def key(self):
        return f"{self.name}#{self.index}"


def _conv(ctype, raw):
    raw = raw.strip()
    if ctype == "BOOLEAN":
        return raw == "on"
    if ctype == "BYTES":
        return int(raw, 16) if raw.startswith("0x") else int(raw)
    try:
        return int(raw)
    except ValueError:
        return raw


def parse_amixer(text):
    ctrls, cur = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("numid="):
            m = re.match(r"numid=(\d+),iface=\w+,name='([^']*)'(?:,index=(\d+))?", line)
            cur = None
            if m:
                cur = Control(int(m.group(1)), m.group(2), int(m.group(3) or 0))
                ctrls[cur.numid] = cur
        elif cur is None:
            continue
        elif line.startswith("; type="):
            kv = dict(p.split("=", 1) for p in line[2:].split(",") if "=" in p)
            cur.type, cur.access = kv.get("type"), kv.get("access", "")
            cur.min, cur.max = int(kv.get("min", 0)), int(kv.get("max", 0))
        elif line.startswith("; Item #"):
            m = re.match(r"; Item #\d+ '(.*)'", line)
            if m:
                cur.items.append(m.group(1))
        elif line.startswith(": values="):
            cur.values = [_conv(cur.type, v) for v in line[len(": values="):].split(",")]
    return ctrls


def find_mbox_card():
    try:
        text = (PROCFS / "asound/cards").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+\[.*\]:\s*(.*)", line)
        if m and "mbox" in m.group(2).lower():
            return int(m.group(1))
    return None


class MboxNotAnswering(RuntimeError):
    """amixer timed out: the Mbox (or its control service) has wedged."""


class DirectReader:
    """Reads ALSA controls straight from /dev/snd/controlC<n> (one ioctl, no process).
    Only switched on after it has proven it gets exactly what amixer gets."""
    ELEM_READ = 0xC4C85512          # _IOWR('U', 0x12, struct snd_ctl_elem_value), 1224 bytes on 64-bit
    SIZE, VALUE_OFF, NAME_OFF = 1224, 72, 16

    def __init__(self, card):
        import ctypes
        if ctypes.sizeof(ctypes.c_long) != 8:
            raise OSError("only on 64-bit systems")
        path = f"/dev/snd/controlC{card}"
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def read(self, numid, ctype, count):
        """→ (name, values). ENUMERATED values are 32-bit item numbers, everything else 64-bit."""
        import fcntl
        import struct
        buf = bytearray(self.SIZE)
        struct.pack_into("I", buf, 0, numid)
        fcntl.ioctl(self.fd, self.ELEM_READ, buf)
        name = bytes(buf[self.NAME_OFF:self.NAME_OFF + 44]).split(b"\0", 1)[0].decode(errors="replace")
        fmt = f"{count}I" if ctype == "ENUMERATED" else f"{count}q"
        vals = list(struct.unpack_from(fmt, buf, self.VALUE_OFF))
        if ctype == "BOOLEAN":
            vals = [bool(v) for v in vals]
        return name, vals

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class Backend:
    def __init__(self, card):
        self.card = str(card)
        self.direct, self.shapes = None, {}       # numid → (name, type, count, index) learned from amixer
        self.direct_tried = False

    def enable_direct(self):
        """Try the fast path once; keep it only if it matches amixer exactly on unchanging controls."""
        if self.direct_tried or os.environ.get("MBOX_AMIXER") or os.environ.get("MBOX_NO_DIRECT") or \
                load_json(STATE_FILE, {}).get("direct_meters") is False:
            return self.direct is not None
        self.direct_tried = True
        try:
            ctrls = self.read_all()
            rd = DirectReader(self.card)
            checks = [c for c in ctrls.values() if c.type in ("INTEGER", "BOOLEAN", "ENUMERATED")
                      and "meter" not in c.name][:6]
            if not checks:
                raise OSError("nothing to compare")
            for c in checks:
                name, vals = rd.read(c.numid, c.type, len(c.values))
                if name != c.name or vals != c.values:
                    raise OSError(f"mismatch on {c.name}")
            self.direct = rd
        except Exception:  # noqa: BLE001
            self.direct = None
        return self.direct is not None

    def _run(self, *args):
        if is_hung(AMIXER):
            raise MboxNotAnswering("The Mbox isn't answering")
        try:
            r = subprocess.run([AMIXER, "-c", self.card, *args], capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            mark_hung(AMIXER)
            raise MboxNotAnswering("The Mbox isn't answering") from None
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or "amixer failed")
        return r.stdout

    def read_all(self):
        ctrls = parse_amixer(self._run("contents"))
        self.shapes = {n: c for n, c in ctrls.items()}
        return ctrls

    def read(self, numid):
        shape = self.shapes.get(numid)
        if self.direct and shape:
            try:
                name, vals = self.direct.read(numid, shape.type, len(shape.values))
                if name == shape.name:                 # same control → fast answer
                    c = Control(numid, shape.name, shape.index)
                    c.type, c.access, c.items, c.min, c.max = shape.type, shape.access, shape.items, shape.min, shape.max
                    c.values = vals
                    return c
            except OSError:
                pass                                    # card gone or renumbered: amixer will say what's up
        return parse_amixer(self._run("cget", f"numid={numid}")).get(numid)

    # A runaway loop that keeps sending the same command can lock the Mbox up until it's power-cycled.
    # These limits are far above anything a person or a scene does, and they log where the loop came from.
    REPEAT_LIMIT, REPEAT_WINDOW = 8, 10.0      # same control + same value, 8× in 10 s → refuse
    BURST_LIMIT, BURST_WINDOW = 200, 3.0       # any 200 writes in 3 s → refuse for a moment

    def _guard(self, ctrl, s):
        now = time.time()
        hist = self.__dict__.setdefault("_hist", [])
        hist[:] = [h for h in hist if now - h[0] < max(self.REPEAT_WINDOW, self.BURST_WINDOW)]
        same = sum(1 for t, n, v in hist if n == ctrl.numid and v == s and now - t < self.REPEAT_WINDOW)
        burst = sum(1 for t, _, _ in hist if now - t < self.BURST_WINDOW)
        if same >= self.REPEAT_LIMIT or burst >= self.BURST_LIMIT:
            why = (f"{ctrl.name} set to the same value {same + 1}× in {self.REPEAT_WINDOW:.0f} s"
                   if same >= self.REPEAT_LIMIT else f"{burst + 1} writes in {self.BURST_WINDOW:.0f} s")
            key = (ctrl.numid, s)
            if self.__dict__.get("_last_storm") != key:      # log each storm once, with where it came from
                self._last_storm = key
                import traceback
                try:
                    CONF_DIR.mkdir(parents=True, exist_ok=True)
                    with open(CRASH_LOG, "a") as f:
                        f.write(f"--- write storm blocked {datetime.now():%H:%M:%S}: {why} ---\n"
                                + "".join(traceback.format_stack(limit=12)))
                except OSError:
                    pass
            raise RuntimeError(f"blocked a write loop ({why}) to protect the Mbox")
        hist.append((now, ctrl.numid, s))

    def write(self, ctrl, values):
        if ctrl.type == "BOOLEAN":
            s = ",".join("on" if v else "off" for v in values)
        else:
            s = ",".join(str(int(v)) for v in values)
        self._guard(ctrl, s)
        self._run("cset", f"numid={ctrl.numid}", s)
        ctrl.values = list(values)


class Poller(QtCore.QThread):
    """Reads meters (and anything that changed) in the background so the UI never waits."""
    got = QtCore.Signal(int, list)

    def __init__(self, backend, meter_ids, fast_ids):
        super().__init__()
        self.backend, self.meter_ids, self.fast_ids = backend, list(meter_ids), list(fast_ids)
        self.pending, self.lock = set(), threading.Lock()
        self.running, self.events_ok = True, False
        self.interval = 0.07

    def request(self, numid):
        with self.lock:
            self.pending.add(numid)

    def run(self):
        while self.running:
            t0 = time.time()
            with self.lock:
                todo, self.pending = list(self.pending), set()
            ids = todo + [i for i in self.meter_ids if i not in todo]
            self.cycle = getattr(self, "cycle", 0) + 1
            if not self.events_ok or self.cycle % 7 == 0:
                ids += [i for i in self.fast_ids if i not in ids]
            for numid in ids:
                if not self.running:
                    break
                try:
                    r = self.backend.read(numid)
                except Exception:  # noqa: BLE001
                    continue
                if r:
                    self.got.emit(numid, r.values)
            time.sleep(max(0.0, self.interval - (time.time() - t0)))

    def stop(self):
        self.running = False


class EventWatcher(QtCore.QThread):
    """Follows `alsactl monitor` so knob/button changes arrive instantly."""
    changed = QtCore.Signal(int)
    alive = QtCore.Signal(bool)

    def __init__(self, card):
        super().__init__()
        self.card, self.proc, self.running = card, None, True

    def run(self):
        if not shutil.which(ALSACTL):
            self.alive.emit(False)
            return
        try:
            cmd = [ALSACTL, "monitor", f"hw:{self.card}"]
            if shutil.which("stdbuf"):
                cmd = ["stdbuf", "-oL", "-eL"] + cmd   # alsactl buffers output when piped
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            self.alive.emit(False)
            return
        self.alive.emit(True)
        for line in self.proc.stdout:
            if not self.running:
                break
            m = re.search(r"#(\d+)\s", line)
            if m and "VALUE" in line:
                self.changed.emit(int(m.group(1)))
        self.alive.emit(False)

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ================================================================== system helpers

# ---- packages: the same app on Arch, Debian/Ubuntu, Fedora and openSUSE ------------------
PKGS = {   # feature → package names per package manager
    "avahi": {"pacman": ["avahi"], "apt-get": ["avahi-daemon"], "dnf": ["avahi"], "zypper": ["avahi"]},
    "guitarix": {"pacman": ["guitarix", "pipewire-jack"], "apt-get": ["guitarix", "pipewire-jack"],
                 "dnf": ["guitarix", "pipewire-jack-audio-connection-kit"],
                 "zypper": ["guitarix", "pipewire-libjack-0_3"]},
    "audacity": {"pacman": ["audacity"], "apt-get": ["audacity"], "dnf": ["audacity"], "zypper": ["audacity"]},
    "chromaprint": {"pacman": ["chromaprint"], "apt-get": ["libchromaprint-tools"], "dnf": ["chromaprint-tools"],
                    "zypper": ["chromaprint-fpcalc"]},
    "pyudev": {"pacman": ["python-pyudev"], "apt-get": ["python3-pyudev"], "dnf": ["python3-pyudev"],
               "zypper": ["python3-pyudev"]},
    "numpy": {"pacman": ["python-numpy"], "apt-get": ["python3-numpy"], "dnf": ["python3-numpy"],
              "zypper": ["python3-numpy"]},
    "ffmpeg": {"pacman": ["ffmpeg"], "apt-get": ["ffmpeg"], "dnf": ["ffmpeg-free"], "zypper": ["ffmpeg"]},
    "compressor": {"pacman": ["swh-plugins"], "apt-get": ["swh-plugins"], "dnf": ["ladspa-swh-plugins"],
                   "zypper": ["ladspa-swh-plugins"]},
    "pipewire-jack": {"pacman": ["pipewire-jack"], "apt-get": ["pipewire-jack"],
                      "dnf": ["pipewire-jack-audio-connection-kit"], "zypper": ["pipewire-libjack-0_3"]},
}
PM_INSTALL = {"pacman": ["pacman", "-S", "--needed", "--noconfirm"], "apt-get": ["apt-get", "install", "-y"],
              "dnf": ["dnf", "install", "-y"], "zypper": ["zypper", "--non-interactive", "install"]}


def package_manager():
    return next((pm for pm in ("pacman", "apt-get", "dnf", "zypper") if shutil.which(pm)), None)


def install_argv(feature):
    """The root command that installs a feature here, or None if we don't know this system."""
    pm = package_manager()
    if not pm or feature not in PKGS:
        return None
    return PM_INSTALL[pm] + PKGS[feature][pm]


def install_hint(feature):
    """What to tell someone to type, e.g. 'sudo apt-get install -y audacity'."""
    argv = install_argv(feature)
    return "sudo " + " ".join(argv) if argv else f"install {feature} with your package manager"


def install_feature(feature):
    """Install with one password prompt. → True when it worked."""
    argv = install_argv(feature)
    if not argv or not shutil.which("pkexec"):
        return False
    return subprocess.run(["pkexec", *argv], capture_output=True, text=True).returncode == 0



HUNG = {}            # program → time until which we don't wait on it again (it just timed out)
HUNG_PAUSE = 30.0


def is_hung(prog):
    return HUNG.get(Path(prog).name, 0) > time.time()


def mark_hung(prog):
    HUNG[Path(prog).name] = time.time() + HUNG_PAUSE


def run_quiet(*cmd, timeout=4):
    """Run a helper and return its output, or None. A helper that stops answering (PipeWire or the Mbox
    wedged) is skipped for 30 s instead of freezing the window again and again."""
    if not shutil.which(cmd[0]) or is_hung(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except subprocess.TimeoutExpired:
        mark_hung(cmd[0])
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def mbox_pw_node(kind):
    out = run_quiet("pactl", "list", f"{kind}s", "short") or ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and "firewire" in parts[1] and "pro-" in parts[1] \
                and not parts[1].endswith(".monitor"):
            return parts[1]
    return None


def pw_node_exists(name, kind="source"):
    out = run_quiet("pactl", "list", f"{kind}s", "short") or ""
    return any(line.split("\t")[1:2] == [name] for line in out.splitlines())


def pw_set(key, value):
    return run_quiet("pw-metadata", "-n", "settings", "0", key, str(value)) is not None


def write_clock_conf(rate, quantum):
    PW_CONF_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by Mbox Control", "context.properties = {"]
    if rate:
        lines += [f"    default.clock.rate = {rate}", f"    default.clock.allowed-rates = [ {rate} ]"]
    if quantum:
        lines += [f"    default.clock.quantum = {quantum}", f"    default.clock.min-quantum = {min(quantum, 32)}"]
    lines.append("}")
    (PW_CONF_DIR / "mbox-clock.conf").write_text("\n".join(lines) + "\n")


NAMED_INPUTS = [
    ("mbox_tape_in", "Rip input (Mbox)", ["AUX4", "AUX5"], ["FL", "FR"]),
    ("mbox_in12", "Mbox Inputs 1+2 (stereo)", ["AUX0", "AUX1"], ["FL", "FR"]),
    ("mbox_in1", "Mbox Input 1 (mic/guitar)", ["AUX0"], ["MONO"]),
    ("mbox_in2", "Mbox Input 2", ["AUX1"], ["MONO"]),
    ("mbox_in3", "Mbox Input 3", ["AUX2"], ["MONO"]),
    ("mbox_in4", "Mbox Input 4", ["AUX3"], ["MONO"]),
    ("mbox_loopback", "Loopback (Mbox)", ["AUX6", "AUX7"], ["FL", "FR"]),
]


def write_named_inputs_conf(source):
    PW_CONF_DIR.mkdir(parents=True, exist_ok=True)
    mods = []
    for node, desc, src_pos, out_pos in NAMED_INPUTS:
        mods.append(f"""  {{ name = libpipewire-module-loopback
    args = {{
      node.description = "{desc}"
      capture.props = {{
        node.name = "{node}_cap"
        target.object = "{source}"
        audio.position = [ {' '.join(src_pos)} ]
        stream.dont-remix = true
        node.passive = true
      }}
      playback.props = {{
        node.name = "{node}"
        media.class = "Audio/Source"
        audio.position = [ {' '.join(out_pos)} ]
      }}
    }}
  }}""")
    (PW_CONF_DIR / "mbox-inputs.conf").write_text(
        "# Written by Mbox Control\ncontext.modules = [\n" + "\n".join(mods) + "\n]\n")


def restart_pipewire():
    return run_quiet("systemctl", "--user", "restart", "pipewire", "pipewire-pulse",
                     "wireplumber", timeout=20) is not None


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))   # TEST-NET address: picks the LAN interface, sends nothing
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ================================================================== small helpers

def friendly(item):
    m = re.fullmatch(r"Analog-A-(\d)", item)
    if m:
        n = int(m.group(1))
        return f"Input {n}" if n <= 4 else f"Aux in {n}"
    m = re.fullmatch(r"Stream-(\d+)", item)
    if m:
        return f"PC {m.group(1)}"
    m = re.fullmatch(r"S/PDIF-(\d)", item)
    if m:
        return f"S/PDIF in {'L' if m.group(1) == '1' else 'R'}"
    m = re.fullmatch(r"Reverb-(\d)", item)
    if m:
        return f"FX return {'L' if m.group(1) == '1' else 'R'}"
    m = re.fullmatch(r"Mixer-(\d+)", item)
    if m:
        n = int(m.group(1))
        k = (n + 1) // 2
        if k <= 6:
            return f"Stereo Mix {k} {'L' if n % 2 else 'R'}"
        return f"FX send {'L' if n == 13 else 'R'}" if n in (13, 14) else f"Spare bus {n}"
    return item


def meter_db(v):
    return METER_FLOOR_DB if not v or v <= 0 else max(METER_FLOOR_DB, 20 * math.log10(v / METER_MAX))


def gain_db(v):
    return -math.inf if v <= 0 else 20 * math.log10(v / UNITY)


def gain_text(v):
    return "−∞" if v <= 0 else f"{gain_db(v):+.1f} dB"


def to_pan(l, r):
    level = min(GAIN_MAX, round(math.hypot(l, r)))
    if level == 0:
        return 0, 0
    return level, round(math.atan2(r, l) / (math.pi / 2) * 200 - 100)


def from_pan(level, pan):
    th = (pan + 100) / 200 * (math.pi / 2)
    return min(GAIN_MAX, round(level * math.cos(th))), min(GAIN_MAX, round(level * math.sin(th)))


def pan_text(p):
    return "C" if p == 0 else (f"L{-p}" if p < 0 else f"R{p}")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "Untitled"


# ================================================================== widgets

CARD_ICONS = {   # card title → icon names (KDE Breeze first, then freedesktop fallbacks)
    "AirPlay speakers": ["network-wireless", "network-wireless-symbolic"], "Always on": ["media-playlist-repeat"],
    "Appearance": ["preferences-desktop-theme", "preferences-desktop-color"], "Audio engine": ["audio-card"],
    "Background service & launcher": ["system-run", "applications-system"], "Big knob": ["audio-volume-high"],
    "Connections": ["network-connect", "insert-link"], "Default output": ["audio-speakers", "audio-volume-high"],
    "Dim, Mute & Mono": ["audio-volume-muted"], "Discogs": ["media-optical-audio", "media-optical"],
    "Effect": ["preferences-desktop-sound", "audio-x-generic"], "Front-panel buttons": ["configure"],
    "Guitar mode": ["audio-input-line", "audio-x-generic"], "Headphones output for apps": ["audio-headphones"],
    "Inputs": ["audio-input-microphone"], "Keyboard shortcuts": ["input-keyboard", "preferences-desktop-keyboard"],
    "Live analysis": ["view-statistics", "office-chart-line"], "MIDI controller": ["audio-midi", "input-gaming"],
    "Make a tape": ["media-tape", "media-record"], "Master": ["audio-volume-high"],
    "Mic & instrument guide": ["help-hint", "audio-input-microphone"], "Mixer": ["view-media-equalizer", "audio-card"],
    "Music library": ["folder-music", "media-optical-audio"], "Named inputs for your apps": ["tag", "audio-input-line"],
    "Output trims": ["audio-volume-medium"], "Playing now": ["media-playback-start"],
    "Quick take": ["audio-input-microphone", "media-record"], "Rip queue": ["view-list-details", "format-list-ordered"],
    "Rip tape / vinyl": ["media-record"], "Ripping": ["media-record"], "Scenes": ["view-media-playlist", "view-grid"],
    "Song identification": ["search", "edit-find"], "Speaker sets": ["audio-speakers", "audio-speakers-symbolic", "speaker", "audio-volume-high"], "Split & tag": ["edit-cut"],
    "Standalone mode": ["network-disconnect", "system-shutdown"], "Startup & tray": ["system-run"],
    "Suggestions": ["help-hint", "dialog-information"], "Tuner": ["view-media-visualization", "audio-x-generic"],
    "What each output feeds": ["audio-speakers", "audio-speakers-symbolic", "speaker", "audio-volume-high"], "What's plugged into each input": ["audio-input-line", "audio-card"],
    "Where sound goes": ["view-media-visualization", "audio-speakers"], "Wiring": ["insert-link", "network-connect"],
    "Convert audio": ["document-export", "document-save-as"],
    "Loopback": ["media-repeat-all", "media-playlist-repeat", "view-refresh"],
    "EQ for each speaker set": ["view-media-equalizer"], "Tone": ["view-media-equalizer", "audio-volume-high"],
    "Clean mic (voice chain)": ["audio-input-microphone", "view-media-equalizer"],
    "Capture what I just played": ["document-save", "media-record"], "Multitrack": ["view-split-left-right", "media-record"],
    "Level check": ["audio-volume-high", "view-statistics"], "Automatic scenes": ["system-run", "media-playlist-shuffle"], "Practice player": ["media-repeat-all", "media-playback-start"], "Metronome": ["chronometer", "clock", "media-playback-start"],
}
TAB_ICONS = {
    "Rip": ["media-record"], "Quick take": ["audio-input-microphone"], "Practice": ["media-repeat-all", "view-refresh"], "Split & tag": ["edit-cut"],
    "Tools": ["applications-utilities", "tools"], "Your setup": ["document-properties", "user-identity"],
    "Sound": ["audio-card"], "Mbox": ["audio-card"], "Controls": ["input-keyboard"], "App": ["configure"],
    "Speaker sets": ["audio-speakers", "audio-speakers-symbolic", "speaker", "audio-volume-high"], "Apps": ["applications-multimedia", "audio-volume-high"],
    "Signal map": ["view-media-visualization", "network-connect"], "The box": ["audio-card"],
    "EQ": ["view-media-equalizer", "configure"],
}


def ensure_icon_theme():
    """pip-installed PySide6 doesn't look in /usr/share/icons: point it at the system's icons and theme."""
    paths = list(QtGui.QIcon.themeSearchPaths())
    dirs = [Path(d) / "icons" for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")]
    for d in [HOME / ".local/share/icons", HOME / ".icons", *dirs]:
        if d.is_dir() and str(d) not in paths:
            paths.append(str(d))
    QtGui.QIcon.setThemeSearchPaths(paths)
    if QtGui.QIcon.hasThemeIcon("media-record"):
        return
    want = ""
    try:
        m = re.search(r"^\[Icons\][^\[]*?^Theme=(.+)$", (XDG_CONFIG / "kdeglobals").read_text(), re.M | re.S)
        want = m.group(1).strip() if m else ""
    except OSError:
        pass
    if not want:
        want = (run_quiet("gsettings", "get", "org.gnome.desktop.interface", "icon-theme") or "").strip().strip("'")
    for name in [want, "breeze-dark" if DARK else "breeze", "breeze", "Adwaita", "Papirus", "hicolor"]:
        if name:
            QtGui.QIcon.setThemeName(name)
            if QtGui.QIcon.hasThemeIcon("media-record"):
                return


def themed_icon(names):
    for n in names or []:
        if QtGui.QIcon.hasThemeIcon(n):
            return QtGui.QIcon.fromTheme(n)
    return None


def icon_tabs(tabs):
    """Give a tab bar's tabs their icons (by tab text)."""
    for i in range(tabs.count()):
        ic = themed_icon(TAB_ICONS.get(tabs.tabText(i).replace("&&", "&")))
        if ic:
            tabs.setTabIcon(i, ic)


def card(title=None, subtitle=None):
    f = QtWidgets.QFrame()
    f.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(f)
    lay.setContentsMargins(18, 14, 18, 16)
    lay.setSpacing(10)
    if title:
        t = QtWidgets.QLabel(title)
        t.setObjectName("title")
        ic = themed_icon(CARD_ICONS.get(title))
        if ic:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)
            il = QtWidgets.QLabel()
            il.setPixmap(ic.pixmap(18, 18))
            row.addWidget(il)
            row.addWidget(t)
            row.addStretch(1)
            lay.addLayout(row)
        else:
            lay.addWidget(t)
    if subtitle:
        lay.addWidget(muted(subtitle))
    return f, lay


def muted(text):
    l = QtWidgets.QLabel(text)
    l.setObjectName("muted")
    l.setWordWrap(True)
    if not text:
        l.setProperty("keep_long", True)      # filled in later with live status: never shortened
    return l


def page(title, subtitle=None):
    w = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(w)
    lay.setContentsMargins(26, 20, 26, 20)
    lay.setSpacing(14)
    h = QtWidgets.QLabel(title)
    h.setObjectName("h1")
    lay.addWidget(h)
    if subtitle:
        lay.addWidget(muted(subtitle))
    return w, lay


def scroll(w):
    s = QtWidgets.QScrollArea()
    s.setWidgetResizable(True)
    s.setFrameShape(QtWidgets.QFrame.NoFrame)
    s.setWidget(w)
    return s


def meter_colour(db):
    return QtGui.QColor("#f85149") if db > -1 else QtGui.QColor("#e3a52a") if db > -12 \
        else QtGui.QColor("#3fb950")


def ink(alpha):
    """A faint line or fill that shows on both dark and light backgrounds."""
    return QtGui.QColor(255, 255, 255, alpha) if DARK else QtGui.QColor(0, 0, 0, min(255, int(alpha * 1.4)))


class Meter(QtWidgets.QWidget):
    """Hand-painted level meter (dB) with peak hold."""

    def __init__(self, orientation=Qt.Horizontal, thick=10):
        super().__init__()
        self.orientation = orientation
        if orientation == Qt.Horizontal:
            self.setFixedHeight(thick)
            self.setMinimumWidth(90)
        else:
            self.setFixedWidth(thick)
            self.setMinimumHeight(120)
        self.frac = self.peak = 0.0
        self.peak_t = 0.0

    def set_level(self, raw):
        db = meter_db(raw)
        frac = (db - METER_FLOOR_DB) / -METER_FLOOR_DB if raw else 0.0
        now = time.time()
        if frac >= self.peak or now - self.peak_t > 1.5:
            self.peak, self.peak_t = frac, now
        if abs(frac - self.frac) > 0.003 or now - self.peak_t < 0.05:
            self.frac = frac
            self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(ink(16))
        p.drawRoundedRect(r, 3, 3)
        horiz = self.orientation == Qt.Horizontal
        if self.frac > 0:
            grad = QtGui.QLinearGradient(r.bottomLeft() if not horiz else r.topLeft(),
                                         r.topLeft() if not horiz else r.topRight())
            grad.setColorAt(0.0, QtGui.QColor("#2ea043"))
            grad.setColorAt(0.78, QtGui.QColor("#3fb950"))
            grad.setColorAt(0.82, QtGui.QColor("#e3a52a"))
            grad.setColorAt(0.97, QtGui.QColor("#e3a52a"))
            grad.setColorAt(1.0, QtGui.QColor("#f85149"))
            p.setBrush(grad)
            if horiz:
                p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * self.frac, r.height()), 3, 3)
            else:
                h = r.height() * self.frac
                p.drawRoundedRect(QRectF(r.x(), r.bottom() - h, r.width(), h), 3, 3)
        if self.peak > 0.02:
            p.setBrush(ink(200))
            if horiz:
                x = r.x() + r.width() * self.peak
                p.drawRect(QRectF(x - 1.5, r.y(), 2, r.height()))
            else:
                y = r.bottom() - r.height() * self.peak
                p.drawRect(QRectF(r.x(), y - 1, r.width(), 2))


class ValueBar(QtWidgets.QWidget):
    def __init__(self, maximum=255, colour=None):
        super().__init__()
        self.maximum, self.value_, self.colour = maximum, 0, QtGui.QColor(colour or ACCENT)
        self.setFixedHeight(18)
        self.setMinimumWidth(120)

    def setValue(self, v):
        if v != self.value_:
            self.value_ = v
            self.update()

    def value(self):
        return self.value_

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 3, -50, -3)
        p.setPen(Qt.NoPen)
        p.setBrush(ink(16))
        p.drawRoundedRect(r, 6, 6)
        frac = max(0.0, min(1.0, self.value_ / self.maximum))
        p.setBrush(self.colour)
        p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * frac, r.height()), 6, 6)
        p.setPen(QtGui.QColor(TEXT))
        p.drawText(QRectF(r.right() + 8, 0, 44, self.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   f"{round(frac * 100)}%")


class Fader(QtWidgets.QWidget):
    """Painted channel fader with dB scale and built-in level meter."""
    gainChanged = QtCore.Signal(int)
    TOP_PAD, BOT_PAD = 12, 12

    def __init__(self, max_gain=UNITY):
        super().__init__()
        self.max_gain, self.gain, self.level = max_gain, 0, 0
        self.peak, self.peak_t = 0.0, 0.0
        self.dragging, self.drag_off = False, 0.0
        self.setMinimumSize(60, 200)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.WheelFocus)

    def top_db(self):
        return 20 * math.log10(self.max_gain / UNITY)

    def _track(self):
        return self.TOP_PAD, self.height() - self.BOT_PAD

    def y_of_gain(self, g):
        y0, y1 = self._track()
        if g <= 0:
            return y1
        t = (gain_db(g) - METER_FLOOR_DB) / (self.top_db() - METER_FLOOR_DB)
        return y1 - max(0.0, min(1.0, t)) * (y1 - y0)

    def gain_at(self, y):
        y0, y1 = self._track()
        t = (y1 - y) / (y1 - y0)
        if t <= 0.01:
            return 0
        db = METER_FLOOR_DB + min(1.0, t) * (self.top_db() - METER_FLOOR_DB)
        return min(self.max_gain, round(UNITY * 10 ** (db / 20)))

    def setGain(self, g):
        if not self.dragging and g != self.gain:
            self.gain = g
            self.update()

    def setMax(self, m):
        if m != self.max_gain:
            self.max_gain = m
            self.update()

    def setLevel(self, raw):
        db = meter_db(raw)
        frac = (db - METER_FLOOR_DB) / -METER_FLOOR_DB if raw else 0.0
        now = time.time()
        if frac >= self.peak or now - self.peak_t > 1.5:
            self.peak, self.peak_t = frac, now
        if raw != self.level:
            self.level = raw
            self.update()

    def _set_user(self, g):
        g = max(0, min(self.max_gain, g))
        if g != self.gain:
            self.gain = g
            self.update()
            self.gainChanged.emit(g)

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w = self.width()
        y0, y1 = self._track()
        cx = w * 0.42
        # scale
        f = p.font()
        f.setPointSizeF(7)
        p.setFont(f)
        for db in (0, -5, -10, -20, -40, -60) + ((6, 12) if self.top_db() > 1 else ()):
            if db > self.top_db():
                continue
            y = self.y_of_gain(UNITY * 10 ** (db / 20)) if db > METER_FLOOR_DB else y1
            p.setPen(ink(50 if db else 110))
            p.drawLine(QPointF(cx - 13, y), QPointF(cx - 8, y))
            p.setPen(QtGui.QColor(SUBTLE))
            p.drawText(QRectF(0, y - 7, cx - 15, 14), Qt.AlignRight | Qt.AlignVCenter,
                       "∞" if db == -60 else str(abs(db)) if db <= 0 else f"+{db}")
        # groove + fill
        groove = QRectF(cx - 3, y0, 6, y1 - y0)
        p.setPen(Qt.NoPen)
        p.setBrush(ink(20))
        p.drawRoundedRect(groove, 3, 3)
        gy = self.y_of_gain(self.gain)
        p.setBrush(QtGui.QColor(ACCENT))
        p.drawRoundedRect(QRectF(cx - 3, gy, 6, y1 - gy), 3, 3)
        # meter
        mx = cx + 14
        p.setBrush(ink(14))
        p.drawRoundedRect(QRectF(mx, y0, 6, y1 - y0), 3, 3)
        if self.level:
            db = meter_db(self.level)
            frac = (db - METER_FLOOR_DB) / -METER_FLOOR_DB
            h = (y1 - y0) * frac
            p.setBrush(meter_colour(db))
            p.drawRoundedRect(QRectF(mx, y1 - h, 6, h), 3, 3)
        if self.peak > 0.02:
            p.setBrush(ink(190))
            p.drawRect(QRectF(mx, y1 - (y1 - y0) * self.peak - 1, 6, 2))
        # cap
        cap = QRectF(cx - 13, gy - 9, 26, 18)
        p.setBrush(QtGui.QColor("#eef1f5"))
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 120), 1))
        p.drawRoundedRect(cap, 5, 5)
        p.setPen(QtGui.QPen(QtGui.QColor(60, 60, 60), 1.5))
        p.drawLine(QPointF(cap.left() + 5, gy), QPointF(cap.right() - 5, gy))

    def mousePressEvent(self, e):
        y = e.position().y()
        gy = self.y_of_gain(self.gain)
        self.dragging = True
        self.drag_off = (y - gy) if abs(y - gy) <= 11 else 0.0
        if not abs(y - gy) <= 11:
            self._set_user(self.gain_at(y))

    def mouseMoveEvent(self, e):
        if self.dragging:
            self._set_user(self.gain_at(e.position().y() - self.drag_off))

    def mouseReleaseEvent(self, _):
        self.dragging = False

    def mouseDoubleClickEvent(self, _):
        self._set_user(UNITY)

    def wheelEvent(self, e):
        step = 1 if e.angleDelta().y() > 0 else -1
        db = gain_db(self.gain) if self.gain > 0 else METER_FLOOR_DB - 1
        db = METER_FLOOR_DB if db < METER_FLOOR_DB and step > 0 else db + step
        self._set_user(0 if db < METER_FLOOR_DB else round(UNITY * 10 ** (db / 20)))
        e.accept()


class Knob(QtWidgets.QWidget):
    """Painted arc knob. Drag up/down, scroll, double-click to reset."""
    valueChanged = QtCore.Signal(int)

    def __init__(self, lo, hi, default=0, size=40, bipolar=False, colour=None, label=None):
        super().__init__()
        self.lo, self.hi, self.default, self.bipolar = lo, hi, default, bipolar
        self.value_, self.colour, self.label = default, QtGui.QColor(colour or ACCENT), label
        self.setFixedSize(size, size + (14 if label else 0))
        self.drag_y, self.drag_v = None, 0
        self.setCursor(Qt.SizeVerCursor)

    def value(self):
        return self.value_

    def setValue(self, v, emit=False):
        v = max(self.lo, min(self.hi, int(v)))
        if v != self.value_:
            self.value_ = v
            self.update()
            if emit:
                self.valueChanged.emit(v)

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        s = min(self.width(), self.height() - (14 if self.label else 0))
        r = QRectF(4, 4, s - 8, s - 8)
        pen = QtGui.QPen(ink(30), 4, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, 225 * 16, -270 * 16)
        frac = (self.value_ - self.lo) / (self.hi - self.lo)
        start = 225 - (135 if self.bipolar else 0)
        span = -270 * frac + (135 if self.bipolar else 0)
        pen.setColor(self.colour)
        p.setPen(pen)
        if abs(span) > 0.5:
            p.drawArc(r, int(start * 16), int(span * 16))
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(BG2).lighter(130) if DARK else QtGui.QColor(BG0).darker(108))
        inner = r.adjusted(6, 6, -6, -6)
        p.drawEllipse(inner)
        ang = math.radians(225 - 270 * frac)
        c = inner.center()
        p.setPen(QtGui.QPen(QtGui.QColor("white" if DARK else TEXT), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c, QPointF(c.x() + math.cos(ang) * inner.width() * 0.4,
                              c.y() - math.sin(ang) * inner.width() * 0.4))
        if self.label:
            f = p.font()
            f.setPointSizeF(7.5)
            p.setFont(f)
            p.setPen(QtGui.QColor(SUBTLE))
            p.drawText(QRectF(0, s - 2, self.width(), 14), Qt.AlignCenter, self.label)

    def mousePressEvent(self, e):
        self.drag_y, self.drag_v = e.position().y(), self.value_

    def mouseMoveEvent(self, e):
        if self.drag_y is not None:
            dv = (self.drag_y - e.position().y()) * (self.hi - self.lo) / 160
            self.setValue(self.drag_v + dv, emit=True)

    def mouseReleaseEvent(self, _):
        self.drag_y = None

    def mouseDoubleClickEvent(self, _):
        self.setValue(self.default, emit=True)

    def wheelEvent(self, e):
        step = (self.hi - self.lo) / 50 * (1 if e.angleDelta().y() > 0 else -1)
        self.setValue(self.value_ + step, emit=True)
        e.accept()


class Chip(QtWidgets.QLabel):
    clicked = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(34)

    def make_clickable(self, tip):
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip)
        self.setFocusPolicy(Qt.StrongFocus)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def set(self, text, colour=None):
        self.setText(text)
        self.setStyleSheet("border-radius:10px; padding:6px 14px; font-weight:600;"
                           + (f"background:{colour}; color:#0b0d10;" if colour
                              else "background:rgba(255,255,255,0.06);"))


class Debounced:
    def __init__(self, fn, ms=40):
        self.fn, self.args = fn, None
        self.timer = QtCore.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(ms)
        self.timer.timeout.connect(self._fire)

    def __call__(self, *args):
        self.args = args
        self.timer.start()

    def _fire(self):
        if self.args is not None:
            a, self.args = self.args, None
            self.fn(*a)


# ============================================================ hardware drawing

class HardwareView(QtWidgets.QWidget):
    """Live drawing of the Mbox Pro front and back panels."""
    W, FRONT_H, BACK_H, GAP = 1100.0, 250.0, 210.0, 34.0

    jackClicked = QtCore.Signal(str)
    phantomClicked = QtCore.Signal()

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.highlight = set()
        self.setMouseTracking(True)
        self.setMinimumHeight(420)
        self.hover = None
        self.shapes = []      # (id, QRectF, kind, label)
        self._build_shapes()

    # geometry in panel units ---------------------------------------
    def _build_shapes(self):
        S = self.shapes
        fy = 0
        # front
        S += [("front1", QRectF(32, fy + 100, 62, 62), "combo", "Mic/DI 1 (front)"),
              ("front2", QRectF(232, fy + 100, 62, 62), "combo", "Mic/DI 2 (front)"),
              ("hpA", QRectF(766, fy + 150, 30, 30), "trs_s", "Headphone A"),
              ("hpB", QRectF(856, fy + 150, 30, 30), "trs_s", "Headphone B"),
              ("phantom", QRectF(696, fy + 140, 34, 26), "button", "+48V phantom"),
              ("sw1", QRectF(114, fy + 140, 26, 20), "button", "Input 1 Front/Rear button"),
              ("sw2", QRectF(314, fy + 140, 26, 20), "button", "Input 2 Front/Rear button"),
              ("sw3", QRectF(414, fy + 140, 26, 20), "button", "Input 3 Mic/Line button"),
              ("sw4", QRectF(514, fy + 140, 26, 20), "button", "Input 4 Mic/Line button")]
        by = self.FRONT_H + self.GAP
        S += [("foot", QRectF(24, by + 130, 34, 34), "trs", "Foot switch"),
              ("breakout", QRectF(110, by + 120, 70, 34), "db15", "Word clock / MIDI / S/PDIF breakout"),
              ("fw1", QRectF(200, by + 120, 22, 30), "fw", "FireWire"),
              ("fw2", QRectF(228, by + 120, 22, 30), "fw", "FireWire")]
        for n, (x, y) in {5: (270, 55), 3: (320, 55), 1: (370, 55),
                          6: (270, 125), 4: (320, 125), 2: (370, 125)}.items():
            S.append((f"out{n}", QRectF(x, by + y, 40, 40), "trs", f"Output {n}"))
        S += [("aux5", QRectF(440, by + 55, 30, 30), "rca_w", "Aux in 5 / L (RCA)"),
              ("aux6", QRectF(440, by + 115, 30, 30), "rca_r", "Aux in 6 / R (RCA)"),
              ("auxmini", QRectF(486, by + 88, 22, 22), "trs_s", "Aux in 5/6 (1/8\")")]
        for n, (x, y) in {3: (556, 55), 4: (556, 125), 1: (646, 55), 2: (646, 125)}.items():
            S.append((f"line{n}", QRectF(x, by + y, 40, 40), "trs", f"Line input {n}"))
        for n, (x, y) in {3: (726, 55), 1: (776, 55), 4: (726, 125), 2: (776, 125)}.items():
            S.append((f"ins{n}", QRectF(x, by + y, 40, 40), "trs", f"Insert {n}"))
        S += [("mic4", QRectF(852, by + 70, 70, 70), "xlr", "Mic input 4 (XLR)"),
              ("mic3", QRectF(958, by + 70, 70, 70), "xlr", "Mic input 3 (XLR)")]

    def jack_tag_key(self, jid):
        m = {"front1": "in0:a", "line1": "in0:b", "front2": "in1:a", "line2": "in1:b",
             "mic3": "in2:a", "line3": "in2:b", "mic4": "in3:a", "line4": "in3:b",
             "aux5": "in4", "aux6": "in5", "auxmini": "in4", "hpA": "out6", "hpB": "out8"}
        if jid in m:
            return m[jid]
        mm = re.fullmatch(r"out(\d)", jid)
        return f"out{(int(mm.group(1)) - 1) // 2 * 2}" if mm else f"jack:{jid}"

    # painting --------------------------------------------------------
    def _scale(self):
        total_h = self.FRONT_H + self.GAP + self.BACK_H
        s = min(self.width() / self.W, self.height() / total_h)
        ox = (self.width() - self.W * s) / 2
        return s, ox

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        s, ox = self._scale()
        p.translate(ox, 0)
        p.scale(s, s)
        self._panel(p, 0, self.FRONT_H, "FRONT")
        self._panel(p, self.FRONT_H + self.GAP, self.BACK_H, "BACK")
        self._front(p)
        self._back(p)
        for jid, r, kind, label in self.shapes:
            self._jack(p, jid, r, kind, label)

    def _panel(self, p, y, h, label):
        grad = QtGui.QLinearGradient(0, y, 0, y + h)
        grad.setColorAt(0, QtGui.QColor(44, 46, 50))
        grad.setColorAt(1, QtGui.QColor(26, 27, 30))
        p.setPen(QtGui.QPen(QtGui.QColor(140, 140, 145), 3))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(4, y + 4, self.W - 8, h - 8), 34, 34)
        p.setPen(QtGui.QColor(255, 255, 255, 70))
        f = p.font()
        f.setPointSizeF(9)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(30, y + 12, 200, 20), Qt.AlignLeft, label)

    def _text(self, p, rect, text, size=8.5, colour=(220, 220, 220), align=Qt.AlignCenter, bold=False):
        f = p.font()
        f.setPointSizeF(size)
        f.setBold(bold)
        p.setFont(f)
        p.setPen(QtGui.QColor(*colour))
        p.drawText(rect, align, text)

    def _knob(self, p, cx, cy, r, value=None, label=None):
        p.setPen(QtGui.QPen(QtGui.QColor(15, 15, 15), 2))
        p.setBrush(QtGui.QColor(70, 72, 76))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(QtGui.QColor(105, 108, 112))
        p.drawEllipse(QPointF(cx, cy), r * 0.78, r * 0.78)
        if value is not None:
            ang = math.radians(225 - 270 * value)
            p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), 3))
            p.drawLine(QPointF(cx, cy), QPointF(cx + math.cos(ang) * r * 0.75,
                                                cy - math.sin(ang) * r * 0.75))
        if label:
            self._text(p, QRectF(cx - 40, cy - r - 20, 80, 16), label, 7.5)

    def _button(self, p, rect, label, led=None, lit=False):
        p.setPen(QtGui.QPen(QtGui.QColor(10, 10, 10), 1.5))
        p.setBrush(QtGui.QColor(60, 62, 66))
        p.drawRoundedRect(rect, 6, 6)
        if led:
            c = QtGui.QColor(led) if lit else QtGui.QColor(45, 45, 45)
            p.setBrush(c)
            p.setPen(Qt.NoPen)
            p.drawEllipse(rect.center(), 4.5, 4.5)
            if lit:
                glow = QtGui.QColor(led)
                glow.setAlpha(90)
                p.setBrush(glow)
                p.drawEllipse(rect.center(), 10, 10)
        self._text(p, QRectF(rect.x() - 20, rect.bottom() + 2, rect.width() + 40, 14), label, 7.5)

    def _front(self, p):
        w = self.win
        # gain knobs + per-channel buttons
        for n, x in ((1, 150), (2, 350), (3, 450), (4, 550)):
            self._knob(p, x, 80, 20, None, f"{n:02d}  Gain")
            rear = w.input_side(n - 1) == "b"
            self._button(p, QRectF(x - 36, 140, 26, 20),
                         ("Rear" if rear else "Front") if n <= 2 else ("Line" if rear else "Mic"),
                         led="#5aa9ff", lit=rear)
            self._button(p, QRectF(x + 10, 140, 26, 20), "Limit")
        # meter ladders
        for i in range(4):
            x = 610 + i * 18
            level = w.input_level(f"Analog-A-{i + 1}")
            db = meter_db(level)
            lit = int(max(0, min(8, (db + 48) / 6 + 0.5))) if level else 0
            for seg in range(8):
                y = 170 - seg * 12
                on = seg < lit
                col = (QtGui.QColor("#f85149") if seg == 7 else QtGui.QColor("#d29922") if seg >= 5
                       else QtGui.QColor("#3fb950"))
                if not on:
                    col = QtGui.QColor(40, 42, 40)
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(x, y, 12, 8), 2, 2)
            self._text(p, QRectF(x - 6, 50, 24, 14), f"0{i + 1}", 7.5)
        # multi / +48V
        self._button(p, QRectF(696, 80, 34, 26), "Multi")
        ph = w.ctrl("phantom-powering")
        self._button(p, QRectF(696, 140, 34, 26), "+48V", "#f85149", bool(ph and ph.values[0]))
        # headphone volumes
        self._knob(p, 781, 95, 20, None, "Volume A")
        self._knob(p, 871, 95, 20, None, "Volume B")
        # dim/mute, mono, spkr
        mute = w.ctrl_item("mute-button")
        mono = w.ctrl_item("mono-button")
        spk = w.ctrl_item("spkr-button")
        self._button(p, QRectF(930, 70, 34, 26), "Dim/Mute", "#d29922",
                     mute not in (None, "Off") and (mute != "Blink" or int(time.time() * 2) % 2 == 0))
        self._button(p, QRectF(930, 140, 34, 26), "Mono", "#d29922", mono == "On")
        sc = SPEAKERS.get((spk or "Off").split("-")[0])
        self._button(p, QRectF(978, 140, 34, 26), "Spkr", sc[1] if sc else None, bool(sc))
        # power LED + big knob
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(ACCENT))
        p.drawEllipse(QPointF(995, 82), 4, 4)
        self._text(p, QRectF(975, 62, 40, 14), "Power", 7)
        knob = w.ctrl("master-knob-value")
        v = (knob.values[0] / 255) if knob else 0
        self._knob(p, 1045, 125, 42, v)
        self._text(p, QRectF(1000, 176, 90, 14), f"Master {int(v * 100)}%", 7.5)
        # logo
        self._text(p, QRectF(812, 150, 18, 30), "M", 13, (230, 230, 230), bold=True)

    def _back(self, p):
        by = self.FRONT_H + self.GAP
        self._button(p, QRectF(26, by + 55, 30, 40), "Power")
        self._text(p, QRectF(100, by + 40, 100, 14), "9V DC", 7.5)
        p.setPen(QtGui.QPen(QtGui.QColor(10, 10, 10), 2))
        p.setBrush(QtGui.QColor(30, 30, 30))
        p.drawEllipse(QPointF(140, by + 70), 10, 10)
        for text, x, y in (("Outputs", 300, by + 14), ("Aux In 5/6", 440, by + 14),
                           ("Line In 3/4", 556, by + 14), ("Line In 1/2", 646, by + 14),
                           ("Inserts", 756, by + 14), ("Mic Inputs", 950, by + 14)):
            self._text(p, QRectF(x - 40, y, 120, 16), text, 8, bold=True)
        for x in (526, 616):
            self._button(p, QRectF(x, by + 95, 18, 26), "+4/−10")
        for num, cx in (("04", 887), ("03", 993)):        # numbered like the real panel
            self._text(p, QRectF(cx - 20, by + 40, 40, 16), num, 9, bold=True)

    def _jack(self, p, jid, r, kind, label):
        hl = jid in self.highlight
        hov = jid == self.hover
        tag = self.win.label(self.jack_tag_key(jid), "")
        if hl:
            glow = QtGui.QColor(ACCENT)
            glow.setAlpha(110)
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(r.adjusted(-9, -9, 9, 9))
        ring = QtGui.QColor(ACCENT) if hov else QtGui.QColor(150, 150, 155)
        if kind == "button":
            return
        p.setPen(QtGui.QPen(ring, 2.5 if hov else 1.5))
        p.setBrush(QtGui.QColor(18, 18, 20))
        if kind == "db15":
            p.drawRoundedRect(r, 6, 6)
        elif kind == "fw":
            p.drawRoundedRect(r, 3, 3)
        elif kind == "xlr":
            p.drawEllipse(r)
            p.setBrush(QtGui.QColor(60, 60, 64))
            c = r.center()
            for a in (210, 330, 90):
                p.drawEllipse(QPointF(c.x() + math.cos(math.radians(a)) * r.width() * 0.22,
                                      c.y() - math.sin(math.radians(a)) * r.width() * 0.22), 4, 4)
        elif kind == "combo":
            p.drawEllipse(r)
            p.setBrush(QtGui.QColor(60, 60, 64))
            p.drawEllipse(r.center(), r.width() * 0.18, r.width() * 0.18)
        else:
            if kind == "rca_w":
                p.setPen(QtGui.QPen(QtGui.QColor(235, 235, 235), 3))
            elif kind == "rca_r":
                p.setPen(QtGui.QPen(QtGui.QColor("#e5534b"), 3))
            p.drawEllipse(r)
            p.setBrush(QtGui.QColor(5, 5, 5))
            p.setPen(Qt.NoPen)
            p.drawEllipse(r.center(), r.width() * 0.22, r.width() * 0.22)
        short = re.sub(r"\s*\(.*\)", "", label).replace("Output ", "").replace("Line input ", "") \
            .replace("Insert ", "").replace("Mic input ", "0").replace("Aux in ", "")
        if kind in ("trs", "rca_w", "rca_r") and not jid.startswith(("foot",)):
            self._text(p, QRectF(r.x() - 10, r.y() - 15, r.width() + 20, 13), short.split(" /")[0], 7.5)
        if tag:
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(ACCENT))
            p.drawEllipse(QPointF(r.right() - 2, r.top() + 2), 5, 5)

    # interaction -----------------------------------------------------
    def _hit(self, pos):
        s, ox = self._scale()
        x, y = (pos.x() - ox) / s, pos.y() / s
        for jid, r, kind, label in self.shapes:
            if r.adjusted(-4, -4, 4, 4).contains(QPointF(x, y)):
                return jid, label
        return None, None

    def mouseMoveEvent(self, e):
        jid, label = self._hit(e.position())
        if jid != self.hover:
            self.hover = jid
            if jid and jid.startswith("sw"):
                QtWidgets.QToolTip.showText(e.globalPosition().toPoint(),
                                            f"<b>{label}</b><br>The app can't see this button — click here "
                                            "whenever you press it on the Mbox, so the right name shows.")
            elif jid:
                tag = self.win.label(self.jack_tag_key(jid), "")
                QtWidgets.QToolTip.showText(e.globalPosition().toPoint(),
                                            f"<b>{label}</b><br>{tag or 'click to tag what is plugged in'}")
            self.update()

    def mousePressEvent(self, e):
        jid, _ = self._hit(e.position())
        if jid == "phantom":
            self.phantomClicked.emit()
        elif jid and jid.startswith("sw"):
            self.win.toggle_input_side(int(jid[2]) - 1)
        elif jid:
            self.jackClicked.emit(jid)

    def label_for(self, jid):
        for j, _, _, label in self.shapes:
            if j == jid:
                return label
        return jid


# ============================================================ tuner

NOTE_NAMES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"]


class TunerWorker(QtCore.QThread):
    pitch = QtCore.Signal(float)
    failed = QtCore.Signal(str)

    def __init__(self, source, channel, rate=48000):
        super().__init__()
        self.source, self.channel, self.rate = source, channel, rate
        self.proc, self.running = None, True

    def run(self):
        try:
            import numpy as np
        except ImportError:
            self.failed.emit("The tuner needs numpy:  " + install_hint("numpy"))
            return
        if not shutil.which("parec"):
            self.failed.emit("The tuner needs parec (package: libpulse).")
            return
        cmap = ",".join(f"aux{i}" for i in range(8))
        cmd = ["parec", "-d", self.source, "--raw", "--format=float32le", f"--rate={self.rate}",
               "--channels=8", f"--channel-map={cmap}", "--latency-msec=40"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.failed.emit(str(e))
            return
        n = 4096
        frame_bytes = n * 8 * 4
        while self.running:
            data = self.proc.stdout.read(frame_bytes)
            if not data or len(data) < frame_bytes:
                break
            x = np.frombuffer(data, dtype=np.float32).reshape(-1, 8)[:, self.channel]
            if np.sqrt(np.mean(x * x)) < 0.003:
                self.pitch.emit(0.0)
                continue
            x = x - x.mean()
            spec = np.fft.rfft(x * np.hanning(n), 2 * n)
            ac = np.fft.irfft(np.abs(spec) ** 2)[:n]
            lo, hi = int(self.rate / 1200), int(self.rate / 30)
            seg = ac[lo:hi]
            if seg.size == 0 or ac[0] <= 0:
                continue
            i = int(np.argmax(seg)) + lo
            if ac[i] / ac[0] < 0.3:
                self.pitch.emit(0.0)
                continue
            if 1 <= i < n - 1:
                a, b, c = ac[i - 1], ac[i], ac[i + 1]
                d = (a - c) / (2 * (a - 2 * b + c)) if (a - 2 * b + c) else 0
                i = i + d
            self.pitch.emit(self.rate / i)
        self.stop()

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class TunerDisplay(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.freq = 0.0
        self.setMinimumHeight(170)

    def set_freq(self, f):
        self.freq = f
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor(20, 21, 24))
        if self.freq <= 0:
            p.setPen(QtGui.QColor(255, 255, 255, 90))
            f = p.font()
            f.setPointSize(14)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "Play a note…")
            return
        midi = 69 + 12 * math.log2(self.freq / 440)
        nearest = round(midi)
        cents = (midi - nearest) * 100
        name = NOTE_NAMES[nearest % 12] + str(nearest // 12 - 1)
        good = abs(cents) < 5
        col = QtGui.QColor("#3fb950") if good else QtGui.QColor("#d29922")
        # scale
        y = h * 0.62
        p.setPen(QtGui.QPen(QtGui.QColor(90, 90, 95), 2))
        p.drawLine(int(w * 0.08), int(y), int(w * 0.92), int(y))
        for c in range(-50, 51, 10):
            x = w / 2 + c / 50 * w * 0.42
            p.drawLine(int(x), int(y - (12 if c == 0 else 6)), int(x), int(y + (12 if c == 0 else 6)))
        x = w / 2 + max(-50, min(50, cents)) / 50 * w * 0.42
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(x, y), 11, 11)
        f = p.font()
        f.setPointSize(30)
        f.setBold(True)
        p.setFont(f)
        p.setPen(col)
        p.drawText(QRectF(0, 0, w, h * 0.45), Qt.AlignCenter, name)
        f.setPointSize(10)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QtGui.QColor(200, 200, 200))
        p.drawText(QRectF(0, h * 0.75, w, 24), Qt.AlignCenter,
                   f"{self.freq:.1f} Hz   {cents:+.0f} cents")




# ================================================================== built-in scenes

SCENES = [
    {"id": "listen", "name": "🔊  Listening", "short": "Computer → your amplifier only",
     "what": "Computer playback → Outputs 1/2 (your amplifier), nothing else in that mix.",
     "check": ["Receiver on AUX, switcher set to the Mbox."],
     "jacks": ["out1", "out2"]},
    {"id": "rip", "name": "📼  Rip tape / vinyl", "short": "Aux In 5/6 → computer + speakers",
     "what": "Aux In 5/6 (RCA) → computer, and you hear the source through Outputs 1/2 while it records.",
     "check": ["Deck or phono preamp OUT → rear Aux Input 5/6 (RCA).",
               "Nothing in the 1/8\" Aux jack — it overrides the RCAs.",
               "Then open the Record page and hit Record. Aim for peaks around −6 dB."],
     "jacks": ["aux5", "aux6", "out1", "out2"]},
    {"id": "tape", "name": "📼  Make a tape", "short": "Computer → Outputs 3/4 → tape deck",
     "what": "Computer playback → Outputs 3/4 as well as 1/2, so whatever plays also feeds the deck.",
     "check": ["Rear Outputs 3 and 4 → tape deck LINE IN (1/4\" TRS-to-RCA cables).",
               "Set record level on the deck."],
     "jacks": ["out3", "out4"]},
    {"id": "record", "name": "🎤  Record mic / instrument",
     "short": "Inputs 1–4 → computer + Headphone A",
     "what": "Inputs 1–4 → computer, with zero-latency monitoring of inputs 1–4 plus computer "
             "playback on Headphone A.",
     "check": ["Inputs 1–2: Front/Rear switch OUT for the front Mic/DI jacks.",
               "Inputs 3–4: Mic/Line switch OUT for the rear XLRs.",
               "+48V only for condenser mics — it powers all four at once.",
               "Headphones in jack A."],
     "jacks": ["front1", "front2", "mic3", "mic4", "hpA"]},
    {"id": "guitar", "name": "🎸  Guitar through the computer",
     "short": "Input 1 → Guitarix → speakers",
     "what": "Starts guitar mode: lowest latency, dry guitar muted, your effects app opened and "
             "wired up (Input 1 → app → speakers).",
     "check": ["Guitar into front Mic/DI 1 (Front/Rear button OUT, gain knob pushed IN).",
               "Turn the guitar's own volume up.",
               "When you're done: Effects page → Stop guitar mode."],
     "jacks": ["front1", "out1", "out2"]},
]


# ================================================================== ripping

class Recorder(QtCore.QObject):
    """Records a PipeWire source to FLAC with pw-record."""
    finished = QtCore.Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.proc, self.path, self.started = None, None, 0.0

    def start(self, target, path, rate):
        if not shutil.which("pw-record"):
            raise RuntimeError("pw-record isn't installed (package: pipewire).")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cmd = ["pw-record", "--target", target, "--rate", str(rate), "--channels", "2",
               "--format", "s24", str(path)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.path, self.started = str(path), time.time()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def elapsed(self):
        return time.time() - self.started if self.running() else 0.0

    def stop(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        ok = Path(self.path).exists() and Path(self.path).stat().st_size > 4096
        self.proc = None
        self.finished.emit(self.path, ok)


class TrimJob(QtCore.QThread):
    """Cuts a recording down to [start, end] seconds in place."""
    done = QtCore.Signal(str, str)

    def __init__(self, path, start, end):
        super().__init__()
        self.path, self.start_s, self.end_s = Path(path), start, end

    def run(self):
        tmp = self.path.with_suffix(".trim.flac")
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.path),
                            "-ss", f"{self.start_s:.2f}", "-to", f"{self.end_s:.2f}", "-c:a", "flac", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            self.done.emit(str(self.path), r.stderr.strip()[-200:] or "ffmpeg failed")
            return
        tmp.replace(self.path)
        self.done.emit(str(self.path), "")


def audio_duration(path):
    out = run_quiet("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                    "default=nw=1:nk=1", str(path), timeout=30)
    try:
        return float(out.strip())
    except (AttributeError, ValueError):
        return 0.0


class SilenceSplitter(QtCore.QThread):
    """Finds track boundaries at silences with ffmpeg's silencedetect."""
    done = QtCore.Signal(list, float, str)

    def __init__(self, path, threshold_db=-45, min_silence=2.0):
        super().__init__()
        self.path, self.th, self.min_sil = path, threshold_db, min_silence

    def run(self):
        if not shutil.which("ffmpeg"):
            self.done.emit([], 0.0, "ffmpeg isn't installed:  " + install_hint("ffmpeg"))
            return
        dur = audio_duration(self.path)
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", self.path, "-af",
               f"silencedetect=noise={self.th}dB:d={self.min_sil}", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
        ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
        silences = list(zip(starts, ends + [dur] * (len(starts) - len(ends))))
        tracks, pos = [], 0.0
        for s, e in silences:
            if s - pos > 5:
                tracks.append((pos, s))
            pos = e
        if dur - pos > 5:
            tracks.append((pos, dur))
        if not tracks and dur > 0:
            tracks = [(0.0, dur)]
        self.done.emit(tracks, dur, "")


class Exporter(QtCore.QThread):
    progress = QtCore.Signal(int, int)
    done = QtCore.Signal(str, str)

    def __init__(self, src, tracks, folder, artist, album, year, af="", first=1, layout="artist",
                 cover=None, originals=None, replaygain=False):
        super().__init__()
        self.replaygain = replaygain
        self.af, self.first, self.layout, self.cover = af, first, layout, cover
        self.originals = originals or []
        self.src, self.tracks, self.folder = src, tracks, Path(folder)
        self.artist, self.album, self.year = artist, album, year

    def run(self):
        if not shutil.which("ffmpeg"):
            self.done.emit("", "ffmpeg isn't installed:  " + install_hint("ffmpeg"))
            return
        out_dir = album_dir(self.folder, self.artist, self.album, self.layout)
        out_dir.mkdir(parents=True, exist_ok=True)
        album_rg = []
        if self.replaygain:
            a0, b1 = self.tracks[0][0], self.tracks[-1][1]
            lufs, pk = measure_loudness(self.src, a0, b1 - a0)
            if lufs is not None:
                album_rg = rg_tags("ALBUM", lufs, max(measure_loudness(self.src, a, b - a)[1] or -70
                                                      for a, b, _ in self.tracks))
        art = None
        if self.cover and Path(self.cover).exists():
            art = out_dir / "folder.jpg"
            if Path(self.cover).resolve() != art.resolve() and not prepare_cover(self.cover, art):
                art = None
        n = len(self.tracks)
        for i, (start, end, title) in enumerate(self.tracks, self.first):
            out = out_dir / track_filename(i, title, self.layout)
            orig = self.originals[i - self.first] if i - self.first < len(self.originals) else ""
            track_rg = []
            if self.replaygain:
                lufs, pk = measure_loudness(self.src, start, end - start)
                if lufs is not None:
                    track_rg = rg_tags("TRACK", lufs, pk) + album_rg
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", self.src,
                   *(["-i", str(art)] if art else []),
                   "-t", f"{end - start:.3f}", *(["-af", self.af] if self.af else []),
                   "-map", "0:a", "-c:a", "flac",
                   *(["-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic",
                      "-metadata:s:v", "comment=Cover (front)"] if art else []),
                   *(["-metadata", f"ORIGINALTITLE={orig}"] if orig else []), *track_rg,
                   "-metadata", f"title={title or f'Track {i}'}", "-metadata", f"artist={self.artist}",
                   "-metadata", f"album_artist={self.artist}", "-metadata", f"album={self.album}",
                   "-metadata", f"track={i}"]
            if self.year:
                cmd += ["-metadata", f"date={self.year}"]
            r = subprocess.run(cmd + [str(out)], capture_output=True, text=True)
            if r.returncode != 0:
                self.done.emit("", r.stderr.strip()[-300:])
                return
            self.progress.emit(i - self.first + 1, n)
        self.done.emit(str(out_dir), "")


class MusicBrainzLookup(QtCore.QThread):
    """Looks up a release's track list by artist + album title."""
    done = QtCore.Signal(list, str)
    UA = f"MboxControl/{VERSION} (personal Linux control panel for an Avid Mbox Pro)"

    def __init__(self, artist, album):
        super().__init__()
        self.artist, self.album = artist, album

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": self.UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def run(self):
        try:
            q = f'release:"{self.album}"' + (f' AND artist:"{self.artist}"' if self.artist else "")
            data = self._get("https://musicbrainz.org/ws/2/release/?fmt=json&limit=8&query="
                             + urllib.parse.quote(q))
            results = []
            for rel in data.get("releases", [])[:8]:
                artist = "".join(c.get("name", "") + c.get("joinphrase", "")
                                 for c in rel.get("artist-credit", []))
                fmt = ", ".join(m.get("format", "?") for m in rel.get("media", []) if m.get("format"))
                results.append({"id": rel["id"], "title": rel.get("title", ""), "artist": artist,
                                "date": rel.get("date", ""), "country": rel.get("country", ""),
                                "format": fmt, "tracks": rel.get("track-count", 0)})
            self.done.emit(results, "" if results else "No matches on MusicBrainz.")
        except Exception as e:  # noqa: BLE001
            self.done.emit([], f"Lookup failed: {e}")


class MusicBrainzTracks(QtCore.QThread):
    done = QtCore.Signal(list, str, str)

    def __init__(self, release_id):
        super().__init__()
        self.rid = release_id

    def run(self):
        try:
            req = urllib.request.Request(
                f"https://musicbrainz.org/ws/2/release/{self.rid}?inc=recordings&fmt=json",
                headers={"User-Agent": MusicBrainzLookup.UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            titles = [t.get("title", "") for m in data.get("media", []) for t in m.get("tracks", [])]
            self.done.emit(titles, (data.get("date") or "")[:4], "")
        except Exception as e:  # noqa: BLE001
            self.done.emit([], "", f"Couldn't fetch tracks: {e}")


# ================================================================== phone remote

REMOTE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#14161a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Mbox">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png"><link rel="apple-touch-icon" href="/icon-192.png">
<title>Mbox Remote</title><style>
:root{--bg:#14161a;--card:#1b1e23;--c2:#23272e;--fg:#e8eaed;--sub:#8b93a1;--acc:#5aa9ff;--red:#f85149;--grn:#3fb950}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;-webkit-user-select:none;user-select:none}
body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif;
 padding:calc(14px + env(safe-area-inset-top)) 14px calc(20px + env(safe-area-inset-bottom))}
header{display:flex;align-items:center;gap:10px;margin:2px 2px 12px}
header h1{font-size:20px;margin:0;flex:1}
.dot{width:10px;height:10px;border-radius:50%;background:var(--grn)}.dot.off{background:var(--red)}
.card{background:var(--card);border-radius:18px;padding:14px;margin-bottom:12px}
.lbl{display:flex;justify-content:space-between;color:var(--sub);font-size:13px;margin:10px 0 6px}
.lbl:first-child{margin-top:0}
.row{display:flex;gap:8px}.row>*{flex:1}
button{background:var(--c2);color:var(--fg);border:1px solid #2f3540;border-radius:14px;padding:14px 10px;
 font:600 15px system-ui;width:100%;min-height:52px}
button:active{transform:scale(.97)}
.spk small{display:block;font-weight:400;font-size:12px;color:var(--sub);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spk.on small{color:#0b0d10}
.mute{flex:0 0 92px}.mute.on{background:var(--red);border-color:var(--red);color:#fff}
input[type=range]{flex:1;width:100%;accent-color:var(--acc);height:40px;margin:0}
.bar{height:8px;background:#2a2f37;border-radius:6px;overflow:hidden}.bar>div{height:100%;background:var(--grn);width:0;transition:width .15s}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.scene{text-align:left;font-size:14px;padding:12px}
.scene small{display:block;color:var(--sub);font-weight:400;font-size:12px;margin-top:3px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{padding:5px 10px;border-radius:10px;background:var(--c2);font-size:13px;font-weight:600}
#rec .big{font:700 34px ui-monospace,monospace;margin:4px 0}
#rec.live{outline:2px solid var(--red)}
#takebtn.on,#recbtn.on{background:var(--red);border-color:var(--red);color:#fff}
.sub{color:var(--sub);font-size:13px;line-height:1.35}
h2{font-size:12px;color:var(--sub);font-weight:700;margin:16px 4px 8px;text-transform:uppercase;letter-spacing:.08em}
#toast{position:fixed;left:50%;bottom:calc(18px + env(safe-area-inset-bottom));transform:translateX(-50%);
 background:#2b313a;padding:10px 16px;border-radius:12px;font-size:14px;opacity:0;transition:opacity .3s;pointer-events:none}
</style></head><body>
<header><div class="dot" id="conn"></div><h1>Mbox</h1><div class="chips"><span class="chip" id="rate"></span></div></header>

<div class="card">
 <div class="row" id="spkrow">
  <button class="spk" data-l="A">A<small></small></button>
  <button class="spk" data-l="B">B<small></small></button>
  <button class="spk" data-l="C">C<small></small></button>
 </div>
 <div class="lbl"><span>Volume</span><span id="volv"></span></div>
 <div class="row" style="align-items:center"><input type="range" id="vol" min="0" max="255">
  <button class="mute" id="mute">Mute</button></div>
 <div class="lbl"><span>Computer</span></div><div class="bar"><div id="m_pc"></div></div>
 <div class="lbl"><span id="auxl">Aux in 5/6</span></div><div class="bar"><div id="m_aux"></div></div>
</div>

<div class="card" id="rec">
 <div class="lbl"><span id="recsrc">Record</span><span id="recq"></span></div>
 <div class="big" id="rectime">0:00</div>
 <div class="bar"><div id="m_rec"></div></div>
 <p class="sub" id="recinfo"></p>
 <div class="row"><button id="takebtn">🎙 Quick take</button><button id="recbtn">● Record</button></div>
 <button id="retrobtn" style="margin-top:8px;display:none">💾 Save what I just played</button>
 <p class="sub" id="saving"></p>
</div>

<h2>Scenes</h2><div class="grid" id="scenes"></div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let vt=null,busy=0,fails=0,last={};
const qk=new URLSearchParams(location.search).get('k');let K='';
try{if(qk)localStorage.setItem('mboxkey',qk);K=qk||localStorage.getItem('mboxkey')||''}catch(e){K=qk||''}
if(qk)history.replaceState(null,'','/');
function toast(t){const e=$('#toast');e.textContent=t;e.style.opacity=1;clearTimeout(e.t);e.t=setTimeout(()=>e.style.opacity=0,1800)}
async function go(a,b){try{const r=await fetch('/do/'+a+'/'+encodeURIComponent(b||''),{method:'POST',headers:{'X-Mbox-Key':K}});
 if(r.status==403)locked();setTimeout(tick,150)}catch(e){toast('Not connected')}}
function locked(){document.body.innerHTML='<div style="padding:40px 20px;text-align:center"><h1>🔒 Mbox</h1><p class="sub" style="font-size:16px">This phone isn\'t paired.<br><br>On the computer open <b>Mbox Control → Remote</b> and scan the QR code.</p></div>';clearTimeout(tick.t);tick.t=null;locked.on=1}
document.querySelectorAll('#spkrow button').forEach(b=>b.onclick=()=>{go('speakers',b.dataset.l);if(navigator.vibrate)navigator.vibrate(15)});
$('#mute').onclick=()=>go('mute',last.mute?'0':'1');
$('#recbtn').onclick=()=>go('record','');$('#takebtn').onclick=()=>go('take','');$('#retrobtn').onclick=()=>{go('retro','');toast('Saved the last minute')};
$('#vol').oninput=e=>{busy=Date.now();$('#volv').textContent=Math.round(e.target.value/2.55)+'%';clearTimeout(vt);vt=setTimeout(()=>go('volume',e.target.value),90)};
function setBar(id,v){$(id).style.width=Math.max(0,Math.min(100,v))+'%'}
function render(s){last=s;
 $('#rate').textContent=s.rate;
 document.querySelectorAll('#spkrow button').forEach(b=>{const L=b.dataset.l,on=L==s.speaker;
  b.classList.toggle('on',on);b.style.background=on?(s.speaker_colour||'var(--acc)'):'';b.style.borderColor=on?(s.speaker_colour||'var(--acc)'):'';
  b.style.color=on?'#0b0d10':'';b.querySelector('small').textContent=(s.spk_names||{})[L]||''});
 if(Date.now()-busy>1500){$('#vol').value=s.volume;$('#volv').textContent=Math.round(s.volume/2.55)+'%'}
 $('#mute').classList.toggle('on',!!s.mute);$('#mute').textContent=s.mute?'Muted':'Mute';
 setBar('#m_pc',s.m_pc);setBar('#m_aux',s.m_aux);if(s.aux_label)$('#auxl').textContent=s.aux_label;
 const r=s.rec||{},tk=r.state=='take';$('#rec').classList.toggle('live',r.state=='rec'||tk);
 $('#recsrc').textContent=tk?'Quick take':'Record · '+(r.src||'');
 $('#takebtn').classList.toggle('on',tk);$('#takebtn').textContent=tk?'■ Stop take':'🎙 Quick take';
 $('#takebtn').style.display=(r.state=='rec'||r.state=='armed')?'none':'';$('#recbtn').style.display=tk?'none':'';$('#recq').textContent=r.queue||'';
 $('#rectime').textContent=r.time||'0:00';setBar('#m_rec',r.level||0);$('#recinfo').textContent=r.info||'';
 const b=$('#recbtn');b.classList.toggle('on',r.state=='rec'||r.state=='armed');
 b.textContent=r.state=='armed'?'■ Disarm':r.state=='rec'?'■ Stop':(r.auto?'● Arm':'● Record');
 $('#saving').textContent=r.saving||'';$('#retrobtn').style.display=r.retro?'':'none';
 const sc=$('#scenes'),key=JSON.stringify(s.scenes);if(sc.dataset.k!=key){sc.innerHTML='';s.scenes.forEach(x=>{const e=document.createElement('button');
  e.className='scene';e.innerHTML='';e.append(x.name);const sm=document.createElement('small');sm.textContent=x.short;e.append(sm);
  e.onclick=()=>{go('scene',x.id);toast(x.name)};sc.appendChild(e)});sc.dataset.k=key}}
async function tick(){if(locked.on)return;clearTimeout(tick.t);try{const rr=await fetch('/state',{cache:'no-store',headers:{'X-Mbox-Key':K}});
 if(rr.status==403){locked();return}const s=await rr.json();fails=0;$('#conn').classList.remove('off');render(s)}
 catch(e){if(++fails>2)$('#conn').classList.add('off')}tick.t=setTimeout(tick,document.hidden?3000:500)}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick()});tick();
if('serviceWorker' in navigator&&window.isSecureContext)navigator.serviceWorker.register('/sw.js').catch(()=>{});
</script></body></html>"""


REMOTE_MANIFEST = json.dumps({
    "name": "Mbox Remote", "short_name": "Mbox", "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#14161a", "theme_color": "#14161a",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]})

REMOTE_SW = "self.addEventListener('fetch', () => {});"   # installable when served over HTTPS; nothing cached


def remote_icon_png(size):
    """Home-screen icon: the app icon on the remote's dark background (safe zone for round masks)."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtGui.QColor("#14161a"))
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
    pad = int(size * 0.18)
    try:
        from PySide6.QtSvg import QSvgRenderer
        QSvgRenderer(QtCore.QByteArray(ICON_SVG.encode())).render(p, QRectF(pad, pad, size - 2 * pad, size - 2 * pad))
    except ImportError:
        p.drawPixmap(pad, pad, app_icon().pixmap(size - 2 * pad, size - 2 * pad))
    p.end()
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.WriteOnly)
    pm.save(buf, "PNG")
    return bytes(buf.data())


class RemoteBridge(QtCore.QObject):
    command = QtCore.Signal(str, str)


class RemoteServer:
    def __init__(self, bridge):
        self.bridge, self.httpd, self.thread = bridge, None, None
        self.state = {}
        self.lock = threading.Lock()
        self.files = {}                               # path → (bytes, content type); filled on the GUI thread
        self.token = ""                               # phones must know it (it's in the QR link)

    def set_state(self, st):
        with self.lock:
            self.state = st

    def start(self, port=REMOTE_PORT):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _authed(self):
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                given = (q.get("k") or [""])[0] or self.headers.get("X-Mbox-Key", "")
                import hmac
                return bool(srv.token) and hmac.compare_digest(given, srv.token)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == "/state" and not self._authed():
                    self._send(403, b'{"locked":true}', "application/json")
                    return
                self.path = path
                if self.path in ("/", "/index.html"):
                    self._send(200, REMOTE_HTML.encode(), "text/html; charset=utf-8")
                elif self.path in srv.files:
                    body, ctype = srv.files[self.path]
                    self._send(200, body, ctype)
                elif self.path == "/state":
                    with srv.lock:
                        body = json.dumps(srv.state).encode()
                    self._send(200, body, "application/json")
                else:
                    self._send(404)

            def do_POST(self):
                if not self._authed():
                    self._send(403)
                    return
                m = re.fullmatch(r"/do/(\w+)/(.*)", urllib.parse.urlsplit(self.path).path)
                if not m:
                    self._send(404)
                    return
                srv.bridge.command.emit(m.group(1), urllib.parse.unquote(m.group(2)))
                self._send(204)

        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def running(self):
        return self.httpd is not None


class MiniMbox(QtWidgets.QWidget):
    """Small live drawing of the Mbox for the sidebar. Click to open the Hardware page."""
    clicked = QtCore.Signal()

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setFixedHeight(128)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Your Mbox, live — click to open the Hardware page")

    def mousePressEvent(self, _):
        self.clicked.emit()

    def paintEvent(self, _):
        w = self.win
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        W = self.width() - 24
        x0, y0, H = 12, 18, 56
        # body with a soft shadow
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, 90))
        p.drawRoundedRect(QRectF(x0 + 2, y0 + 5, W, H), 16, 16)
        body = QtGui.QLinearGradient(0, y0, 0, y0 + H)
        body.setColorAt(0, QtGui.QColor(70, 73, 79))
        body.setColorAt(0.08, QtGui.QColor(40, 42, 47))
        body.setColorAt(1, QtGui.QColor(22, 23, 26))
        p.setBrush(body)
        p.setPen(QtGui.QPen(QtGui.QColor(150, 152, 158), 1.6))
        p.drawRoundedRect(QRectF(x0, y0, W, H), 16, 16)
        s = W / 176.0
        cy = y0 + H / 2

        def circ(cx, r, fill, ring=(15, 15, 15)):
            p.setPen(QtGui.QPen(QtGui.QColor(*ring), 1))
            p.setBrush(QtGui.QColor(fill))
            p.drawEllipse(QPointF(x0 + cx * s, cy), r * s, r * s)

        # combo jacks + gain knobs
        for cx in (14, 40):
            circ(cx, 7, "#141416", (120, 120, 125))
        for cx in (26, 52, 64, 76):
            circ(cx, 4.2, "#6b6e74")
        # LED ladders (live)
        for i in range(4):
            lvl = w.input_level(f"Analog-A-{i + 1}")
            lit = int(max(0, min(5, (meter_db(lvl) + 48) / 9.6 + 0.5))) if lvl else 0
            for seg in range(5):
                on = seg < lit
                col = "#f85149" if seg == 4 else "#e3a52a" if seg == 3 else "#3fb950"
                p.setPen(Qt.NoPen)
                p.setBrush(QtGui.QColor(col) if on else QtGui.QColor(48, 50, 48))
                p.drawRect(QRectF(x0 + (88 + i * 4.2) * s, cy + 8 * s - seg * 3.6 * s, 3 * s, 2.4 * s))
        # +48V
        ph = w.ctrl("phantom-powering")
        on48 = bool(ph and ph.values[0])
        circ(108, 2.6, "#f85149" if on48 else "#3a3c40", (0, 0, 0))
        # headphone knobs
        circ(122, 4, "#6b6e74")
        circ(134, 4, "#6b6e74")
        # Spkr LED
        spk = w.ctrl_item("spkr-button") or "Off"
        sc = SPEAKERS.get(spk.split("-")[0])
        circ(147, 2.8, sc[1] if sc else "#3a3c40", (0, 0, 0))
        # big knob (live)
        knob = w.ctrl("master-knob-value")
        v = knob.values[0] / 255 if knob else 0
        kx, kr = x0 + 163 * s, 10.5 * s
        p.setPen(QtGui.QPen(QtGui.QColor(10, 10, 10), 1))
        p.setBrush(QtGui.QColor(58, 60, 65))
        p.drawEllipse(QPointF(kx, cy), kr, kr)
        ang = math.radians(225 - 270 * v)
        p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(kx, cy), QPointF(kx + math.cos(ang) * kr * 0.75, cy - math.sin(ang) * kr * 0.75))
        # caption
        p.setPen(QtGui.QColor(SUBTLE))
        f = p.font()
        f.setPointSizeF(8.5)
        p.setFont(f)
        rate = w.rate() / 1000
        cap = f"Mbox Pro · {rate:g} kHz" + (" · +48V" if on48 else "")
        p.drawText(QRectF(0, y0 + H + 10, self.width(), 18), Qt.AlignCenter, cap)


class FireWireBadge(QtWidgets.QWidget):
    """A cheeky nod to the bus everyone else gave up on: a 6-pin IEEE 1394 port, drawn from scratch."""
    AMBER = "#f2b33d"

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setFixedHeight(40)
        self.setContentsMargins(0, 0, 0, 0)
        self.setCursor(Qt.PointingHandCursor)

    def days_alive(self):
        try:
            since = date.fromisoformat(self.win.app_state.get("revived", ""))
        except ValueError:
            return 0
        return (date.today() - since).days

    def enterEvent(self, _):
        d = self.days_alive()
        self.setToolTip("IEEE 1394 / FireWire 400 — released 1995.\n"
                        "Apple pulled it from macOS. Avid stopped updating the software.\n"
                        + ("This Mbox came back from the dead today." if d == 0 else
                           f"This Mbox has been back from the dead for {d} day{'s' if d != 1 else ''}."))

    def mousePressEvent(self, _):
        self.win.toast(next(self.quips), 5000)

    quips = iter(())

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        amber = QtGui.QColor(self.AMBER)
        # the port: a rectangle with one rounded "bullet" end, like the real 6-pin socket
        pw, ph = 34.0, 16.0
        x, y = 22.0, (self.height() - ph) / 2
        path = QtGui.QPainterPath()
        r = ph / 2
        path.moveTo(x + 2, y)
        path.lineTo(x + pw - r, y)
        path.arcTo(QRectF(x + pw - ph, y, ph, ph), 90, -180)
        path.lineTo(x + 2, y + ph)
        path.quadTo(x, y + ph, x, y + ph - 2)
        path.lineTo(x, y + 2)
        path.quadTo(x, y, x + 2, y)
        p.setPen(QtGui.QPen(amber, 1.6))
        p.setBrush(QtGui.QColor("#101215"))
        p.drawPath(path)
        # the centre tongue with six contacts (three a side)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor("#3a3d44"))
        tongue = QRectF(x + 4, y + ph / 2 - 2.2, pw - 10, 4.4)
        p.drawRoundedRect(tongue, 1.2, 1.2)
        p.setBrush(amber)
        for i in range(3):
            cx = tongue.left() + 3.5 + i * (tongue.width() - 7) / 2
            p.drawRect(QRectF(cx - 1.4, tongue.top() - 1.6, 2.8, 1.6))
            p.drawRect(QRectF(cx - 1.4, tongue.bottom(), 2.8, 1.6))
        # words
        f = p.font()
        f.setPointSizeF(8.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(amber)
        tx = x + pw + 10
        p.drawText(QRectF(tx, 2, self.width() - tx, 18), Qt.AlignLeft | Qt.AlignBottom, "IEEE 1394 · still kicking")
        f.setBold(False)
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(QtGui.QColor(SUBTLE))
        p.drawText(QRectF(tx, 20, self.width() - tx, 16), Qt.AlignLeft | Qt.AlignTop, "400 Mbit/s of pure spite")


FIREWIRE_QUIPS = [
    "FireWire: hot-pluggable since before USB 2.0 existed.",
    "Apple invented FireWire, then left it in a ditch. We picked it up.",
    "Isochronous transfer: fancy talk for ‘the audio arrives on time’.",
    "Daisy-chain up to 63 devices. You have one. It's enough.",
    "Avid says this box is end-of-life. The box disagrees.",
    "Running on snd-dice and stubbornness.",
]




# ================================================================== app icon

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<defs>
 <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2b303a"/><stop offset="1" stop-color="#121418"/></linearGradient>
 <radialGradient id="cap" cx=".4" cy=".35" r=".75"><stop offset="0" stop-color="#5a5f68"/><stop offset="1" stop-color="#25282d"/></radialGradient>
</defs>
<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#bg)"/>
<path d="M75.3 180.7 A74.5 74.5 0 1 1 180.7 180.7" fill="none" stroke="#ffffff" stroke-opacity=".12" stroke-width="16" stroke-linecap="round"/>
<path d="M75.3 180.7 A74.5 74.5 0 1 1 206.2 100.9" fill="none" stroke="#5aa9ff" stroke-width="16" stroke-linecap="round"/>
<circle cx="128" cy="128" r="50" fill="url(#cap)" stroke="#0b0c0e" stroke-width="4"/>
<line x1="128" y1="128" x2="158" y2="104" stroke="#ffffff" stroke-width="10" stroke-linecap="round"/>
<circle cx="203" cy="203" r="11" fill="#3fb950"/>
</svg>"""


def app_icon():
    """Render the icon with QtSvg if available; otherwise paint a simple version."""
    icon = QtGui.QIcon()
    try:
        from PySide6.QtSvg import QSvgRenderer
        r = QSvgRenderer(QtCore.QByteArray(ICON_SVG.encode()))
        for s in (16, 22, 24, 32, 48, 64, 128, 256):
            pm = QtGui.QPixmap(s, s)
            pm.fill(Qt.transparent)
            p = QtGui.QPainter(pm)
            r.render(p)
            p.end()
            icon.addPixmap(pm)
        return icon
    except Exception:  # noqa: BLE001
        pass
    for s in (16, 32, 64, 256):
        pm = QtGui.QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setBrush(QtGui.QColor(BG2))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
        p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), max(1.5, s / 14), Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(s * 0.25, s * 0.25, s * 0.5, s * 0.5), 225 * 16, -200 * 16)
        p.end()
        icon.addPixmap(pm)
    return icon


def knob_icon(colour=ACCENT, muted=False, value=0.75):
    """Square, transparent, tray-friendly knob icon. Ring = speaker-set colour; red + slash when muted."""
    icon = QtGui.QIcon()
    for sz in (16, 22, 24, 32, 48, 64, 128):
        pm = QtGui.QPixmap(sz, sz)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w = max(2.0, sz * 0.12)
        r = QRectF(w / 2 + 0.5, w / 2 + 0.5, sz - w - 1, sz - w - 1)
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 60), w, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, 225 * 16, -270 * 16)
        ring = QtGui.QColor("#f85149" if muted else colour)
        pen.setColor(ring)
        p.setPen(pen)
        p.drawArc(r, 225 * 16, int(-270 * 16 * max(0.08, value)))
        cap = r.adjusted(w * 1.1, w * 1.1, -w * 1.1, -w * 1.1)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor("#d9dde3"))
        p.drawEllipse(cap)
        c = cap.center()
        ang = math.radians(225 - 270 * value)
        p.setPen(QtGui.QPen(QtGui.QColor("#1b1e23"), max(1.5, sz * 0.08), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c, QPointF(c.x() + math.cos(ang) * cap.width() * 0.38,
                              c.y() - math.sin(ang) * cap.width() * 0.38))
        if muted:
            p.setPen(QtGui.QPen(QtGui.QColor("#f85149"), max(2.0, sz * 0.11), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(sz * 0.18, sz * 0.82), QPointF(sz * 0.82, sz * 0.18))
        p.end()
        icon.addPixmap(pm)
    return icon


def install_icon_files():
    base = HOME / ".local/share/icons/hicolor"
    svg = base / "scalable/apps/mbox-control.svg"
    try:
        svg.parent.mkdir(parents=True, exist_ok=True)
        if not svg.exists() or svg.read_text() != ICON_SVG:
            svg.write_text(ICON_SVG)
            png = base / "256x256/apps/mbox-control.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            app_icon().pixmap(256, 256).save(str(png))
            run_quiet("gtk-update-icon-cache", "-f", "-t", str(base))
    except OSError:
        pass


def desktop_entry(extra_args=""):
    return ("[Desktop Entry]\nType=Application\nName=Mbox Control\nGenericName=Audio interface control\n"
            "Comment=Mixer, routing and recording for the Avid Mbox 3 Pro\n"
            f"Exec={shlex.quote(sys.executable)} {Path(__file__).resolve()}{extra_args}\nIcon=mbox-control\nTerminal=false\n"
            "Categories=AudioVideo;Audio;Mixer;\nStartupWMClass=mbox-control\nKeywords=mbox;avid;firewire;mixer;\n")


AUTOSTART_FILE = XDG_CONFIG / "autostart/mbox-control.desktop"
LAUNCHER_FILE = HOME / ".local/share/applications/mbox-control.desktop"


def refresh_launcher_if_old():
    """Upgrade an older launcher file (generic icon) to the new icon, quietly."""
    try:
        if LAUNCHER_FILE.exists() and "Icon=mbox-control" not in LAUNCHER_FILE.read_text():
            LAUNCHER_FILE.write_text(desktop_entry())
    except OSError:
        pass


# ================================================================== background service install

SERVICE_UNIT = HOME / ".config/systemd/user/mbox-ctl.service"
SERVICE_WRAPPER = HOME / ".local/bin/mbox-ctl-start"
WAKE_HOOK = Path("/usr/lib/systemd/system-sleep/mbox-rebind")   # the old (pre-4.20) wake fix; removed on install


def install_service_unit():
    """Service that finds the Mbox by name (not a fixed card number) and restarts itself."""
    SERVICE_WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_WRAPPER.write_text(
        "#!/bin/sh\n# Written by Mbox Control: start the control service for whichever card is the Mbox\n"
        "c=$(awk '/Mbox/ {print $1; exit}' /proc/asound/cards)\n"
        "[ -n \"$c\" ] || exit 1\n"
        f"exec \"{SERVICE_PATH}\" \"$c\"\n")
    SERVICE_WRAPPER.chmod(0o755)
    SERVICE_UNIT.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_UNIT.write_text(
        "[Unit]\nDescription=Mbox Pro control service\nStartLimitIntervalSec=0\n\n"
        f"[Service]\nExecStart={SERVICE_WRAPPER}\nRestart=always\nRestartSec=3\n\n"
        "[Install]\nWantedBy=default.target\n")
    run_quiet("systemctl", "--user", "daemon-reload")
    run_quiet("systemctl", "--user", "enable", "mbox-ctl.service")
    return run_quiet("systemctl", "--user", "restart", "mbox-ctl.service") is not None


def service_unit_ok():
    try:
        return SERVICE_UNIT.exists() and str(SERVICE_WRAPPER) in SERVICE_UNIT.read_text()
    except OSError:
        return False


MAIN_CONF = PW_CONF_DIR / "mbox-main.conf"
MAIN_SINK = "mbox_main"
OLD_MAIN_CONF, OLD_MAIN_SINK = PW_CONF_DIR / "mbox-sa600.conf", "sa600_sink"     # before 4.24


def chain_wanted(state):
    """The main output needs the filter chain when the EQ is on or any speaker set uses the subsonic filter."""
    return bool(state.get("eq_on")) or any(state.get("subsonic_sets", {}).values())


def write_main_conf(sink, desc="Speakers (Mbox)", eq=None):
    PW_CONF_DIR.mkdir(parents=True, exist_ok=True)
    if eq is None:
        eq = chain_wanted(load_json(STATE_FILE, {}))
    if eq:                                         # same node names and channels, with the EQ in between
        MAIN_CONF.write_text(eq_chain_conf(desc, MAIN_SINK, sink, ["AUX7", "AUX6"]))
        return
    MAIN_CONF.write_text(f"""# Written by Mbox Control
context.modules = [
  {{ name = libpipewire-module-loopback
    args = {{
      node.description = "{desc}"
      capture.props = {{
        node.name = "{MAIN_SINK}"
        media.class = "Audio/Sink"
        audio.position = [ FL FR ]
      }}
      playback.props = {{
        node.name = "mbox_main_out"
        target.object = "{sink}"
        audio.position = [ AUX7 AUX6 ]
        stream.dont-remix = true
        node.passive = true
      }}
    }}
  }}
]
""")


def migrate_main_output(state):
    """4.24 renamed the main output's PipeWire nodes (sa600_sink → mbox_main) and its config file.
    Rewrites the old file under the new names, fixes saved settings that name the old node, and says
    whether PipeWire needs a restart (and whether the old node was the default output). → (restart, was_default)"""
    blob = json.dumps(state)
    if OLD_MAIN_SINK in blob:
        state.clear()
        state.update(json.loads(blob.replace(OLD_MAIN_SINK, MAIN_SINK)))
    if not OLD_MAIN_CONF.exists():
        return False, False
    text = OLD_MAIN_CONF.read_text()
    text = text.replace('"sa600_sink_out"', '"mbox_main_out"').replace('"sa600_out"', '"mbox_main_out"')
    MAIN_CONF.write_text(text.replace(f'"{OLD_MAIN_SINK}"', f'"{MAIN_SINK}"'))
    OLD_MAIN_CONF.unlink()
    was_default = (run_quiet("pactl", "get-default-sink") or "").strip() == OLD_MAIN_SINK
    rlog(f"migrated {OLD_MAIN_CONF.name} → {MAIN_CONF.name} (default output: {was_default})")
    return True, was_default


def set_default_when_ready(name, tries=10):
    """After a PipeWire restart: make `name` the default output as soon as it's there."""
    def attempt(n):
        if pw_node_exists(name, "sink"):
            run_quiet("pactl", "set-default-sink", name)
        elif n > 0:
            QtCore.QTimer.singleShot(700, lambda: attempt(n - 1))
    attempt(tries)


def mbox_pw_card():
    out = run_quiet("pactl", "list", "cards", "short") or ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and "firewire" in parts[1]:
            return parts[1]
    return None


def pw_sink_inputs():
    """[(index, node_name, target, sink_index)] for every playback stream, from pactl's JSON."""
    try:
        data = json.loads(run_quiet("pactl", "-f", "json", "list", "sink-inputs", timeout=5) or "[]")
    except ValueError:
        return []
    out = []
    for si in data:
        props = si.get("properties", {})
        out.append((si.get("index"), props.get("node.name", ""), props.get("target.object", ""), si.get("sink")))
    return out


def pw_sinks():
    """{name: index} of every sink."""
    out = {}
    for line in (run_quiet("pactl", "list", "sinks", "short", timeout=5) or "").splitlines():
        f = line.split("\t")
        if len(f) > 1 and f[0].strip().isdigit():
            out[f[1]] = int(f[0])
    return out


OUR_LOOPBACKS = {"mbox_main_out", "mbox_headphones_out"}


def relink_mbox_streams():
    """When the Mbox drops out, PipeWire moves our loopbacks ("Speakers (Mbox)", "Headphones (Mbox)") to some
    other device and doesn't move them back. Put every stream that targets the Mbox back on it.
    Returns [(stream, from_index, to_sink)] for what was moved."""
    sinks = pw_sinks()
    mbox = next((n for n in sinks if "firewire" in n and "pro-output" in n), None)
    moved = []
    for idx, name, target, sink in pw_sink_inputs():
        if not target and name in OUR_LOOPBACKS:          # older PipeWire: target not in the props
            target = mbox or ""
        if not target or "firewire" not in target or target not in sinks or idx is None:
            continue
        if sink != sinks[target]:
            run_quiet("pactl", "move-sink-input", str(idx), target, timeout=5)
            moved.append((name or str(idx), sink, target))
    return moved


# ================================================================== sidebar "now" panel

class NowPanel(QtWidgets.QWidget):
    """Compact live status under the mini Mbox: speakers, knob, level, quick mute."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 2, 14, 6)
        lay.setSpacing(6)
        self.spk = Chip()
        self.spk.setMinimumHeight(28)
        lay.addWidget(self.spk)
        r1 = QtWidgets.QHBoxLayout()
        r1.addWidget(muted("Knob"))
        self.knob = ValueBar(255)
        self.knob.setMinimumWidth(80)
        r1.addWidget(self.knob, 1)
        lay.addLayout(r1)
        r2 = QtWidgets.QHBoxLayout()
        r2.addWidget(muted("Out 1/2"))
        self.meter = Meter(thick=8)
        self.meter.setMinimumWidth(80)
        r2.addWidget(self.meter, 1)
        lay.addLayout(r2)
        self.mute = QtWidgets.QPushButton("🔇  Mute speakers")
        self.mute.setCheckable(True)
        self.mute.setToolTip("Silence the speaker outputs; headphones keep playing")
        self.mute.toggled.connect(win.mute_speakers)
        lay.addWidget(self.mute)

    def refresh(self):
        w = self.win
        spk = w.ctrl_item("spkr-button") or "Off"
        sc = SPEAKERS.get(spk.split("-")[0])
        if sc:
            name = w.label(f"spk{sc[0]}", "")
            self.spk.set(f"Speakers {sc[0]}" + (f" · {name[:12]}" if name else ""), sc[1])
        else:
            self.spk.set("Speakers off")
        knob = w.ctrl("master-knob-value")
        if knob:
            self.knob.setValue(int(knob.values[0]))
        m = w.ctrl("output-source-meter")
        if m and len(m.values) > 1:
            self.meter.set_level(max(m.values[0], m.values[1]))
        muted_now = bool(w.app_state.get("spk_mute"))
        if self.mute.isChecked() != muted_now:
            self.mute.blockSignals(True)
            self.mute.setChecked(muted_now)
            self.mute.blockSignals(False)
        self.mute.setText("🔈  Unmute speakers" if muted_now else "🔇  Mute speakers")


NAV_ICONS = {
    "Home": ["go-home", "user-home"],
    "Hardware": ["audio-card", "soundcard"],
    "Mixer": ["view-media-equalizer", "audio-volume-high"],
    "Speakers": ["audio-speakers", "speaker", "audio-volume-medium"],
    "Record": ["media-record"],
    "Effects": ["draw-star", "starred", "special-effects"],
    "Remote": ["smartphone", "phone", "kdeconnect"],
    "Apps": ["preferences-desktop-multimedia", "applications-multimedia", "audio-volume-high"],
    "Your setup": ["document-properties", "view-list-details", "configure"],
    "Settings": ["configure", "preferences-system", "settings-configure"],
    "Advanced": ["applications-utilities", "tools", "configure-toolbars"],
}
NAV_EMOJI = {"Home": "🏠", "Hardware": "🎛️", "Mixer": "🎚️", "Speakers": "🔈", "Record": "⏺️",
             "Effects": "✨", "Remote": "📱", "Settings": "⚙️", "Advanced": "🔧",
             "Apps": "🧩", "Your setup": "🗂️"}


def nav_icon(name):
    for n in NAV_ICONS.get(name, []):
        if QtGui.QIcon.hasThemeIcon(n):
            return QtGui.QIcon.fromTheme(n)
    return None




# ================================================================== responsive helpers

class ResponsiveBox(QtWidgets.QWidget):
    """Side by side when there's room, stacked when the window is narrow."""

    def __init__(self, threshold=1050, spacing=14):
        super().__init__()
        self.threshold = threshold
        self.box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(spacing)

    def addWidget(self, w, stretch=1):
        self.box.addWidget(w, stretch)

    def resizeEvent(self, e):
        kids = [self.box.itemAt(i).widget() for i in range(self.box.count())]
        need = sum(k.minimumSizeHint().width() for k in kids if k) + self.box.spacing() * max(0, len(kids) - 1)
        d = (QtWidgets.QBoxLayout.LeftToRight if self.width() >= max(self.threshold, need)
             else QtWidgets.QBoxLayout.TopToBottom)
        if self.box.direction() != d:
            self.box.setDirection(d)
        super().resizeEvent(e)


class FlowGrid(QtWidgets.QWidget):
    """Grid whose column count follows the available width."""

    def __init__(self, min_col=330, spacing=10):
        super().__init__()
        self.min_col, self.items, self.cols = min_col, [], 0
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(spacing)

    def set_widgets(self, widgets):
        for w in self.items:
            self.grid.removeWidget(w)
            w.hide()
            w.deleteLater()
        self.items = list(widgets)
        self.cols = 0
        self._relayout()

    def _relayout(self):
        cols = max(1, min(3, self.width() // self.min_col)) if self.width() > 50 else 3
        if cols == self.cols:
            return
        self.cols = cols
        for w in self.items:
            self.grid.removeWidget(w)
        for i, w in enumerate(self.items):
            self.grid.addWidget(w, i // cols, i % cols)
        for c in range(3):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, e):
        self._relayout()
        super().resizeEvent(e)


# ================================================================== single instance & commands

SOCKET_NAME = f"mbox-control-{os.getuid()}"
COMMANDS = {
    "show": "Open the window",
    "save-retro": "Save what I just played (last minute)",
    "quick-take": "Start / stop a quick take",
    "metronome": "Metronome on / off",
    "toggle-mute": "Mute / unmute the speakers",
    "mute": "Mute the speakers",
    "unmute": "Unmute the speakers",
    "volume-up": "Speaker volume up (Outputs 1/2 trim)",
    "volume-down": "Speaker volume down",
    "speakers:A": "Switch to speakers A",
    "speakers:B": "Switch to speakers B",
    "speakers:C": "Switch to speakers C",
    "scene:listen": "Listening scene",
    "scene:rip": "Rip tape / vinyl scene",
}


def send_command(cmd, timeout=800):
    from PySide6.QtNetwork import QLocalSocket
    s = QLocalSocket()
    s.connectToServer(SOCKET_NAME)
    if not s.waitForConnected(timeout):
        return False
    s.write((cmd + "\n").encode())
    s.flush()
    s.waitForBytesWritten(timeout)
    s.disconnectFromServer()
    return True


# ================================================================== headphones output for apps

HEADPHONES_CONF = PW_CONF_DIR / "mbox-headphones.conf"
PC_HP_LEFT, PC_HP_RIGHT = "Stream-1", "Stream-2"


NOSUSPEND_CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / \
    "wireplumber/wireplumber.conf.d/51-mbox-no-suspend.conf"


def write_nosuspend_conf():
    """Keep the Mbox streaming when idle, so recordings don't lose their first second while it wakes."""
    NOSUSPEND_CONF.parent.mkdir(parents=True, exist_ok=True)
    NOSUSPEND_CONF.write_text("""# Written by Mbox Control: never put the Mbox to sleep when idle
monitor.alsa.rules = [
  {
    matches = [ { node.name = "~alsa_input.firewire*" }, { node.name = "~alsa_output.firewire*" } ]
    actions = { update-props = { session.suspend-timeout-seconds = 0 } }
  }
]
""")


def firewall_kind():
    """Which firewall is running, if any: 'ufw', 'firewalld' or None."""
    if shutil.which("ufw") and (run_quiet("systemctl", "is-active", "ufw") or "").strip() == "active":
        return "ufw"
    if shutil.which("firewall-cmd") and (run_quiet("systemctl", "is-active", "firewalld") or "").strip() == "active":
        return "firewalld"
    return None


def firewall_open_cmd(port):
    """Root command that lets devices on the home network reach `port`, or None if no firewall is on."""
    kind = firewall_kind()
    if kind is None:
        return None
    ip = lan_ip()
    subnet = ".".join(ip.split(".")[:3]) + ".0/24" if ip.count(".") == 3 else None
    if kind == "ufw":
        return ["ufw", "allow", "from", subnet, "to", "any", "port", str(port), "proto", "tcp"] if subnet \
            else ["ufw", "allow", f"{port}/tcp"]
    return ["sh", "-c", f"firewall-cmd --permanent --add-port={port}/tcp && firewall-cmd --reload"]


def write_headphones_conf(sink):
    PW_CONF_DIR.mkdir(parents=True, exist_ok=True)
    HEADPHONES_CONF.write_text(f"""# Written by Mbox Control: an app output that only reaches the headphones
context.modules = [
  {{ name = libpipewire-module-loopback
    args = {{
      node.description = "Headphones (Mbox)"
      capture.props = {{
        node.name = "mbox_headphones"
        media.class = "Audio/Sink"
        audio.position = [ FL FR ]
      }}
      playback.props = {{
        node.name = "mbox_headphones_out"
        target.object = "{sink}"
        audio.position = [ AUX0 AUX1 ]
        stream.dont-remix = true
        node.passive = true
      }}
    }}
  }}
]
""")


# ================================================================== loudness / spectrum / phase

def _kweight_power(freqs):
    """|H(f)|² of the ITU-R BS.1770 K-weighting filter (48 kHz design, evaluated per frequency)."""
    import numpy as np
    w = 2 * np.pi * np.minimum(freqs, 23999.0) / 48000.0
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    b1 = (1.53512485958697, -2.69169618940638, 1.19839281085285)
    a1 = (1.0, -1.69065929318241, 0.73248077421585)
    b2 = (1.0, -2.0, 1.0)
    a2 = (1.0, -1.99004745483398, 0.99007225036621)
    h1 = (b1[0] + b1[1] * z1 + b1[2] * z2) / (a1[0] + a1[1] * z1 + a1[2] * z2)
    h2 = (b2[0] + b2[1] * z1 + b2[2] * z2) / (a2[0] + a2[1] * z1 + a2[2] * z2)
    return np.abs(h1 * h2) ** 2


class Analyzer(QtCore.QThread):
    """Live loudness (LUFS), spectrum and L/R correlation from a PipeWire source."""
    frame = QtCore.Signal(dict)
    failed = QtCore.Signal(str)
    BANDS = 40

    def __init__(self, source, channels, rate=48000):
        super().__init__()
        self.source, self.channels, self.rate = source, channels, rate   # channels: (count, l_idx, r_idx, map)
        self.proc, self.running = None, True
        self.reset_integrated()

    def reset_integrated(self):
        self.blocks, self.gated, self.nblk = [], [], 0

    def run(self):
        try:
            import numpy as np
        except ImportError:
            self.failed.emit("Live analysis needs numpy:  " + install_hint("numpy"))
            return
        if not shutil.which("parec"):
            self.failed.emit("Live analysis needs parec (package: libpulse).")
            return
        count, li, ri, cmap = self.channels
        cmd = ["parec", "-d", self.source, "--raw", "--format=float32le", f"--rate={self.rate}",
               f"--channels={count}", f"--channel-map={cmap}", "--latency-msec=50"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.failed.emit(str(e))
            return
        n = self.rate // 10                      # 100 ms blocks
        need = n * count * 4
        hist = []
        nfft = 4096
        freqs = np.fft.rfftfreq(n, 1.0 / self.rate)
        kw = _kweight_power(freqs)
        sfreqs = np.fft.rfftfreq(nfft, 1.0 / self.rate)
        edges = np.geomspace(30, min(20000, self.rate / 2 - 1), self.BANDS + 1)
        idx = np.searchsorted(sfreqs, edges)
        win = np.hanning(nfft)
        tail = np.zeros((nfft, 2), dtype=np.float32)
        integ = -70.0
        while self.running:
            data = self.proc.stdout.read(need)
            if not data or len(data) < need:
                break
            x = np.frombuffer(data, dtype=np.float32).reshape(-1, count)[:, (li, ri)]
            # loudness: K-weighted mean square via Parseval on this block
            spec = np.fft.rfft(x, axis=0)
            pw = (np.abs(spec) ** 2) * kw[:, None]
            pw[1:-1] *= 2
            ms = pw.sum(axis=0) / (n * n)
            block = float(ms.sum())
            hist.append(block)
            hist = hist[-30:]
            self.blocks.append(block)
            self.nblk += 1
            if len(self.blocks) > 8:
                del self.blocks[:-8]

            def lufs(v):
                return -0.691 + 10 * math.log10(v) if v > 1e-12 else -70.0

            mom = lufs(sum(hist[-4:]) / min(4, len(hist)))
            short = lufs(sum(hist) / len(hist))
            # integrated (gated, BS.1770): 400 ms blocks, absolute −70 and relative −10 LU gates
            if len(self.blocks) >= 4:
                g = self.blocks[-4:]
                b4 = sum(g) / 4
                if lufs(b4) > -70:
                    self.gated.append(b4)
            if self.gated and (self.nblk % 10 == 0 or integ <= -70):
                rel = lufs(sum(self.gated) / len(self.gated)) - 10
                gb2 = [b for b in self.gated if lufs(b) > rel]
                integ = lufs(sum(gb2) / len(gb2)) if gb2 else -70.0
            # spectrum on the latest 4096 samples
            tail = np.vstack([tail, x])[-nfft:]
            mono = tail.mean(axis=1) * win
            mag = np.abs(np.fft.rfft(mono)) / (nfft / 4)
            bands = []
            for a, b in zip(idx[:-1], idx[1:]):
                seg = mag[a:max(b, a + 1)]
                v = float(seg.max()) if seg.size else 0.0
                bands.append(20 * math.log10(v) if v > 1e-9 else -90.0)
            l, r = x[:, 0], x[:, 1]
            den = math.sqrt(float((l * l).sum()) * float((r * r).sum()))
            corr = float((l * r).sum()) / den if den > 1e-12 else 0.0
            peak = float(np.abs(x).max())
            self.frame.emit({"m": mom, "s": short, "i": integ, "bands": bands, "corr": corr,
                             "peak": 20 * math.log10(peak) if peak > 1e-9 else -90.0})
        self.stop()

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class AnalyzerView(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.data = None
        self.peak_hold = -90.0
        self.setMinimumHeight(190)

    def set_data(self, d):
        self.data = d
        self.peak_hold = max(d["peak"], self.peak_hold - 0.3)
        self.update()

    def clear(self):
        self.data = None
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor(BG2))
        side = 210
        spec = QRectF(12, 12, W - side - 24, H - 40)
        f = p.font()
        if not self.data:
            p.setPen(QtGui.QColor(SUBTLE))
            p.drawText(self.rect(), Qt.AlignCenter, "Live analysis is off")
            return
        d = self.data
        n = len(d["bands"])
        bw = spec.width() / n
        for i, db in enumerate(d["bands"]):
            frac = max(0.0, min(1.0, (db + 72) / 72))
            h = spec.height() * frac
            c = QtGui.QColor(ACCENT)
            c.setAlpha(110 + int(140 * frac))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(spec.x() + i * bw + 1, spec.bottom() - h, bw - 2, h), 2, 2)
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(QtGui.QColor(SUBTLE))
        for hz, label in ((100, "100"), (1000, "1k"), (10000, "10k")):
            t = math.log(hz / 30) / math.log(20000 / 30)
            p.drawText(QRectF(spec.x() + t * spec.width() - 20, spec.bottom() + 4, 40, 14), Qt.AlignCenter, label)
        # loudness numbers
        x0 = W - side
        rows = (("Momentary", d["m"]), ("Short-term", d["s"]), ("Integrated", d["i"]))
        for r, (lbl, v) in enumerate(rows):
            y = 14 + r * 40
            f.setPointSizeF(8)
            p.setFont(f)
            p.setPen(QtGui.QColor(SUBTLE))
            p.drawText(QRectF(x0, y, side - 12, 14), Qt.AlignLeft, lbl)
            f.setPointSizeF(15)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(QRectF(x0, y + 12, side - 12, 24), Qt.AlignLeft,
                       "—" if v <= -69 else f"{v:.1f} LUFS")
            f.setBold(False)
        # correlation
        y = 14 + 3 * 40 + 6
        f.setPointSizeF(8)
        p.setFont(f)
        p.setPen(QtGui.QColor(SUBTLE))
        p.drawText(QRectF(x0, y, side - 12, 14), Qt.AlignLeft,
                   f"Phase  {d['corr']:+.2f}   ·   peak {self.peak_hold:.1f} dBFS")
        bar = QRectF(x0, y + 18, side - 24, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255, 25))
        p.drawRoundedRect(bar, 4, 4)
        cx = bar.center().x()
        px = cx + d["corr"] * bar.width() / 2
        p.setBrush(QtGui.QColor("#f85149" if d["corr"] < -0.2 else "#3fb950"))
        p.drawRoundedRect(QRectF(min(cx, px), bar.y(), abs(px - cx) + 2, bar.height()), 4, 4)


# ================================================================== Discogs

class DiscogsLookup(QtCore.QThread):
    done = QtCore.Signal(list, str)

    def __init__(self, token, artist, album):
        super().__init__()
        self.token, self.artist, self.album = token, artist, album

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": MusicBrainzLookup.UA,
                                                   "Authorization": f"Discogs token={self.token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def run(self):
        try:
            q = urllib.parse.urlencode({"type": "release", "release_title": self.album,
                                        "artist": self.artist, "per_page": 10})
            data = self._get("https://api.discogs.com/database/search?" + q)
            res = [{"id": r["id"], "title": r.get("title", ""), "year": r.get("year", ""),
                    "country": r.get("country", ""), "format": ", ".join(r.get("format", [])[:3])}
                   for r in data.get("results", [])]
            self.done.emit(res, "" if res else "No matches on Discogs.")
        except Exception as e:  # noqa: BLE001
            self.done.emit([], f"Discogs lookup failed: {e}")


class DiscogsTracks(QtCore.QThread):
    done = QtCore.Signal(list, str, str, str)

    def __init__(self, token, rid):
        super().__init__()
        self.token, self.rid = token, rid

    def run(self):
        try:
            req = urllib.request.Request(f"https://api.discogs.com/releases/{self.rid}",
                                         headers={"User-Agent": MusicBrainzLookup.UA,
                                                  "Authorization": f"Discogs token={self.token}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            titles = [t.get("title", "") for t in d.get("tracklist", []) if t.get("type_", "track") == "track"]
            artist = ", ".join(a.get("name", "") for a in d.get("artists", []))
            self.done.emit(titles, str(d.get("year") or ""), re.sub(r"\s*\(\d+\)$", "", artist), "")
        except Exception as e:  # noqa: BLE001
            self.done.emit([], "", "", f"Couldn't fetch Discogs tracks: {e}")


# ================================================================== MIDI

def list_midi_ports():
    out = run_quiet("aseqdump", "-l") or ""
    ports = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\d+:\d+)\s+(.*?)\s{2,}(.*)$", line)
        if m and "Midi Through" not in line and not m.group(2).startswith("System"):
            ports.append((m.group(1), f"{m.group(3).strip()} ({m.group(1)})"))
    return ports


def parse_midi_line(line):
    """'Control change   0, controller 7, value 100' → ('cc', 0, 7, 100); notes → ('note', ch, n, vel)."""
    m = re.search(r"Control change\s+(\d+),\s*controller\s+(\d+),\s*value\s+(\d+)", line)
    if m:
        return ("cc", int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"Note (on|off)\s+(\d+),\s*note\s+(\d+)(?:,\s*velocity\s+(\d+))?", line)
    if m:
        vel = int(m.group(4) or 0) if m.group(1) == "on" else 0
        return ("note", int(m.group(2)), int(m.group(3)), vel)
    return None


class MidiWatcher(QtCore.QThread):
    event = QtCore.Signal(str, int, int, int)
    failed = QtCore.Signal(str)

    def __init__(self, port):
        super().__init__()
        self.port, self.proc, self.running = port, None, True

    def run(self):
        if not shutil.which("aseqdump"):
            self.failed.emit("MIDI needs aseqdump (package: alsa-utils).")
            return
        cmd = ["aseqdump", "-p", self.port]
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-oL"] + cmd
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            self.failed.emit(str(e))
            return
        for line in self.proc.stdout:
            if not self.running:
                break
            ev = parse_midi_line(line)
            if ev:
                self.event.emit(*ev)

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def midi_to_gain(v):
    """0…127 → gain on the fader's dB curve (0 = off, 127 = 0 dB)."""
    if v <= 0:
        return 0
    db = METER_FLOOR_DB * (1 - v / 127)
    return round(UNITY * 10 ** (db / 20))


# ================================================================== installer & updater

def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def find_update():
    """A newer mbox_control*.py waiting in ~/Downloads? → (path, version)."""
    best = None
    for p in (HOME / "Downloads").glob("mbox_control*.py"):
        try:
            m = re.search(r'^VERSION = "([\d.]+)"', p.read_text(errors="ignore")[:4000], re.M)
        except OSError:
            continue
        if m and version_tuple(m.group(1)) > version_tuple(VERSION):
            if not best or version_tuple(m.group(1)) > version_tuple(best[1]):
                best = (p, m.group(1))
    return best


class UpdateCheck(QtCore.QThread):
    """Asks GitHub for the latest release (only when the person turned checking on)."""
    found = QtCore.Signal(str, str)        # version, page url  ("" when nothing newer)

    def run(self):
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                         headers={"User-Agent": f"MboxControl/{VERSION}",
                                                  "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            ver = str(data.get("tag_name", "")).lstrip("v")
            if re.fullmatch(r"[\d.]+", ver) and version_tuple(ver) > version_tuple(VERSION):
                self.found.emit(ver, data.get("html_url", ""))
                return
        except Exception:  # noqa: BLE001
            pass
        self.found.emit("", "")


class UpdateDownload(QtCore.QThread):
    """Fetches mbox_control.py for a release tag and checks it before anything is replaced."""
    done = QtCore.Signal(str, str)         # path, error

    def __init__(self, ver):
        super().__init__()
        self.ver = ver

    def run(self):
        url = f"https://raw.githubusercontent.com/{UPDATE_REPO}/v{self.ver}/mbox_control.py"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"MboxControl/{VERSION}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
            if f'VERSION = "{self.ver}"' not in text[:5000]:
                raise ValueError("the download isn't the version it should be")
            compile(text, "mbox_control.py", "exec")          # never install something that won't start
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            out = CONF_DIR / f"mbox_control-{self.ver}.py"
            out.write_text(text)
            self.done.emit(str(out), "")
        except Exception as e:  # noqa: BLE001
            self.done.emit("", str(e))


def cli_install():
    """Used by install.sh: icon, launcher, background service."""
    install_icon_files()
    LAUNCHER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_FILE.write_text(desktop_entry())
    print("✓ Launcher and icon installed")
    if SERVICE_PATH.exists():
        print("✓ Background service installed" if install_service_unit() else "! Service installed but not started")
    else:
        print(f"! Control service not found at {SERVICE_PATH} — build snd-firewire-ctl-services first")

# ================================================================== 4.3: your setup

INPUT_PAIRS = [("Analog-A-1", "Analog-A-2", "Inputs 1/2"),
               ("Analog-A-3", "Analog-A-4", "Inputs 3/4"),
               ("Analog-A-5", "Analog-A-6", "Aux In 5/6 (RCA)")]
INPUT_JACKS = [("Analog-A-1", "Input 1"), ("Analog-A-2", "Input 2"), ("Analog-A-3", "Input 3"),
               ("Analog-A-4", "Input 4"), ("Analog-A-5", "Aux In 5"), ("Analog-A-6", "Aux In 6")]
SETUP_DEFAULT = {"rip": "Analog-A-5", "rip_monitor": False, "always_on": [], "rip_tip": "", "rip_app": "builtin"}


def pair_label(left):
    return next((n for l, _, n in INPUT_PAIRS if l == left), left)


def pair_right(left):
    return next((r for l, r, _ in INPUT_PAIRS if l == left), left)


# ================================================================== 4.3: tape tools

def make_tone(path, freq, dbfs, secs=60, rate=48000):
    """Write a stereo 24-bit sine test tone."""
    import wave
    import struct
    amp = 10 ** (dbfs / 20) * (2 ** 23 - 1)
    n = int(secs * rate)
    frames = bytearray()
    step = 2 * math.pi * freq / rate
    for i in range(n):
        v = int(amp * math.sin(step * i))
        b = struct.pack("<i", v)[:3]
        frames += b + b
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


TAPE_LENGTHS = [("C46", 23), ("C60", 30), ("C90", 45), ("C100", 50), ("C120", 60)]
AUDIO_EXT = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".alac"}


def plan_sides(tracks, side_secs, gap=2.0):
    """tracks: [(name, secs)] in order → (side_a, side_b, overflow). Fills side A, then B."""
    sides, cur, used, overflow = [[], []], 0, 0.0, []
    for name, secs in tracks:
        need = secs + (gap if sides[cur] else 0)
        if used + need > side_secs and cur == 0:
            cur, used = 1, 0.0
            need = secs
        if cur == 1 and used + need > side_secs:
            overflow.append((name, secs))
            continue
        sides[cur].append((name, secs))
        used += need
    return sides[0], sides[1], overflow


class TrackProber(QtCore.QThread):
    done = QtCore.Signal(list)

    def __init__(self, paths):
        super().__init__()
        self.paths = paths

    def run(self):
        out = []
        for p in self.paths:
            d = audio_duration(p)
            if d:
                out.append((str(p), d))
        self.done.emit(out)


# ================================================================== 4.3: apps (per-app routing)

def pactl_json(*args):
    out = run_quiet("pactl", "-f", "json", *args, timeout=6)
    try:
        return json.loads(out) if out.strip() else []
    except ValueError:
        return []


def list_app_streams():
    res = []
    for s in pactl_json("list", "sink-inputs"):
        p = s.get("properties", {})
        name = p.get("application.name") or p.get("node.name") or "App"
        what = p.get("media.name", "")
        if p.get("node.name", "").startswith(("sa600_", "mbox_")):
            continue                                 # our own loopbacks
        res.append({"id": s.get("index"), "sink": s.get("sink"), "app": name, "what": what,
                    "icon": p.get("application.icon_name", "")})
    return res


def list_sinks():
    return [{"id": s.get("index"), "name": s.get("name"), "desc": s.get("description") or s.get("name")}
            for s in pactl_json("list", "sinks")
            if not s.get("name", "").startswith("alsa_output.firewire")]


# ================================================================== 4.3: rip clean-up & library

CLEANUP = {"declick": ("Remove clicks and pops (vinyl)", "adeclick=w=55:o=75"),
           "dehiss": ("Reduce tape hiss", "afftdn=nr=10:nf=-50:tn=1"),
           "rumble": ("Cut rumble below 20 Hz", "highpass=f=20:poles=2")}


def spectrogram_png(src, out, width=1600, height=600):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-lavfi", f"showspectrumpic=s={width}x{height}:legend=1:scale=log:color=intensity",
                        str(out)], capture_output=True, text=True)
    return r.returncode == 0, r.stderr.strip()[-300:]


class SpectrogramJob(QtCore.QThread):
    done = QtCore.Signal(str, str)

    def __init__(self, src, out):
        super().__init__()
        self.src, self.out = src, out

    def run(self):
        ok, err = spectrogram_png(self.src, self.out)
        self.done.emit(str(self.out) if ok else "", err)


class LibraryJob(QtCore.QThread):
    """Copies a finished album to the library inbox, then runs an optional import command."""
    done = QtCore.Signal(str, str)

    def __init__(self, album_dir, dest, command, root=None):
        super().__init__()
        self.album_dir, self.dest, self.command = Path(album_dir), Path(dest), command.strip()
        self.root = Path(root) if root else None

    def run(self):
        try:
            try:
                rel = self.album_dir.relative_to(self.root) if self.root else None
            except ValueError:
                rel = None
            target = self.dest / (rel or Path(self.album_dir.parent.name) / self.album_dir.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self.album_dir, target, dirs_exist_ok=True)
        except OSError as e:
            self.done.emit("", f"Couldn't copy to the library: {e}")
            return
        msg = f"Copied to {target}"
        if self.command:
            cmd = self.command.replace("{path}", shlex.quote(str(target)))
            r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=1800)
            msg += " and imported." if r.returncode == 0 else f", but the import command failed: {r.stderr.strip()[-200:]}"
        self.done.emit(msg, "")


class DiscogsMarkRipped(QtCore.QThread):
    """Adds a release to a 'Ripped' folder in your Discogs collection."""
    done = QtCore.Signal(str)

    def __init__(self, token, release_id, folder="Ripped"):
        super().__init__()
        self.token, self.rid, self.folder = token, release_id, folder

    def _req(self, url, method="GET", data=None):
        req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data else None,
                                     headers={"Authorization": f"Discogs token={self.token}",
                                              "User-Agent": f"MboxControl/{VERSION}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            return json.loads(body) if body else {}

    def run(self):
        try:
            user = self._req("https://api.discogs.com/oauth/identity")["username"]
            base = f"https://api.discogs.com/users/{urllib.parse.quote(user)}/collection/folders"
            folders = self._req(base).get("folders", [])
            fid = next((f["id"] for f in folders if f["name"] == self.folder), None)
            if fid is None:
                fid = self._req(base, "POST", {"name": self.folder})["id"]
            self._req(f"{base}/{fid}/releases/{self.rid}", "POST")
            self.done.emit(f"Added to your Discogs “{self.folder}” folder.")
        except Exception as e:  # noqa: BLE001
            self.done.emit(f"Discogs: couldn't mark as ripped ({e}).")


# ================================================================== 4.3: guitar mode

EFFECT_APPS = [("guitarix", "Guitarix"), ("carla", "Carla"), ("ardour8", "Ardour"), ("ardour", "Ardour"),
               ("reaper", "REAPER"), ("qtractor", "Qtractor")]


def find_effect_apps():
    return [(cmd, name) for cmd, name in EFFECT_APPS if shutil.which(cmd)]


# ================================================================== 4.3: plain-English tooltips

TIPS = {
    "Speakers A": "Which speaker set the Spkr button is on. Click to cycle A → B → C.",
    "Speakers B": "Which speaker set the Spkr button is on. Click to cycle A → B → C.",
    "Speakers C": "Which speaker set the Spkr button is on. Click to cycle A → B → C.",
    "Speakers off": "Which speaker set the Spkr button is on. Click to cycle A → B → C.",
    "Dim/Mute": "The front Dim/Mute button. Tap = quieter, hold = mute.",
    "Mono": "Folds left and right together. Handy for checking a mix or a mono record.",
    "kHz": "The sample rate the Mbox is running at. Change it in Settings → Audio engine.",
    "+48V": "Phantom power for condenser mics. Powers all four mic inputs at once.",
    "Mute speakers": "Silences the speaker outputs. Headphones keep playing.",
    "Undo last scene": "Puts every setting back the way it was before the last scene.",
    "Save current setup": "Saves everything as a scene you can bring back with one click.",
    "Swap L/R": "Swaps left and right on this output. Use it if the stereo image is backwards.",
    "Only channels in use": "Hides mixer channels that are silent and turned down.",
    "Record": "Starts recording the rip input to a FLAC file. Click again to stop.",
    "Set up the Mbox for ripping": "Routes your rip input to the computer (Settings → Your setup decides which).",
    "Find tracks": "Finds the gaps between songs and suggests where to split.",
    "Save tracks as FLAC": "Cuts the recording into tagged FLAC files sorted as Artist / Album.",
    "MusicBrainz": "Fills in track titles from MusicBrainz using the artist and album you typed.",
    "Discogs": "Fills in track titles from Discogs (needs a free token in Settings).",
    "Spectrogram": "Shows the recording as a picture of its frequencies. Clicks look like thin "
                   "vertical lines; hiss is a haze at the top; a dull transfer stops short of the top.",
    "Start analysis": "Live loudness, spectrum and stereo phase of the rip input.",
    "Reset integrated": "Starts the overall loudness reading over.",
    "Apply": "Applies the sample rate and buffer size now.",
    "Back to automatic": "Lets PipeWire pick the sample rate and buffer size again.",
    "Copy": "Copies the command so you can paste it into KDE's shortcut settings.",
    "Install Headphones (Mbox) output": "Adds an app output that only plays in Headphone A.",
    "Refresh": "Looks for MIDI controllers again.",
    "Enable": "Starts listening to the chosen MIDI controller.",
    "Clear all MIDI mappings": "Forgets every learned MIDI control.",
    "Play 1 kHz": "Plays a steady reference tone to your tape outputs for setting the deck's record level.",
    "Play 10 kHz": "A high test tone. If it sounds dull on the tape, the heads need cleaning or adjusting.",
    "Stop tone": "Stops the test tone.",
    "Plan sides": "Fits the chosen songs onto side A and side B of the tape length you picked.",
    "Start guitar mode": "Low latency, dry guitar muted, your effects app opened, tuner ready.",
    "Stop guitar mode": "Puts latency and the mixer back to normal.",
    "Wire effect input": "Fixes the effect's input if the reverb seems silent.",
}


def apply_tips(root):
    """Give every button, checkbox and dropdown without a tooltip one from TIPS (matched by its text)."""
    for wdg in root.findChildren(QtWidgets.QAbstractButton):
        if wdg.toolTip():
            continue
        text = wdg.text().replace("&&", "&")
        for key, tip in TIPS.items():
            if key in text:
                wdg.setToolTip(tip)
                break

# ================================================================== 4.5: visual splitter

class EnvelopeJob(QtCore.QThread):
    """Decodes a recording at low rate and returns a loudness envelope (one value per 50 ms)."""
    done = QtCore.Signal(object, float, str)
    HOP = 0.05

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            import numpy as np
        except ImportError:
            self.done.emit(None, 0.0, "The waveform needs numpy:  " + install_hint("numpy"))
            return
        rate = 4000
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(self.path), "-ac", "1",
                            "-ar", str(rate), "-f", "s16le", "-"], capture_output=True)
        if r.returncode != 0 or not r.stdout:
            self.done.emit(None, 0.0, r.stderr.decode(errors="ignore")[-200:] or "Couldn't read the file")
            return
        x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        hop = int(rate * self.HOP)
        n = len(x) // hop
        frames = x[:n * hop].reshape(n, hop)
        env = np.sqrt((frames ** 2).mean(axis=1))          # RMS per 50 ms
        peak = np.abs(frames).max(axis=1)
        self.done.emit((env, peak), len(x) / rate, "")


def deepest_dips(env, hop, count, min_gap=25.0, edge=10.0):
    """Pick `count` split points at the quietest moments, at least `min_gap` s apart."""
    import numpy as np
    if count <= 0 or len(env) < 10:
        return []
    win = max(1, int(1.0 / hop))                              # 1 s smoothing
    sm = np.convolve(env, np.ones(win) / win, mode="same")
    order = np.argsort(sm)
    picks = []
    lo, hi = edge / hop, len(sm) - edge / hop
    for i in order:
        if not lo < i < hi:
            continue
        if all(abs(i - p) * hop >= min_gap for p in picks):
            picks.append(int(i))
            if len(picks) == count:
                break
    return sorted(p * hop for p in picks)


class WaveformView(QtWidgets.QWidget):
    """The whole side as a loudness picture. Splits are draggable lines."""
    splitsChanged = QtCore.Signal(list)        # new list of split times (s)
    previewAt = QtCore.Signal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(130)
        self.setMouseTracking(True)
        self.env = self.peak = None
        self.dur, self.splits, self.drag, self.hover, self.cursor_t = 0.0, [], None, None, None
        self.setToolTip("Drag a line to move a split · double-click to add one · right-click a line to "
                        "remove it · click to listen there")

    def set_data(self, env, peak, dur):
        self.env, self.peak, self.dur = env, peak, dur
        self.update()

    def set_splits(self, splits):
        self.splits = sorted(splits)
        self.update()

    def clear(self):
        self.env = self.peak = None
        self.splits, self.dur = [], 0.0
        self.update()

    def _x(self, t):
        return 8 + (self.width() - 16) * (t / self.dur if self.dur else 0)

    def _t(self, x):
        return max(0.0, min(self.dur, (x - 8) / max(1, self.width() - 16) * self.dur))

    def _near(self, x):
        best = None
        for i, s in enumerate(self.splits):
            d = abs(self._x(s) - x)
            if d < 7 and (best is None or d < best[1]):
                best = (i, d)
        return best[0] if best else None

    def mousePressEvent(self, e):
        if not self.dur:
            return
        x = e.position().x()
        i = self._near(x)
        if e.button() == Qt.RightButton and i is not None:
            del self.splits[i]
            self.splitsChanged.emit(list(self.splits))
        elif e.button() == Qt.LeftButton:
            if i is not None:
                self.drag = i
            else:
                self.cursor_t = self._t(x)
                self.previewAt.emit(self.cursor_t)
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.dur:
            self.splits = sorted(self.splits + [self._t(e.position().x())])
            self.splitsChanged.emit(list(self.splits))
            self.update()

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self.drag is not None:
            self.splits[self.drag] = self._t(x)
            self.update()
        else:
            h = self._near(x)
            if h != self.hover:
                self.hover = h
                self.setCursor(Qt.SizeHorCursor if h is not None else Qt.ArrowCursor)
                self.update()

    def mouseReleaseEvent(self, _):
        if self.drag is not None:
            t = self.splits[self.drag]
            self.splits.sort()
            self.drag = None
            self.splitsChanged.emit(list(self.splits))
            self.previewAt.emit(max(0.0, t - 3))

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self.rect()
        p.fillRect(r, QtGui.QColor(BG2))
        if self.env is None:
            p.setPen(QtGui.QColor(SUBTLE))
            p.drawText(r, Qt.AlignCenter, getattr(self, "empty_text", "Find tracks to see the recording here"))
            return
        w, h = self.width() - 16, self.height()
        mid = h / 2
        n = len(self.peak)
        cols = max(1, w)
        top = float(self.peak.max()) or 1.0
        path_pk = QtGui.QPainterPath()
        path_rms = QtGui.QPainterPath()
        pts_pk, pts_rms = [], []
        for c in range(cols):
            a, b = int(c * n / cols), max(int(c * n / cols) + 1, int((c + 1) * n / cols))
            pk = float(self.peak[a:b].max()) / top
            rm = float(self.env[a:b].max()) / top
            pts_pk.append((8 + c, pk))
            pts_rms.append((8 + c, rm))
        for path, pts in ((path_pk, pts_pk), (path_rms, pts_rms)):
            path.moveTo(8, mid)
            for x, v in pts:
                path.lineTo(x, mid - v * (mid - 6))
            for x, v in reversed(pts):
                path.lineTo(x, mid + v * (mid - 6))
            path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(90, 169, 255, 70))
        p.drawPath(path_pk)
        p.setBrush(QtGui.QColor(ACCENT))
        p.drawPath(path_rms)
        # track shading + splits
        bounds = [0.0] + self.splits + [self.dur]
        f = p.font()
        f.setPointSizeF(8)
        p.setFont(f)
        for k in range(len(bounds) - 1):
            x0 = self._x(bounds[k])
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(QRectF(x0 + 4, 2, 40, 14), Qt.AlignLeft, str(k + self.first_no()))
        for i, s in enumerate(self.splits):
            x = self._x(s)
            p.setPen(QtGui.QPen(QtGui.QColor("#f2b33d" if i in (self.hover, self.drag) else "#e3a52a"), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, h))
        if self.cursor_t is not None:
            p.setPen(QtGui.QPen(QtGui.QColor("#3fb950"), 1))
            x = self._x(self.cursor_t)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
        # minute ticks
        p.setPen(QtGui.QColor(SUBTLE))
        step = 60 if self.dur < 1800 else 300
        t = step
        while t < self.dur:
            x = self._x(t)
            p.drawText(QRectF(x - 20, h - 14, 40, 12), Qt.AlignCenter, fmt_time(t))
            t += step

    def first_no(self):
        return getattr(self, "first", 1)


class PreviewPlayer:
    """Plays a few seconds of a recording around a point."""

    def __init__(self):
        self.proc = None

    def play(self, path, start, secs=7.0, af=""):
        self.stop()
        if not shutil.which("ffmpeg") or not shutil.which("pw-play"):
            return
        tmp = CONF_DIR / "preview.wav"
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}",
                        "-t", f"{secs:.2f}", "-i", str(path), *(["-af", af] if af else []), str(tmp)],
                       capture_output=True)
        target = ["--target", MAIN_SINK] if pw_node_exists(MAIN_SINK, "sink") else []
        self.proc = subprocess.Popen(["pw-play", *target, str(tmp)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


# ================================================================== 4.5: covers, layout, tracklists

def prepare_cover(src, dest, max_side=1200):
    img = QtGui.QImage(str(src))
    if img.isNull():
        return False
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return img.save(str(dest), "JPG", 92)


def album_dir(root, artist, album, layout):
    a = safe_name(artist or "Unknown Artist")
    b = safe_name(album or "Untitled")
    if layout == "letter":
        first = a[:1].upper()
        letter = first if first.isalpha() else "#"
        return Path(root) / letter / a / b
    return Path(root) / a / b


def track_filename(num, title, layout):
    t = safe_name(title or f"Track {num}")
    return f"{num:02d} {t}.flac" if layout == "letter" else f"{num:02d} - {t}.flac"


def tracklist_text(rows, first, side_letters=True):
    """rows: [(start, end, title, original)] → paste-ready lines for Discogs / MusicBrainz."""
    side = "A" if first == 1 else "B"
    out = []
    for i, (a, b, title, orig) in enumerate(rows):
        pos = f"{side}{i + 1}" if side_letters else str(first + i)
        name = title or f"Track {first + i}"
        if orig:
            name += f" ({orig})"
        out.append(f"{pos}  {name}  {fmt_time(max(0, b - a))}")
    return "\n".join(out)

# ================================================================== 4.7: quick panel window

class QuickPanel(QtWidgets.QFrame):
    """A small always-on-top window (not a popup: popups need a visible parent on Wayland)."""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("quick")
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.hidden_at = 0.0

    def event(self, e):
        if e.type() == QtCore.QEvent.WindowDeactivate and self.isVisible():
            self.hide()
            self.hidden_at = time.time()
        return super().event(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)


# ================================================================== 4.7: command palette

class CommandPalette(QtWidgets.QDialog):
    def __init__(self, parent, entries):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.entries = entries                         # [(label, hint, fn)]
        self.setObjectName("palette")
        self.setStyleSheet(f"QDialog#palette {{ background:{BG1}; border:1px solid {ACCENT}; border-radius:14px; }}")
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        self.edit = QtWidgets.QLineEdit()
        self.edit.setPlaceholderText("Type a command, scene or page…   (Enter runs it, Esc closes)")
        self.edit.setStyleSheet("font-size: 12pt; padding: 8px;")
        v.addWidget(self.edit)
        self.list = QtWidgets.QListWidget()
        self.list.setStyleSheet("QListWidget { border: none; background: transparent; } "
                                "QListWidget::item { padding: 7px; border-radius: 8px; } "
                                f"QListWidget::item:selected {{ background: {ACCENT}; color: #0b0d10; }}")
        v.addWidget(self.list)
        self.edit.textChanged.connect(self.filter)
        self.edit.installEventFilter(self)
        self.list.itemActivated.connect(lambda _: self.run())
        self.resize(620, 440)
        self.filter("")

    @staticmethod
    def score(label, q):
        l, q = label.lower(), q.lower().strip()
        if not q:
            return 1
        if all(w in l for w in q.split()):
            return 3 if l.startswith(q) else 2
        it = iter(l)
        return 1 if all(c in it for c in q.replace(" ", "")) else 0

    def filter(self, q):
        self.list.clear()
        ranked = sorted(((self.score(lbl, q), i) for i, (lbl, _, _) in enumerate(self.entries)),
                        key=lambda x: (-x[0], x[1]))
        for sc, i in ranked:
            if sc:
                lbl, hint, _ = self.entries[i]
                it = QtWidgets.QListWidgetItem(f"{lbl}    —  {hint}" if hint else lbl)
                it.setData(Qt.UserRole, i)
                self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, e):
        if obj is self.edit and e.type() == QtCore.QEvent.KeyPress:
            if e.key() in (Qt.Key_Down, Qt.Key_Up):
                r = self.list.currentRow() + (1 if e.key() == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, r)))
                return True
            if e.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.run()
                return True
        return super().eventFilter(obj, e)

    def run(self):
        it = self.list.currentItem()
        if it is None:
            return
        fn = self.entries[it.data(Qt.UserRole)][2]
        self.accept()
        QtCore.QTimer.singleShot(0, fn)


# ================================================================== 4.7: loudness (ReplayGain 2.0)

def measure_loudness(src, start, dur):
    """→ (integrated LUFS, true peak dBTP) or (None, None)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(src), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True)
    tail = r.stderr[r.stderr.rfind("Summary:"):] if "Summary:" in r.stderr else ""
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", tail)
    pk = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    if not i:
        return None, None
    peak = float(pk.group(1)) if pk and pk.group(1) != "-inf" else -70.0
    return float(i.group(1)), peak


def rg_tags(prefix, lufs, peak_db):
    return ["-metadata", f"REPLAYGAIN_{prefix}_GAIN={-18.0 - lufs:+.2f} dB",
            "-metadata", f"REPLAYGAIN_{prefix}_PEAK={10 ** (peak_db / 20):.6f}"]


# ================================================================== 4.7: song identification (AcoustID)

class IdentifyJob(QtCore.QThread):
    """Fingerprints each track and asks AcoustID / MusicBrainz what it is."""
    found = QtCore.Signal(int, str, str)      # row, title, artist
    done = QtCore.Signal(int, str)            # how many identified, error

    def __init__(self, src, rows, key):
        super().__init__()
        self.src, self.rows, self.key = src, rows, key   # rows: [(row_index, start, end)]

    def run(self):
        if not shutil.which("fpcalc"):
            self.done.emit(0, "fpcalc isn't installed (package: chromaprint).")
            return
        hits = 0
        tmp = CONF_DIR / "ident.wav"
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        for row, a, b in self.rows:
            dur = b - a
            if dur < 20:
                continue
            take = min(60.0, dur - 4)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a + 2:.2f}",
                            "-t", f"{take:.2f}", "-i", str(self.src), "-ac", "2", str(tmp)], capture_output=True)
            out = run_quiet("fpcalc", "-json", str(tmp), timeout=60)
            try:
                fp = json.loads(out)["fingerprint"]
            except (ValueError, KeyError):
                continue
            data = urllib.parse.urlencode({"client": self.key, "meta": "recordings", "duration": int(dur),
                                           "fingerprint": fp, "format": "json"}).encode()
            try:
                req = urllib.request.Request("https://api.acoustid.org/v2/lookup", data=data,
                                             headers={"User-Agent": f"MboxControl/{VERSION}"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read())
            except Exception as e:  # noqa: BLE001
                self.done.emit(hits, f"AcoustID: {e}")
                return
            if res.get("status") != "ok":
                self.done.emit(hits, f"AcoustID: {res.get('error', {}).get('message', 'lookup failed')}")
                return
            best = None
            for r in sorted(res.get("results", []), key=lambda r: -r.get("score", 0)):
                for rec in r.get("recordings", []):
                    if rec.get("title"):
                        best = rec
                        break
                if best:
                    break
            if best:
                artist = ", ".join(a.get("name", "") for a in best.get("artists", []))
                self.found.emit(row, best["title"], artist)
                hits += 1
            time.sleep(0.4)                       # AcoustID asks for ≤ 3 requests a second
        tmp.unlink(missing_ok=True)
        self.done.emit(hits, "")

# ================================================================== 4.7.2: mic & instrument guide

SOURCE_GUIDE = {
    "Voice (speaking or singing)": dict(lowcut=True, dist="About a hand's width (15 cm); back off a bit for loud singing.",
        aim="Logo side towards your mouth, slightly off-centre to avoid pops.",
        level="Loudest words just reach the first amber light.", phantom=True),
    "Wooden flute": dict(lowcut=True, dist="30–50 cm, slightly above the flute.",
        aim="Between the mouth hole and your left hand — not at the mouth hole (breath noise).",
        level="Loud low notes reach the first amber light.", phantom=True),
    "Tin whistle": dict(lowcut=True, dist="50 cm or more — it's piercing up close.",
        aim="At the finger holes, not the mouthpiece.",
        level="Gain low; Sft-lmt on for the sharp attacks.", phantom=True),
    "Harmonica": dict(lowcut=True, dist="20–30 cm, a little off to one side.",
        aim="Logo side at your hands.",
        level="Harmonica is loud — start with low gain.", phantom=True),
    "Acoustic guitar": dict(lowcut=True, dist="20–30 cm.",
        aim="Where the neck meets the body, not straight into the soundhole (boomy).",
        level="Strummed chords reach the first amber light.", phantom=True),
    "Electric guitar or bass (plugged in)": dict(lowcut=False, dist="No mic — cable into the front Mic/DI jack.",
        aim="Front/Rear button OUT (front jack). Guitar volume knob all the way up.",
        level="Hard picking reaches the first amber light. Guitar mode adds effects.", phantom=False),
}


# ================================================================== QR codes (no extra packages)

def qr_matrix(text):
    """Minimal QR encoder: byte mode, error correction level L, versions 1–4 (up to 78 bytes), mask 0."""
    data = text.encode("utf-8")
    caps = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20)}      # data codewords, ECC codewords (1 block)
    ver = next((v for v, (dc, _) in caps.items() if len(data) + 2 <= dc), None)
    if ver is None:
        raise ValueError("Text too long for this little QR encoder")
    dcw, ecw = caps[ver]
    size = 17 + 4 * ver
    # --- bit stream
    bits = [0, 1, 0, 0] + [int(b) for b in f"{len(data):08b}"]
    for byte in data:
        bits += [int(b) for b in f"{byte:08b}"]
    bits += [0] * min(4, dcw * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(words) < dcw:
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    # --- Reed–Solomon over GF(256), poly 0x11d
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    def mul(a, b):
        return 0 if a == 0 or b == 0 else exp[log[a] + log[b]]
    gen = [1]
    for i in range(ecw):
        gen = [a ^ b for a, b in zip(gen + [0], [0] + [mul(g, exp[i]) for g in gen])]
    rem = list(words) + [0] * ecw
    for i in range(len(words)):
        coef = rem[i]
        if coef:
            for j, g in enumerate(gen):
                rem[i + j] ^= mul(g, coef)
    final = words + rem[len(words):]
    # --- function patterns
    m = [[None] * size for _ in range(size)]

    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    m[rr][cc] = (0 <= dr <= 6 and dc in (0, 6)) or (0 <= dc <= 6 and dr in (0, 6)) or \
                                (2 <= dr <= 4 and 2 <= dc <= 4)
    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)
    for i in range(8, size - 8):
        m[6][i] = m[i][6] = i % 2 == 0
    if ver >= 2:
        a = size - 7
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                m[a + dr][a + dc] = max(abs(dr), abs(dc)) != 1
    m[size - 8][8] = True                                              # dark module
    reserved = set()
    for i in range(9):
        reserved |= {(8, i), (i, 8)}
    for i in range(8):
        reserved |= {(8, size - 1 - i), (size - 1 - i, 8)}
    for r, c in reserved:
        if m[r][c] is None:
            m[r][c] = False
    func = {(r, c) for r in range(size) for c in range(size) if m[r][c] is not None}
    # --- data placement (zig-zag) with mask 0
    stream = [int(b) for w in final for b in f"{w:08b}"]
    k, up, col = 0, True, size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (col, col - 1):
                if (r, c) in func:
                    continue
                bit = stream[k] if k < len(stream) else 0
                k += 1
                m[r][c] = bool(bit) ^ ((r + c) % 2 == 0)
        up = not up
        col -= 2
    # --- format info: level L (01), mask 0 (000)
    fmt = 0b01000 << 10
    g = 0b10100110111
    v = fmt
    for i in range(14, 9, -1):
        if v & (1 << i):
            v ^= g << (i - 10)
    fmt = (fmt | v) ^ 0b101010000010010
    fb = [(fmt >> i) & 1 for i in range(15)]                          # bit 0 = LSB
    for i in range(6):
        m[i][8] = bool(fb[i])
    m[7][8] = bool(fb[6])
    m[8][8] = bool(fb[7])
    m[8][7] = bool(fb[8])
    for i in range(9, 15):
        m[8][14 - i] = bool(fb[i])
    for i in range(8):
        m[8][size - 1 - i] = bool(fb[i])
    for i in range(8, 15):
        m[size - 15 + i][8] = bool(fb[i])
    m[size - 8][8] = True
    return m


def qr_pixmap(text, px=220, dark="#000000", light="#ffffff"):
    m = qr_matrix(text)
    n = len(m)
    quiet = 4
    cell = max(2, px // (n + 2 * quiet))
    img = QtGui.QPixmap(cell * (n + 2 * quiet), cell * (n + 2 * quiet))
    img.fill(QtGui.QColor(light))
    p = QtGui.QPainter(img)
    p.setPen(Qt.NoPen)
    p.setBrush(QtGui.QColor(dark))
    for r in range(n):
        for c in range(n):
            if m[r][c]:
                p.drawRect((c + quiet) * cell, (r + quiet) * cell, cell, cell)
    p.end()
    return img

# ================================================================== 4.8: live signal map

class SignalMap(QtWidgets.QWidget):
    """Inputs → Mbox mixes → outputs, built from the live router/mixer. Cables glow while signal flows."""
    boxClicked = QtCore.Signal(str, str)          # kind ('in', 'out', 'mix', 'pc', 'cap'), key

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setMinimumHeight(560)
        self.setMouseTracking(True)
        self.hit = []                               # (QRectF, kind, key)
        self.hover = None
        self.setToolTip("Click a box to rename it or open its settings. Lit cables are carrying sound "
                        "right now.")

    # ---- model -------------------------------------------------------------------
    def model(self):
        w = self.win
        srcs = [("pc", "PC", "Computer playback", "")]
        for r, (item, jack) in enumerate(INPUT_JACKS):
            srcs.append(("in", item, jack, w.label(f"in{r}", "")))
        mixes, edges_in, edges_out, direct, caps = [], [], [], [], []
        outs = []
        for name, first, _ in OUTPUT_PAIRS:
            outs.append((first, name, w.label(f"out{first}", "")))
            kind, val, _sw = w.pair_source(first)
            if kind == "mix":
                if val not in mixes:
                    mixes.append(val)
                edges_out.append((val, first))
            elif kind == "direct":
                for it in val:
                    src = "PC" if str(it).startswith("Stream-") else it
                    if (src, first) not in direct:
                        direct.append((src, first))
        mixes.sort()
        msrc = w.ctrl("mixer-source")
        for k in mixes:
            if not msrc:
                break
            pc_on = False
            for slot, v in enumerate(msrc.values):
                if not 0 <= v < len(msrc.items):
                    continue
                it = msrc.items[v]
                if max(w.mix_lr(k, slot)) <= 0:
                    continue
                if it.startswith("Stream-"):
                    pc_on = True
                elif it.startswith("Analog-A-"):
                    edges_in.append((it, k))
            if pc_on:
                edges_in.append(("PC", k))
        ss = w.ctrl("stream-source")
        if ss:
            seen = set()
            for v in ss.values:
                if 0 <= v < len(ss.items) and ss.items[v].startswith("Analog-A-") and ss.items[v] not in seen:
                    seen.add(ss.items[v])
                    caps.append(ss.items[v])
        return srcs, mixes, outs, edges_in, edges_out, direct, caps

    def level_db(self, item):
        w = self.win
        if item == "PC":
            return max(meter_db(w.input_level(PC_LEFT)), meter_db(w.input_level(PC_RIGHT)))
        return w.item_db(item)

    def out_db(self, first):
        c = self.win.ctrl("output-source-meter")
        if not c or first + 1 >= len(c.values):
            return METER_FLOOR_DB
        return max(meter_db(c.values[first]), meter_db(c.values[first + 1]))

    # ---- drawing -----------------------------------------------------------------
    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        try:
            srcs, mixes, outs, e_in, e_out, direct, caps = self.model()
        except Exception:  # noqa: BLE001
            return
        W, H = self.width(), self.height()
        colw = min(250, (W - 120) / 3.2)
        x_in, x_mid, x_out = 10, W / 2 - colw / 2, W - colw - 10
        bh, gap = 50, 12
        self.hit = []

        tallest = max(len(srcs) * (bh + gap), (len(mixes) + 1) * (58 + gap), len(outs) * (bh + gap)) - gap
        top_y = max(34, (H - tallest) / 2)

        def col(n, top=34, h=bh):
            total = n * h + (n - 1) * gap
            y0 = max(top, (H - total) / 2)
            return [QRectF(0, y0 + i * (h + gap), colw, h) for i in range(n)]

        pos = {}
        for r, (kind, key, title, sub) in zip(col(len(srcs)), srcs):
            r.moveLeft(x_in)
            pos[("src", key)] = r
            self.hit.append((r, kind, key))
        mid_items = [("mix", k) for k in mixes] + [("cap", "cap")]
        for r, it in zip(col(len(mid_items), h=58), mid_items):
            r.moveLeft(x_mid)
            pos[it] = r
            self.hit.append((r, it[0], str(it[1])))
        for r, (first, name, lbl) in zip(col(len(outs)), outs):
            r.moveLeft(x_out)
            pos[("out", first)] = r
            self.hit.append((r, "out", str(first)))

        # column titles
        f = p.font()
        f.setPointSizeF(9)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QtGui.QColor(SUBTLE))
        for x, t in ((x_in, "PLUGGED IN"), (x_mid, "INSIDE THE MBOX"), (x_out, "GOES OUT TO")):
            p.drawText(QRectF(x, top_y - 26, colw, 18), Qt.AlignLeft, t)

        def cable(a, b, db, dashed=False):
            live = db > -55
            strength = max(0.0, min(1.0, (db + 60) / 50))
            c = QtGui.QColor("#3fb950") if live else ink(60)
            if live:
                c.setAlpha(int(110 + 145 * strength))
            pen = QtGui.QPen(c, 2.5 + 2.5 * strength if live else 1.6)
            if dashed:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            s, e = QPointF(a.right(), a.center().y()), QPointF(b.left(), b.center().y())
            dx = (e.x() - s.x()) / 2
            path = QtGui.QPainterPath(s)
            path.cubicTo(QPointF(s.x() + dx, s.y()), QPointF(e.x() - dx, e.y()), e)
            p.drawPath(path)

        for src, k in e_in:
            if ("src", src) in pos and ("mix", k) in pos:
                cable(pos[("src", src)], pos[("mix", k)], self.level_db(src))
        for k, first in e_out:
            if ("mix", k) in pos and ("out", first) in pos:
                cable(pos[("mix", k)], pos[("out", first)], self.out_db(first))
        for src, first in direct:
            if ("src", src) in pos and ("out", first) in pos:
                cable(pos[("src", src)], pos[("out", first)], self.out_db(first))
        for it in caps:                              # only while sound is actually being sent — less spaghetti
            if ("src", it) in pos and self.level_db(it) > -55:
                cable(pos[("src", it)], pos[("cap", "cap")], self.level_db(it), dashed=True)

        def box(r, title, sub, accent=None, db=None):
            hov = self.hover is not None and self.hover == r
            p.setPen(QtGui.QPen(QtGui.QColor(accent or ink(40)), 1.4 if accent else 1))
            p.setBrush(QtGui.QColor(BG2).lighter(115) if hov and DARK else
                       QtGui.QColor(BG2).darker(104) if hov else QtGui.QColor(BG2))
            p.drawRoundedRect(r, 10, 10)
            if db is not None and db > -55:
                p.setPen(Qt.NoPen)
                p.setBrush(QtGui.QColor("#3fb950"))
                p.drawEllipse(QPointF(r.right() - 12, r.top() + 12), 4.5, 4.5)
            f = p.font()
            f.setPointSizeF(9.5)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QtGui.QColor(TEXT))
            fm = QtGui.QFontMetrics(f)
            p.drawText(QRectF(r.left() + 12, r.top() + 6, r.width() - 30, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(title, Qt.ElideRight, int(r.width() - 30)))
            f.setBold(False)
            f.setPointSizeF(8.5)
            p.setFont(f)
            p.setPen(QtGui.QColor(SUBTLE if sub else ink(70)))
            fm = QtGui.QFontMetrics(f)
            p.drawText(QRectF(r.left() + 12, r.top() + 26, r.width() - 20, 18), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(sub or "click to name it", Qt.ElideRight, int(r.width() - 20)))

        for kind, key, title, sub in srcs:
            r = pos[("src", key)]
            if kind == "pc":
                box(r, title, "your apps — click to see which", None, self.level_db(key))
            else:
                box(r, sub or title, title if sub else "", None, self.level_db(key))
            tag = self.win.item_colour(key) if hasattr(self.win, "item_colour") else ""
            if tag:                                  # 4.18 colour tag: a stripe down the left edge
                p.setPen(Qt.NoPen)
                p.setBrush(QtGui.QColor(tag))
                p.drawRoundedRect(QRectF(r.left() + 3, r.top() + 8, 4, r.height() - 16), 2, 2)
        for k in mixes:
            users = [n for f_, n, _ in outs if (k, f_) in e_out]
            box(pos[("mix", k)], f"Stereo Mix {k}", "→ " + ", ".join(users), ACCENT)
        names = [self.win.label(f"in{[j for j, _ in INPUT_JACKS].index(c)}", "") or dict(INPUT_JACKS)[c]
                 for c in caps if c in dict(INPUT_JACKS)]
        lb = self.win.app_state.get("loopback", -1)
        if lb >= 0:
            names.append("loopback of " + (self.win.label(f"out{lb}", "") or
                                           next(n for n, f, _ in OUTPUT_PAIRS if f == lb)))
        box(pos[("cap", "cap")], "To the computer", "apps can record: " + (", ".join(names) or "nothing"),
            ACCENT)
        for first, name, lbl in outs:
            box(pos[("out", first)], lbl or name, name if lbl else "", None, self.out_db(first))

    # ---- interaction -------------------------------------------------------------
    def _at(self, pt):
        for r, kind, key in self.hit:
            if r.contains(pt):
                return r, kind, key
        return None

    def mouseMoveEvent(self, e):
        h = self._at(e.position())
        r = h[0] if h else None
        if r != self.hover:
            self.hover = r
            self.setCursor(Qt.PointingHandCursor if r else Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, e):
        h = self._at(e.position())
        if h:
            self.boxClicked.emit(h[1], h[2])

# ================================================================== 4.9: AirPlay speakers, tape speed

AIRPLAY_CONF = Path.home() / ".config/pipewire/pipewire.conf.d/52-mbox-airplay.conf"
AIRPLAY_UDP = "6000:6130"          # PipeWire's AirPlay control/timing ports (6001/6002, retries upward)


def airplay_status():
    """→ dict(avahi_installed, avahi_on, enabled, speakers=[{name, desc}])"""
    speakers = [{"name": s.get("name"), "desc": s.get("description") or s.get("name")}
                for s in pactl_json("list", "sinks") if str(s.get("name", "")).startswith("raop_sink.")]
    return {"avahi_installed": bool(shutil.which("avahi-daemon") or Path("/usr/bin/avahi-daemon").exists()),
            "avahi_on": (run_quiet("systemctl", "is-active", "avahi-daemon") or "").strip() == "active",
            "enabled": AIRPLAY_CONF.exists(),
            "speakers": speakers}


def airplay_root_cmd():
    """The one root command AirPlay needs (Avahi, firewall), or None if nothing needs root."""
    st = airplay_status()
    root = []
    if not st["avahi_installed"]:
        argv = install_argv("avahi")
        if argv:
            root.append(" ".join(argv))
    if not st["avahi_on"]:
        root.append("systemctl enable --now avahi-daemon.service")
    kind = firewall_kind()
    ip = lan_ip()
    subnet = ".".join(ip.split(".")[:3]) + ".0/24" if ip.count(".") == 3 else None
    if kind == "ufw":
        for port in ("5353", AIRPLAY_UDP):
            root.append(f"ufw allow from {subnet} to any port {port} proto udp" if subnet
                        else f"ufw allow {port}/udp")
    elif kind == "firewalld":
        root.append("firewall-cmd --permanent --add-service=mdns && firewall-cmd --permanent "
                    f"--add-port={AIRPLAY_UDP.replace(':', '-')}/udp && firewall-cmd --reload")
    return ["sh", "-c", " && ".join(root)] if root else None


def finish_airplay():
    """The no-root part: tell PipeWire to look for speakers, now and at every login."""
    AIRPLAY_CONF.parent.mkdir(parents=True, exist_ok=True)
    AIRPLAY_CONF.write_text("""# Written by Mbox Control: find AirPlay speakers on the home network
context.modules = [
  { name = libpipewire-module-raop-discover
    args = { }
  }
]
""")
    # load it now too, so nothing has to restart (the file covers the next login)
    if "raop-discover" not in (run_quiet("pactl", "list", "short", "modules") or ""):
        run_quiet("pactl", "load-module", "module-raop-discover")


def disable_airplay():
    try:
        AIRPLAY_CONF.unlink()
    except FileNotFoundError:
        pass
    for line in (run_quiet("pactl", "list", "short", "modules") or "").splitlines():
        if "raop-discover" in line:
            run_quiet("pactl", "unload-module", line.split()[0])


def mirror_sources(main_desc):
    """What can be copied to a speaker: [(key, label)]. key 'sink:<name>' = what plays on that output,
    'src:<name>' = a named Mbox input."""
    out = [(f"sink:{MAIN_SINK}", f"Everything playing on {main_desc}")]
    for s in pactl_json("list", "sources"):
        n = str(s.get("name", ""))
        if n.startswith("mbox_") and not n.endswith(".monitor"):
            out.append((f"src:{n}", s.get("description") or n))
    return out


def start_mirror(key, sink):
    kind, name = key.split(":", 1)
    tag = re.sub(r"\W", "_", sink)[-40:]
    cap = f"node.name=mbox_mirror_in_{tag}" + (" stream.capture.sink=true" if kind == "sink" else "")
    return subprocess.Popen(["pw-loopback", "-C", name, "-P", sink, "--capture-props", cap,
                             "--playback-props", f"node.name=mbox_mirror_out_{tag} node.dont-reconnect=true"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- tape speed ------------------------------------------------------------------

def audio_rate(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return int(r.stdout.strip().split()[0])
    except (ValueError, IndexError):
        return 48000


def len_change(pct):
    """'45:00 side becomes 44:33' style hint for a speed change."""
    f = 1 / (1 + pct / 100)
    m = 45 * 60 * f
    return f"45-minute side becomes {int(m // 60)}:{int(m % 60):02d}"


def speed_filter(path, pct):
    """Tape-style speed change: pitch and tempo move together, exactly like the deck did."""
    if not pct:
        return ""
    sr = audio_rate(path)
    return f"asetrate={round(sr * (1 + pct / 100))},aresample={sr}"


def tuning_offset(x, rate):
    """How far the music sits from A440 tuning, in cents (−50…+50), and how sure (0…1).
    A tape deck running fast pushes every note sharp by the same amount."""
    import numpy as np
    n, hop = 8192, 4096
    if len(x) < n:
        return 0.0, 0.0
    win = np.hanning(n).astype(np.float32)
    starts = np.arange(0, len(x) - n, hop)
    if len(starts) > 1500:
        starts = starts[np.linspace(0, len(starts) - 1, 1500).astype(int)]
    rms = np.array([np.sqrt(np.mean(x[s:s + n] ** 2)) for s in starts])
    keep = starts[rms > max(1e-4, np.percentile(rms, 40))]
    freqs = np.fft.rfftfreq(n, 1 / rate)
    lo, hi = np.searchsorted(freqs, 100), np.searchsorted(freqs, 2000)
    acc, wsum = 0j, 0.0
    for s in keep:
        mag = np.abs(np.fft.rfft(x[s:s + n] * win))
        band = mag[lo:hi]
        floor = np.median(band) * 8
        pk = np.where((band[1:-1] > band[:-2]) & (band[1:-1] >= band[2:]) & (band[1:-1] > floor))[0] + 1
        if not len(pk):
            continue
        pk = pk[np.argsort(band[pk])[-8:]]
        for i in pk:
            a, b, c = np.log(band[i - 1] + 1e-12), np.log(band[i] + 1e-12), np.log(band[i + 1] + 1e-12)
            d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
            f = freqs[lo + i] + d * (freqs[1] - freqs[0])
            if f <= 0:
                continue
            cents = 1200 * np.log2(f / 440.0)
            w = float(band[i])
            acc += w * np.exp(2j * np.pi * cents / 100.0)
            wsum += w
    if not wsum:
        return 0.0, 0.0
    return float(np.angle(acc) / (2 * np.pi) * 100.0), float(abs(acc) / wsum)


class TuningJob(QtCore.QThread):
    """Listens to a recording and works out how far off speed the deck was."""
    done = QtCore.Signal(float, float, str)      # cents, confidence, error

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            import numpy as np
        except ImportError:
            self.done.emit(0.0, 0.0, "Needs numpy:  " + install_hint("numpy"))
            return
        rate = 11025
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(self.path), "-ac", "1",
                            "-ar", str(rate), "-f", "s16le", "-"], capture_output=True)
        if r.returncode != 0 or not r.stdout:
            self.done.emit(0.0, 0.0, r.stderr.decode(errors="ignore")[-200:] or "Couldn't read the file")
            return
        x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        cents, conf = tuning_offset(x, rate)
        self.done.emit(cents, conf, "")

# ================================================================== 4.11: first-run setup, plug-in hints

DEFAULT_MAIN = "Speakers (Mbox)"


def main_name_from_conf():
    """The name an older version gave the main output (so updating never renames it)."""
    try:
        m = re.search(r'node\.description\s*=\s*"([^"]+)"', MAIN_CONF.read_text())
        return m.group(1) if m else ""
    except OSError:
        return ""


class SetupWizard(QtWidgets.QDialog):
    """First run: what's plugged in where. Everything is optional and editable later."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Set up Mbox Control")
        self.setMinimumWidth(640)
        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(12)
        t = QtWidgets.QLabel("<h2>Hi! Tell the app about your setup</h2>")
        v.addWidget(t)
        v.addWidget(muted("Names make every page talk about your gear instead of “Input 3”. Leave anything "
                          "blank you don't use — it's all in Settings → Your setup later. Nothing here changes "
                          "the sound."))
        st = win.app_state["setup"]
        f = QtWidgets.QFormLayout()
        f.setSpacing(8)
        self.out_edit = QtWidgets.QLineEdit(win.label("out0", ""))
        self.out_edit.setPlaceholderText("e.g. Living-room amp, Studio monitors, Powered speakers")
        f.addRow("<b>Outputs 1/2</b> (main) go to", self.out_edit)
        self.in_edits = []
        for r, (item, jack) in enumerate(INPUT_JACKS):
            e = QtWidgets.QLineEdit(win.label(f"in{r}", ""))
            e.setPlaceholderText(["e.g. Microphone", "e.g. Guitar", "e.g. Turntable / phono preamp",
                                  "", "e.g. TV, tape deck, receiver tape out", ""][r] if r < 6 else "")
            f.addRow(f"<b>{jack}</b> has", e)
            self.in_edits.append(e)
        v.addLayout(f)
        self.rip = QtWidgets.QComboBox()
        self.rip.addItem("I don't rip tapes or records", "")
        for left, _, name in INPUT_PAIRS:
            self.rip.addItem(name, left)
        self.rip.setCurrentIndex(max(0, self.rip.findData(st.get("rip") if st.get("rip_set") else "")))
        f2 = QtWidgets.QFormLayout()
        f2.addRow("Tapes / records come in on", self.rip)
        v.addLayout(f2)
        v.addWidget(muted("Always hear these on the main speakers (e.g. a TV or record player that should "
                          "just work):"))
        self.always = {}
        row = QtWidgets.QHBoxLayout()
        for left, _, name in INPUT_PAIRS:
            cb = QtWidgets.QCheckBox(name)
            cb.setChecked(left in (st.get("always_on") or []))
            row.addWidget(cb)
            self.always[left] = cb
        row.addStretch(1)
        v.addLayout(row)
        bb = QtWidgets.QDialogButtonBox()
        skip = bb.addButton("Skip for now", QtWidgets.QDialogButtonBox.RejectRole)
        ok = bb.addButton("Done", QtWidgets.QDialogButtonBox.AcceptRole)
        ok.setObjectName("primary")
        skip.clicked.connect(self.reject)
        ok.clicked.connect(self.save)
        v.addWidget(bb)

    def save(self):
        w = self.win
        st = w.app_state["setup"]
        out = self.out_edit.text().strip()
        w.set_label("out0", out)
        for r, e in enumerate(self.in_edits):
            w.set_label(f"in{r}", e.text().strip())
        if self.rip.currentData():
            st["rip"], st["rip_set"] = self.rip.currentData(), True
        st["always_on"] = [k for k, cb in self.always.items() if cb.isChecked()]
        w.app_state["setup_done"] = True
        w.save_app_state()
        if out and not st.get("main_name"):
            w.rename_main_output(f"{out} (Mbox)")
        if st["always_on"]:
            w.restore_always_on()
        w.run_refreshers()
        self.accept()


class HintBar(QtWidgets.QFrame):
    """A slim bar above the pages: one suggestion, a couple of buttons, and a way to make it go away."""

    def __init__(self):
        super().__init__()
        self.setObjectName("hintbar")
        self.setStyleSheet(f"QFrame#hintbar {{ background:{BG2}; border:1px solid {ACCENT}; border-radius:12px; }}")
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(14, 8, 8, 8)
        self.text = QtWidgets.QLabel()
        self.text.setWordWrap(True)
        h.addWidget(self.text, 1)
        self.btns = QtWidgets.QHBoxLayout()
        h.addLayout(self.btns)
        self.hide()

    def offer(self, text, actions, never=None):
        """actions: [(label, fn)], first is the main one. never: fn for “Don't ask again”."""
        while self.btns.count():
            it = self.btns.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        self.text.setText(text)
        for i, (label, fn) in enumerate(actions):
            b = QtWidgets.QPushButton(label)
            if i == 0:
                b.setObjectName("primary")
            b.clicked.connect(lambda _=False, fn=fn: (self.hide(), fn()))
            self.btns.addWidget(b)
        later = QtWidgets.QPushButton("Not now")
        later.clicked.connect(self.hide)
        self.btns.addWidget(later)
        if never:
            nb = QtWidgets.QToolButton()
            nb.setText("✕")
            nb.setToolTip("Don't suggest anything for this input again (turn back on in Settings → Your setup)")
            nb.clicked.connect(lambda: (self.hide(), never()))
            self.btns.addWidget(nb)
        self.show()

# ================================================================== 4.14: loopback, OSD, app rules, backup

LOOP_AUX = (6, 7)                       # capture channels 7/8 carry the loopback


def mix_items(k):
    """Stereo Mix k as the router names it."""
    return f"Mixer-{2 * k - 1}", f"Mixer-{2 * k}"


# ============================================================ 4.19: formats & converting
AUDIO_FORMATS = [   # key, label, extension, ffmpeg audio args, keeps cover art
    ("flac", "FLAC — lossless (default)", "flac", ["-c:a", "flac", "-compression_level", "8"], True),
    ("wav24", "WAV 24-bit — lossless, big", "wav", ["-c:a", "pcm_s24le"], False),
    ("wav16", "WAV 16-bit — CD quality", "wav", ["-c:a", "pcm_s16le"], False),
    ("aiff", "AIFF 24-bit — lossless (Apple)", "aiff", ["-c:a", "pcm_s24be", "-write_id3v2", "1"], False),
    ("alac", "ALAC — lossless (Apple, .m4a)", "m4a", ["-c:a", "alac"], True),
    ("mp3_v0", "MP3 — best quality (V0)", "mp3", ["-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3"], True),
    ("mp3_320", "MP3 320 kbps", "mp3", ["-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"], True),
    ("mp3_192", "MP3 192 kbps — small, for sharing", "mp3",
     ["-c:a", "libmp3lame", "-b:a", "192k", "-id3v2_version", "3"], True),
    ("aac", "AAC 256 kbps (.m4a) — phones", "m4a", ["-c:a", "aac", "-b:a", "256k"], True),
    ("opus", "Opus 160 kbps — smallest for the quality", "opus", ["-c:a", "libopus", "-b:a", "160k"], False),
    ("ogg", "Ogg Vorbis (q6)", "ogg", ["-c:a", "libvorbis", "-q:a", "6"], False),
]
AUDIO_EXTS = {".flac", ".wav", ".aiff", ".aif", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".ape",
              ".wv", ".mka", ".alac", ".caf", ".mp2", ".ac3"}


def audio_format(key):
    return next((f for f in AUDIO_FORMATS if f[0] == key), AUDIO_FORMATS[0])


def transcode(src, dst, key):
    """Convert one file; tags (and cover art where the format holds it) come along."""
    _, _, ext, args, art = audio_format(key)
    src, dst = Path(src), Path(dst)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=sample_rate", "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    try:
        rate = int((probe.stdout or "0").strip().splitlines()[0])
    except (ValueError, IndexError):
        rate = 0
    extra = []
    # MP3/Vorbis/AAC top out at 48 kHz and Opus *is* 48 kHz: resample 96 kHz takes 
    if (key in ("aac",) or ext in ("mp3", "ogg")) and rate > 48000 or ext == "opus" and rate not in (0, 48000):
        extra = ["-ar", "48000"]
    if key == "wav16":
        extra = ["-af", "aresample=osf=s16:dither_method=triangular"]
    maps = ["-map", "0:a:0"] + (["-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic"] if art else [])
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".part." + dst.suffix.lstrip("."))
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *maps,
                        "-map_metadata", "0", *args, *extra, str(tmp)], capture_output=True, text=True)
    if r.returncode != 0 and art:                       # odd cover art: try again without it
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:a:0",
                            "-map_metadata", "0", *args, *extra, str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError((r.stderr or "ffmpeg failed").strip().splitlines()[-1][:200])
    tmp.replace(dst)
    return dst


def convert_target(src, key, out_dir=None):
    """Where a converted copy goes: a “Converted (FORMAT)” folder beside the original, or out_dir."""
    _, label, ext, _, _ = audio_format(key)
    src = Path(src)
    folder = Path(out_dir) if out_dir else src.parent / f"Converted ({label.split(' —')[0].split(' (')[0]})"
    dst = folder / f"{src.stem}.{ext}"
    if dst.resolve() == src.resolve():
        dst = folder / f"{src.stem} (converted).{ext}"
    return dst


TAKE_POLISH = [("none", "Leave as is"),
               ("normalize", "Normalize — loudest point at −1 dB"),
               ("compress", "Even out + normalize (gentle compressor)")]


def peak_db(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null",
                        "-"], capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else None


def polish_take(path, mode, target=-1.0):
    """Normalize (peak → −1 dB) or gently compress then normalize a take, in place (FLAC, same bit depth)."""
    path = Path(path)
    if mode not in ("normalize", "compress"):
        return path
    src = path
    tmp = path.with_name(path.stem + ".polish.flac")
    if mode == "compress":                       # level it to full scale first, so the threshold means the same
        pre = peak_db(path)                      # thing for a quiet take and a loud one; then ~3:1 above −20 dB
        pre_gain = (-1.0 - pre) if pre is not None and pre > -90 else 0.0
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-af",
                            f"volume={pre_gain:.2f}dB,"
                            "acompressor=threshold=-20dB:ratio=3:attack=8:release=180:knee=6",
                            "-c:a", "flac", "-sample_fmt", "s32", str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError((r.stderr or "compressor failed").strip()[-200:])
        src = tmp
    peak = peak_db(src)
    if peak is None or peak < -90:               # silence: nothing to normalize
        if src is tmp:
            tmp.replace(path)
        return path
    gain = target - peak
    out = path.with_name(path.stem + ".norm.flac")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-af",
                        f"volume={gain:.2f}dB", "-c:a", "flac", "-sample_fmt", "s32", str(out)],
                       capture_output=True, text=True)
    tmp.unlink(missing_ok=True)
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError((r.stderr or "normalize failed").strip()[-200:])
    out.replace(path)
    return path


class ConvertJob(QtCore.QThread):
    progress = QtCore.Signal(int, int, str)
    done = QtCore.Signal(int, list)

    def __init__(self, files, key, out_dir=None):
        super().__init__()
        self.files, self.key, self.out_dir = list(files), key, out_dir
        self.cancelled = False

    def run(self):
        ok, errors = 0, []
        for i, f in enumerate(self.files):
            if self.cancelled:
                break
            self.progress.emit(i, len(self.files), Path(f).name)
            try:
                transcode(f, convert_target(f, self.key, self.out_dir), self.key)
                ok += 1
            except (RuntimeError, OSError) as e:
                errors.append(f"{Path(f).name}: {e}")
        self.done.emit(ok, errors)


class DropList(QtWidgets.QListWidget):
    """A file list you can drop audio files or folders onto."""
    dropped = QtCore.Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.dropped.emit([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
        e.acceptProposedAction()


# ================================================================== 4.22: J-card maker
# Standard cassette J-card (Norelco case): 101.6 mm tall; panels left → right on the printed side:
# rear flap 26.99 mm | spine 12.7 mm | front 65.09 mm | (fold-out panels 63.5, 61.91 … mm, each 1/16" less).
JCARD_H = 101.6
JCARD_FLAP, JCARD_SPINE, JCARD_FRONT = 26.99, 12.7, 65.09
JCARD_FOLDOUTS = (63.5, 61.91)
TAPE_TYPES = ["", "Type I (normal)", "Type II (chrome)", "Type IV (metal)"]
NOISE_REDUCTION = ["", "Dolby B", "Dolby C", "dbx"]


def jcard_panels(spec):
    """[(name, x_mm, width_mm)] left → right."""
    widths = [("flap", JCARD_FLAP), ("spine", JCARD_SPINE), ("front", JCARD_FRONT)]
    for i in range(max(0, int(spec.get("panels", 3)) - 3)):
        widths.append((f"fold{i + 1}", JCARD_FOLDOUTS[i]))
    out, x = [], 0.0
    for name, w in widths:
        out.append((name, x, w))
        x += w
    return out


def jcard_size(spec):
    p = jcard_panels(spec)
    return p[-1][1] + p[-1][2], JCARD_H


def track_tags(path):
    """(title, artist) from a file's tags, falling back to its name."""
    out = run_quiet("ffprobe", "-v", "error", "-show_entries", "format_tags=title,artist", "-of", "json",
                    str(path), timeout=10) or "{}"
    try:
        tags = {k.lower(): v for k, v in json.loads(out).get("format", {}).get("tags", {}).items()}
    except ValueError:
        tags = {}
    return tags.get("title") or Path(path).stem, tags.get("artist", "")


def extract_cover(paths, dest):
    """Cover art for the card: embedded art from the first song that has some, else cover/folder.jpg/png
    beside it. → path or None."""
    for p in paths:
        p = Path(p)
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-an", "-frames:v", "1", str(dest)],
                           capture_output=True)
        if r.returncode == 0 and Path(dest).exists() and Path(dest).stat().st_size > 0:
            return Path(dest)
        for name in ("cover", "folder", "front", "Cover", "Folder", "Front"):
            for ext in ("jpg", "jpeg", "png"):
                f = p.parent / f"{name}.{ext}"
                if f.exists():
                    return f
    return None


def auto_ink(bg):
    c = QtGui.QColor(bg)
    lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return "#111111" if lum > 0.55 else "#f5f5f5"


def _fit_lines(p, lines, rect, k, max_mm, min_mm, bold_first=False):
    """Largest font size (mm) at which `lines` fit in rect (device px). → size_mm."""
    size = max_mm
    while size > min_mm:
        f = QtGui.QFont(p.font())
        f.setPixelSize(max(1, round(size * k)))
        fm = QtGui.QFontMetricsF(f)
        if fm.lineSpacing() * len(lines) <= rect.height():
            return size
        size -= 0.1
    return min_mm


def render_jcard(p, spec, k, x0=0.0, y0=0.0, guides=False):
    """Draw the card's printed (outer) side. k = device pixels per mm; (x0, y0) top-left in px."""
    bg = QtGui.QColor(spec.get("bg") or "#1d2b44")
    ink = QtGui.QColor(spec.get("ink") or auto_ink(bg))
    accent = QtGui.QColor(spec.get("accent") or "#e3a52a")
    title, artist = spec.get("title", "").strip(), spec.get("artist", "").strip()
    panels = jcard_panels(spec)
    W, H = jcard_size(spec)
    R = lambda x, y, w, h: QRectF(x0 + x * k, y0 + y * k, w * k, h * k)  # noqa: E731

    def font(size_mm, bold=False, italic=False):
        f = QtGui.QFont(spec.get("font") or p.font().family())
        f.setPixelSize(max(1, round(size_mm * k)))
        f.setBold(bold)
        f.setItalic(italic)
        return f

    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
    p.fillRect(R(0, 0, W, H), bg)
    geo = {n: (x, w) for n, x, w in panels}
    pad = 3.0                                                    # safe area inside the trim

    # front: cover art fills it; title band at the bottom (or big type when there's no art)
    fx, fw = geo["front"]
    cover = spec.get("cover")
    img = QtGui.QImage(str(cover)) if cover and Path(cover).exists() else QtGui.QImage()
    if not img.isNull():
        target = R(fx, 0, fw, H)
        s = max(target.width() / img.width(), target.height() / img.height())
        sw, sh = target.width() / s, target.height() / s
        src = QRectF((img.width() - sw) / 2, (img.height() - sh) / 2, sw, sh)
        p.drawImage(target, img, src)
        if title or artist:
            band = R(fx, H - 22, fw, 22)
            shade = QtGui.QColor(bg)
            shade.setAlpha(215)
            p.fillRect(band, shade)
            p.setPen(ink)
            p.setFont(font(5.0, bold=True))
            p.drawText(R(fx + pad, H - 21, fw - 2 * pad, 11), Qt.AlignLeft | Qt.AlignVCenter,
                       QtGui.QFontMetricsF(p.font()).elidedText(title, Qt.ElideRight, (fw - 2 * pad) * k))
            p.setFont(font(3.4))
            p.drawText(R(fx + pad, H - 11, fw - 2 * pad, 8), Qt.AlignLeft | Qt.AlignVCenter,
                       QtGui.QFontMetricsF(p.font()).elidedText(artist, Qt.ElideRight, (fw - 2 * pad) * k))
    else:
        p.setPen(accent)
        p.setBrush(Qt.NoBrush)
        pen = QtGui.QPen(accent, 0.6 * k)
        p.setPen(pen)
        p.drawLine(QPointF(x0 + (fx + pad) * k, y0 + 30 * k), QPointF(x0 + (fx + fw - pad) * k, y0 + 30 * k))
        p.setPen(ink)
        p.setFont(font(7.0, bold=True))
        p.drawText(R(fx + pad, 34, fw - 2 * pad, 40), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, title)
        p.setFont(font(4.0))
        p.drawText(R(fx + pad, 76, fw - 2 * pad, 18), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, artist)

    # spine: one line along its length
    sx, sw = geo["spine"]
    spine = spec.get("spine") or " — ".join(t for t in (artist, title) if t)
    p.save()
    centre = QPointF(x0 + (sx + sw / 2) * k, y0 + H / 2 * k)
    p.translate(centre)
    p.rotate(90 if spec.get("flip_spine") else -90)
    p.setPen(ink)
    size = min(5.2, sw * 0.42)
    p.setFont(font(size, bold=True))
    fm = QtGui.QFontMetricsF(p.font())
    while fm.horizontalAdvance(spine) > (H - 2 * pad) * k and size > 2.2:
        size -= 0.2
        p.setFont(font(size, bold=True))
        fm = QtGui.QFontMetricsF(p.font())
    p.drawText(QRectF(-(H / 2 - pad) * k, -sw / 2 * k, (H - 2 * pad) * k, sw * k), Qt.AlignCenter,
               fm.elidedText(spine, Qt.ElideRight, (H - 2 * pad) * k))
    p.restore()
    p.setPen(QtGui.QPen(accent, 0.35 * k))
    for edge in (sx, sx + sw):                                   # thin accent rules along the folds
        p.drawLine(QPointF(x0 + edge * k, y0 + 4 * k), QPointF(x0 + edge * k, y0 + (H - 4) * k))

    # track list: on the fold-out panel when there is one, else on the rear flap
    list_panel = "fold1" if "fold1" in geo else "flap"
    lx, lw = geo[list_panel]
    sides = spec.get("sides", {})
    show_times = spec.get("times", True)
    lines = []
    for side in ("A", "B"):
        items = sides.get(side, [])
        if not items:
            continue
        lines.append(("head", f"SIDE {side}", sum(d for _, d in items)))
        for n, (name, secs) in enumerate(items, 1):
            lines.append(("track", f"{n}. {name}", secs))
        lines.append(("gap", "", 0))
    if lines and lines[-1][0] == "gap":
        lines.pop()
    bits = [x for x in (spec.get("tape_type", ""), spec.get("nr", ""), spec.get("date", "")) if x]
    narrow = lw < 40
    info = ("\n" if narrow else " · ").join(bits)
    foot = (2.6 * len(bits) if narrow else 4.0) if bits else 0.0
    area = R(lx + 2.0, pad, lw - 4.0, H - 2 * pad - foot - 1)

    def layout(size, draw=False):
        """Lay the list out at `size` mm, wrapping long names under themselves. → height used (px)."""
        f_track, f_head = font(size), font(size * 1.05, bold=True)
        fm_t, fm_h = QtGui.QFontMetricsF(f_track), QtGui.QFontMetricsF(f_head)
        y = area.top()
        for kind, text, secs in lines:
            if kind == "gap":
                y += fm_t.lineSpacing() * 0.6
                continue
            fm = fm_h if kind == "head" else fm_t
            t = fmt_time(secs) if (show_times and secs) else ""
            tw = fm.horizontalAdvance(t) + (1.0 * k if t else 0)
            box = QRectF(area.left(), y, area.width() - tw, 10_000)
            hgt = fm.boundingRect(box, Qt.AlignLeft | Qt.TextWordWrap, text).height()
            if draw:
                p.setFont(f_head if kind == "head" else f_track)
                p.setPen(accent if kind == "head" else ink)
                p.drawText(QRectF(area.left(), y, area.width() - tw, hgt), Qt.AlignLeft | Qt.TextWordWrap, text)
                if t:
                    p.drawText(QRectF(area.left(), y, area.width(), fm.lineSpacing()),
                               Qt.AlignRight | Qt.AlignTop, t)
            y += hgt + (0.15 * k if kind == "track" else 0.25 * k)
        return y - area.top()
    size = 3.2 if not narrow else 2.4
    while size > 1.25 and layout(size) > area.height():
        size -= 0.05
    layout(size, draw=True)
    if info:
        p.setFont(font(1.9))
        p.setPen(ink)
        p.drawText(R(lx + 2.0, H - pad - foot, lw - 4.0, foot), Qt.AlignLeft | Qt.AlignBottom | Qt.TextWordWrap,
                   info)
    if list_panel != "flap":                                    # flap then carries the title, small
        fx2, fw2 = geo["flap"]
        p.setPen(ink)
        p.setFont(font(3.0, bold=True))
        fm = QtGui.QFontMetricsF(p.font())
        box = R(fx2 + 2, pad, fw2 - 4, 60)
        th = fm.boundingRect(box, Qt.AlignLeft | Qt.TextWordWrap, title).height() if title else 0
        p.drawText(box, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, title)
        p.setFont(font(2.3))
        p.drawText(QRectF(box.left(), box.top() + th + 1.0 * k, box.width(), 40 * k),
                   Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, artist)

    if guides:                                                   # preview only: where it folds
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 120), max(1.0, 0.25 * k), Qt.DashLine)
        p.setPen(pen)
        for _, x, _ in panels[1:]:
            p.drawLine(QPointF(x0 + x * k, y0), QPointF(x0 + x * k, y0 + H * k))
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 90), max(1.0, 0.2 * k)))
        p.setBrush(Qt.NoBrush)
        p.drawRect(R(0, 0, W, H))
    p.restore()


def jcard_marks(p, spec, k, x0, y0):
    """Cut marks at the corners and fold marks above/below the folds, outside the card."""
    W, H = jcard_size(spec)
    pen = QtGui.QPen(QtGui.QColor("#000000"), max(1.0, 0.15 * k))
    p.setPen(pen)
    gap, ln = 2.0 * k, 5.0 * k
    for cx in (x0, x0 + W * k):
        for cy in (y0, y0 + H * k):
            dx = -1 if cx == x0 else 1
            dy = -1 if cy == y0 else 1
            p.drawLine(QPointF(cx + dx * gap, cy), QPointF(cx + dx * (gap + ln), cy))
            p.drawLine(QPointF(cx, cy + dy * gap), QPointF(cx, cy + dy * (gap + ln)))
    p.setPen(QtGui.QPen(QtGui.QColor("#000000"), max(1.0, 0.15 * k), Qt.DashLine))
    for _, x, _ in jcard_panels(spec)[1:]:
        X = x0 + x * k
        p.drawLine(QPointF(X, y0 - gap), QPointF(X, y0 - gap - ln))
        p.drawLine(QPointF(X, y0 + H * k + gap), QPointF(X, y0 + H * k + gap + ln))


LETTER_COUNTRIES = {"UnitedStates", "Canada", "Mexico", "Philippines", "Chile", "Colombia", "Venezuela",
                    "CostaRica", "Guatemala", "ElSalvador", "Panama", "PuertoRico", "DominicanRepublic"}


def default_paper():
    """'Letter' in the countries that print on it (Canada is metric but uses Letter), else 'A4'."""
    loc = QtCore.QLocale()
    terr = loc.territory() if hasattr(loc, "territory") else loc.country()
    name = getattr(terr, "name", str(terr)).split(".")[-1]
    return "Letter" if name in LETTER_COUNTRIES else "A4"


def jcard_page_size(paper=None):
    return QtGui.QPageSize(QtGui.QPageSize.Letter if (paper or default_paper()) == "Letter" else QtGui.QPageSize.A4)


def save_jcard_pdf(spec, path, page=None):
    """Actual size, centred near the top of the page, with cut/fold marks and a 50 mm check line."""
    w = QtGui.QPdfWriter(str(path))
    w.setPageSize(page or jcard_page_size(spec.get("paper")))
    w.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))
    w.setResolution(1200)
    w.setTitle(spec.get("title") or "J-card")
    w.setCreator(f"{APP_NAME} {VERSION}")
    k = 1200 / 25.4
    p = QtGui.QPainter(w)
    pw = w.width()
    W, H = jcard_size(spec)
    x0, y0 = (pw - W * k) / 2, 25 * k
    render_jcard(p, spec, k, x0, y0)
    jcard_marks(p, spec, k, x0, y0)
    p.setPen(QtGui.QPen(QtGui.QColor("#000000"), 0.3 * k))
    ly = y0 + (H + 16) * k
    p.drawLine(QPointF(x0, ly), QPointF(x0 + 50 * k, ly))
    for x in (x0, x0 + 50 * k):
        p.drawLine(QPointF(x, ly - 1.5 * k), QPointF(x, ly + 1.5 * k))
    f = QtGui.QFont()
    f.setPixelSize(round(2.8 * k))
    p.setFont(f)
    p.drawText(QRectF(x0 + 53 * k, ly - 3 * k, 150 * k, 6 * k), Qt.AlignLeft | Qt.AlignVCenter,
               "← this line should measure exactly 50 mm. Print at 100% / “actual size” — not “fit to page”.")
    p.drawText(QRectF(x0, ly + 4 * k, W * k + 80 * k, 14 * k), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
               "Cut along the solid corner marks. Fold at the dashed marks: spine first, then the flap. "
               "Card stock around 160–200 g/m² folds cleanly.")
    p.end()
    return Path(path)


def save_jcard_png(spec, path, dpi=600):
    k = dpi / 25.4
    W, H = jcard_size(spec)
    img = QtGui.QImage(round(W * k), round(H * k), QtGui.QImage.Format_RGB32)
    img.setDotsPerMeterX(round(dpi / 0.0254))
    img.setDotsPerMeterY(round(dpi / 0.0254))
    img.fill(QtGui.QColor("white"))
    p = QtGui.QPainter(img)
    render_jcard(p, spec, k)
    p.end()
    img.save(str(path))
    return Path(path)


class JCardDialog(QtWidgets.QDialog):
    """Fill in the card, see it live, save a print-ready PDF (or a PNG for a print shop)."""

    def __init__(self, win, sides, files):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Make a J-card")
        self.resize(1080, 640)
        saved = dict(win.app_state.get("jcard", {}))
        self.spec = {"title": saved.get("title", ""), "artist": saved.get("artist", ""), "spine": "",
                     "sides": sides, "cover": None, "bg": saved.get("bg", "#1d2b44"),
                     "accent": saved.get("accent", "#e3a52a"), "panels": saved.get("panels", 3),
                     "tape_type": saved.get("tape_type", ""), "nr": saved.get("nr", ""),
                     "date": str(datetime.now().year), "times": saved.get("times", True),
                     "flip_spine": saved.get("flip_spine", False)}
        if files:
            art = extract_cover(files, CONF_DIR / "jcard-cover.jpg")
            if art:
                self.spec["cover"] = str(art)
        outer = QtWidgets.QHBoxLayout(self)
        form = QtWidgets.QFormLayout()
        outer.addLayout(form, 2)
        self.title = QtWidgets.QLineEdit(self.spec["title"])
        self.title.setPlaceholderText("e.g. Summer mix")
        self.artist = QtWidgets.QLineEdit(self.spec["artist"])
        self.artist.setPlaceholderText("Artist, or “Various” / a dedication")
        self.spine = QtWidgets.QLineEdit()
        self.spine.setPlaceholderText("Artist — Title (automatic)")
        for w_ in (self.title, self.artist, self.spine):
            w_.textChanged.connect(self.changed)
        form.addRow("Title", self.title)
        form.addRow("Artist / subtitle", self.artist)
        form.addRow("Spine", self.spine)
        self.side_edit = {}
        for side in ("A", "B"):
            e = QtWidgets.QPlainTextEdit("\n".join(f"{n}  {fmt_time(d)}" if d else n for n, d in sides.get(side, [])))
            e.setToolTip("One song per line. A time at the end (3:45) is shown on the right.")
            e.setMinimumHeight(90)
            e.textChanged.connect(self.changed)
            form.addRow(f"Side {side}", e)
            self.side_edit[side] = e
        crow = QtWidgets.QHBoxLayout()
        self.cover_lbl = muted(Path(self.spec["cover"]).name if self.spec["cover"] else "none — big type instead")
        cb = QtWidgets.QPushButton("Choose…")
        cb.clicked.connect(self.choose_cover)
        cx = QtWidgets.QPushButton("None")
        cx.clicked.connect(lambda: self.set_cover(None))
        crow.addWidget(self.cover_lbl, 1)
        crow.addWidget(cb)
        crow.addWidget(cx)
        form.addRow("Cover art", crow)
        colrow = QtWidgets.QHBoxLayout()
        self.colour_btns = {}
        for key, label in (("bg", "Background"), ("accent", "Accent")):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, key=key: self.pick_colour(key))
            colrow.addWidget(b)
            self.colour_btns[key] = b
        colrow.addStretch(1)
        form.addRow("Colours", colrow)
        self.panels = QtWidgets.QComboBox()
        self.panels.addItem("Standard (3 panels)", 3)
        self.panels.addItem("With a fold-out for the track list (4 panels)", 4)
        self.panels.addItem("Two fold-outs (5 panels)", 5)
        self.panels.setCurrentIndex(max(0, self.panels.findData(self.spec["panels"])))
        self.panels.currentIndexChanged.connect(self.changed)
        form.addRow("Layout", self.panels)
        trow = QtWidgets.QHBoxLayout()
        self.tape = QtWidgets.QComboBox()
        self.tape.addItems([t or "Tape type —" for t in TAPE_TYPES])
        self.tape.setCurrentIndex(max(0, TAPE_TYPES.index(self.spec["tape_type"]) if self.spec["tape_type"] in
                                      TAPE_TYPES else 0))
        self.nr = QtWidgets.QComboBox()
        self.nr.addItems([n or "Noise reduction —" for n in NOISE_REDUCTION])
        self.nr.setCurrentIndex(NOISE_REDUCTION.index(self.spec["nr"]) if self.spec["nr"] in NOISE_REDUCTION else 0)
        self.date = QtWidgets.QLineEdit(self.spec["date"])
        self.date.setMaximumWidth(90)
        for w_ in (self.tape, self.nr):
            w_.currentIndexChanged.connect(self.changed)
        self.date.textChanged.connect(self.changed)
        trow.addWidget(self.tape)
        trow.addWidget(self.nr)
        trow.addWidget(self.date)
        form.addRow("On the card", trow)
        orow = QtWidgets.QHBoxLayout()
        self.times = QtWidgets.QCheckBox("Song times")
        self.times.setChecked(self.spec["times"])
        self.flip = QtWidgets.QCheckBox("Flip spine text")
        self.flip.setChecked(self.spec["flip_spine"])
        self.flip.setToolTip("Which way the spine reads. Flip it if it's upside-down on your shelf.")
        for w_ in (self.times, self.flip):
            w_.toggled.connect(self.changed)
            orow.addWidget(w_)
        orow.addStretch(1)
        form.addRow("", orow)
        self.paper = QtWidgets.QComboBox()
        self.paper.addItems(["Letter", "A4"])
        self.paper.setCurrentText(saved.get("paper") or default_paper())
        self.paper.currentIndexChanged.connect(self.changed)
        form.addRow("Paper", self.paper)
        right = QtWidgets.QVBoxLayout()
        outer.addLayout(right, 3)
        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(480, 420)
        right.addWidget(self.preview, 1)
        right.addWidget(muted("Dashed lines: folds (not printed). The PDF is actual size with cut and fold "
                              "marks — print at 100%."))
        brow = QtWidgets.QHBoxLayout()
        pdf = QtWidgets.QPushButton("Save PDF to print…")
        pdf.setObjectName("primary")
        pdf.clicked.connect(self.save_pdf)
        png = QtWidgets.QPushButton("Save PNG (600 dpi)…")
        png.setToolTip("For a print shop or an image editor: the flat card, no marks.")
        png.clicked.connect(self.save_png)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        brow.addStretch(1)
        brow.addWidget(png)
        brow.addWidget(pdf)
        brow.addWidget(close)
        right.addLayout(brow)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.render_preview)
        self.paint_colour_btns()
        self.changed()

    @staticmethod
    def parse_side(text):
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(.*?)\s+(\d{1,2}):(\d{2})$", line)
            out.append((m.group(1), int(m.group(2)) * 60 + int(m.group(3))) if m else (line, 0))
        return out

    def collect(self):
        s = self.spec
        s.update(title=self.title.text(), artist=self.artist.text(), spine=self.spine.text(),
                 panels=self.panels.currentData(), tape_type=TAPE_TYPES[self.tape.currentIndex()],
                 nr=NOISE_REDUCTION[self.nr.currentIndex()], date=self.date.text(), times=self.times.isChecked(),
                 flip_spine=self.flip.isChecked(), paper=self.paper.currentText(),
                 sides={k: self.parse_side(e.toPlainText()) for k, e in self.side_edit.items()})
        keep = {k: s[k] for k in ("title", "artist", "bg", "accent", "panels", "tape_type", "nr", "times",
                                  "flip_spine", "paper")}
        self.win.app_state["jcard"] = keep
        return s

    def changed(self, *_):
        self.timer.start(120)

    def render_preview(self):
        spec = self.collect()
        W, H = jcard_size(spec)
        avail = self.preview.size()
        k = min((avail.width() - 20) / W, (avail.height() - 20) / H)
        img = QtGui.QImage(round(W * k) + 2, round(H * k) + 2, QtGui.QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QtGui.QPainter(img)
        render_jcard(p, spec, k, 1, 1, guides=True)
        p.end()
        self.preview.setPixmap(QtGui.QPixmap.fromImage(img))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.changed()

    def set_cover(self, path):
        self.spec["cover"] = str(path) if path else None
        self.cover_lbl.setText(Path(path).name if path else "none — big type instead")
        self.changed()

    def choose_cover(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Cover art", str(HOME / "Pictures"),
                                                     "Images (*.jpg *.jpeg *.png *.webp *.bmp)")
        if f:
            self.set_cover(f)

    def pick_colour(self, key):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.spec[key]), self, "Colour")
        if c.isValid():
            self.spec[key] = c.name()
            self.paint_colour_btns()
            self.changed()

    def paint_colour_btns(self):
        for key, b in self.colour_btns.items():
            b.setStyleSheet(f"background:{self.spec[key]}; color:{auto_ink(self.spec[key])};")

    def default_name(self, ext):
        base = safe_name(self.title.text() or "J-card")
        return str((HOME / "Documents" if (HOME / "Documents").exists() else HOME) / f"{base} J-card.{ext}")

    def save_pdf(self):
        f, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save J-card PDF", self.default_name("pdf"), "PDF (*.pdf)")
        if f:
            save_jcard_pdf(self.collect(), f)
            self.win.save_app_state()
            self.win.toast(f"Saved {Path(f).name} — print at 100% (actual size).", 8000)
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(f))

    def save_png(self):
        f, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save J-card PNG", self.default_name("png"), "PNG (*.png)")
        if f:
            save_jcard_png(self.collect(), f)
            self.win.save_app_state()
            self.win.toast(f"Saved {Path(f).name}.", 6000)


class SpectrumHub(QtCore.QObject):
    """One live analyser per watched input, shared by every strip showing it; stops when nobody looks."""

    def __init__(self, win):
        super().__init__(win)
        self.win, self.feeds = win, {}          # key → [Analyzer, set(strips)]

    def channels_for(self, key):
        """Which capture channels (AUX n) carry these inputs right now."""
        ss = self.win.ctrl("stream-source")
        if not ss:
            return None
        idx = []
        for item in key:
            hit = next((a for a, v in enumerate(ss.values) if 0 <= v < len(ss.items) and ss.items[v] == item), None)
            if hit is None:
                return None
            idx.append(hit)
        return idx

    def subscribe(self, strip, key):
        if key in self.feeds:
            self.feeds[key][1].add(strip)
            return
        idx = self.channels_for(key)
        src = mbox_pw_node("source")
        if idx is None or not src or not shutil.which("parec"):
            return
        li, ri = idx[0], idx[-1]
        an = Analyzer(src, (8, li, ri, ",".join(f"aux{i}" for i in range(8))), self.win.rate())
        feed = [an, {strip}]
        an.frame.connect(lambda d, feed=feed: [s.set_bands(d["bands"]) for s in list(feed[1])])
        an.failed.connect(lambda _m, key=key: self.drop(key))
        self.feeds[key] = feed
        an.start()

    def unsubscribe(self, strip):
        for key, (an, strips) in list(self.feeds.items()):
            strips.discard(strip)
            if not strips:
                self.drop(key)

    def drop(self, key):
        feed = self.feeds.pop(key, None)
        if feed:
            feed[0].stop()
            feed[0].wait(1500)

    def stop_all(self):
        for key in list(self.feeds):
            self.drop(key)


class LevelStrip(QtWidgets.QWidget):
    """The last minute of an input, scrolling, plus a live meter with clip warning. `items()` returns the
    Mbox inputs to watch; `marked()` returns how many seconds to shade (what a save would grab), or 0."""
    SPAN, STEP = 60.0, 0.05

    def __init__(self, win, items, marked=lambda: 0, spectrum=True, title="", compact=False):
        super().__init__()
        self.win, self.items, self.marked = win, items, marked
        self.spectrum, self.title = spectrum, title
        self.hist = collections.deque(maxlen=int(self.SPAN / self.STEP))
        self.now, self.peak, self.peak_t, self.clip_t = -60.0, -60.0, 0.0, 0.0
        self.setMinimumHeight(40 if compact else 64)
        self.setMaximumHeight(44 if compact else 72)
        self.setToolTip("The last minute of this input. Aim for peaks around the dashed −6 dB line; "
                        "red means turn the gain knob down.")
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(self.STEP * 1000))
        self.bands, self.band_peaks, self.sub_key = None, None, None

    SPEC_W = 230

    def set_bands(self, bands):
        if self.band_peaks is None or len(self.band_peaks) != len(bands):
            self.band_peaks = list(bands)
        self.band_peaks = [max(b, pk - 1.5) for b, pk in zip(bands, self.band_peaks)]   # slow fall
        self.bands = bands

    def showEvent(self, e):
        super().showEvent(e)
        self.resubscribe()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.win.spectrum_hub.unsubscribe(self)
        self.sub_key, self.bands = None, None

    def resubscribe(self):
        if not self.spectrum:
            return
        key = tuple(i for i in self.items() if i)
        if key != self.sub_key:
            self.win.spectrum_hub.unsubscribe(self)
            self.bands, self.band_peaks = None, None
            self.sub_key = key
            if key:
                self.win.spectrum_hub.subscribe(self, key)

    def tick(self):
        if not self.isVisible():
            return
        its = [i for i in self.items() if i]
        if tuple(its) != self.sub_key:
            self.resubscribe()
        db = max((self.win.item_db(i) for i in its), default=-60.0)
        t = time.time()
        self.now = db
        self.hist.append(db)
        if db >= self.peak or t - self.peak_t > 2:
            self.peak, self.peak_t = db, t
        if db > -1:
            self.clip_t = t
        self.update()

    @staticmethod
    def colour(db):
        return QtGui.QColor("#f85149" if db > -3 else "#d29922" if db > -12 else "#3fb950")

    @staticmethod
    def y_of(db, top, h):
        return top + h * (1 - max(0.0, min(1.0, (db + 60) / 60)))

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        meter_w = 118
        spec_w = self.SPEC_W if w > 620 and self.spectrum else 0
        gw = w - meter_w - 10 - (spec_w + 10 if spec_w else 0)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(BG2))
        p.drawRoundedRect(QRectF(0, 0, gw, h), 8, 8)
        n = self.hist.maxlen
        mark = self.marked()
        if mark:                                          # what “Save last 30 s” would catch
            x0 = gw * (1 - min(mark, self.SPAN) / self.SPAN)
            c = QtGui.QColor(ACCENT)
            c.setAlpha(40)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x0, 0, gw - x0, h), 8, 8)
            p.setPen(QtGui.QColor(ACCENT))
            f = p.font()
            f.setPointSizeF(7.5)
            p.setFont(f)
            p.drawText(QRectF(x0 + 6, 2, gw - x0 - 8, 14), Qt.AlignLeft | Qt.AlignTop, f"last {int(mark)} s")
            p.setPen(Qt.NoPen)
        vals = list(self.hist)
        cols = max(1, int(gw) - 8)
        per = n / cols
        base = h - 4
        for x in range(cols):                              # newest on the right
            a = int(len(vals) - (cols - x) * per)
            b = int(len(vals) - (cols - x - 1) * per)
            if b <= 0:
                continue
            db = max(vals[max(0, a):b], default=-60.0)
            if db <= -59:
                continue
            y = self.y_of(db, 4, h - 8)
            p.setBrush(self.colour(db))
            p.drawRect(QRectF(4 + x, y, 1.0, base - y))
        if self.title:
            f = p.font()
            f.setPointSizeF(8)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QtGui.QColor(SUBTLE))
            p.drawText(QRectF(10, 2, gw - 20, 14), Qt.AlignLeft | Qt.AlignTop, self.title)
        pen = QtGui.QPen(ink(70), 1, Qt.DashLine)          # target: peaks around −6 dB
        p.setPen(pen)
        y6 = self.y_of(-6, 4, h - 8)
        p.drawLine(QPointF(4, y6), QPointF(gw - 4, y6))
        if spec_w:                                        # spectrum: low notes left, high notes right
            sx = gw + 10
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(BG2))
            p.drawRoundedRect(QRectF(sx, 0, spec_w, h), 8, 8)
            if self.bands:
                nb = len(self.bands)
                bw = (spec_w - 10) / nb
                for i, (b, pk) in enumerate(zip(self.bands, self.band_peaks)):
                    lvl = max(0.0, min(1.0, (b + 80) / 80))
                    x = sx + 5 + i * bw
                    bh = (h - 18) * lvl
                    c = QtGui.QColor(ACCENT)
                    c.setAlpha(110 + int(140 * lvl))
                    p.setBrush(c)
                    p.drawRect(QRectF(x, h - 14 - bh, max(1.0, bw - 1), bh))
                    py = h - 14 - (h - 18) * max(0.0, min(1.0, (pk + 80) / 80))
                    p.setBrush(ink(160))
                    p.drawRect(QRectF(x, py, max(1.0, bw - 1), 1.5))
            f = p.font()
            f.setPointSizeF(7)
            p.setFont(f)
            p.setPen(QtGui.QColor(SUBTLE))
            for txt, frac in (("100", 0.22), ("1k", 0.56), ("10k", 0.9)):
                p.drawText(QRectF(sx + 5 + (spec_w - 10) * frac - 15, h - 13, 30, 12), Qt.AlignCenter, txt)
            if not self.bands:
                p.drawText(QRectF(sx, 0, spec_w, h - 12), Qt.AlignCenter, "spectrum")
            mx = sx + spec_w + 10
        else:
            mx = gw + 10
        # live meter
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(BG2))
        p.drawRoundedRect(QRectF(mx, 0, 16, h), 4, 4)
        y = self.y_of(self.now, 3, h - 6)
        p.setBrush(self.colour(self.now))
        p.drawRoundedRect(QRectF(mx + 3, y, 10, h - 3 - y), 2, 2)
        yp = self.y_of(self.peak, 3, h - 6)
        p.setBrush(QtGui.QColor(TEXT))
        p.drawRect(QRectF(mx + 3, yp, 10, 2))
        f = p.font()
        f.setPointSizeF(10)
        f.setBold(True)
        p.setFont(f)
        clipped = time.time() - self.clip_t < 3
        p.setPen(QtGui.QColor("#f85149") if clipped else QtGui.QColor(TEXT))
        txt = "—" if self.peak <= -59 else f"{self.peak:+.0f} dB".replace("+", "") if self.peak < 0 else "0 dB"
        compact = h < 56
        if compact:                                     # short strips: number and one word on two tight lines
            p.drawText(QRectF(mx + 24, 0, meter_w - 26, h / 2 + 2), Qt.AlignLeft | Qt.AlignBottom, txt)
            f.setBold(False)
            f.setPointSizeF(8)
            p.setFont(f)
            p.setPen(QtGui.QColor("#f85149") if clipped else QtGui.QColor(SUBTLE))
            word = ("CLIPPING" if clipped else "silent" if self.peak <= -59 else "quiet" if self.peak < -24 else
                    "good" if self.peak < -3 else "hot")
            p.drawText(QRectF(mx + 24, h / 2 + 2, meter_w - 26, h / 2 - 2), Qt.AlignLeft | Qt.AlignTop, word)
            return
        p.drawText(QRectF(mx + 24, 4, meter_w - 26, 22), Qt.AlignLeft | Qt.AlignVCenter, txt)
        f.setBold(False)
        f.setPointSizeF(8)
        p.setFont(f)
        p.setPen(QtGui.QColor("#f85149") if clipped else QtGui.QColor(SUBTLE))
        hint = ("CLIPPING — gain down" if clipped else "silent" if self.peak <= -59 else
                "quiet — gain up" if self.peak < -24 else "good" if self.peak < -3 else "hot")
        p.drawText(QRectF(mx + 24, 28, meter_w - 26, h - 30), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, hint)


class Osd:
    """The volume/speaker pop-up. Uses Plasma's own OSD when it's there, otherwise a small window."""

    def __init__(self):
        self.win = None
        self.timer = None
        self.kde = None

    def _kde(self):
        if self.kde is None:
            try:
                from PySide6 import QtDBus
                iface = QtDBus.QDBusInterface("org.kde.plasmashell", "/org/kde/osdService", "org.kde.osdService",
                                              QtDBus.QDBusConnection.sessionBus())
                self.kde = iface if iface.isValid() else False
            except Exception:  # noqa: BLE001
                self.kde = False
        return self.kde

    def volume(self, percent, title):
        kde = self._kde()
        if kde:
            kde.call("mediaPlayerVolumeChanged", int(percent), title, "audio-card")
            return
        self._show(f"{title}   {int(percent)}%", percent)

    def text(self, icon, text):
        kde = self._kde()
        if kde:
            kde.call("showText", icon, text)
            return
        self._show(text, None)

    def _show(self, text, percent):
        if self.win is None:
            w = QtWidgets.QFrame(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            w.setAttribute(Qt.WA_ShowWithoutActivating)
            w.setStyleSheet(f"QFrame {{ background:{BG1}; border:1px solid {ACCENT}; border-radius:14px; }}"
                            f"QLabel {{ border:none; font-size:12pt; color:{TEXT}; }}")
            v = QtWidgets.QVBoxLayout(w)
            v.setContentsMargins(22, 14, 22, 14)
            w.label = QtWidgets.QLabel()
            v.addWidget(w.label)
            w.bar = QtWidgets.QProgressBar()
            w.bar.setTextVisible(False)
            w.bar.setFixedHeight(8)
            v.addWidget(w.bar)
            self.win = w
            self.timer = QtCore.QTimer()
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(w.hide)
        self.win.label.setText(text)
        self.win.bar.setVisible(percent is not None)
        if percent is not None:
            self.win.bar.setValue(int(percent))
        self.win.adjustSize()
        scr = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.win.move(scr.center().x() - self.win.width() // 2, scr.bottom() - self.win.height() - 80)
        self.win.show()
        self.timer.start(1600)


BACKUP_FILES = ["app-state.json", "labels.json"]
SECRET_KEYS = ("discogs_token", "acoustid_key")


def export_settings(dest, include_keys=False):
    """Everything you set up, in one file: names, scenes, speaker sets, rules, and the PipeWire bits
    the app wrote. The phone-remote key is never included."""
    import zipfile
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name in BACKUP_FILES:
            p = CONF_DIR / name
            if not p.exists():
                continue
            if name == "app-state.json":
                st = load_json(p, {})
                st.pop("remote_token", None)
                if not include_keys:
                    for k in SECRET_KEYS:
                        st.pop(k, None)
                z.writestr(name, json.dumps(st, indent=1))
            else:
                z.write(p, name)
        for p in sorted(SCENES_DIR.glob("*.json")) if SCENES_DIR.exists() else []:
            z.write(p, f"scenes/{p.name}")
        for p in sorted(PW_CONF_DIR.glob("*mbox*.conf")) if PW_CONF_DIR.exists() else []:
            z.write(p, f"pipewire/{p.name}")
        nos = NOSUSPEND_CONF
        if nos.exists():
            z.write(nos, f"wireplumber/{nos.name}")
        z.writestr("mbox-control-backup.json", json.dumps({"version": VERSION, "made": datetime.now().isoformat()}))


def import_settings(src):
    """→ (ok, message, touched_pipewire). Keeps your own keys if the backup didn't include them."""
    import zipfile
    try:
        z = zipfile.ZipFile(src)
    except (OSError, zipfile.BadZipFile):
        return False, "That isn't a Mbox Control backup.", False
    names = z.namelist()
    if "mbox-control-backup.json" not in names:
        return False, "That isn't a Mbox Control backup.", False
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    pw = False
    for n in names:
        if n.endswith("/") or ".." in n or n.startswith("/"):
            continue
        data = z.read(n)
        if n == "app-state.json":
            new = json.loads(data.decode())
            old = load_json(STATE_FILE, {})
            for k in SECRET_KEYS + ("remote_token",):
                if k in old and not new.get(k):
                    new[k] = old[k]
            save_json(STATE_FILE, new)
        elif n == "labels.json":
            (CONF_DIR / n).write_bytes(data)
        elif n.startswith("scenes/") and n.endswith(".json"):
            SCENES_DIR.mkdir(parents=True, exist_ok=True)
            (SCENES_DIR / Path(n).name).write_bytes(data)
        elif n.startswith("pipewire/") and n.endswith(".conf"):
            PW_CONF_DIR.mkdir(parents=True, exist_ok=True)
            (PW_CONF_DIR / Path(n).name).write_bytes(data)
            pw = True
        elif n.startswith("wireplumber/") and n.endswith(".conf"):
            NOSUSPEND_CONF.parent.mkdir(parents=True, exist_ok=True)
            NOSUSPEND_CONF.write_bytes(data)
            pw = True
    return True, "", pw

# ================================================================== 4.15: EQ (headphones / speakers), voice chain

# Fixed slots so any profile can be loaded live, without restarting PipeWire:
# preamp + 2 low shelves + 2 high shelves + 12 peaking filters, per channel.
# sub1/sub2: a 4th-order (24 dB/oct) Butterworth high-pass — the subsonic filter. Off = parked at 5 Hz (inaudible),
# so switching it never rebuilds the chain or restarts PipeWire.
SUBSONIC_HZ, SUBSONIC_OFF_HZ, SUBSONIC_Q = 30.0, 5.0, (0.541, 1.307)
EQ_SLOTS = [("sub1", "bq_highpass"), ("sub2", "bq_highpass"), ("pre", "bq_lowshelf")] + \
           [(f"ls{i}", "bq_lowshelf") for i in (1, 2)] + \
           [(f"hs{i}", "bq_highshelf") for i in (1, 2)] + [(f"pk{i}", "bq_peaking") for i in range(1, 13)]
EQ_FLAT = {"name": "Off (flat)", "preamp": 0.0, "bands": []}
AUTOEQ_RAW = "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master/results/"
AUTOEQ_INDEX = CONF_DIR / "autoeq-index.md"


def parse_parametric_eq(text, name="Custom"):
    """AutoEQ / Equalizer APO 'ParametricEQ.txt' → profile. Accepts LSC/LS, HSC/HS and PK filters."""
    pre, bands = 0.0, []
    for line in text.splitlines():
        m = re.match(r"\s*Preamp:\s*(-?[\d.]+)\s*dB", line, re.I)
        if m:
            pre = float(m.group(1))
            continue
        m = re.match(r"\s*Filter\s*\d*:\s*ON\s+(LSC|LS|HSC|HS|PK|PEQ)\b.*?Fc\s+([\d.]+)\s*Hz\s+Gain\s+(-?[\d.]+)\s*dB"
                     r"(?:\s+Q\s+([\d.]+))?", line, re.I)
        if m:
            kind = {"LS": "LSC", "HS": "HSC", "PEQ": "PK"}.get(m.group(1).upper(), m.group(1).upper())
            bands.append({"type": kind, "f": float(m.group(2)), "g": float(m.group(3)),
                          "q": float(m.group(4) or 0.707)})
    if not bands and not pre:
        raise ValueError("No filters found — is this a ParametricEQ.txt file?")
    return {"name": name, "preamp": pre, "bands": bands}


def eq_params(profile):
    """Profile → filter-chain control values for every slot (unused slots flat)."""
    vals = {"pre": (20000.0, 0.707, float(profile.get("preamp", 0.0)))}   # shelf below 20 kHz = overall gain
    hz = SUBSONIC_HZ if profile.get("subsonic") else SUBSONIC_OFF_HZ
    vals["sub1"], vals["sub2"] = (hz, SUBSONIC_Q[0], 0.0), (hz, SUBSONIC_Q[1], 0.0)
    slots = {"LSC": [s for s, _ in EQ_SLOTS if s.startswith("ls")], "HSC": [s for s, _ in EQ_SLOTS if s.startswith("hs")],
             "PK": [s for s, _ in EQ_SLOTS if s.startswith("pk")]}
    for b in profile.get("bands", []):
        free = slots.get(b["type"])
        if not free:
            free = slots["PK"]                           # out of shelf slots: approximate with a peak
        if not free:
            break
        vals[free.pop(0)] = (float(b["f"]), max(0.1, float(b["q"])), float(b["g"]))
    for s, _ in EQ_SLOTS:
        vals.setdefault(s, (1000.0, 0.707, 0.0))
    return vals


def band_db(b, f, rate=48000.0):
    """Gain in dB of one EQ band (RBJ biquad, as PipeWire builds it) at frequency f."""
    A, w0 = 10 ** (float(b["g"]) / 40), 2 * math.pi * float(b["f"]) / rate
    alpha, c = math.sin(w0) / (2 * max(0.1, float(b["q"]))), math.cos(w0)
    t = b["type"]
    if t == "HP":
        bb = ((1 + c) / 2, -(1 + c), (1 + c) / 2); aa = (1 + alpha, -2 * c, 1 - alpha)
    elif t == "PK":
        bb = (1 + alpha * A, -2 * c, 1 - alpha * A); aa = (1 + alpha / A, -2 * c, 1 - alpha / A)
    else:
        k2, sgn = 2 * math.sqrt(A) * alpha, (1 if t == "LSC" else -1)
        bb = (A * ((A + 1) - sgn * (A - 1) * c + k2), sgn * 2 * A * ((A - 1) - sgn * (A + 1) * c),
              A * ((A + 1) - sgn * (A - 1) * c - k2))
        aa = ((A + 1) + sgn * (A - 1) * c + k2, -sgn * 2 * ((A - 1) + sgn * (A + 1) * c),
              (A + 1) + sgn * (A - 1) * c - k2)
    z = complex(math.cos(2 * math.pi * f / rate), -math.sin(2 * math.pi * f / rate))
    h = (bb[0] + bb[1] * z + bb[2] * z * z) / (aa[0] + aa[1] * z + aa[2] * z * z)
    return 20 * math.log10(max(abs(h), 1e-9))


def eq_curve(profile, freqs):
    """What a profile does to the sound, in dB at each frequency: bands + overall level + subsonic filter."""
    bands = list(profile.get("bands", []))
    if profile.get("subsonic"):
        bands += [{"type": "HP", "f": SUBSONIC_HZ, "q": q, "g": 0.0} for q in SUBSONIC_Q]
    pre = float(profile.get("preamp", 0.0))
    return [pre + sum(band_db(b, f) for b in bands) for f in freqs]


class EqGraph(QtWidgets.QWidget):
    """The EQ as a picture: the line is how much louder or quieter each pitch comes out than without the EQ.
    A dashed line shows the profile alone, so the Tone sliders' share is visible."""
    FREQS = [20 * 1000 ** (i / 159) for i in range(160)]

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(170)
        self.cur, self.base, self.title = [], [], ""
        self.setToolTip("Above the middle line: that pitch comes out louder. Below: quieter. Left = bass, "
                        "right = treble.")

    def show_profile(self, prof, base=None, title=""):
        self.cur = eq_curve(prof, self.FREQS)
        self.base = eq_curve(base, self.FREQS) if base is not None else []
        self.title = title
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self.rect().adjusted(40, 10, -10, -22)
        pal = self.palette()
        fg, dim = pal.color(QtGui.QPalette.WindowText), pal.color(QtGui.QPalette.Mid)
        vals = self.cur + self.base
        span = max(12.0, min(48.0, math.ceil((max((abs(v) for v in vals), default=0) + 2) / 6) * 6))
        x = lambda f: r.left() + r.width() * math.log10(f / 20) / 3                    # noqa: E731
        y = lambda db: r.center().y() - r.height() / 2 * max(-1.0, min(1.0, db / span))  # noqa: E731
        p.setPen(QtGui.QPen(dim, 1))
        small = p.font()
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.8))
        p.setFont(small)
        for f, lab in ((50, "50"), (100, "100"), (500, "500"), (1000, "1k"), (5000, "5k"), (10000, "10k")):
            p.drawLine(QtCore.QPointF(x(f), r.top()), QtCore.QPointF(x(f), r.bottom()))
            p.drawText(QtCore.QRectF(x(f) - 20, r.bottom() + 3, 40, 16), Qt.AlignCenter, lab)
        for db in (-span, -span / 2, 0, span / 2, span):
            p.setPen(QtGui.QPen(fg if db == 0 else dim, 1.4 if db == 0 else 1))
            p.drawLine(QtCore.QPointF(r.left(), y(db)), QtCore.QPointF(r.right(), y(db)))
            p.drawText(QtCore.QRectF(0, y(db) - 8, 36, 16), Qt.AlignRight | Qt.AlignVCenter, f"{db:+.0f}")
        p.drawText(QtCore.QRectF(r.left(), r.bottom() + 3, 60, 16), Qt.AlignLeft, "bass")
        p.drawText(QtCore.QRectF(r.right() - 60, r.bottom() + 3, 60, 16), Qt.AlignRight, "treble")

        def line(pts, pen):
            if pts:
                path = QtGui.QPainterPath(QtCore.QPointF(x(self.FREQS[0]), y(pts[0])))
                for f, v in zip(self.FREQS[1:], pts[1:]):
                    path.lineTo(x(f), y(v))
                p.setPen(pen)
                p.drawPath(path)
        accent = pal.color(QtGui.QPalette.Highlight)
        line(self.base, QtGui.QPen(dim, 1.5, Qt.DashLine))
        line(self.cur, QtGui.QPen(accent, 2.5))
        if self.title:
            p.setPen(fg)
            p.drawText(QtCore.QRectF(r.left() + 6, r.top(), r.width() - 12, 18), Qt.AlignRight, self.title)
        p.end()


def eq_peak_db(bands):
    """Highest point of the combined curve, 20 Hz–20 kHz: how much the level must drop so nothing clips."""
    if not bands:
        return 0.0
    freqs = [20 * 1000 ** (i / 199) for i in range(200)]
    return max(0.0, max(sum(band_db(b, f) for b in bands) for f in freqs))


def eq_pw_params(profile, per_call=5, only=None):
    """→ pw-cli parameter lists, a few slots each. pw-cli silently drops lists longer than ~500 characters
    (and still says OK), so a whole profile is sent in small pieces. `only`: slots the running chain has."""
    items = []
    for slot, (f, q, g) in eq_params(profile).items():
        if only is not None and slot not in only:
            continue
        items.append(f'"{slot}:Freq" {f:.2f} "{slot}:Q" {q:.3f} "{slot}:Gain" {g:.2f}')
    return ["[ " + " ".join(items[i:i + per_call]) + " ]" for i in range(0, len(items), per_call)]


def eq_chain_conf(desc, sink_node, target, positions, profile=None):
    """A filter-chain that replaces the plain main-output loopback: same node names, same channels."""
    vals = eq_params(profile or EQ_FLAT)
    nodes = "\n".join(
        f'          {{ type = builtin name = {s} label = {lbl} control = {{ "Freq" = {vals[s][0]:.2f} '
        f'"Q" = {vals[s][1]:.3f} "Gain" = {vals[s][2]:.2f} }} }}' for s, lbl in EQ_SLOTS)
    links = "\n".join(f'          {{ output = "{a}:Out" input = "{b}:In" }}'
                      for (a, _), (b, _) in zip(EQ_SLOTS, EQ_SLOTS[1:]))
    return f"""# Written by Mbox Control: main output with EQ (live-adjustable, flat = no change)
context.modules = [
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "{desc}"
      media.name = "{desc}"
      filter.graph = {{
        nodes = [
{nodes}
        ]
        links = [
{links}
        ]
        inputs = [ "{EQ_SLOTS[0][0]}:In" ]
        outputs = [ "{EQ_SLOTS[-1][0]}:Out" ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.name = "{sink_node}"
        media.class = "Audio/Sink"
        audio.position = [ FL FR ]
      }}
      playback.props = {{
        node.name = "{sink_node}_out"
        target.object = "{target}"
        audio.position = [ {' '.join(positions)} ]
        stream.dont-remix = true
        node.passive = true
      }}
    }}
  }}
]
"""


def pw_node_id(name):
    for o in json.loads(run_quiet("pw-dump", timeout=4) or "[]"):
        if o.get("type") == "PipeWire:Interface:Node" and \
                o.get("info", {}).get("props", {}).get("node.name") == name:
            return o["id"]
    return None


def set_eq_live(sink_node, profile):
    """Load a profile into the running chain. → True if PipeWire took it."""
    nid = sink_node if pw_node_exists(sink_node, "sink") else pw_node_id(sink_node)
    if nid is None:
        return False
    ok = True
    try:                                                 # a chain written by an older version lacks some slots
        text = MAIN_CONF.read_text() if sink_node == MAIN_SINK else ""
        only = {s for s, _ in EQ_SLOTS if f"name = {s} " in text} or None
    except OSError:
        only = None
    for chunk in eq_pw_params(profile, only=only):
        out = run_quiet("pw-cli", "set-param", str(nid), "Props", f"{{ params = {chunk} }}")
        ok = ok and out is not None and "size 8," not in out          # "size 8" = pw-cli dropped it
    return ok


class AutoEqSearch(QtCore.QThread):
    """Downloads AutoEQ's index once (cached), then answers searches; and fetches a chosen profile."""
    listed = QtCore.Signal(list, str)            # [(name, source, path)], error
    fetched = QtCore.Signal(dict, str)           # profile, error

    def __init__(self, query="", path=None, name=""):
        super().__init__()
        self.query, self.path, self.name = query, path, name

    @staticmethod
    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": f"MboxControl/{VERSION}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")

    def run(self):
        try:
            if self.path:
                url = autoeq_url(self.path, self.name)
                self.fetched.emit(parse_parametric_eq(self._get(url), self.name), "")
                return
            if not AUTOEQ_INDEX.exists() or time.time() - AUTOEQ_INDEX.stat().st_mtime > 30 * 86400:
                CONF_DIR.mkdir(parents=True, exist_ok=True)
                AUTOEQ_INDEX.write_text(self._get(AUTOEQ_RAW + "INDEX.md"))
            self.listed.emit(autoeq_search(AUTOEQ_INDEX.read_text(), self.query), "")
        except Exception as e:  # noqa: BLE001
            (self.fetched if self.path else self.listed).emit({} if self.path else [], str(e))


def autoeq_url(path, name):
    """INDEX.md path (already URL-encoded, ends in the model's folder) → its ParametricEQ.txt."""
    return AUTOEQ_RAW + path.strip("/") + "/" + urllib.parse.quote(name) + "%20ParametricEQ.txt"


def autoeq_search(index_text, query, limit=40):
    """INDEX.md lines '- [Name](./source/rig/Name) by source on rig' → best matches for the query."""
    words = [w for w in query.lower().split() if w]
    out = []
    for m in re.finditer(r"^- \[(.+?)\]\(\./(.+?)\) by (.+)$", index_text, re.M):
        name, path, by = m.group(1), m.group(2), m.group(3)
        if all(w in name.lower() for w in words):
            out.append((name, by, path))
    pref = {"oratory1990": 0, "crinacle": 1, "Rtings": 2}
    out.sort(key=lambda r: (not r[0].lower().startswith(query.lower()), pref.get(r[1].split()[0], 9), r[0]))
    return out[:limit]


# ---- voice chain (mic) -------------------------------------------------------------

LADSPA_DIRS = [Path(p) for p in os.environ.get("LADSPA_PATH", "").split(":") if p] + \
    [Path("/usr/lib/ladspa"), Path("/usr/lib64/ladspa"), Path("/usr/local/lib/ladspa"),
     Path("/usr/lib/x86_64-linux-gnu/ladspa")]


def find_ladspa(*names):
    for d in LADSPA_DIRS:
        for n in names:
            if (d / n).exists():
                return str(d / n)
    return None


def voice_chain_conf(source, aux, desc="Mic — clean (Mbox)", denoise=True, compress=True):
    """Mic → low-cut → (noise suppression) → (gentle compressor) → a new input apps can pick."""
    rnn = find_ladspa("librnnoise_ladspa.so") if denoise else None
    sc4 = find_ladspa("sc4m_1916.so") if compress else None
    nodes = ['          { type = builtin name = lowcut label = bq_highpass control = { "Freq" = 80.0 "Q" = 0.707 } }']
    chain = ["lowcut"]
    if rnn:
        nodes.append(f'          {{ type = ladspa name = denoise plugin = "{rnn}" label = noise_suppressor_mono '
                     '}')
        chain.append("denoise")
    if sc4:
        nodes.append(f'          {{ type = ladspa name = comp plugin = "{sc4}" label = sc4m control = {{ '
                     '"Attack time (ms)" = 10 "Release time (ms)" = 200 "Threshold level (dB)" = -20 '
                     '"Ratio (1:n)" = 3 "Knee radius (dB)" = 6 "Makeup gain (dB)" = 4 } }')
        chain.append("comp")
    ports = {"lowcut": ("In", "Out"), "denoise": ("Input", "Output"), "comp": ("Input", "Output")}
    links = "\n".join(f'          {{ output = "{a}:{ports[a][1]}" input = "{b}:{ports[b][0]}" }}'
                      for a, b in zip(chain, chain[1:]))
    return f"""# Written by Mbox Control: a cleaned-up mic input
context.modules = [
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "{desc}"
      media.name = "{desc}"
      filter.graph = {{
        nodes = [
{chr(10).join(nodes)}
        ]
        links = [
{links}
        ]
        inputs = [ "{chain[0]}:{ports[chain[0]][0]}" ]
        outputs = [ "{chain[-1]}:{ports[chain[-1]][1]}" ]
      }}
      audio.channels = 1
      audio.position = [ MONO ]
      capture.props = {{
        node.name = "mbox_voice_cap"
        target.object = "{source}"
        audio.position = [ {aux} ]
        stream.dont-remix = true
        node.passive = true
      }}
      playback.props = {{
        node.name = "mbox_voice"
        media.class = "Audio/Source"
        audio.position = [ MONO ]
      }}
    }}
  }}
]
""", bool(rnn), bool(sc4)

# ================================================================== 4.16: retro-record, multitrack, metronome, level
import collections

def capture_auxes(auxes):
    """Channels to ask pw-record for. A lone "AUX0" is read as a *layout* ("0 aux channels") by newer
    PipeWire (1.4+), so one channel is always captured as a pair; callers keep the first."""
    auxes = list(auxes)
    if len(auxes) == 1:
        n = int(auxes[0][3:])
        auxes.append(f"AUX{n ^ 1}")
    return auxes


_PW_RAW = None


def pw_record_raw_flag():
    """Newer pw-record (1.4+) wraps stdout in an AU container (big-endian, with a header) unless asked
    for --raw; older ones write raw already and don't know the flag."""
    global _PW_RAW
    if _PW_RAW is None:
        try:
            r = subprocess.run(["pw-record", "--help"], capture_output=True, text=True, timeout=5)
            _PW_RAW = "--raw" in (r.stdout + r.stderr)
        except (OSError, subprocess.SubprocessError):
            _PW_RAW = False
    return ["--raw"] if _PW_RAW else []


def play_stdin(cmd):
    """pw-play reading raw samples from a pipe. Newer pw-play (1.4+) expects a sound *file* on stdin unless
    told --raw, rejects our samples and quits — silently, if nobody looks. stderr is kept for the error."""
    return subprocess.Popen(cmd[:1] + pw_record_raw_flag() + cmd[1:] + ["-"], stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE)


def player_error(proc):
    try:
        proc.wait(1)
        return ((proc.stderr.read() or b"").decode(errors="ignore").strip().splitlines() or ["pw-play stopped"])[0]
    except Exception:  # noqa: BLE001
        return "pw-play stopped"


def capture_cmd(source, auxes, rate, out="-", fmt="s16"):
    """pw-record taking exact Mbox capture channels (AUX0…) with no remixing."""
    auxes = capture_auxes(auxes)
    return ["pw-record", *(pw_record_raw_flag() if out == "-" else []), "--target", source, "--rate", str(rate), "--channels", str(len(auxes)),
            "--channel-map", ",".join(auxes), "-P", "stream.dont-remix=true", "-P", "node.dont-reconnect=true",
            "--format", fmt, out]


class RetroBuffer(QtCore.QThread):
    """Keeps the last N seconds of an input in memory — nothing touches the disk until you save."""
    failed = QtCore.Signal(str)

    def __init__(self, source, auxes, rate=48000, seconds=120):
        super().__init__()
        self.source, self.auxes, self.rate, self.seconds = source, list(auxes), rate, seconds
        self.frame = 2 * len(self.auxes)                     # s16
        self.limit = self.seconds * self.rate * self.frame
        self.chunks, self.size = collections.deque(), 0
        self.lock = threading.Lock()
        self.proc = None
        self.stopping = False

    def run(self):
        import array
        wide = len(capture_auxes(self.auxes))               # a lone channel arrives as a pair
        try:
            self.proc = subprocess.Popen(capture_cmd(self.source, self.auxes, self.rate), stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        except OSError as e:
            self.failed.emit(str(e))
            return
        while not self.stopping:
            data = self.proc.stdout.read(self.rate // 10 * 2 * wide)      # ~0.1 s
            if not data:
                break
            if wide != len(self.auxes):
                a = array.array("h")
                a.frombytes(data[:len(data) // (2 * wide) * 2 * wide])
                data = a[0::wide].tobytes()
            with self.lock:
                self.chunks.append(data)
                self.size += len(data)
                while self.size - len(self.chunks[0]) >= self.limit:
                    self.size -= len(self.chunks.popleft())
        if not self.stopping:
            err = ""
            try:
                self.proc.wait(2)
                err = (self.proc.stderr.read() or b"").decode(errors="ignore").strip().splitlines()
                err = err[0] if err else ""
            except Exception:  # noqa: BLE001
                pass
            self.failed.emit(f"pw-record stopped: {err}" if err else "The input went away")

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.wait(2000)

    def held(self):
        return self.size / (self.rate * self.frame)

    def snapshot(self, seconds):
        """The last `seconds` of audio as raw s16 bytes (whole frames)."""
        with self.lock:
            data = b"".join(self.chunks)
        want = int(seconds * self.rate) * self.frame
        return data[-want:] if want < len(data) else data


def save_raw(data, path, rate, channels, trim=True):
    """Raw s16 → FLAC; trims silence off the start so the take begins when you started playing."""
    af = ["-af", "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3"] if trim else []
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(rate),
                        "-ac", str(channels), "-i", "-", *af, "-c:a", "flac", str(path)], input=data,
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="ignore")[-200:] or "ffmpeg failed")


def split_multitrack(src, names, folder):
    """One multichannel recording → one mono FLAC per input, sample-aligned."""
    folder.mkdir(parents=True, exist_ok=True)
    outs = []
    maps = []
    for i, name in enumerate(names):
        out = folder / f"{i + 1:02d} {safe_name(name)}.flac"
        maps += ["-map_channel", f"0.0.{i}", "-c:a", "flac", str(out)]
        outs.append(out)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *maps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # newer ffmpeg dropped -map_channel: use the pan filter instead
        for i, out in enumerate(outs):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-af",
                                f"pan=mono|c0=c{i}", "-c:a", "flac", str(out)], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-200:])
    return outs


# Time signatures → (clicks per bar as accent levels, clicks per tempo beat).
# Levels: 3 = the one, 2 = a group start, 1 = a beat, 0 = a soft subdivision tick.
# x/4: the tempo is the quarter note. 6/8, 9/8, 12/8: the tempo is the dotted quarter (how you feel a jig), with
# the eighths ticking softly. 5/8, 7/8: the tempo is the eighth note, grouped 3+2 and 2+2+3.
METERS = {
    "2/4": ([3, 1], 1), "3/4": ([3, 1, 1], 1), "4/4": ([3, 1, 1, 1], 1), "5/4": ([3, 1, 1, 1, 1], 1),
    "6/8": ([3, 0, 0, 2, 0, 0], 3), "9/8": ([3, 0, 0, 2, 0, 0, 2, 0, 0], 3),
    "12/8": ([3, 0, 0, 2, 0, 0, 2, 0, 0, 2, 0, 0], 3),
    "5/8": ([3, 1, 1, 2, 1], 1), "7/8": ([3, 1, 2, 1, 2, 1, 1], 1),
    "none": ([1], 1),
}
METER_LABELS = [("4/4", "4/4"), ("3/4", "3/4 (waltz)"), ("2/4", "2/4 (march, polka)"), ("5/4", "5/4"),
                ("6/8", "6/8 (jig) — tempo = dotted quarter"), ("9/8", "9/8 (slip jig) — tempo = dotted quarter"),
                ("12/8", "12/8 (slow blues) — tempo = dotted quarter"), ("5/8", "5/8 (3+2) — tempo = eighth note"),
                ("7/8", "7/8 (2+2+3) — tempo = eighth note"), ("none", "No accent")]


class Metronome(QtCore.QThread):
    """Clicks streamed live to an output; tempo, time signature and volume change on the fly."""
    failed = QtCore.Signal(str)

    def __init__(self, target, bpm=100, meter="4/4", volume=0.6, rate=48000):
        super().__init__()
        self.target, self.bpm, self.meter, self.volume, self.rate = target, bpm, meter, volume, rate
        self.stopping = False
        self.proc = None
        self.beat_no = 0

    def click(self, level):
        import numpy as np
        n = int(self.rate * 0.03)
        t = np.arange(n) / self.rate
        f, amp = {3: (1760.0, 1.0), 2: (1480.0, 0.85), 1: (1175.0, 0.7), 0: (1175.0, 0.32)}[level]
        return (np.sin(2 * np.pi * f * t) * np.exp(-t * 180) * amp).astype(np.float32)

    def run(self):
        import numpy as np
        cmd = ["pw-play", "--rate", str(self.rate), "--channels", "1", "--format", "f32", "-P",
               "media.role=Production", "-P", "media.name=Metronome"]
        if self.target:
            cmd += ["--target", self.target]
        try:
            self.proc = play_stdin(cmd)
        except OSError as e:
            self.failed.emit(str(e))
            return
        clicks = {lvl: self.click(lvl) for lvl in (0, 1, 2, 3)}
        block = int(self.rate * 0.02)                       # write 20 ms at a time: tempo changes land fast
        pos, next_click = 0, 0
        pending = np.zeros(0, np.float32)                   # the end of a click that crossed a block edge
        start = time.time()
        while not self.stopping and self.proc.poll() is None:
            buf = np.zeros(block, np.float32)
            k = min(len(pending), block)
            buf[:k] += pending[:k]
            pending = pending[k:]
            pattern, per_beat = METERS.get(self.meter, METERS["4/4"])
            period = max(1, int(self.rate * 60.0 / max(20, min(300, self.bpm)) / per_beat))
            while next_click < pos + block:
                c = clicks[pattern[self.beat_no % len(pattern)]]
                off = next_click - pos
                seg = c[:block - off]
                buf[off:off + len(seg)] += seg
                pending = c[len(seg):]
                self.beat_no += 1
                next_click += period
            try:
                self.proc.stdin.write((buf * self.volume).tobytes())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            pos += block
            ahead = pos / self.rate - (time.time() - start)
            if ahead > 0.15:                                # stay ~150 ms ahead of the speakers, no more
                time.sleep(ahead - 0.15)
        if not self.stopping:
            self.failed.emit(player_error(self.proc))
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def stop(self):
        self.stopping = True
        self.wait(1500)


def level_advice(peak_db, clips):
    """What to do with the gain knob, from the loudest moment of a 10-second check."""
    if clips or peak_db > -1:
        return "too_hot", ("Too loud — it clipped. Turn the gain knob down a good bit (about a third of a turn) "
                           "and check again.")
    if peak_db > -6:
        return "hot", f"A little hot (loudest {peak_db:.0f} dB). Turn the gain down slightly — a small nudge."
    if peak_db >= -18:
        return "good", f"Good level (loudest {peak_db:.0f} dB). Leave the knob where it is."
    if peak_db >= -30:
        need = -12 - peak_db
        return "low", (f"Quiet (loudest {peak_db:.0f} dB). Turn the gain up — roughly {need:.0f} dB, about "
                       f"{'a quarter' if need < 12 else 'a third'} of a turn — and check again.")
    return "silent", ("Almost nothing is coming in. Check the cable, the Mic/DI button, +48V for condenser "
                      "mics, and that you're playing during the check.")

# ================================================================== 4.17: practice player

_RUBBERBAND = None


def has_rubberband():
    global _RUBBERBAND
    if _RUBBERBAND is None:
        out = run_quiet("ffmpeg", "-hide_banner", "-filters", timeout=6) or ""
        _RUBBERBAND = " rubberband " in out
    return _RUBBERBAND


def practice_filter(speed, semitones, rate=48000):
    """Slow down / speed up without changing pitch, and/or transpose without changing speed."""
    ratio = 2 ** (semitones / 12)
    if abs(speed - 1) < 1e-3 and not semitones:
        return ""
    if has_rubberband():
        return f"rubberband=tempo={speed:.4f}:pitch={ratio:.6f}:transients=crisp:pitchq=quality"
    parts = []
    if semitones:                                       # pitch by resampling, then undo the speed change
        parts.append(f"asetrate={rate * ratio:.2f},aresample={rate}")
    tempo = speed / ratio if semitones else speed
    while tempo < 0.5:                                  # atempo works in 0.5–2.0 steps
        parts.append("atempo=0.5")
        tempo /= 0.5
    while tempo > 2.0:
        parts.append("atempo=2.0")
        tempo /= 2.0
    parts.append(f"atempo={tempo:.5f}")
    return ",".join(parts)


NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_MAJ = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]     # Krumhansl–Kessler
_MIN = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def detect_key(x, rate):
    """→ (tonic 0–11, 'major'/'minor', confidence 0–1) from a mono recording."""
    import numpy as np
    n = 8192
    if len(x) < n * 4:
        return None
    win = np.hanning(n).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1 / rate)
    ok = (freqs > 55) & (freqs < 2000)
    pc = np.round(12 * np.log2(freqs[ok] / 440.0)).astype(int) % 12        # pitch class of each bin (A = 0)
    chroma = np.zeros(12)
    starts = np.arange(0, len(x) - n, n // 2)
    if len(starts) > 1200:
        starts = starts[np.linspace(0, len(starts) - 1, 1200).astype(int)]
    for s in starts:
        mag = np.abs(np.fft.rfft(x[s:s + n] * win))[ok]
        np.add.at(chroma, pc, mag ** 2)
    chroma = np.roll(chroma, 9)                        # A-based → C-based
    best = None
    for mode, prof in (("major", _MAJ), ("minor", _MIN)):
        for t in range(12):
            r = np.corrcoef(chroma, np.roll(prof, t))[0, 1]
            if best is None or r > best[2]:
                best = (t, mode, r)
    return best[0], best[1], float(max(0.0, best[2]))


def shift_to_key(tonic, mode, instrument_key):
    """Semitones (−6…+5) that move a song into a key an instrument plays in.
    Minor songs go to the instrument's relative minor (a D whistle plays B minor)."""
    target = NOTE_NAMES.index(instrument_key)
    if mode == "minor":
        target = (target + 9) % 12
    d = (target - tonic) % 12
    return d - 12 if d > 6 else d


class PracticeRender(QtCore.QThread):
    """Renders the section you're practising (slowed / transposed) into memory, as fast as ffmpeg can."""
    ready = QtCore.Signal(object, str)                 # bytes (s16 stereo), error

    def __init__(self, path, start, end, speed, semitones, rate=48000):
        super().__init__()
        self.path, self.start_s, self.end_s = path, start, end
        self.speed, self.semis, self.rate = speed, semitones, rate

    def run(self):
        af = practice_filter(self.speed, self.semis, self.rate)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{self.start_s:.3f}"]
        if self.end_s:
            cmd += ["-t", f"{max(0.05, self.end_s - self.start_s):.3f}"]
        cmd += ["-i", str(self.path), *(["-af", af] if af else []), "-ac", "2", "-ar", str(self.rate),
                "-f", "s16le", "-"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not r.stdout:
            self.ready.emit(None, r.stderr.decode(errors="ignore")[-200:] or "Couldn't read the file")
            return
        self.ready.emit(r.stdout, "")


class PracticePlayer(QtCore.QThread):
    """Plays rendered audio to an output, looping if asked. Reports where it is (in source seconds)."""
    position = QtCore.Signal(float)
    looped = QtCore.Signal()
    finished_play = QtCore.Signal()
    failed = QtCore.Signal(str)

    def __init__(self, data, rate, target, loop, src_start, speed):
        super().__init__()
        self.data, self.rate, self.target, self.loop = data, rate, target, loop
        self.src_start, self.speed = src_start, speed
        self.stopping = False
        self.offset = 0
        self.next_data = None                           # a faster version waiting for the next time round

    def run(self):
        cmd = ["pw-play", "--rate", str(self.rate), "--channels", "2", "--format", "s16", "-P",
               "media.name=Practice", "-P", "media.role=Music"]
        if self.target:
            cmd += ["--target", self.target]
        try:
            proc = play_stdin(cmd)
        except OSError as e:
            self.failed.emit(str(e))
            return
        self.proc = proc
        frame = 4
        block = self.rate // 50 * frame                 # 20 ms
        start = time.time()
        sent = 0
        while not self.stopping:
            if self.offset >= len(self.data):
                if not self.loop:
                    break
                if self.next_data is not None:
                    self.data, self.next_data = self.next_data, None
                self.offset = 0
                self.looped.emit()
            chunk = self.data[self.offset:self.offset + block]
            try:
                proc.stdin.write(chunk)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            self.offset += len(chunk)
            sent += len(chunk)
            self.position.emit(self.src_start + (self.offset / frame / self.rate) * self.speed)
            ahead = sent / frame / self.rate - (time.time() - start)
            if ahead > 0.2:
                time.sleep(ahead - 0.2)
        try:
            proc.stdin.close()
        except OSError:
            pass
        if self.stopping:
            proc.terminate()
        else:
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.returncode not in (0, None, -15):
                self.failed.emit(player_error(proc))
            else:
                self.finished_play.emit()

    def stop(self):
        self.stopping = True
        self.wait(1500)


class PracticeStream(QtCore.QThread):
    """Whole-song playback: ffmpeg streams processed audio straight into pw-play (starts instantly)."""
    position = QtCore.Signal(float)
    finished_play = QtCore.Signal()
    failed = QtCore.Signal(str)

    def __init__(self, path, start, speed, semitones, rate, target):
        super().__init__()
        self.path, self.start_s, self.speed, self.semis = path, start, speed, semitones
        self.rate, self.target = rate, target
        self.stopping = False

    def run(self):
        af = practice_filter(self.speed, self.semis, self.rate)
        try:
            dec = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{self.start_s:.3f}",
                                    "-i", str(self.path), *(["-af", af] if af else []), "-ac", "2", "-ar",
                                    str(self.rate), "-f", "s16le", "-"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
            cmd = ["pw-play", "--rate", str(self.rate), "--channels", "2", "--format", "s16", "-P",
                   "media.name=Practice", "-P", "media.role=Music"] + (["--target", self.target] if self.target else [])
            out = play_stdin(cmd)
        except OSError as e:
            self.failed.emit(str(e))
            return
        self.proc = out
        block = self.rate // 50 * 4
        start, sent = time.time(), 0
        while not self.stopping:
            chunk = dec.stdout.read(block)
            if not chunk:
                break
            try:
                out.stdin.write(chunk)
                out.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            sent += len(chunk)
            self.position.emit(self.start_s + sent / 4 / self.rate * self.speed)
            ahead = sent / 4 / self.rate - (time.time() - start)
            if ahead > 0.2:
                time.sleep(ahead - 0.2)
        dec.kill()
        try:
            out.stdin.close()
        except OSError:
            pass
        if self.stopping:
            out.terminate()
        else:
            try:
                out.wait(3)
            except subprocess.TimeoutExpired:
                out.kill()
            if out.returncode not in (0, None, -15):
                self.failed.emit(player_error(out))
            else:
                self.finished_play.emit()

    def stop(self):
        self.stopping = True
        self.wait(1500)


class KeyJob(QtCore.QThread):
    done = QtCore.Signal(object)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            import numpy as np
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(self.path), "-t", "240",
                                "-ac", "1", "-ar", "22050", "-f", "f32le", "-"], capture_output=True)
            self.done.emit(detect_key(np.frombuffer(r.stdout, np.float32), 22050))
        except Exception:  # noqa: BLE001
            self.done.emit(None)


class PracticeWave(WaveformView):
    """Waveform for practising: click to jump, drag to choose the bit to loop."""
    seek = QtCore.Signal(float)
    loopChosen = QtCore.Signal(float, float)

    def __init__(self):
        super().__init__()
        self.empty_text = "Open a song to practise with"
        self.loop_ab = None
        self.play_t = None
        self.press_x = None

    def mousePressEvent(self, e):
        if self.dur and e.button() == Qt.LeftButton:
            self.press_x = e.position().x()

    def mouseMoveEvent(self, e):
        if self.press_x is not None and abs(e.position().x() - self.press_x) > 6:
            a, b = sorted((self._t(self.press_x), self._t(e.position().x())))
            self.loop_ab = (max(0.0, a), min(self.dur, b))
            self.update()

    def mouseDoubleClickEvent(self, e):
        pass

    def mouseReleaseEvent(self, e):
        if self.press_x is None:
            return
        if abs(e.position().x() - self.press_x) > 6 and self.loop_ab:
            self.loopChosen.emit(*self.loop_ab)
        else:
            self.seek.emit(self._t(e.position().x()))
        self.press_x = None

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self.dur:
            return
        p = QtGui.QPainter(self)
        if self.loop_ab:
            x1, x2 = self._x(self.loop_ab[0]), self._x(self.loop_ab[1])
            c = QtGui.QColor(ACCENT)
            c.setAlpha(55)
            p.fillRect(QRectF(x1, 0, x2 - x1, self.height()), c)
            p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), 2))
            for x, t in ((x1, "A"), (x2, "B")):
                p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
                p.drawText(QPointF(x + 4, 14), t)
        if self.play_t is not None:
            p.setPen(QtGui.QPen(QtGui.QColor("#3fb950"), 2))
            x = self._x(self.play_t)
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))

# ================================================================== 4.18: auto scenes, mixer history, compact mixer

MIX_HISTORY_NAMES = ("mixer-source-gain", "mixer-source", "output-source")


def running_programs():
    """Names of programs running right now (from /proc, no subprocess)."""
    names = set()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            names.add((d / "comm").read_text().strip().lower())
            exe = os.readlink(d / "exe")
            names.add(Path(exe).name.lower())
        except OSError:
            continue
    return names


AUTO_SCENE_HINTS = [("obs", "OBS Studio"), ("guitarix", "Guitarix"), ("audacity", "Audacity"),
                    ("ardour", "Ardour"), ("reaper", "REAPER"), ("bitwig-studio", "Bitwig Studio"),
                    ("discord", "Discord"), ("zoom", "Zoom"), ("steam", "Steam"), ("mixxx", "Mixxx"),
                    ("kdenlive", "Kdenlive"), ("vlc", "VLC"), ("spotify", "Spotify")]


class CompactMixer(QtWidgets.QWidget):
    """A small always-on-top mixer for one output's mix: name, level, meter and mute per source."""

    def __init__(self, win):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.win = win
        self.setWindowTitle("Mbox mixer")
        self.setWindowIcon(app_icon())
        self.resize(360, 420)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        top = QtWidgets.QHBoxLayout()
        self.out = QtWidgets.QComboBox()
        for name, first, _ in OUTPUT_PAIRS:
            own = win.label(f"out{first}", "")
            self.out.addItem(own or name, first)
        self.out.currentIndexChanged.connect(lambda _: self.rebuild())
        top.addWidget(self.out, 1)
        self.only = QtWidgets.QCheckBox("In use")
        self.only.setChecked(True)
        self.only.toggled.connect(lambda _: self.rebuild())
        top.addWidget(self.only)
        v.addLayout(top)
        self.rows_w = QtWidgets.QWidget()
        self.rows = QtWidgets.QGridLayout(self.rows_w)
        self.rows.setColumnStretch(2, 1)
        self.rows.setVerticalSpacing(6)
        sc = QtWidgets.QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QtWidgets.QFrame.NoFrame)
        sc.setWidget(self.rows_w)
        v.addWidget(sc, 1)
        self.note = muted("")
        v.addWidget(self.note)
        self.items = []
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(120)
        self.sig = None
        self.rebuild()

    def mix(self):
        kind, k, _ = self.win.pair_source(self.out.currentData())
        return k if kind == "mix" else None

    def rebuild(self):
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        self.items = []
        k = self.mix()
        if k is None:
            self.note.setText("This output isn't playing a Stereo Mix — pick another, or set it on the Mixer page.")
            return
        self.note.setText(f"Stereo Mix {k}. Changes here are the same as on the Mixer page.")
        w = self.win
        r = 0
        for item in [f"Analog-A-{n}" for n in range(1, 7)] + ["S/PDIF-1", "S/PDIF-2"] + \
                [f"Stream-{n}" for n in range(1, 9)] + ["Reverb-1", "Reverb-2"]:
            slot = w.slot_of(item)
            if slot is None:
                continue
            l_, r_ = w.mix_lr(k, slot)
            if self.only.isChecked() and not (l_ or r_) and f"{k}:{item}" not in w.app_state.get("mutes", {}):
                continue
            col = w.item_colour(item)
            dot = QtWidgets.QLabel("●")
            dot.setStyleSheet(f"color:{col or ink(70).name()}; font-size:14pt;")
            self.rows.addWidget(dot, r, 0)
            name = QtWidgets.QLabel(w.item_title(item))
            name.setMinimumWidth(90)
            self.rows.addWidget(name, r, 1)
            sl = QtWidgets.QSlider(Qt.Horizontal)
            sl.setRange(-60, 6)
            g = math.hypot(l_, r_)
            sl.setValue(max(-60, round(20 * math.log10(g / UNITY))) if g else -60)
            sl.setToolTip("dB — far left is off")
            sl.valueChanged.connect(lambda v, it=item: self.set_gain(it, v))
            self.rows.addWidget(sl, r, 2)
            bar = QtWidgets.QProgressBar()
            bar.setTextVisible(False)
            bar.setFixedSize(50, 8)
            self.rows.addWidget(bar, r, 3)
            mute = QtWidgets.QToolButton()
            mute.setText("M")
            mute.setCheckable(True)
            mute.setChecked(bool(w.app_state.get("mutes", {}).get(f"{k}:{item}")))
            mute.toggled.connect(lambda on, it=item: self.win.set_strip_mute(self.mix(), it, on))
            self.rows.addWidget(mute, r, 4)
            self.items.append((item, sl, bar))
            r += 1
        self.sig = self.state_sig()

    def state_sig(self):
        k = self.mix()
        return (k, tuple(self.win.mix_lr(k, self.win.slot_of(i)) for i, _, _ in self.items) if k else ())

    def set_gain(self, item, db):
        k = self.mix()
        slot = self.win.slot_of(item)
        if k is None or slot is None:
            return
        g = 0 if db <= -60 else int(UNITY * 10 ** (db / 20))
        pan = self.win.app_state.get("pans", {}).get(f"{k}:{item}", 0)
        self.win.mix_set_lr(k, slot, *from_pan(g, pan))
        self.sig = self.state_sig()

    def tick(self):
        if not self.isVisible():
            return
        for item, _, bar in self.items:
            db = self.win.item_db(item) if item.startswith(("Analog", "S/PDIF")) else \
                meter_db(self.win.input_level(item))
            bar.setValue(int(max(0, min(100, (db + 60) / 60 * 100))))
        if self.state_sig() != self.sig:          # changed elsewhere (Mixer page, a scene, MIDI)
            self.rebuild()

# ================================================================== main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, backend):
        super().__init__()
        self.backend = backend
        self.card_no = backend.card
        self.ctrls, self.by_name = {}, {}
        self.refreshers, self.meters = [], []
        self.labels = load_json(LABELS_FILE, {})
        self.first_run = not STATE_FILE.exists()
        self.app_state = load_json(STATE_FILE, {})
        for k, v in (("mutes", {}), ("solo", {}), ("links", {}), ("pans", {}), ("boost", False),
                     ("cr", {"A": 0, "B": None, "C": None}), ("cr_enabled", False),
                     ("cr_aux_hold", True), ("cr_saved", {}), ("rip_dir", str(HOME / "Music" / "Rips")),
                     ("spk_mute", {}), ("startup_scene", ""), ("only_used", True), ("close_to_tray", True),
                     ("tray_hint", False), ("last_spk", ""), ("midi", {"port": "", "on": False, "map": {}}),
                     ("discogs_token", ""), ("setup", dict(SETUP_DEFAULT)), ("library_dir", ""),
                     ("library_cmd", ""), ("cleanup", []), ("discogs_mark", False), ("effects_app", ""),
                     ("replaygain", True), ("acoustid_key", ""), ("queue", []), ("queue_cur", -1),
                     ("theme", "system"), ("accent", "system")):
            self.app_state.setdefault(k, v)
        self.migrate_input_names()
        restart, was_default = migrate_main_output(self.app_state)
        self.save_app_state()
        if restart and not os.environ.get("MBOX_AMIXER"):
            restart_pipewire()
            if was_default:
                QtCore.QTimer.singleShot(1500, lambda: set_default_when_ready(MAIN_SINK))
        sets = self.app_state.get("eq_sets", {})
        if "H" not in sets and sets.get("B", "Off (flat)") != "Off (flat)":
            sets["H"] = sets["B"]                       # before 4.23 the AutoEQ dialog put headphones on B
        install_wheel_guard()
        if "take_restore" in self.app_state:            # the app closed during a quick take: undo its borrowing
            self.app_state["rec_src"] = self.app_state.pop("take_restore") or None
        self.spectrum_hub = SpectrumHub(self)
        self.asleep, self.recovering, self.woke_at, self.recover_offered = False, False, 0.0, False
        self.sleep_watch = SleepWatch(self)
        self.sleep_watch.sleeping.connect(self.on_sleep)
        self.udev = None
        if UdevWatch.available() and not os.environ.get("MBOX_AMIXER"):
            self.udev = UdevWatch()
            self.udev.changed.connect(self.on_udev)
            self.udev.start()
        self.tuner = None
        self.last_spk = None
        self.events_alive = False
        self.disconnected = False
        self.quitting = False
        self.tray = None
        self.poller = self.events = None
        self.tick_n = 0
        for k, v in SETUP_DEFAULT.items():
            self.app_state["setup"].setdefault(k, v)
        if not self.app_state["setup"].get("main_name"):        # keep what older versions called it
            old = main_name_from_conf()
            if old:
                self.app_state["setup"]["main_name"] = old
        if not self.first_run:
            self.app_state.setdefault("setup_done", True)        # existing users already set things up
            self.app_state.setdefault("tester_updates", True)    # they've been updating from Downloads
        self.sniff_since, self.hinted = {}, {}
        self.analyzer = None
        self.meters_frozen_since, self.last_meter_revive = 0.0, 0.0
        self.meters_dead_since, self.last_rate = 0.0, None
        self.discogs_release = None
        self.tone_proc = None
        self.guitar_on = False
        self.midi = None
        self.midi_learning = None
        self.update_offer = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1320, 860)
        self._build_menu()
        if not self.reload_state():
            return
        if not ORIGINAL_SNAPSHOT.exists():
            save_json(ORIGINAL_SNAPSHOT, self.snapshot())
        self.ensure_metering()

        self.nav = QtWidgets.QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(230)
        self.nav.setIconSize(QtCore.QSize(20, 20))
        self.stack = QtWidgets.QStackedWidget()
        pages = [("Home", self._build_home), ("Hardware", self._build_hardware),
                 ("Mixer", self._build_mixer), ("Speakers", self._build_speakers),
                 ("Record", self._build_record), ("Effects", self._build_effects),
                 ("Remote", self._build_remote),
                 ("Settings", self._build_settings),
                 ("Advanced", self._build_advanced)]
        for name, builder in pages:
            ic = nav_icon(name)
            item = QtWidgets.QListWidgetItem(ic, "  " + name) if ic else \
                QtWidgets.QListWidgetItem(f"{NAV_EMOJI[name]}   {name}")
            self.nav.addItem(item)
            self.stack.addWidget(builder())
        brand = QtWidgets.QLabel(f"  Mbox Control <span style='color:{SUBTLE}'>{VERSION}</span>")
        brand.setStyleSheet(f"background:{BG1}; font-size:12pt; font-weight:700; padding:16px 8px 4px 16px;")
        side = QtWidgets.QWidget()
        side.setStyleSheet(f"background:{BG1};")
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(brand)
        sl.addWidget(self.nav, 1)
        self.mini = MiniMbox(self)
        self.mini.clicked.connect(lambda: self.nav.setCurrentRow(1))
        sl.addWidget(self.mini)
        if not self.app_state.get("revived"):
            self.app_state["revived"] = date.today().isoformat()
            self.save_app_state()
        FireWireBadge.quips = itertools.cycle(random.sample(FIREWIRE_QUIPS, len(FIREWIRE_QUIPS)))
        self.fw_badge = FireWireBadge(self)
        sl.addWidget(self.fw_badge)
        sl.addSpacing(8)
        self.now = NowPanel(self)
        sl.addWidget(self.now)
        sl.addSpacing(10)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side.installEventFilter(self)
        self.side = side
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        central = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(side)
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self.hint = HintBar()
        hw = QtWidgets.QWidget()
        hl_ = QtWidgets.QHBoxLayout(hw)
        hl_.setContentsMargins(24, 12, 24, 0)
        hl_.addWidget(self.hint)
        rv.addWidget(hw)
        rv.addWidget(self.stack, 1)
        h.addWidget(right, 1)
        self.setCentralWidget(central)
        self.run_refreshers()

        # live data: background meter poller + instant hardware events
        self.start_live()
        self.watchdog_timer = QtCore.QTimer(self)
        self.watchdog_timer.timeout.connect(self.watchdog)
        self.watchdog_timer.start(4000)
        self.health_timer = QtCore.QTimer(self)
        self.health_timer.timeout.connect(self.refresh_health)
        self.health_timer.start(15000)
        QtCore.QTimer.singleShot(600, self.check_reset)
        QtCore.QTimer.singleShot(1200, self.restore_speakers)
        self.start_ipc()
        self.setup_shortcuts()
        apply_tips(self)
        self.shorten_blurbs()
        if self.app_state["midi"].get("on") and self.app_state["midi"].get("port"):
            QtCore.QTimer.singleShot(1500, self.start_midi)
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(self.check_update)
        self.update_timer.start(60000)
        QtCore.QTimer.singleShot(2000, self.check_update)
        QtCore.QTimer.singleShot(900, self.refresh_health)
        self.osd = Osd()
        self.mix_hist, self.mix_pos = [], -1
        self.ab_slots, self.ab_active = {}, "A"
        self.hist_timer = QtCore.QTimer(self)
        self.hist_timer.setSingleShot(True)
        self.hist_timer.timeout.connect(self.record_mix_state)
        QtCore.QTimer.singleShot(1500, self.record_mix_state)
        self.dead_timer = QtCore.QTimer(self)
        self.dead_timer.timeout.connect(self.watch_dead_input)
        self.dead_timer.start(1000)
        self.auto_active = {}
        self.auto_timer = QtCore.QTimer(self)
        self.auto_timer.timeout.connect(self.check_auto_scenes)
        self.auto_timer.start(3000)
        QtCore.QTimer.singleShot(3000, self.apply_eq)
        QtCore.QTimer.singleShot(1200, self.start_rules_watch)
        self.sniff_timer = QtCore.QTimer(self)
        self.sniff_timer.timeout.connect(self.sniff_inputs)
        self.sniff_timer.start(1000)
        if not self.app_state.get("setup_done"):
            QtCore.QTimer.singleShot(800, self.run_setup_wizard)
        self.setup_tray()
        self.refresh_soon = Debounced(self._refresh_all, 250)
        self.slow = QtCore.QTimer(self)
        self.slow.timeout.connect(self.poll_state)
        self.slow.start(5000)
        self.ui_tick = QtCore.QTimer(self)
        self.ui_tick.timeout.connect(self.ui_update)
        self.ui_tick.start(66)
        self.dirty_ui = True

        # remote
        self.bridge = RemoteBridge()
        self.bridge.command.connect(self.on_remote)
        self.remote = RemoteServer(self.bridge)
        if self.app_state.get("remote_on"):
            self.start_remote(quiet=True)

        if not ORIGINAL_SNAPSHOT.with_suffix(".shown").exists():
            ORIGINAL_SNAPSHOT.with_suffix(".shown").write_text("1")
            self.toast("Saved the Mbox's starting settings — File → Load snapshot → "
                       "original-state.json puts them back.", 15000)


    # ---------------------------------------------------------------- live / reliability
    def start_live(self):
        self.backend.direct_tried = False
        if self.backend.direct:
            self.backend.direct.close()
            self.backend.direct = None
        self.backend.enable_direct()
        meter_ids = [self.ctrl(n).numid for n in METER_CTRLS if self.ctrl(n)]
        fast_ids = [self.ctrl(n).numid for n in FAST_CTRLS if self.ctrl(n)]
        self.poller = Poller(self.backend, meter_ids, fast_ids)
        self.poller.got.connect(self.on_values)
        self.poller.start()
        self.events = EventWatcher(self.card_no)
        self.events.changed.connect(self.on_event)
        self.events.alive.connect(self.on_events_alive)
        self.events.start()

    def stop_live(self):
        for t in (self.poller, self.events):
            if t:
                t.stop()
        for t in (self.poller, self.events):
            if t:
                t.wait(1500)
        self.poller = self.events = None

    def watchdog(self):
        if os.environ.get("MBOX_AMIXER"):        # simulated hardware: nothing to watch
            return
        if self.asleep or self.recovering:
            return
        card = find_mbox_card()
        if card is None:
            if not self.disconnected:
                self.disconnected = True
                self.close_card_handles("card gone")
                self.toast("The Mbox isn't connected — waiting for it to come back…", 0)
                self.refresh_health()
            self.maybe_offer_recovery()
            return
        # Something stopped answering (amixer = the Mbox's control service, pactl = PipeWire talking to
        # the Mbox). Don't keep waiting on it — say what to do, and pick things up again once it answers.
        if is_hung(AMIXER) or is_hung("pactl"):
            self.show_not_answering()
            return
        if getattr(self, "not_answering", False):
            self.not_answering = False
            self.hint.hide()
            self.toast("The Mbox is answering again.", 6000)
            self.reconnect(card)
            return
        if self.disconnected or str(card) != self.backend.card or not service_running():
            self.reconnect(card)
            return
        # The Mbox switches metering off whenever it resets; without it every meter freezes.
        c = self.ctrl("metering")
        if c:
            try:
                fresh = self.backend.read(c.numid)
            except Exception:  # noqa: BLE001
                fresh = None
            if fresh is None or fresh.name != c.name:
                # The control service restarted and renumbered everything: re-learn the controls.
                self.reconnect(card)
                return
            if not fresh.values[0]:
                self.write(c, [True])
        # A sample-rate change can leave the Mbox reporting silence on every meter: kick metering.
        rate = self.rate()
        if self.last_rate is not None and rate != self.last_rate:
            QtCore.QTimer.singleShot(1500, self.kick_metering)
        self.last_rate = rate
        # Every meter at zero while PipeWire is playing to the Mbox = dead meters, not silence.
        vals = [v for n in METER_CTRLS if self.ctrl(n) for v in self.ctrl(n).values]
        if vals and not any(vals) and mbox_playing():
            if not self.meters_dead_since:
                self.meters_dead_since = time.time()
            elif time.time() - self.meters_dead_since > 5 and time.time() - self.last_meter_revive > 30:
                self.meters_dead_since = 0.0
                self.kick_metering()
        else:
            self.meters_dead_since = 0.0
        # Meters stuck on the same non-zero reading for a while = we're not getting live data.
        if self.meters_frozen_since and time.time() - self.meters_frozen_since > 6 and \
                time.time() - self.last_meter_revive > 30:
            self.last_meter_revive = time.time()
            self.meters_frozen_since = 0.0
            m = self.ctrl("metering")
            if m:
                self.write(m, [False])
                self.write(m, [True])
            self.reconnect(card)

    # ---------------------------------------------------------------- 4.20: sleep / wake / recovery
    def close_card_handles(self, why):
        """Let go of everything this app holds on the card (meters, events, direct reads) — before sleep
        or a driver reload, or the kernel module stays 'in use'."""
        self.stop_live()
        if self.backend.direct:
            try:
                self.backend.direct.close()
            except Exception:  # noqa: BLE001
                pass
            self.backend.direct = None
        self.backend.direct_tried = False
        rlog(f"closed this app's handles to the card ({why})")

    def stop_ctl_daemon(self):
        """Stop the ctl daemon (it holds the card). Returns how it was running, so wake can restart it."""
        u = mbox_unit()
        daemon = u["daemon"] if u else "snd-dice-ctl-service"
        if unit_active("mbox-ctl.service"):
            systemd_user("stop", "mbox-ctl.service")
            rlog("stopped mbox-ctl.service")
            return "unit"
        if run_quiet("pkill", "-x", daemon) is not None:
            rlog(f"stopped {daemon}")
            return "proc"
        return None

    def on_sleep(self, going):
        if going:
            self.asleep = True
            try:
                if not self.disconnected and self.ctrls:
                    snap = CONF_DIR / "before-sleep.json"
                    save_json(snap, self.snapshot())
                    self.restore_after_wake = snap
            except Exception as e:  # noqa: BLE001
                rlog(f"couldn't save the pre-sleep state: {e}")
            self.remember_default_sink()
            self.close_card_handles("going to sleep")
            self.spectrum_hub.stop_all()
            self.sleep_ctl = self.stop_ctl_daemon()
            self.disconnected = True
            self.sleep_watch.release_inhibitor()            # done: the PC may sleep now
            return
        self.asleep = False
        self.woke_at = time.time()
        self.recover_offered = False
        self.sleep_watch.take_inhibitor()
        self.toast("Woke up — reconnecting to the Mbox…", 0)
        self.audio_check_pending = True
        rlog("woke up: waiting for the Mbox's card")
        QtCore.QTimer.singleShot(1500, self.watchdog)

    def on_udev(self, what):
        rlog(f"udev: {what}")
        if not getattr(self, "udev_pending", False):          # a burst of events → one check
            self.udev_pending = True
            QtCore.QTimer.singleShot(800, lambda: (setattr(self, "udev_pending", False), self.watchdog()))

    def remember_default_sink(self):
        cur = (run_quiet("pactl", "get-default-sink", timeout=4) or "").strip()
        if cur:
            self.sleep_default_sink = cur
            rlog(f"default output before: {cur}")

    def restore_audio_after_wake(self):
        """The card is back — now the PipeWire side: Mbox outputs present, our loopbacks linked to the Mbox
        again, and the default output what it was. Logs what it finds, so a report shows the state."""
        if os.environ.get("MBOX_AMIXER") or self.asleep or self.recovering:
            return
        sinks = pw_sinks()
        mbox = [n for n in sinks if "firewire" in n]
        if not mbox:
            card = mbox_pw_card()
            rlog(f"after wake: no Mbox output in PipeWire (card {card or 'not found'}) — switching it to pro-audio")
            if card:
                run_quiet("pactl", "set-card-profile", card, "pro-audio", timeout=5)
            return                                       # the next check (a few seconds later) finishes up
        moved = relink_mbox_streams()
        for name, frm, to in moved:
            rlog(f"after wake: moved {name} back to the Mbox (it had been moved to sink {frm})")
        want = getattr(self, "sleep_default_sink", "")
        cur = (run_quiet("pactl", "get-default-sink", timeout=4) or "").strip()
        if want and want in sinks and cur != want:
            run_quiet("pactl", "set-default-sink", want, timeout=4)
            rlog(f"after wake: default output was {cur or 'none'} — put back to {want}")
        elif want and want not in sinks:
            rlog(f"after wake: the old default output {want} isn't there (yet)")
        else:
            rlog(f"after wake: audio OK — default {cur}, Mbox outputs {', '.join(mbox)}")
        self.refresh_health()

    def maybe_offer_recovery(self):
        """Card gone but the FireWire unit is there: the stuck-after-sleep case. Give the sleep hook time if
        it's installed; otherwise (or if it didn't manage) offer the one-click recovery."""
        if self.recover_offered or self.recovering:
            return
        if not card_missing_after_wake():
            return
        since_wake = time.time() - self.woke_at if self.woke_at else 1e9
        if sleep_fix_installed() and since_wake < 45:
            return                                       # the root hook is on it
        if since_wake < 12:
            return                                       # sometimes it just takes a moment
        self.recover_offered = True
        u = mbox_unit()
        rlog(f"card missing while {u['unit']} is present — offering recovery")
        self.hint.offer("<b>The Mbox didn't come back after sleep.</b> The FireWire connection is fine, but its "
                        "sound card is stuck. Recover it: the app restarts the audio system and reloads the "
                        "Mbox's driver (asks for your password).",
                        [("Recover Mbox", self.recover_mbox)])
        self.announce("dialog-warning", "The Mbox didn't come back — Recover it in Mbox Control", 10000)

    def recover_mbox(self, force=False):
        if self.recovering:
            return
        u = mbox_unit()
        if not u:
            QtWidgets.QMessageBox.information(self, APP_NAME, "No FireWire audio interface found. Is the Mbox "
                                              "switched on and its FireWire cable plugged in?")
            return
        if u["card"] is not None and not force and QtWidgets.QMessageBox.question(
                self, APP_NAME, "The Mbox looks fine right now. Restart the audio system and reload its driver "
                "anyway? Sound stops for a few seconds.") != QtWidgets.QMessageBox.Yes:
            return
        self.recovering = True
        self.remember_default_sink()
        rlog(f"recovery started ({'manual' if u['card'] is not None or not self.recover_offered else 'offered'})")
        self.close_card_handles("recovery")
        self.spectrum_hub.stop_all()
        self.disconnected = True
        self.hint.offer("<b>Recovering the Mbox…</b> Sound stops for a few seconds.", [])
        self.recovery = RecoveryJob(u)
        self.recovery.step.connect(lambda m: self.toast(m, 0))
        self.recovery.done.connect(self.recovered)
        self.recovery.start()

    def recovered(self, ok, msg):
        job, self.recovery = self.recovery, None
        if job:
            job.wait(2000)
        self.recovering = False
        if ok:
            self.hint.hide()
            self.toast("The Mbox is back.", 6000)
            self.audio_check_pending = True
            rlog(f"recovery finished: {msg}")
            self.watchdog()
            return
        rlog(f"recovery failed: {msg}")
        self.hint.offer(f"<b>Couldn't bring the Mbox back.</b> {html_escape(msg)} Try again, or restart the PC. "
                        "Settings → App → Sleep & wake → Copy diagnostics has every step.",
                        [("Try again", lambda: self.recover_mbox(force=True)), ("Open log", self.open_recovery_log)])

    def open_recovery_log(self):
        rlog("log opened")
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(RECOVERY_LOG)))

    def sleep_fix_script(self, install):
        """One root script for pkexec: install (or remove) the helper, polkit policy and sleep hook."""
        packaged = SYSTEM_DIRS[0]
        local = SYSTEM_DIRS[1]
        hook = SLEEP_HOOK_DIR / SLEEP_HOOK_NAME
        lines = ["#!/bin/sh", "set -e", f"rm -f {WAKE_HOOK}   # the old unbind/bind fix (can leave a stuck card)"]
        if not install:
            lines += [f"rm -f {hook}"]
            if (local / "mbox-wake-recover").exists():
                lines += [f"rm -rf {local}",
                          f"rm -f /usr/share/polkit-1/actions/{POLKIT_ACTION}.local.policy"]
            return "\n".join(lines) + "\n"
        if (packaged / "mbox-wake-recover").exists():             # installed from a package: just the hook
            recover = packaged / "mbox-wake-recover"
        else:
            recover = local / "mbox-wake-recover"
            stage = CONF_DIR / "system"
            stage.mkdir(parents=True, exist_ok=True)
            (stage / "mbox-reload-driver").write_text(RELOAD_HELPER)
            (stage / "mbox-wake-recover").write_text(WAKE_RECOVER)
            (stage / "policy").write_text(polkit_policy(local / "mbox-reload-driver").replace(
                f'id="{POLKIT_ACTION}"', f'id="{POLKIT_ACTION}.local"'))
            lines += [f"install -d -m 755 {local}",
                      f"install -m 755 '{stage / 'mbox-reload-driver'}' {local}/mbox-reload-driver",
                      f"install -m 755 '{stage / 'mbox-wake-recover'}' {local}/mbox-wake-recover",
                      f"install -D -m 644 '{stage / 'policy'}' "
                      f"/usr/share/polkit-1/actions/{POLKIT_ACTION}.local.policy"]
        stage = CONF_DIR / "system"
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "hook").write_text(sleep_hook_text(recover))
        lines += [f"install -D -m 755 '{stage / 'hook'}' {hook}"]
        return "\n".join(lines) + "\n"

    def set_sleep_fix(self, install):
        if not shutil.which("pkexec"):
            QtWidgets.QMessageBox.warning(self, APP_NAME, "pkexec isn't available, so the app can't ask for "
                                          "admin rights.")
            return
        script = CONF_DIR / "sleep-fix.sh"
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        script.write_text(self.sleep_fix_script(install))
        r = subprocess.run(["pkexec", "/bin/sh", str(script)], capture_output=True, text=True)
        ok = r.returncode == 0
        rlog(("installed" if install else "removed") + " the sleep/wake fix" if ok else
             f"sleep/wake fix {'install' if install else 'removal'} failed: {(r.stderr or '').strip()}")
        self.toast(("Sleep/wake fix installed." if install else "Sleep/wake fix removed.") if ok else
                   "Not changed" + (f": {r.stderr.strip()}" if r.stderr.strip() else "."), 8000)
        self.refresh_sleep_card()
        self.refresh_health()

    def build_sleep_card(self, lay):
        c, l = card("Sleep & wake", "Keeps the Mbox working after the PC sleeps. The app lets go of the Mbox "
                                    "before sleep and reconnects by itself after. If the Mbox's sound card "
                                    "gets stuck anyway, Recover brings it back without a reboot.")
        self.sleep_status = muted("")
        self.sleep_status.setWordWrap(True)
        l.addWidget(self.sleep_status)
        row = QtWidgets.QHBoxLayout()
        rb = QtWidgets.QPushButton("Recover Mbox now…")
        rb.setToolTip("Stops the audio system, reloads the Mbox's sound driver (asks for your password) and "
                      "starts everything again. Never touches the FireWire controller.")
        rb.clicked.connect(lambda: self.recover_mbox())
        row.addWidget(rb)
        self.sleep_fix_btn = QtWidgets.QPushButton()
        self.sleep_fix_btn.clicked.connect(lambda: self.set_sleep_fix(not sleep_fix_installed()))
        row.addWidget(self.sleep_fix_btn)
        lg = QtWidgets.QPushButton("Open recovery log")
        lg.clicked.connect(self.open_recovery_log)
        row.addWidget(lg)
        dg = QtWidgets.QPushButton("Copy diagnostics")
        dg.setToolTip("Copies your system details and both recovery logs (no personal info) — paste them "
                      "into a bug report or a chat.")
        dg.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(diagnostics()),
                                    self.toast("Diagnostics copied — paste them with Ctrl+V.", 6000)))
        row.addWidget(dg)
        row.addStretch(1)
        l.addLayout(row)
        lay.addWidget(c)
        self.refresh_sleep_card()

    def refresh_sleep_card(self):
        if not hasattr(self, "sleep_status"):
            return
        on = sleep_fix_installed()
        self.sleep_fix_btn.setText("Remove sleep/wake fix…" if on else "Install sleep/wake fix…")
        self.sleep_fix_btn.setToolTip(
            "Removes the system hook." if on else
            "Asks for your password once: adds a small system hook that, after waking, checks whether the "
            "Mbox's sound card came back and recovers it if not — no password needed then. Off by default.")
        u = mbox_unit() if not os.environ.get("MBOX_AMIXER") else None
        bits = [f"Sleep/wake fix: {'installed' if on else 'not installed'}",
                f"sleep watch: {'on' if self.sleep_watch.connected else 'unavailable'}",
                f"instant reconnect (udev): {'on' if self.udev else 'off — install python-pyudev'}"]
        if u:
            bits.append(f"Mbox: {u['unit']} · driver {u['driver']} · " +
                        (f"card {u['card']}" if u["card"] is not None else "card MISSING"))
        if WAKE_HOOK.exists():
            bits.append("an older wake-up fix (mbox-rebind) is installed — installing replaces it")
        self.sleep_status.setText(" · ".join(bits))

    def show_not_answering(self):
        if getattr(self, "not_answering", False):
            return
        self.not_answering = True
        if self.poller:
            self.poller.interval = 0.5                  # go easy on it until it's back
        text = ("<b>The Mbox has stopped answering.</b> Switch it off at the back, wait 5 seconds and switch it "
                "back on — the app picks it up again by itself.")
        self.hint.offer(text, [])
        if not self.isVisible() and self.tray:
            self.tray.showMessage(APP_NAME, "The Mbox has stopped answering — switch it off and on again.",
                                  app_icon(), 15000)
        try:
            with open(CRASH_LOG, "a") as f:
                f.write(f"--- Mbox not answering at {datetime.now():%H:%M:%S} "
                        f"(amixer hung: {is_hung(AMIXER)}, pactl hung: {is_hung('pactl')}) ---\n")
        except OSError:
            pass

    def kick_metering(self):
        """Metering off and on again — what brings dead meters back without touching the sound."""
        self.last_meter_revive = time.time()
        m = self.ctrl("metering")
        if m:
            self.write(m, [False])
            self.write(m, [True])

    def reconnect(self, card):
        self.toast("Reconnecting to the Mbox…", 0)
        self.stop_live()
        self.backend.card = self.card_no = str(card)
        if not service_running():
            ensure_service(card)
        deadline = time.time() + 8
        while time.time() < deadline and not service_ready(self.backend):
            QtWidgets.QApplication.processEvents()
            QtCore.QThread.msleep(250)
        if not service_ready(self.backend):
            self.disconnected = True
            self.toast("Still waiting for the Mbox control service…", 0)
            return
        self.reload_state()
        self.ensure_metering()
        self.disconnected = False
        self.recover_offered = False
        snap = getattr(self, "restore_after_wake", None)
        if snap:
            self.restore_after_wake = None
            if not self.marker_present() and snap.exists():
                self.apply_snapshot(load_json(snap, {}))
                self.ensure_marker()
                rlog("restored the routing/mixer state from before sleep")
                self.toast("The Mbox had forgotten its settings while asleep — put them back.", 8000)
        self.start_live()
        self.run_refreshers()
        self.check_reset()
        QtCore.QTimer.singleShot(2500, lambda: (fix_default_sink(), self.refresh_health()))
        if getattr(self, "audio_check_pending", False):  # after sleep/recovery: make sure sound flows again
            self.audio_check_pending = False
            for delay in (2000, 6000, 15000):
                QtCore.QTimer.singleShot(delay, self.restore_audio_after_wake)
        rlog(f"reconnected to the Mbox (card {card})")
        self.toast("Reconnected to the Mbox.", 5000)

    # The Mbox forgets its mixer when powered off (and Avid's driver rewrites it in Windows).
    # We park a harmless marker on an unused router slot; if it's gone, the box was reset.
    MARKER_SLOT, MARKER_ITEM = 15, "S/PDIF-2"

    def marker_present(self):
        c = self.ctrl("output-source")
        return bool(c and len(c.values) > self.MARKER_SLOT and self.MARKER_ITEM in c.items
                    and c.values[self.MARKER_SLOT] == c.items.index(self.MARKER_ITEM))

    def ensure_marker(self):
        c = self.ctrl("output-source")
        if c and len(c.values) > self.MARKER_SLOT and self.MARKER_ITEM in c.items:
            self.write_element("output-source", 0, self.MARKER_SLOT, c.items.index(self.MARKER_ITEM))

    def check_reset(self):
        if self.marker_present():
            return
        name = self.app_state.get("startup_scene")
        if name and (SCENES_DIR / f"{name}.json").exists():
            self.apply_snapshot(load_json(SCENES_DIR / f"{name}.json", {}))
            self.toast(f"The Mbox had been reset — restored your “{name}” setup.", 10000)
            if self.tray:
                self.tray.showMessage(APP_NAME, f"Mbox was reset — restored “{name}”.", app_icon(), 4000)
        else:
            self.ensure_marker()
            self.app_state["reset_seen"] = True
            self.save_app_state()
            self.refresh_health()

    # ---------------------------------------------------------------- health checks
    def health_items(self):
        items = []
        if self.disconnected:
            items.append(("error", "The Mbox isn't connected. Switch it on — the app reconnects by itself.",
                          None, None))
            return items
        have_pactl = bool(shutil.which("pactl")) and not os.environ.get("MBOX_AMIXER")
        if have_pactl:
            if not mbox_pw_node("sink"):
                items.append(("error", "The Mbox's audio is switched off in PipeWire.", "Fix", self.fix_profile))
            elif not pw_node_exists(MAIN_SINK, "sink"):
                items.append(("error", f"The “{self.main_output_name()}” output is missing.", "Fix",
                              self.fix_main_output))
            else:
                cur = (run_quiet("pactl", "get-default-sink") or "").strip()
                if "firewire" in cur and "pro-output" in cur:
                    items.append(("error", "Sound is going to the raw Mbox output (PC 1/2), which isn't in "
                                           "your mix.", "Fix", lambda: (fix_default_sink(), self.refresh_health())))
        if not service_unit_ok() and not os.environ.get("MBOX_AMIXER"):
            items.append(("warn", "The background service isn't set up to find the Mbox by name.",
                          "Repair", self.repair_service))
        if have_pactl and not NOSUSPEND_CONF.exists():
            items.append(("tip", "Recordings can miss their first second while the Mbox wakes up.",
                          "Keep it awake", self.install_nosuspend))
        muted_on = self.always_on_muted()
        if muted_on:
            items.append(("warn", f"{muted_on} is turned down in the speaker mix, so it's silent.",
                          "Turn it back up", self.restore_always_on_now))
        if self.update_offer:
            o = self.update_offer
            where = "is in your Downloads folder" if o["kind"] == "file" else "is out"
            items.insert(0, ("tip", f"Mbox Control {o['ver']} {where}.", "Update now", self.install_offer))
        if not self.app_state.get("startup_scene"):
            items.append(("tip", "Pick a startup scene so your setup comes back automatically after the "
                                 "Mbox is switched off or used in Windows.", "Choose…", self.choose_startup))
        if not self.app_state.get("subsonic_asked") and not any(self.app_state.get("subsonic_sets", {}).values()):
            items.append(("tip", "New: a subsonic filter stops bass thumps from popping your speakers (it cuts "
                                 f"only below {SUBSONIC_HZ:.0f} Hz — you can turn it off per speaker set in "
                                 "Speakers → EQ).", "Turn it on", self.enable_subsonic_everywhere,
                          lambda: (self.app_state.__setitem__("subsonic_asked", True), self.save_app_state())))
        if WAKE_HOOK.exists() and not os.environ.get("MBOX_AMIXER"):
            items.append(("warn", "An older wake-up fix is installed that can leave the Mbox stuck after "
                                  "sleep. Replace it with the new one.", "Replace…",
                          lambda: self.set_sleep_fix(True)))
        return items

    def refresh_health(self):
        if not hasattr(self, "health_box"):
            return
        lay = self.health_box.layout()
        while lay.count() > 1:
            it = lay.takeAt(1)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        items = self.health_items()
        self.health_box.setVisible(bool(items))
        colours = {"error": "#f85149", "warn": "#e3a52a", "tip": ACCENT}
        icons = {"error": "●", "warn": "●", "tip": "💡"}
        for item in items:
            level, text, label, fn = item[:4]
            dismiss = item[4] if len(item) > 4 else None
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            dot = QtWidgets.QLabel(icons[level])
            dot.setStyleSheet(f"color:{colours[level]}; font-size:12pt;")
            h.addWidget(dot)
            t = QtWidgets.QLabel(text)
            t.setWordWrap(True)
            h.addWidget(t, 1)
            if label:
                b = QtWidgets.QPushButton(label)
                b.setObjectName("primary" if level == "error" else "")
                b.clicked.connect(fn)
                h.addWidget(b)
            if dismiss:
                x = QtWidgets.QToolButton()
                x.setText("✕")
                x.setToolTip("No thanks — don't suggest this again")
                x.clicked.connect(lambda _=False, d=dismiss: (d(), self.refresh_health()))
                h.addWidget(x)
            lay.addWidget(row)
        self.health_title.setText("Needs attention" if any(i[0] == "error" for i in items)
                                  else "Suggestions")

    def fix_profile(self):
        card = mbox_pw_card()
        if card:
            run_quiet("pactl", "set-card-profile", card, "pro-audio")
        QtCore.QTimer.singleShot(1500, lambda: (fix_default_sink(), self.refresh_health()))

    # ---------------------------------------------------------------- 4.11: setup wizard, plug-in hints
    def run_setup_wizard(self):
        if not self.isVisible():
            self.show_window()
        d = SetupWizard(self)
        if d.exec() == QtWidgets.QDialog.Accepted:
            self.names_changed()
        else:
            self.app_state["setup_done"] = True            # skipped: don't nag every launch
            self.save_app_state()
            self.toast("Set it up any time: Settings → Your setup → Run the setup again.", 8000)

    def names_changed(self):
        """Names were set somewhere other than the Your setup page: redraw everything that shows them."""
        t = getattr(self, "settings_tabs", None)
        if t is not None:
            cur = t.currentIndex()
            old = t.widget(0)
            new = self._build_setup()
            for h in new.findChildren(QtWidgets.QLabel, "h1"):
                h.setVisible(False)
            t.removeTab(0)
            t.insertTab(0, new, "Your setup")
            t.setCurrentIndex(cur)
            old.deleteLater()
        self.rebuild_scene_grid()
        self.run_refreshers()
        self.shorten_blurbs()

    def item_db(self, item):
        """Live level of an Mbox input, from the mixer meter or, failing that, the capture meter."""
        db = meter_db(self.input_level(item))
        if db <= METER_FLOOR_DB:
            ss, sm = self.ctrl("stream-source"), self.ctrl("stream-source-meter")
            if ss and sm:
                for i, v in enumerate(ss.values):
                    if 0 <= v < len(ss.items) and ss.items[v] == item and i < len(sm.values):
                        db = max(db, meter_db(sm.values[i]))
        return db

    def input_heard(self, item):
        """Is this input reaching any output right now (mix with gain, or routed straight there)?"""
        slot = self.slot_of(item)
        for _, first, _ in OUTPUT_PAIRS:
            kind, val, _sw = self.pair_source(first)
            if kind == "mix" and slot is not None and max(self.mix_lr(val, slot)) > 0:
                return True
            if kind == "direct" and item in val:
                return True
        return False

    def computer_listening(self):
        """Something on the computer is already using an Mbox input (Audacity, a DAW, Guitarix…)."""
        if self.guitar_on or (hasattr(self, "recorder") and self.recorder.running()):
            return True
        for s in pactl_json("list", "source-outputs"):
            p = s.get("properties", {})
            if not str(p.get("node.name", "")).startswith(("mbox_mirror", "mbox_meter")) and \
                    "pavucontrol" not in str(p.get("application.name", "")).lower() and \
                    "plasma" not in str(p.get("application.name", "")).lower():
                return True
        return False

    def sniff_inputs(self):
        """Sound turning up on an input that goes nowhere → offer to do something with it."""
        if not self.app_state.get("plug_hints", True) or self.hint.isVisible() or self.disconnected:
            return
        now = time.time()
        ignore = set(self.app_state.get("plug_ignore", []))
        always = set(self.app_state["setup"].get("always_on") or [])
        always |= {pair_right(a) for a in always}
        for r, (item, jack) in enumerate(INPUT_JACKS):
            if item in ignore or item in always:
                continue
            if self.item_db(item) > -42 and not self.input_heard(item):
                t0 = self.sniff_since.setdefault(item, now)
                if now - t0 >= 2.5 and now - self.hinted.get(item, 0) > 900:
                    if self.computer_listening():
                        self.hinted[item] = now                 # it's in use on the computer: leave it be
                        continue
                    self.hinted[item] = now
                    self.offer_input(r, item, jack)
                    return
            else:
                self.sniff_since.pop(item, None)

    def hear_input(self, item, mono):
        kind, k, _ = self.pair_source(0)
        if kind != "mix":
            self.toast("Outputs 1/2 aren't on a Stereo Mix, so I can't add it — use the Mixer page.", 8000)
            return
        if mono:
            g = int(UNITY * 0.7071)                       # centred, same loudness as one side of a pair
            self.mix_put(k, item, g, g)
        else:
            left = item if item in [p[0] for p in INPUT_PAIRS] else \
                next((p[0] for p in INPUT_PAIRS if pair_right(p[0]) == item), item)
            self.mix_put(k, left, UNITY, 0)
            self.mix_put(k, pair_right(left), 0, UNITY)
        self.refresh_soon()
        self.toast(f"Added it to Stereo Mix {k} — turn it down on the Mixer page if it's too loud.", 8000)

    def offer_input(self, r, item, jack):
        own = self.label(f"in{r}", "")
        what = f"<b>{html_escape(own)}</b> ({jack})" if own else f"<b>{jack}</b>"
        rip = self.app_state["setup"].get("rip")
        never = lambda: (self.app_state.setdefault("plug_ignore", []).append(item),  # noqa: E731
                         self.save_app_state())
        front = r < 2                                        # the two mic/instrument inputs on the front
        if rip and item in (rip, pair_right(rip)):
            # a receiver's tape-out carries whatever you're listening to, so ask once per session at most
            self.hinted[rip] = self.hinted[pair_right(rip)] = time.time() + 10 ** 9
            text = f"Sound on {what}, but it isn't going to your speakers. A tape or record playing?"
            acts = [("Rip it", lambda: (self.run_scene(self.scene_view(next(x for x in SCENES if x["id"] == "rip"))),
                                        self.nav.setCurrentRow(self.page_index("Record")))),
                    ("Just listen", lambda: self.hear_input(item, False))]
        elif front:
            text = f"Sound on {what}, but you can't hear it. Mic or instrument?"

            def take():
                self.app_state["take_src"] = f"mono:{item}"
                if hasattr(self, "take_src_box"):
                    self.take_src_box.setCurrentIndex(max(0, self.take_src_box.findData(f"mono:{item}")))
                self.nav.setCurrentRow(self.page_index("Record"))
                self.toggle_take()
            acts = [("Hear it", lambda: self.hear_input(item, True)), ("Quick take", take)]
        else:
            text = f"Sound on {what}, but it isn't going to your speakers."
            acts = [("Hear it", lambda: self.hear_input(item, False))]
        self.hint.offer(text, acts, never)
        if not self.isVisible() and self.tray:
            self.tray.showMessage(APP_NAME, re.sub("<[^>]+>", "", text) + " Open the app to choose.",
                                  app_icon(), 8000)

    def rename_main_output(self, name):
        st = self.app_state["setup"]
        st["main_name"] = name or DEFAULT_MAIN
        self.save_app_state()
        if getattr(self, "main_name_edit", None):
            self.main_name_edit.setText(st["main_name"])
        sink = mbox_pw_node("sink")
        if sink:
            write_main_conf(sink, st["main_name"])
            restart_pipewire()
            QtCore.QTimer.singleShot(2500, fix_default_sink)
            self.toast(f"Your main output is now called “{st['main_name']}”.", 6000)

    def fix_main_output(self):
        sink = mbox_pw_node("sink")
        if not sink:
            self.fix_profile()
            return
        write_main_conf(sink, self.main_output_name())
        restart_pipewire()
        QtCore.QTimer.singleShot(3000, lambda: (fix_default_sink(), self.refresh_health()))
        self.toast(f"Recreated the {self.main_output_name()} output.")

    def repair_service(self):
        ok = install_service_unit()
        self.toast("Background service repaired." if ok else "Couldn't restart the service — see Settings.")
        QtCore.QTimer.singleShot(3000, self.refresh_health)

    def install_wake(self):
        self.set_sleep_fix(True)

    def choose_startup(self):
        NOW = "What's set up right now (recommended)"
        users = self.user_scenes()
        if users:
            choice, ok = QtWidgets.QInputDialog.getItem(
                self, "Startup scene", "After the Mbox is switched off or used in Windows, put back:",
                [NOW] + users, 0, False)
            if not ok:
                return
        else:
            if QtWidgets.QMessageBox.question(
                    self, "Startup scene",
                    "Save your current setup (mixer, routing, speakers) as “Everyday” and put it back "
                    "automatically whenever the Mbox gets reset?") != QtWidgets.QMessageBox.Yes:
                return
            choice = NOW
        if choice == NOW:
            choice = "Everyday"
            self.reload_state()
            save_json(SCENES_DIR / f"{choice}.json", self.snapshot())
            self.rebuild_scene_grid()
        self.app_state["startup_scene"] = choice
        self.save_app_state()
        if hasattr(self, "refresh_startup_box"):
            self.refresh_startup_box()
        self.refresh_health()
        self.toast(f"Startup scene: {choice}. Change it anytime in Settings → Startup & tray.", 7000)

    # ---------------------------------------------------------------- speakers mute
    def mute_speakers(self, on):
        st = self.app_state["spk_mute"]
        if on:
            pairs = sorted({f for f in self.app_state["cr"].values() if isinstance(f, int) and f >= 0} | {0})
            for f in pairs:
                cur = self.pair_raw(f)
                if cur != [0, 0]:
                    st[str(f)] = cur
                    self.route_pair_raw(f, [0, 0])
        else:
            for f, pair in list(st.items()):
                self.route_pair_raw(int(f), pair)
            st.clear()
        self.save_app_state()
        self.refresh_soon()
        if self.tray and hasattr(self, "tray_mute"):
            self.tray_mute.blockSignals(True)
            self.tray_mute.setChecked(bool(st))
            self.tray_mute.blockSignals(False)
        self.now.refresh()
        self.push_remote_state()

    # ---------------------------------------------------------------- tray
    def wrap_tabs(self, title, subtitle, pages):
        """One page with a header and tabs holding other pages (their own headers hidden)."""
        w, lay = page(title, subtitle)
        tabs = QtWidgets.QTabWidget()
        tabs.setDocumentMode(True)
        for name, pw in pages:
            for h in pw.findChildren(QtWidgets.QLabel, "h1"):
                h.setVisible(False)
                lay_parent = h.parentWidget().layout() if h.parentWidget() else None
                if lay_parent:
                    i = lay_parent.indexOf(h)
                    nxt = lay_parent.itemAt(i + 1).widget() if i >= 0 and lay_parent.itemAt(i + 1) else None
                    if isinstance(nxt, QtWidgets.QLabel) and nxt.objectName() == "muted":
                        nxt.setVisible(False)          # the old page subtitle
            tabs.addTab(pw, name)
        lay.addWidget(tabs, 1)
        return w

    def tabify(self, w, lay, groups):
        """Turn a long page of cards into tabs. Cards not listed go to the last tab."""
        cards = []
        for i in range(lay.count()):
            wd = lay.itemAt(i).widget()
            if wd is not None and wd.objectName() == "card":
                t = wd.findChild(QtWidgets.QLabel, "title")
                cards.append((t.text() if t else "", wd))
        tabs = QtWidgets.QTabWidget()
        tabs.setDocumentMode(True)
        placed = set()
        pages = []
        for name, titles in groups:
            pw = QtWidgets.QWidget()
            pl = QtWidgets.QVBoxLayout(pw)
            pl.setContentsMargins(0, 8, 0, 0)
            pl.setSpacing(14)
            pages.append((name, pw, pl, titles))
        for name, pw, pl, titles in pages:
            for title in titles:
                for t, wd in cards:
                    if t == title and id(wd) not in placed:
                        lay.removeWidget(wd)
                        pl.addWidget(wd)
                        placed.add(id(wd))
        for t, wd in cards:
            if id(wd) not in placed:
                lay.removeWidget(wd)
                pages[-1][2].addWidget(wd)
        for name, pw, pl, titles in pages:
            pl.addStretch(1)
            tabs.addTab(scroll(pw), name)
        # remove trailing stretch from the header layout, add tabs
        for i in reversed(range(lay.count())):
            if lay.itemAt(i).spacerItem():
                lay.takeAt(i)
        lay.addWidget(tabs, 1)
        return w

    def shorten_blurbs(self):
        """Clean pages: every long explanation keeps its first sentence; the rest is one hover away (ⓘ)."""
        for lab in self.findChildren(QtWidgets.QLabel, "muted"):
            txt = lab.text()
            if len(txt) <= 95 or "<" in txt or txt.endswith("ⓘ") or lab.property("keep_long"):
                continue
            m = re.match(r"(.{25,}?[.!?—])\s", txt)
            short = m.group(1).rstrip(" —") if m and len(m.group(1)) <= 110 else \
                txt[:90].rsplit(" ", 1)[0].rstrip(" ·,:;—") + "…"
            if len(short) < len(txt) - 10:
                lab.setText(short + "  ⓘ")
                lab.setToolTip(f"<p style='max-width:420px'>{html_escape(txt)}</p>")
                lab.setCursor(Qt.WhatsThisCursor)
        for t in self.findChildren(QtWidgets.QTabWidget):
            icon_tabs(t)

    # ---------------------------------------------------------------- tray quick panel
    def build_quick_panel(self):
        p = QuickPanel()
        p.setStyleSheet(f"QFrame#quick {{ background:{BG1}; border:1px solid {ACCENT}; border-radius:14px; }}")
        v = QtWidgets.QVBoxLayout(p)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        head = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel("<b>Mbox</b>")
        t.setStyleSheet("font-size: 12pt;")
        head.addWidget(t)
        head.addStretch(1)
        op = QtWidgets.QPushButton("Open app")
        op.clicked.connect(lambda: (p.hide(), self.show_window()))
        head.addWidget(op)
        v.addLayout(head)
        row = QtWidgets.QHBoxLayout()
        self.q_spk = {}
        for L in "ABC":
            b = QtWidgets.QPushButton(f"Speakers {L}")
            b.setCheckable(True)
            b.setToolTip((self.label(f"spk{L}", "") or self.label(f"cr_name{L}", "")) or f"Switch to speaker set {L}")
            b.clicked.connect(lambda _=False, L=L: (self.switch_speakers(L), self.refresh_quick_panel()))
            row.addWidget(b)
            self.q_spk[L] = b
        v.addLayout(row)
        vr = QtWidgets.QHBoxLayout()
        vr.addWidget(QtWidgets.QLabel("Volume"))
        self.q_vol = QtWidgets.QSlider(Qt.Horizontal)
        self.q_vol.setRange(0, 255)
        self.q_vol.setToolTip("Speaker volume (Outputs 1/2 trim)")
        self.q_vol.valueChanged.connect(self.quick_volume)
        vr.addWidget(self.q_vol, 1)
        self.q_mute = QtWidgets.QPushButton("Mute")
        self.q_mute.setCheckable(True)
        self.q_mute.toggled.connect(lambda on: (self.mute_speakers(on), self.refresh_quick_panel()))
        vr.addWidget(self.q_mute)
        v.addLayout(vr)
        self.q_take = QtWidgets.QPushButton("🎙  Quick take")
        self.q_take.setToolTip("Record a quick take from your mic/instrument (Record → Quick take).")
        self.q_take.clicked.connect(lambda: (self.toggle_take(), self.refresh_quick_panel()))
        v.addWidget(self.q_take)
        self.q_retro = QtWidgets.QPushButton("💾  Save what I just played")
        self.q_retro.setToolTip("Saves the last minute from the always-listening buffer (Record → Quick take).")
        self.q_retro.clicked.connect(lambda: self.save_retro(60))
        v.addWidget(self.q_retro)
        v.addWidget(muted("Scenes"))
        self.q_scenes = FlowGrid(200)
        v.addWidget(self.q_scenes)
        p.setFixedWidth(470)
        self.quick = p

    def quick_volume(self, val):
        if getattr(self, "q_syncing", False):
            return
        c = self.ctrl("output-trim")
        if c:
            vals = list(c.values)
            vals[0] = vals[1] = val
            self.write(c, vals)
            self.refresh_soon()

    def refresh_quick_panel(self):
        if not getattr(self, "quick", None):
            return
        self.q_syncing = True
        cur = (self.ctrl_item("spkr-button") or "Off").split("-")[0]
        for L, b in self.q_spk.items():
            b.setChecked(LETTER_COLOUR[L][0] == cur)
            b.setStyleSheet(f"QPushButton:checked {{ background:{LETTER_COLOUR[L][1]}; color:#111; }}")
        c = self.ctrl("output-trim")
        if c:
            self.q_vol.setValue(int(c.values[0]))
        self.q_take.setText("■  Stop take" if getattr(self, "take_mode", False) else "🎙  Quick take")
        self.q_retro.setVisible(bool(getattr(self, "retro", None)))
        self.q_mute.blockSignals(True)
        self.q_mute.setChecked(bool(self.app_state.get("spk_mute")))
        self.q_mute.blockSignals(False)
        boxes = []
        for sc in SCENES:
            b = QtWidgets.QPushButton(sc["name"].strip())
            b.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            b.setToolTip(self.scene_view(sc)["short"])
            b.clicked.connect(lambda _=False, sc=sc: (self.quick.hide(), self.run_scene(self.scene_view(sc))))
            boxes.append(b)
        for n in self.user_scenes():
            b = QtWidgets.QPushButton(f"⭐ {n}")
            b.clicked.connect(lambda _=False, n=n: (self.quick.hide(), self.apply_user_scene(n)))
            boxes.append(b)
        self.q_scenes.set_widgets(boxes)
        self.q_syncing = False

    def show_quick_panel(self):
        if not getattr(self, "quick", None):
            self.build_quick_panel()
        self.reload_state()
        self.refresh_quick_panel()
        self.quick.adjustSize()
        pos = QtGui.QCursor.pos()
        scr = QtGui.QGuiApplication.screenAt(pos) or QtGui.QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x = min(max(g.left() + 8, pos.x() - self.quick.width() // 2), g.right() - self.quick.width() - 8)
        y = pos.y() + 12 if pos.y() < g.center().y() else pos.y() - self.quick.height() - 12
        self.quick.move(x, max(g.top() + 8, y))
        self.quick.show()
        self.quick.raise_()
        self.quick.activateWindow()

    def setup_tray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QtWidgets.QSystemTrayIcon(knob_icon(), self)
        self.tray.messageClicked.connect(self.show_window)
        self.tray_state = None
        menu = QtWidgets.QMenu()
        menu.addAction("Open Mbox Control").triggered.connect(self.show_window)
        menu.addSeparator()
        self.tray_mute = menu.addAction("Mute speakers")
        self.tray_mute.setCheckable(True)
        self.tray_mute.setChecked(bool(self.app_state.get("spk_mute")))
        self.tray_mute.toggled.connect(self.mute_speakers)
        spk = menu.addMenu("Speakers")
        for L in "ABC":
            spk.addAction(f"Speakers {L}").triggered.connect(lambda _=False, L=L: self.switch_speakers(L))
        self.tray_scenes = menu.addMenu("Scenes")
        self.fill_tray_scenes()
        menu.addSeparator()
        menu.addAction("Recover Mbox…").triggered.connect(lambda: self.recover_mbox())
        menu.addAction("Quit").triggered.connect(self.quit_app)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_clicked)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()
        QtWidgets.QApplication.setQuitOnLastWindowClosed(False)

    def fill_tray_scenes(self):
        if not self.tray:
            return
        self.tray_scenes.clear()
        for sc in SCENES:
            self.tray_scenes.addAction(sc["name"].strip()).triggered.connect(
                lambda _=False, sc=sc: self.run_scene(sc))
        names = self.user_scenes()
        if names:
            self.tray_scenes.addSeparator()
            for n in names:
                self.tray_scenes.addAction("⭐ " + n).triggered.connect(
                    lambda _=False, n=n: self.apply_user_scene(n))

    def tray_clicked(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            q = getattr(self, "quick", None)
            if q and q.isVisible():
                q.hide()
            elif q and time.time() - q.hidden_at < 0.4:
                pass                                # this click is what just closed it
            else:
                self.show_quick_panel()
        elif reason in (QtWidgets.QSystemTrayIcon.MiddleClick, QtWidgets.QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self.spectrum_hub.stop_all()
        self.sleep_watch.release_inhibitor()
        if self.udev:
            self.udev.stop()
        self.quitting = True
        self.close()
        QtWidgets.QApplication.quit()


    # ---------------------------------------------------------------- 4.3: your setup helpers
    def main_output_name(self):
        return self.app_state["setup"].get("main_name") or DEFAULT_MAIN

    def rec_source(self):
        """(left, right, label) the Record button captures. Mono sources go to both sides."""
        src = self.app_state.get("rec_src") or f"pair:{self.app_state['setup']['rip']}"
        kind, item = src.split(":", 1)
        names = [j for j, _ in INPUT_JACKS]
        jack = dict(INPUT_JACKS).get(item, item)
        own = self.label(f"in{names.index(item)}", "") if item in names else ""
        if kind == "mono":
            return item, item, f"{jack} (mono, centred)" + (f" — {own}" if own else "")
        return item, pair_right(item), pair_label(item) + (f" — {own}" if own else "")

    def set_rec_source(self, src):
        self.app_state["rec_src"] = src
        self.save_app_state()
        if hasattr(self, "rec_src_box"):
            self.rec_src_box.blockSignals(True)
            self.rec_src_box.setCurrentIndex(max(0, self.rec_src_box.findData(src)))
            self.rec_src_box.blockSignals(False)
        if hasattr(self, "rec_meters"):
            self.rewire_rec_meters()
        if hasattr(self, "auto_rec") and not self.recorder.running():
            self.auto_rec.setChecked(src.startswith("pair:"))     # hands-free is for tapes and records
        if hasattr(self, "rec_info") and not self.recorder.running():
            self.rec_info.setText("Source: " + self.rip_name())

    def rip_name(self):
        return self.rec_source()[2]

    def scene_view(self, s):
        v = dict(s)
        amp = self.label("out0", "") or "your amplifier"
        sub = lambda t: t.replace("your amplifier", amp) if amp != "your amplifier" else t  # noqa: E731
        v["short"], v["what"], v["check"] = sub(v["short"]), sub(v["what"]), [sub(c) for c in v["check"]]
        if s["id"] == "guitar":
            who = " + ".join(self.guitar_names())
            v["short"] = f"{who} → Guitarix → speakers"
            v["what"] = (f"Starts guitar mode: lowest latency, dry signal muted, your effects app opened and "
                         f"wired up ({who} → app → speakers). Pick the inputs on the Effects page.")
            v["check"] = [("Instruments into front Mic/DI 1 and 2 (Front/Rear button OUT, gain knob pushed IN)."
                           if any(int(i[-1]) <= 2 for i in self.guitar_inputs()) else
                           "Instruments into the rear inputs 3/4 need a DI box or a preamp."),
                          "Turn each instrument's own volume up.",
                          "When you're done: Effects page → Stop guitar mode."]
            v["jacks"] = [f"front{int(i[-1])}" if int(i[-1]) <= 2 else f"mic{i[-1]}" for i in self.guitar_inputs()] \
                + ["out1", "out2"]
        if s["id"] == "rip":
            st = self.app_state["setup"]
            v["short"] = f"{pair_label(st['rip'])} → computer"
            v["what"] = (f"{self.rip_name()} → computer for recording"
                         + (", and you hear it through Outputs 1/2." if st.get("rip_monitor") else
                            ". You listen through your own stereo, not the Mbox."))
            n = int(st["rip"][-1])
            v["jacks"] = ["aux5", "aux6"] if n == 5 else [f"line{n}", f"line{n + 1}"]
            v["check"] = ([st["rip_tip"]] if st.get("rip_tip") else []) + [
                "Then open the Record page and hit Record. Aim for peaks around −6 dB."]
        return v

    def route_rip_input(self):
        """Capture channels 5/6 always carry the rip input — that's what “Rip input (Mbox)” records."""
        left, right, _ = self.rec_source()
        self.set_stream(4, left)
        self.set_stream(5, right)

    def rewire_rec_meters(self):
        ids = {id(m) for _, m in self.rec_meters}
        self.meters = [x for x in self.meters if id(x[2]) not in ids]
        for entry, item in zip(self.rec_meters, self.rec_source()[:2]):
            entry[0] = item
            slot = self.slot_of(item)
            if slot is not None:
                self.meters.append(("mixer-source-meter", slot, entry[1]))

    def always_on_muted(self):
        for left in self.app_state["setup"].get("always_on", []):
            slot = self.slot_of(left)
            if slot is not None and self.pair_source(0)[:2] == ("mix", 1) and max(self.mix_lr(1, slot)) == 0:
                return pair_label(left)
        return ""

    def restore_always_on(self):
        for left in self.app_state["setup"].get("always_on", []):
            self.mix_put(1, left, UNITY, 0)
            self.mix_put(1, pair_right(left), 0, UNITY)

    def restore_always_on_now(self):
        self.restore_always_on()
        self.reload_state()
        self.run_refreshers()
        self.refresh_health()

    def save_setup(self):
        self.save_app_state()
        self.rewire_rec_meters()
        self.rebuild_scene_grid()
        self.run_refreshers()
        self.refresh_health()

    # ================================================================ YOUR SETUP
    def _build_setup(self):
        w, lay = page("Your setup", "Tell the app what's plugged in where. Names show up on Home, in the "
                                    "mixer, the scenes and the recorder — nothing here changes the sound.")
        st = self.app_state["setup"]

        gc, gl = card("What's plugged into each input")
        g = QtWidgets.QGridLayout()
        for r, (item, jack) in enumerate(INPUT_JACKS):
            g.addWidget(QtWidgets.QLabel(f"<b>{jack}</b>"), r, 0)
            e = QtWidgets.QLineEdit(self.label(f"in{r}", ""))
            e.setPlaceholderText("e.g. Karaoke box, Receiver tape out, Guitar…")
            e.setToolTip("A name for whatever is plugged into this jack.")
            e.editingFinished.connect(lambda r=r, e=e: (self.set_label(f"in{r}", e.text().strip()),
                                                        self.save_setup()))
            g.addWidget(e, r, 1)
            cb = QtWidgets.QToolButton()                # 4.18 colour tag
            cb.setFixedSize(22, 22)
            cb.setToolTip("Colour tag — shown on the mixer, the compact mixer and the signal map.")
            col = self.item_colour(item)
            cb.setStyleSheet(f"background:{col}; border-radius:9px;" if col else
                             f"border:1px dashed {SUBTLE}; border-radius:9px;")
            cb.clicked.connect(lambda _=False, it=item, b=cb: self.pick_colour(it, b))
            g.addWidget(cb, r, 2)
            if r < 4:                                   # two jacks share this input: which one is in use?
                side = QtWidgets.QComboBox()
                side.addItems(["Front jack", "Rear jack"] if r < 2 else ["Mic (XLR)", "Line (¼\")"])
                side.setToolTip("Match the " + ("F/R" if r < 2 else "Mic/Ln") + " button on the Mbox — "
                                "each jack keeps its own name. The app can't see that button.")

                def sync(r=r, side=side, e=e):
                    side.blockSignals(True)
                    side.setCurrentIndex(self.input_side(r) == "b")
                    side.blockSignals(False)
                    if not e.hasFocus():
                        e.setText(self.label(f"in{r}", ""))
                sync()
                side.currentIndexChanged.connect(lambda i, r=r: self.set_input_side(r, "ab"[i]))
                self.refreshers.append(sync)
                g.addWidget(side, r, 3)
        g.setColumnStretch(1, 1)
        gl.addLayout(g)
        lay.addWidget(gc)

        oc, ol = card("What each output feeds")
        g = QtWidgets.QGridLayout()
        for r, (name, first, _) in enumerate(OUTPUT_PAIRS):
            g.addWidget(QtWidgets.QLabel(f"<b>{name}</b>"), r, 0)
            e = QtWidgets.QLineEdit(self.label(f"out{first}", ""))
            e.setPlaceholderText("e.g. Receiver, Tape deck record in, Studio monitors…")
            e.setToolTip("A name for what this output is cabled to.")
            e.editingFinished.connect(lambda f=first, e=e: (self.set_label(f"out{f}", e.text().strip()),
                                                            self.save_setup()))
            g.addWidget(e, r, 1)
        g.setColumnStretch(1, 1)
        ol.addLayout(g)
        nrow = QtWidgets.QHBoxLayout()
        nrow.addWidget(QtWidgets.QLabel("Name in your sound settings:"))
        main = QtWidgets.QLineEdit(self.main_output_name())
        main.setToolTip("What the computer's main Mbox output is called in KDE's sound settings and apps.")
        nrow.addWidget(main, 1)
        rn = QtWidgets.QPushButton("Rename")
        rn.setToolTip("Renames the output in your sound settings. PipeWire restarts for a second.")

        rn.clicked.connect(lambda: self.rename_main_output(main.text().strip()))
        self.main_name_edit = main
        nrow.addWidget(rn)
        ol.addLayout(nrow)
        lay.addWidget(oc)

        rc, rl = card("Ripping", "Which input the Record button records from.")
        combo = QtWidgets.QComboBox()
        for left, _, name in INPUT_PAIRS:
            combo.addItem(name, left)
        combo.setCurrentIndex(max(0, combo.findData(st["rip"])))
        combo.setToolTip("The stereo pair your tape deck, turntable or receiver's record-out is plugged into.")
        combo.currentIndexChanged.connect(lambda _: (st.__setitem__("rip", combo.currentData()), self.save_setup()))
        f = QtWidgets.QFormLayout()
        f.addRow("Record from", combo)
        mon = QtWidgets.QCheckBox("Also play it through Outputs 1/2 while recording")
        mon.setChecked(bool(st.get("rip_monitor")))
        mon.setToolTip("Turn this OFF if the rip input comes from your receiver — playing it back into "
                       "the receiver makes a feedback loop.")
        mon.toggled.connect(lambda on: (st.__setitem__("rip_monitor", on), self.save_setup()))
        f.addRow("", mon)
        tip = QtWidgets.QLineEdit(st.get("rip_tip", ""))
        tip.setPlaceholderText("e.g. Receiver: TAPE MONITOR on SOURCE, input set to AUX")
        tip.setToolTip("Your own reminder, shown whenever you start the Rip scene.")
        tip.editingFinished.connect(lambda: (st.__setitem__("rip_tip", tip.text().strip()), self.save_setup()))
        f.addRow("Reminder", tip)
        app_cb = QtWidgets.QComboBox()
        app_cb.addItem("Open the Record page (built in: splits, tags, cleans up)", "builtin")
        app_cb.addItem("Open Audacity (for hands-on editing)", "audacity")
        app_cb.setCurrentIndex(max(0, app_cb.findData(st.get("rip_app", "builtin"))))
        app_cb.setToolTip("What happens after the Rip scene sets up the Mbox.")
        app_cb.currentIndexChanged.connect(lambda _: (st.__setitem__("rip_app", app_cb.currentData()),
                                                      self.save_setup()))
        f.addRow("Then", app_cb)
        rl.addLayout(f)
        lay.addWidget(rc)

        ac, al = card("Always on", "Inputs that should always play through Outputs 1/2, like a TV or "
                                   "karaoke box. Scenes leave them alone, and Home warns you if they "
                                   "get turned down.")
        for left, _, name in INPUT_PAIRS:
            cb = QtWidgets.QCheckBox(name)
            cb.setChecked(left in st.get("always_on", []))
            cb.setToolTip("Keep this pair turned up in Stereo Mix 1 (the speakers).")

            def tog(on, left=left):
                lst = [x for x in st.get("always_on", []) if x != left] + ([left] if on else [])
                st["always_on"] = lst
                self.save_setup()
            cb.toggled.connect(tog)
            al.addWidget(cb)
        lay.addWidget(ac)

        lc, ll = card("Music library", "After “Save tracks as FLAC”, also copy the album here — for "
                                       "example your NAS's music inbox — and optionally run an import "
                                       "command such as beets.")
        f = QtWidgets.QFormLayout()
        lib = QtWidgets.QLineEdit(self.app_state.get("library_dir", ""))
        lib.setPlaceholderText("Folder, e.g. a mounted NAS share (leave empty to skip)")
        lib.editingFinished.connect(lambda: (self.app_state.__setitem__("library_dir", lib.text().strip()),
                                             self.save_app_state()))
        brow = QtWidgets.QHBoxLayout()
        brow.addWidget(lib, 1)
        bb = QtWidgets.QPushButton("Browse…")
        bb.clicked.connect(lambda: (lambda d: d and (lib.setText(d), lib.editingFinished.emit()))(
            QtWidgets.QFileDialog.getExistingDirectory(self, "Library inbox", lib.text() or str(HOME))))
        brow.addWidget(bb)
        f.addRow("Copy to", brow)
        cmd = QtWidgets.QLineEdit(self.app_state.get("library_cmd", ""))
        cmd.setPlaceholderText("Optional, e.g. ssh nas docker exec beets beet import -q {path}")
        cmd.setToolTip("Runs after the copy. {path} becomes the copied album folder.")
        cmd.editingFinished.connect(lambda: (self.app_state.__setitem__("library_cmd", cmd.text().strip()),
                                             self.save_app_state()))
        f.addRow("Then run", cmd)
        lay_cb = QtWidgets.QComboBox()
        lay_cb.addItem("Artist / Album / 01 - Title.flac", "artist")
        lay_cb.addItem("A / Artist / Album / 01 Title.flac  (letter folders)", "letter")
        lay_cb.setCurrentIndex(max(0, lay_cb.findData(self.app_state.get("layout", "artist"))))
        lay_cb.setToolTip("How saved albums are arranged — pick the one that matches your existing library.")
        lay_cb.currentIndexChanged.connect(lambda _: (self.app_state.__setitem__("layout", lay_cb.currentData()),
                                                      self.save_app_state()))
        f.addRow("Folder layout", lay_cb)
        dm = QtWidgets.QCheckBox("Add the release to a “Ripped” folder in my Discogs collection")
        dm.setChecked(bool(self.app_state.get("discogs_mark")))
        dm.setToolTip("Only when you looked the album up on Discogs. Needs your token in Settings.")
        dm.toggled.connect(lambda on: (self.app_state.__setitem__("discogs_mark", on), self.save_app_state()))
        f.addRow("", dm)
        ll.addLayout(f)
        lay.addWidget(lc)
        hc, hl = card("Suggestions")
        ph = QtWidgets.QCheckBox("Suggest what to do when sound shows up on an input that isn't going anywhere")
        ph.setChecked(bool(self.app_state.get("plug_hints", True)))
        ph.setToolTip("Plug in a guitar or press play on the deck and the app offers to let you hear it, "
                      "rip it or record it. Stays quiet while another app is recording.")
        ph.toggled.connect(lambda on: (self.app_state.__setitem__("plug_hints", on), self.save_app_state()))
        hl.addWidget(ph)
        hrow = QtWidgets.QHBoxLayout()
        again = QtWidgets.QPushButton("Run the setup again")
        again.clicked.connect(self.run_setup_wizard)
        unig = QtWidgets.QPushButton("Ask about every input again")
        unig.setToolTip("Clears the inputs you told it to stop suggesting things for.")
        unig.clicked.connect(lambda: (self.app_state.__setitem__("plug_ignore", []), self.save_app_state(),
                                      self.hinted.clear(), self.toast("OK — all inputs are watched again.")))
        hrow.addWidget(again)
        hrow.addWidget(unig)
        hrow.addStretch(1)
        hl.addLayout(hrow)
        lay.addWidget(hc)
        lay.addStretch(1)
        return scroll(w)

    # ---------------------------------------------------------------- 4.3: rip clean-up
    def save_cleanup(self):
        self.app_state["cleanup"] = [k for k, cb in self.cleanup_boxes.items() if cb.isChecked()]
        self.save_app_state()

    def show_spectrogram(self):
        src = self.split_src.text().strip()
        if not src or not Path(src).exists():
            self.split_status.setText("Open or record something first.")
            return
        out = Path(src).with_suffix(".spectrogram.png")
        self.split_status.setText("Drawing the spectrogram…")
        self.specjob = SpectrogramJob(src, out)
        self.specjob.done.connect(self.spectrogram_ready)
        self.specjob.start()

    def spectrogram_ready(self, path, err):
        if not path:
            self.split_status.setText(f"Couldn't draw it: {err}")
            return
        self.split_status.setText("")
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Spectrogram — " + Path(path).stem.replace(".spectrogram", ""))
        v = QtWidgets.QVBoxLayout(dlg)
        pic = QtWidgets.QLabel()
        pic.setPixmap(QtGui.QPixmap(path).scaledToWidth(1100, Qt.SmoothTransformation))
        v.addWidget(pic)
        v.addWidget(muted("Time runs left to right, pitch bottom to top, brightness = loudness. "
                          "Thin bright vertical lines = clicks. A haze across the top = hiss. If the "
                          "music fades out well below the top, the transfer is dull (dirty heads, "
                          "worn stylus or a lossy source)."))
        dlg.resize(1140, 560)
        dlg.exec()

    # ---------------------------------------------------------------- 4.3: tape tools
    def _build_tape_tools(self, lay):
        c, l = card("Make a tape", "Set your deck's record level with a test tone, and plan which songs "
                                   "fit on each side.")
        row = QtWidgets.QHBoxLayout()
        for label, freq, db in (("Play 1 kHz", 1000, -18), ("Play 10 kHz", 10000, -20)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, f=freq, d=db: self.play_tone(f, d))
            row.addWidget(b)
        stop = QtWidgets.QPushButton("Stop tone")
        stop.clicked.connect(self.stop_tone)
        row.addWidget(stop)
        row.addStretch(1)
        l.addLayout(row)
        l.addWidget(muted("Run the Make a tape scene first. Then, with the 1 kHz tone playing, set the "
                          "deck's record level so its meters sit at 0 dB. That leaves room for loud "
                          "parts without distorting."))
        prow = QtWidgets.QHBoxLayout()
        self.tape_len = QtWidgets.QComboBox()
        for name, mins in TAPE_LENGTHS:
            self.tape_len.addItem(f"{name} ({mins} min a side)", mins)
        self.tape_len.setCurrentIndex(2)
        prow.addWidget(self.tape_len)
        pick = QtWidgets.QPushButton("Plan sides…")
        pick.clicked.connect(self.plan_tape)
        prow.addWidget(pick)
        jc = QtWidgets.QPushButton("Make a J-card…")
        jc.setToolTip("Design the tape's paper insert — cover, spine, track list — and print it at actual size "
                      "with cut and fold marks. Uses the planned sides if you've planned them.")
        jc.clicked.connect(self.open_jcard)
        prow.addWidget(jc)
        prow.addStretch(1)
        l.addLayout(prow)
        self.plan_out = muted("")
        self.plan_out.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l.addWidget(self.plan_out)
        lay.addWidget(c)

    def open_jcard(self):
        JCardDialog(self, getattr(self, "tape_sides", {"A": [], "B": []}), getattr(self, "tape_files", [])).exec()
        self.save_app_state()

    def play_tone(self, freq, dbfs):
        self.stop_tone()
        if not shutil.which("pw-play"):
            QtWidgets.QMessageBox.warning(self, APP_NAME, "pw-play isn't installed (package: pipewire).")
            return
        path = CONF_DIR / f"tone-{freq}.wav"
        if not path.exists():
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            make_tone(path, freq, dbfs, secs=60, rate=48000)
        target = ["--target", MAIN_SINK] if pw_node_exists(MAIN_SINK, "sink") else []
        self.tone_proc = subprocess.Popen(["pw-play", *target, str(path)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.toast(f"Playing {freq // 1000} kHz at {dbfs} dBFS for a minute.", 6000)

    def stop_tone(self):
        if self.tone_proc and self.tone_proc.poll() is None:
            self.tone_proc.terminate()
        self.tone_proc = None

    def plan_tape(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Songs for the tape, in order", str(HOME / "Music"),
            "Audio (" + " ".join(f"*{e}" for e in sorted(AUDIO_EXT)) + ")")
        if not files:
            return
        self.plan_out.setText("Measuring songs…")
        self.prober = TrackProber(files)
        self.prober.done.connect(self.show_plan)
        self.prober.start()

    def show_plan(self, tracks):
        mins = self.tape_len.currentData()
        self.tape_files = [p for p, _ in tracks]
        names = {}
        for p, _ in tracks:                              # "Title — Artist" from the tags, for the J-card too
            t, a_ = track_tags(p)
            names[p] = f"{t} — {a_}" if a_ else t
        a, b, over = plan_sides([(names[p], d) for p, d in tracks], mins * 60)
        self.tape_sides = {"A": a, "B": b}

        def side(name, items):
            tot = sum(d for _, d in items)
            left = mins * 60 - tot - 2 * max(0, len(items) - 1)
            rows = "".join(f"<br>&nbsp;&nbsp;{fmt_time(d)} &nbsp;{html_escape(n)}" for n, d in items)
            return (f"<b>Side {name}</b> — {fmt_time(tot)} used, {fmt_time(max(0, left))} spare{rows}")
        txt = side("A", a) + "<br><br>" + side("B", b)
        if over:
            txt += "<br><br><b style='color:#f85149'>Doesn't fit:</b>" + "".join(
                f"<br>&nbsp;&nbsp;{fmt_time(d)} &nbsp;{html_escape(n)}" for n, d in over)
        txt += "<br><br>Leave a few seconds of leader at the start of each side."
        self.plan_out.setText(txt)

    # ---------------------------------------------------------------- 4.3: guitar mode
    def _build_guitar_card(self, lay):
        c, l = card("Guitar mode", "One click: lowest safe latency, your dry guitar muted, your effects "
                                   "app opened. Pick which inputs your instruments are in below.")
        row = QtWidgets.QHBoxLayout()
        self.guitar_app = QtWidgets.QComboBox()
        self.guitar_app.currentIndexChanged.connect(lambda _: (self.app_state.__setitem__(
            "effects_app", self.guitar_app.currentData() or ""), self.save_app_state()))
        row.addWidget(self.guitar_app)
        self.guitar_btn = QtWidgets.QPushButton("Start guitar mode")
        self.guitar_btn.setObjectName("primary")
        self.guitar_btn.clicked.connect(self.toggle_guitar)
        row.addWidget(self.guitar_btn)
        self.gx_install = QtWidgets.QPushButton("Install Guitarix")
        self.gx_install.setToolTip("Installs Guitarix plus PipeWire's JACK support. Asks for your password once.")
        self.gx_install.clicked.connect(self.install_guitarix)
        row.addWidget(self.gx_install)
        row.addStretch(1)
        l.addLayout(row)
        self.refresh_effect_apps()
        irow = QtWidgets.QHBoxLayout()
        irow.addWidget(QtWidgets.QLabel("Instruments on:"))
        self.gx_boxes = {}
        for n in range(1, 5):
            item = f"Analog-A-{n}"
            own = self.label(f"in{n - 1}", "")
            cb = QtWidgets.QCheckBox(f"Input {n}" + (f" ({own})" if own else ""))
            cb.setChecked(item in self.guitar_inputs())
            cb.setToolTip("Each ticked input gets its own Guitarix — its own amp and pedals — so a guitar "
                          "and a bass (or two guitars) can play at the same time.")
            cb.toggled.connect(self.guitar_inputs_changed)
            irow.addWidget(cb)
            self.gx_boxes[item] = cb
        irow.addStretch(1)
        l.addLayout(irow)
        l.addWidget(muted("Guitar mode opens your effects app and connects each instrument → app → speakers "
                          "by itself. Guitarix has its own tuner, and there's one on the Record page too."))
        lay.addWidget(c)

    def guitar_inputs(self):
        """Inputs played through the effects app (1–4; each gets its own Guitarix)."""
        v = [x for x in self.app_state.get("guitar_inputs", ["Analog-A-1"])
             if x in {f"Analog-A-{n}" for n in range(1, 5)}]
        return sorted(v) or ["Analog-A-1"]

    def gx_name(self, item):
        """Guitarix instance per input: the first keeps the default name (and your presets)."""
        return "gx_head" if item == self.guitar_inputs()[0] else f"gx_in{item[-1]}"

    def guitar_names(self):
        jacks = dict(INPUT_JACKS)
        return [self.label(f"in{int(i[-1]) - 1}", "") or jacks.get(i, i) for i in self.guitar_inputs()]

    def wire_guitarix(self):
        """Input n → its Guitarix amp, every Guitarix → the main output, once they're all up."""
        outs = run_quiet("pw-link", "-o") or ""
        src = mbox_pw_node("source")
        names = [(item, self.gx_name(item)) for item in self.guitar_inputs()]
        if not src or any(f"{n}_fx:out_0" not in outs for _, n in names):
            self.gx_tries += 1
            if self.gx_tries < 25:
                QtCore.QTimer.singleShot(1000, self.wire_guitarix)
            elif src:
                self.toast("Some Guitarix windows didn't open — wire them in qpwgraph.", 8000)
            return
        links = run_quiet("pw-link", "-l") or ""
        for item, n in names:
            aux = f"{src}:capture_AUX{int(item[-1]) - 1}"
            for a, b in ((aux, f"{n}_amp:in_0"), (f"{n}_fx:out_0", "mbox_main:playback_FL"),
                         (f"{n}_fx:out_1", "mbox_main:playback_FR")):
                run_quiet("pw-link", a, b)
            if aux in links and f"{n}_fx:in_0" in links:      # JACK auto-connect sometimes skips the amp
                run_quiet("pw-link", "-d", aux, f"{n}_fx:in_0")
        self.toast("Guitarix is wired up: " + ", ".join(self.guitar_names()) + " → amp → speakers.", 6000)

    def guitar_inputs_changed(self):
        chosen = [i for i, cb in self.gx_boxes.items() if cb.isChecked()]
        if not chosen:                                 # always at least one
            self.gx_boxes["Analog-A-1"].setChecked(True)
            return
        self.app_state["guitar_inputs"] = chosen
        self.save_app_state()
        self.rebuild_scene_grid()
        if self.guitar_on:
            self.toast("Changed — stop and start guitar mode to rewire.", 6000)

    def refresh_effect_apps(self):
        apps = find_effect_apps()
        ready = bool(apps) and bool(shutil.which("pw-jack"))
        self.gx_install.setVisible(not ready)
        cur = self.app_state.get("effects_app", "")
        self.guitar_app.blockSignals(True)
        self.guitar_app.clear()
        for cmd, name in apps:
            self.guitar_app.addItem(name, cmd)
        if not apps:
            self.guitar_app.addItem("No effects app yet", "")
        self.guitar_app.setCurrentIndex(max(0, self.guitar_app.findData(cur)))
        self.guitar_app.blockSignals(False)

    def install_guitarix(self):
        if not install_argv("guitarix") or not shutil.which("pkexec"):
            QtWidgets.QMessageBox.information(self, APP_NAME, "Install Guitarix and PipeWire's JACK support "
                                              "with your system's package manager, then come back.")
            return
        self.gx_install.setEnabled(False)
        self.gx_install.setText("Installing…")
        self.gx_proc = QtCore.QProcess(self)
        self.gx_proc.finished.connect(self.guitarix_installed)
        self.gx_proc.start("pkexec", install_argv("guitarix"))

    def guitarix_installed(self, code, _status):
        self.gx_install.setEnabled(True)
        self.gx_install.setText("Install Guitarix")
        if code == 0:
            self.refresh_effect_apps()
            self.toast("Guitarix installed — hit Start guitar mode.", 8000)
        else:
            err = bytes(self.gx_proc.readAllStandardError()).decode(errors="ignore").strip()[-300:]
            QtWidgets.QMessageBox.warning(self, APP_NAME, "The install didn't finish.\n\n" + (err or
                                          "Cancelled, or the package manager asked something only a terminal can answer. "
                                          "Try:  " + install_hint("guitarix")))

    def toggle_guitar(self):
        if not self.guitar_on:
            sc = next(x for x in SCENES if x["id"] == "guitar")
            self._run_scene(sc)
            pw_set("clock.force-quantum", 64)
            cmd = self.guitar_app.currentData()
            if cmd and not shutil.which("pw-jack"):
                QtWidgets.QMessageBox.warning(
                    self, APP_NAME, "Effects apps need PipeWire's JACK support, or they start a real "
                    "JACK server that grabs the Mbox and silences everything.\n\nInstall it once:\n" +
                    install_hint("pipewire-jack") + "\n\nThen try Guitar mode again.")
                cmd = None
            if cmd == "guitarix":
                running = run_quiet("pw-link", "-o") or ""
                for item in self.guitar_inputs():
                    n = self.gx_name(item)
                    if f"{n}_fx:out_0" not in running:     # don't open a second copy of one already up
                        subprocess.Popen(["pw-jack", "guitarix", "-n", n], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
            elif cmd:
                subprocess.Popen(["pw-jack", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            if cmd:
                if cmd == "guitarix":
                    self.gx_tries = 0
                    QtCore.QTimer.singleShot(1500, self.wire_guitarix)
            self.guitar_on = True
            self.guitar_btn.setText("Stop guitar mode")
            self.toast("Guitar mode on at the lowest latency. Crackles? Settings → Buffer → 128.", 9000)
        else:
            pw_set("clock.force-quantum", 0)
            self.undo_scene()
            self.guitar_on = False
            self.guitar_btn.setText("Start guitar mode")

    # ================================================================ APPS
    def _build_apps(self):
        w, lay = page("Apps", "Everything playing sound right now, and where it goes. Send a game to the "
                              "headphones while music stays on the speakers.")
        c, l = card("Playing now")
        self.apps_box = QtWidgets.QGridLayout()
        self.apps_box.setColumnStretch(1, 1)
        l.addLayout(self.apps_box)
        self.apps_empty = muted("Nothing is playing.")
        l.addWidget(self.apps_empty)
        lay.addWidget(c)
        dc, dl = card("Default output", "Where new apps play unless you move them.")
        self.default_sink = QtWidgets.QComboBox()
        self.default_sink.setToolTip("Same as choosing the default device in KDE's audio settings.")
        self.default_sink.activated.connect(lambda _: run_quiet(
            "pactl", "set-default-sink", self.default_sink.currentData()))
        dl.addWidget(self.default_sink)
        lay.addWidget(dc)
        ac, al = card("AirPlay speakers", "Play on AirPlay speakers around the house (WiiM, HomePod, AirPort "
                                          "Express, AirPlay receivers). They show up in the lists above like "
                                          "any other output.")
        arow = QtWidgets.QHBoxLayout()
        self.airplay_btn = QtWidgets.QPushButton("Turn on AirPlay")
        self.airplay_btn.setToolTip("Sets up everything AirPlay needs: speaker discovery (Avahi), the "
                                    "firewall rules for speakers on your home network, and PipeWire's AirPlay "
                                    "support. Asks for your password once.")
        self.airplay_btn.clicked.connect(self.toggle_airplay)
        arow.addWidget(self.airplay_btn)
        self.airplay_note = muted("")
        arow.addWidget(self.airplay_note, 1)
        al.addLayout(arow)
        self.airplay_grid = QtWidgets.QGridLayout()
        self.airplay_grid.setColumnStretch(0, 1)
        al.addLayout(self.airplay_grid)
        al.addWidget(muted("AirPlay plays about 1½ seconds behind, so use it for other rooms rather than "
                           "next to your main speakers. “Also play” copies sound to a speaker while it keeps "
                           "playing here. Computer sound copies as-is; for the turntable, tape or karaoke box pick its "
                           "named input (Record → Tools → Named inputs)."))
        lay.addWidget(ac)
        self.mirrors, self.airplay_sig = {}, None
        self.mirror_timer = QtCore.QTimer(self)          # keep "also play" copies alive with the window closed
        self.mirror_timer.timeout.connect(lambda: self.app_state.get("airplay_mirror") and
                                          self.keep_mirrors(airplay_status()["speakers"]))
        self.mirror_timer.start(10000)
        QtCore.QTimer.singleShot(4000, self.mirror_timer.timeout.emit)
        lay.addStretch(1)
        self.apps_timer = QtCore.QTimer(self)
        self.apps_timer.timeout.connect(self.refresh_apps)
        self.apps_timer.start(3000)
        self.apps_sig = None
        QtCore.QTimer.singleShot(0, lambda: self.nav.currentRowChanged.connect(
            lambda _: QtCore.QTimer.singleShot(50, self.refresh_apps)))
        return scroll(w)

    def run_root(self, argv, label, done):
        """Run argv as root (pkexec) in the background. The window stays usable; done(ok, message)."""
        if not shutil.which("pkexec"):
            done(False, "pkexec (polkit) isn't installed.")
            return
        dlg = QtWidgets.QProgressDialog(label + "\n\nEnter your password if asked — the prompt can open "
                                        "behind this window.", None, 0, 0, self)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.NonModal)
        dlg.setMinimumDuration(0)
        dlg.setCancelButton(None)
        dlg.show()
        proc = QtCore.QProcess(self)
        self.root_jobs = getattr(self, "root_jobs", [])
        self.root_jobs.append(proc)

        def finished(code=-1, _status=None):
            if proc not in self.root_jobs:
                return
            self.root_jobs.remove(proc)
            dlg.close()
            err = bytes(proc.readAllStandardError()).decode(errors="ignore").strip()[-300:]
            done(code == 0, err if code != 0 else "")
        proc.finished.connect(finished)
        proc.errorOccurred.connect(lambda e: e == QtCore.QProcess.FailedToStart and finished(-1))
        proc.start("pkexec", list(argv))

    def toggle_airplay(self):
        if airplay_status()["enabled"]:
            for k in list(self.mirrors):
                self.stop_mirror(k)
            disable_airplay()
            self.app_state["airplay_mirror"] = {}
            self.save_app_state()
            self.toast("AirPlay turned off.", 5000)
        else:
            def done(ok, msg):
                if not ok:
                    QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't turn on AirPlay." +
                                                  (f"\n\n{msg}" if msg else ""))
                else:
                    finish_airplay()
                    self.toast("AirPlay is on — speakers appear within a few seconds.", 8000)
                self.airplay_sig = None
                self.refresh_airplay()
            self.airplay_note.setText("Setting up…")
            cmd = airplay_root_cmd()
            if cmd:
                self.run_root(cmd, "Setting up AirPlay (speaker discovery and firewall)…", done)
            else:
                done(True, "")
            return
        self.airplay_sig = None
        self.refresh_airplay()

    def stop_mirror(self, sink):
        p = self.mirrors.pop(sink, None)
        if p and p.poll() is None:
            p.terminate()

    def keep_mirrors(self, speakers):
        """(Re)start the 'also play' copies for speakers that are on the network right now."""
        want = self.app_state.get("airplay_mirror", {})
        names = {sp["name"] for sp in speakers}
        for sink in list(self.mirrors):
            if sink not in want or sink not in names:
                self.stop_mirror(sink)
        for sink, key in want.items():
            if sink in names and (sink not in self.mirrors or self.mirrors[sink].poll() is not None):
                try:
                    self.mirrors[sink] = start_mirror(key, sink)
                except OSError:
                    pass

    def set_mirror(self, sink, key):
        want = self.app_state.setdefault("airplay_mirror", {})
        if key:
            want[sink] = key
        else:
            want.pop(sink, None)
        self.save_app_state()
        self.stop_mirror(sink)
        self.keep_mirrors(airplay_status()["speakers"])

    def refresh_airplay(self):
        if not hasattr(self, "airplay_grid"):
            return
        st = airplay_status()
        self.keep_mirrors(st["speakers"])
        sig = repr(st)
        if sig == self.airplay_sig:
            return
        self.airplay_sig = sig
        self.airplay_btn.setText("Turn off AirPlay" if st["enabled"] else "Turn on AirPlay")
        if not st["enabled"]:
            self.airplay_note.setText("Off.")
        elif not st["avahi_on"]:
            self.airplay_note.setText("Speaker discovery (Avahi) isn't running — turn AirPlay off and on again.")
        elif not st["speakers"]:
            self.airplay_note.setText("Looking for speakers… make sure they're on and on the same Wi-Fi.")
        else:
            self.airplay_note.setText(f"{len(st['speakers'])} speaker{'s' if len(st['speakers']) != 1 else ''} "
                                      "found.")
        while self.airplay_grid.count():
            it = self.airplay_grid.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        srcs = mirror_sources(self.main_output_name())
        want = self.app_state.get("airplay_mirror", {})
        for r, sp in enumerate(st["speakers"]):
            self.airplay_grid.addWidget(QtWidgets.QLabel(f"📶  <b>{html_escape(sp['desc'])}</b>"), r, 0)
            cb = QtWidgets.QComboBox()
            cb.setToolTip("Copy sound to this speaker while it keeps playing on the Mbox.")
            cb.addItem("Also play: nothing", "")
            for key, label in srcs:
                cb.addItem(f"Also play: {label}", key)
                if want.get(sp["name"]) == key:
                    cb.setCurrentIndex(cb.count() - 1)
            cb.activated.connect(lambda _, n=sp["name"], cb=cb: self.set_mirror(n, cb.currentData()))
            self.airplay_grid.addWidget(cb, r, 1)
            pb = QtWidgets.QPushButton("Make default")
            pb.setToolTip("New apps play here (you can still move each app above).")
            pb.clicked.connect(lambda _, n=sp["name"]: (run_quiet("pactl", "set-default-sink", n),
                                                         setattr(self, "apps_sig", None), self.refresh_apps()))
            self.airplay_grid.addWidget(pb, r, 2)

    def refresh_apps(self):
        if not self.isVisible() or not self.apps_box.parentWidget() or \
                not self.apps_box.parentWidget().isVisible():
            return
        if not shutil.which("pactl"):
            self.apps_empty.setText("pactl isn't installed (package: libpulse).")
            return
        self.refresh_airplay()
        streams, sinks = list_app_streams(), list_sinks()
        sig = repr((streams, sinks))
        if sig == self.apps_sig:
            return
        self.apps_sig = sig
        while self.apps_box.count():
            it = self.apps_box.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        for r, s in enumerate(streams):
            ic = QtGui.QIcon.fromTheme(s["icon"]) if s["icon"] else QtGui.QIcon()
            lbl = QtWidgets.QLabel(f"<b>{html_escape(s['app'])}</b><br><span style='color:{SUBTLE}'>"
                                   f"{html_escape(s['what'][:60])}</span>")
            icl = QtWidgets.QLabel()
            if not ic.isNull():
                icl.setPixmap(ic.pixmap(28, 28))
            self.apps_box.addWidget(icl, r, 0)
            self.apps_box.addWidget(lbl, r, 1)
            cb = QtWidgets.QComboBox()
            cb.setToolTip("Send this app's sound somewhere else. Takes effect instantly.")
            for k in sinks:
                cb.addItem(k["desc"], k["name"])
                if k["id"] == s["sink"]:
                    cb.setCurrentIndex(cb.count() - 1)
            rules = self.app_state.get("app_rules", {})
            cb.activated.connect(lambda _, sid=s["id"], cb=cb, app=s["app"]: (
                run_quiet("pactl", "move-sink-input", str(sid), cb.currentData()),
                app in self.app_state.get("app_rules", {}) and self.set_app_rule(app, cb.currentData()),
                setattr(self, "apps_sig", None)))
            self.apps_box.addWidget(cb, r, 2)
            pin = QtWidgets.QToolButton()
            pin.setCheckable(True)
            pin.setChecked(s["app"] in rules)
            pin.setText("📌")
            pin.setStyleSheet(f"QToolButton:checked {{ background:{ACCENT}; border-radius:6px; }}")
            pin.setToolTip(f"Always send {s['app']} here, every time it starts.")
            pin.toggled.connect(lambda on, app=s["app"], cb=cb: self.set_app_rule(app, cb.currentData() if on else ""))
            self.apps_box.addWidget(pin, r, 3)
        self.apps_empty.setVisible(not streams)
        cur = (run_quiet("pactl", "get-default-sink") or "").strip()
        self.default_sink.blockSignals(True)
        self.default_sink.clear()
        for k in sinks:
            self.default_sink.addItem(k["desc"], k["name"])
            if k["name"] == cur:
                self.default_sink.setCurrentIndex(self.default_sink.count() - 1)
        self.default_sink.blockSignals(False)

    # ---------------------------------------------------------------- 4.2: speakers memory, IPC, shortcuts
    def eventFilter(self, obj, ev):
        if obj is getattr(self, "side", None) and ev.type() == QtCore.QEvent.Resize:
            QtCore.QTimer.singleShot(0, self.fit_sidebar)
        return super().eventFilter(obj, ev)

    def fit_sidebar(self):
        """Short window? Drop the pictures before the menu ever has to scroll."""
        last = self.nav.item(self.nav.count() - 1)
        rows = self.nav.visualItemRect(last).bottom() + self.nav.verticalScrollBar().value() + 12
        rows = max(rows, 0) if self.nav.verticalScrollBar().value() == 0 else rows
        fixed = self.nav.y() + (self.side.height() - self.now.y()) + 8
        room = self.side.height() - fixed - rows
        show_mini = room >= self.mini.height() + self.fw_badge.height() + 8
        show_badge = room >= self.fw_badge.height() + 8 + (self.mini.height() if show_mini else 0)
        self.mini.setVisible(show_mini)
        self.fw_badge.setVisible(show_badge or show_mini)

    def skip_unused_set(self, item):
        """With only some sets assigned, jump past the empty ones so Spkr toggles between real sets."""
        if not self.app_state.get("cr_enabled"):
            return False
        cr = self.app_state.get("cr", {})
        used = [L for L in "ABC" if cr.get(L) is not None]
        if len(used) < 2:
            return False
        base = (item or "Off").split("-")[0]
        letter = SPEAKERS.get(base, (None,))[0]
        if letter in used or (base == "Off" and time.monotonic() < getattr(self, "off_wanted_until", 0)):
            return False                                 # Off chosen on screen: headphones only, keep it
        c = self.ctrl("spkr-button")
        target = LETTER_COLOUR[used[0]][0]
        if c and target in c.items:
            self.write(c, [c.items.index(target)])
            self.reload_state()
            self.on_speaker_change(target, force=True)
            self.dirty_ui = True
            return True
        return False

    def restore_speakers(self):
        c = self.ctrl("spkr-button")
        last = self.app_state.get("last_spk") or "Green"
        if c and last in SPEAKERS and (self.ctrl_item("spkr-button") or "Off") == "Off" and last in c.items:
            self.write(c, [c.items.index(last)])
            self.on_speaker_change(last, force=True)
            self.dirty_ui = True

    def start_ipc(self):
        from PySide6.QtNetwork import QLocalServer
        QLocalServer.removeServer(SOCKET_NAME)
        self.ipc = QLocalServer(self)
        self.ipc.newConnection.connect(self._ipc_conn)
        self.ipc.listen(SOCKET_NAME)

    def _ipc_conn(self):
        sock = self.ipc.nextPendingConnection()

        def ready():
            for line in bytes(sock.readAll()).decode(errors="ignore").splitlines():
                self.handle_command(line.strip())
        sock.readyRead.connect(ready)
        if sock.bytesAvailable():
            ready()

    def handle_command(self, cmd):
        if not cmd:
            return
        if cmd == "show":
            self.show_window()
        elif cmd in ("toggle-mute", "mute", "unmute"):
            on = (not self.app_state.get("spk_mute")) if cmd == "toggle-mute" else cmd == "mute"
            self.mute_speakers(on)
        elif cmd in ("volume-up", "volume-down"):
            self.nudge_volume(+12 if cmd == "volume-up" else -12)
        elif cmd.startswith("speakers:") and cmd[-1] in "ABC":
            self.switch_speakers(cmd[-1])
        elif cmd.startswith("scene:"):
            sid = cmd[6:]
            if sid.startswith("user:"):
                self.apply_user_scene(sid[5:])
            else:
                sc = next((x for x in SCENES if x["id"] == sid), None)
                if sc:
                    self.run_scene(sc)
        elif cmd == "save-retro":
            self.save_retro(60)
        elif cmd == "quick-take":
            self.toggle_take()
        elif cmd == "metronome":
            self.toggle_metronome()
        elif cmd == "quit":
            self.quit_app()

    def nudge_volume(self, delta):
        c = self.ctrl("output-trim")
        if c:
            vals = list(c.values)
            v = max(0, min(255, vals[0] + delta))
            vals[0] = vals[1] = v
            self.write(c, vals)
            self.refresh_soon()
            self.toast(f"Speaker volume {v * 100 // 255}%", 1500)

    def setup_shortcuts(self):
        def sc(keys, fn):
            QtGui.QShortcut(QtGui.QKeySequence(keys), self, activated=fn)
        sc("Ctrl+M", lambda: self.handle_command("toggle-mute"))
        sc("Ctrl+K", self.open_palette)
        sc("Ctrl+P", self.open_palette)
        sc("Ctrl+Up", lambda: self.nudge_volume(12))
        sc("Ctrl+Down", lambda: self.nudge_volume(-12))
        for L in "ABC":
            sc(f"Ctrl+Shift+{L}", lambda L=L: self.switch_speakers(L))
        for i in range(9):
            sc(f"Ctrl+{i + 1}", lambda i=i: self.scene_by_index(i))

    def palette_entries(self):
        e = []
        for i in range(self.nav.count()):
            name = self.nav.item(i).text().strip().split(None, 1)[-1]
            e.append((f"Go to {name}", "page", lambda i=i: (self.show_window(), self.nav.setCurrentRow(i))))
        for t in self.findChildren(QtWidgets.QTabWidget):
            host = next((self.nav.item(i).text().strip().split(None, 1)[-1] for i in range(self.stack.count())
                         if self.stack.widget(i).isAncestorOf(t)), "")
            if host in ("Record", "Settings", "Speakers", "Hardware"):
                for j in range(t.count()):
                    e.append((f"{host}: {t.tabText(j).replace('&&', '&')}", "tab",
                              lambda t=t, j=j, host=host: (self.show_window(),
                                                          self.nav.setCurrentRow(self.page_index(host)),
                                                          t.setCurrentIndex(j))))
        for sc in SCENES:
            e.append((f"Scene: {sc['name'].strip()}", self.scene_view(sc)["short"],
                      lambda sc=sc: self.ask_scene(sc)))
        for n in self.user_scenes():
            e.append((f"Scene: ⭐ {n}", "your scene", lambda n=n: self.apply_user_scene(n)))
        for L in "ABC":
            e.append((f"Speakers {L}", "switch speaker set", lambda L=L: self.switch_speakers(L)))
        e += [("Quick take (start / stop)", "record your mic", self.toggle_take),
              ("Mute / unmute speakers", "Ctrl+M", lambda: self.handle_command("toggle-mute")),
              ("Volume up", "Ctrl+↑", lambda: self.nudge_volume(12)),
              ("Volume down", "Ctrl+↓", lambda: self.nudge_volume(-12)),
              ("Arm / stop recording", "Record page", lambda: (self.nav.setCurrentRow(self.page_index("Record")),
                                                                self.toggle_record())),
              ("Start / stop guitar mode", "Effects page", self.toggle_guitar),
              ("Find tracks", "Split & tag", lambda: (self.focus_split_tab(), self.find_tracks())),
              ("Identify songs (AcoustID)", "Split & tag", lambda: (self.focus_split_tab(), self.identify_songs())),
              ("Spectrogram", "Split & tag", self.show_spectrogram),
              ("Save tracks as FLAC", "Split & tag", self.export_tracks),
              ("Copy tracklist", "for Discogs / MusicBrainz", self.copy_tracklist),
              ("Undo last scene", "", self.undo_scene),
              ("Save current setup as a scene", "", self.save_user_scene),
              ("Pick startup scene", "", self.choose_startup),
              ("Install Headphones (Mbox) output", "", self.install_headphones),
              ("Show quick panel", "tray", self.show_quick_panel),
              ("Quit Mbox Control", "", self.quit_app)]
        return e

    def open_palette(self):
        self.show_window()
        dlg = CommandPalette(self, self.palette_entries())
        g = self.geometry()
        dlg.move(g.center().x() - dlg.width() // 2, g.top() + 90)
        dlg.exec()

    def scene_by_index(self, i):
        if i < len(SCENES):
            self.ask_scene(SCENES[i])
        else:
            users = self.user_scenes()
            j = i - len(SCENES)
            if j < len(users):
                self.apply_user_scene(users[j])

    # ---------------------------------------------------------------- 4.2: headphones output
    def install_headphones(self):
        sink = mbox_pw_node("sink")
        if not sink:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't find the Mbox in PipeWire — fix the "
                                                          "profile first (Home will offer a Fix button).")
            return
        if QtWidgets.QMessageBox.question(self, APP_NAME, "Add a “Headphones (Mbox)” output? PipeWire "
                                          "restarts for a second.") != QtWidgets.QMessageBox.Yes:
            return
        self.reload_state()
        save_json(UNDO_SNAPSHOT, self.snapshot())
        write_headphones_conf(sink)
        # PC 1/2 → Headphone A's mix only
        self.route_pair_to_mix(6, 5)
        self.mix_put(5, PC_HP_LEFT, UNITY, 0)
        self.mix_put(5, PC_HP_RIGHT, 0, UNITY)
        for k in (1, 2, 3, 4, 6):
            self.mix_put(k, PC_HP_LEFT, 0, 0)
            self.mix_put(k, PC_HP_RIGHT, 0, 0)
        restart_pipewire()
        QtCore.QTimer.singleShot(2500, fix_default_sink)
        self.reload_state()
        self.run_refreshers()
        self.toast("“Headphones (Mbox)” added — pick it per app in KDE's audio settings.", 9000)

    # ---------------------------------------------------------------- 4.2: mixer channel menu
    def strip_menu(self, item, pos):
        m = QtWidgets.QMenu(self)
        m.addAction("Rename…").triggered.connect(lambda: self.rename_strip(item))
        cm = m.addMenu("Colour")
        for nm, col in (("None", ""), ("Blue", "#5aa9ff"), ("Green", "#3fb950"), ("Amber", "#e3a52a"),
                        ("Red", "#f85149"), ("Purple", "#b48cff"), ("Pink", "#ff7eb6")):
            a = cm.addAction(("● " if col else "") + nm)
            a.triggered.connect(lambda _=False, c=col: self.set_label(f"stripcol:{item}", c))
        k = self.current_mix()
        if k is not None:
            m.addAction("MIDI learn this fader").triggered.connect(
                lambda: self.midi_learn(f"fader:{k}:{item}", f"{friendly(item)} in Stereo Mix {k}"))
        m.exec(pos)

    def rename_strip(self, item):
        text, ok = QtWidgets.QInputDialog.getText(self, "Rename channel", f"Name for {friendly(item)}:",
                                                  text=self.label(f"strip:{item}", ""))
        if ok:
            self.set_label(f"strip:{item}", text.strip())

    # ---------------------------------------------------------------- 4.2: analysis & Discogs
    def toggle_analyzer(self, on):
        if on:
            if pw_node_exists("mbox_tape_in"):
                self.route_rip_input()
                src, ch = "mbox_tape_in", (2, 0, 1, "front-left,front-right")
            else:
                src = mbox_pw_node("source")
                ch = (8, 4, 5, ",".join(f"aux{i}" for i in range(8)))
            if not src:
                self.an_btn.setChecked(False)
                QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't find the Mbox input in PipeWire.")
                return
            self.analyzer = Analyzer(src, ch, self.rate())
            self.analyzer.frame.connect(self.an_view.set_data)
            self.analyzer.failed.connect(lambda m: (self.an_btn.setChecked(False),
                                                    QtWidgets.QMessageBox.warning(self, APP_NAME, m)))
            self.analyzer.start()
            self.an_btn.setText("Stop analysis")
        else:
            if self.analyzer:
                self.analyzer.stop()
                self.analyzer.wait(1500)
                self.analyzer = None
            self.an_view.clear()
            self.an_btn.setText("Start analysis")

    def discogs_lookup(self):
        token = self.app_state.get("discogs_token", "")
        if not token:
            self.split_status.setText("Add your free Discogs token in Settings → Discogs first.")
            return
        if not self.tag_album.text().strip():
            self.split_status.setText("Type at least the album title, then look it up.")
            return
        self.split_status.setText("Searching Discogs…")
        self.dg = DiscogsLookup(token, self.tag_artist.text().strip(), self.tag_album.text().strip())
        self.dg.done.connect(self.discogs_results)
        self.dg.start()

    def discogs_results(self, results, err):
        if not results:
            self.split_status.setText(err)
            return
        items = [f"{r['title']}  ({r['year'] or '?'}, {r['country'] or '?'}, {r['format'] or '?'})" for r in results]
        choice, ok = QtWidgets.QInputDialog.getItem(self, "Pick the release", "Discogs matches:", items, 0, False)
        if not ok:
            return
        rel = results[items.index(choice)]
        self.discogs_release = rel["id"]
        self.split_status.setText("Fetching track list…")
        self.dgt = DiscogsTracks(self.app_state["discogs_token"], rel["id"])
        self.dgt.done.connect(self.discogs_tracks)
        self.dgt.start()

    def discogs_tracks(self, titles, year, artist, err):
        if err:
            self.split_status.setText(err)
            return
        if artist:
            self.tag_artist.setText(artist)
        self.mb_tracks(titles, year, "")

    # ---------------------------------------------------------------- 4.2: MIDI
    def fill_midi_ports(self):
        cur = self.app_state["midi"].get("port", "")
        self.midi_port.blockSignals(True)
        self.midi_port.clear()
        ports = list_midi_ports()
        if not ports:
            self.midi_port.addItem("No MIDI devices found", "")
        for pid, name in ports:
            self.midi_port.addItem(name, pid)
        i = self.midi_port.findData(cur)
        self.midi_port.setCurrentIndex(max(0, i))
        self.midi_port.blockSignals(False)
        self.midi_port.currentIndexChanged.connect(lambda _: self.midi_enable(self.midi_on.isChecked()))

    def midi_enable(self, on):
        self.app_state["midi"]["on"] = on
        self.app_state["midi"]["port"] = self.midi_port.currentData() or ""
        self.save_app_state()
        self.stop_midi()
        if on:
            self.start_midi()

    def start_midi(self):
        port = self.app_state["midi"].get("port")
        if not port:
            return
        self.midi = MidiWatcher(port)
        self.midi.event.connect(self.on_midi)
        self.midi.failed.connect(lambda m: self.toast(m, 8000))
        self.midi.start()

    def stop_midi(self):
        if self.midi:
            self.midi.stop()
            self.midi.wait(1000)
            self.midi = None

    def midi_learn(self, target, label):
        if not self.midi:
            self.toast("Enable MIDI in Settings → MIDI controller first.", 6000)
            return
        self.midi_learning = (target, label)
        self.toast(f"MIDI learn: move a knob/fader or press a pad for “{label}”…", 0)

    def refresh_midi_list(self):
        mp = self.app_state["midi"].get("map", {})
        if not hasattr(self, "midi_list"):
            return
        def nice(k):
            kind, ch, num = k.split(":")
            return (f"Knob/fader CC {num}" if kind == "cc" else f"Pad/key note {num}") + f" (ch {int(ch) + 1})"
        self.midi_list.setText("<br>".join(f"{nice(k)} → {v[1]}" for k, v in mp.items())
                               if mp else "No mappings yet.")

    def on_midi(self, kind, ch, num, val):
        key = f"{kind}:{ch}:{num}"
        if self.midi_learning:
            if kind == "note" and val == 0:
                return
            target, label = self.midi_learning
            self.app_state["midi"]["map"][key] = [target, label]
            self.save_app_state()
            self.midi_learning = None
            self.refresh_midi_list()
            self.toast(f"Learned: {label}", 4000)
            return
        m = self.app_state["midi"]["map"].get(key)
        if not m:
            return
        target = m[0]
        pressed = (kind == "note" and val > 0) or (kind == "cc" and val >= 64)
        if target.startswith("fader:"):
            _, k, item = target.split(":", 2)
            slot = self.slot_of(item)
            if slot is not None:
                key2 = f"{k}:{item}"
                pan = self.app_state["pans"].get(key2, to_pan(*self.mix_lr(int(k), slot))[1])
                self.mix_set_lr(int(k), slot, *from_pan(midi_to_gain(val), pan))
                self.refresh_soon()
        elif target.startswith("trim:"):
            c = self.ctrl("output-trim")
            first = int(target[5:])
            if c:
                vals = list(c.values)
                vals[first] = vals[first + 1] = round(val / 127 * 255)
                self.write(c, vals)
                self.refresh_soon()
        elif target == "mute" and pressed:
            self.handle_command("toggle-mute")
        elif target.startswith("scene:") and pressed:
            self.handle_command(target)

    # ---------------------------------------------------------------- 4.2: updater
    def check_update(self):
        offer = None
        if self.app_state.get("tester_updates"):
            try:
                found = find_update()
                if found:
                    offer = {"kind": "file", "ver": found[1], "path": str(found[0])}
            except Exception:  # noqa: BLE001
                pass
        if offer is None and getattr(self, "web_offer", None):
            offer = self.web_offer
        if UPDATE_REPO and self.app_state.get("update_check") and \
                time.time() - self.app_state.get("update_checked_at", 0) > 86400 and \
                not (getattr(self, "upd_job", None) and self.upd_job.isRunning()):
            self.app_state["update_checked_at"] = time.time()
            self.save_app_state()
            self.upd_job = UpdateCheck()
            self.upd_job.found.connect(self.web_update_found)
            self.upd_job.start()
        if offer != self.update_offer:
            self.update_offer = offer
            self.refresh_health()
            if offer and self.tray:
                self.tray.showMessage(APP_NAME, f"Version {offer['ver']} is ready to install.", app_icon(), 5000)

    def web_update_found(self, ver, page):
        self.web_offer = {"kind": "github", "ver": ver, "page": page} if ver else None
        self.check_update()

    def install_offer(self):
        o = self.update_offer or {}
        if o.get("kind") == "file":
            self.apply_update(o["path"])
            return
        if not os.access(Path(__file__).resolve(), os.W_OK):
            QtWidgets.QMessageBox.information(self, APP_NAME, f"Mbox Control {o.get('ver')} is out. You installed "
                                              "it as a package, so update with your package manager "
                                              "(for example: paru -Syu).")
            return
        self.toast(f"Downloading Mbox Control {o.get('ver')}…", 0)
        self.dl_job = UpdateDownload(o["ver"])
        self.dl_job.done.connect(lambda path, err: self.apply_update(path) if path else
                                 QtWidgets.QMessageBox.warning(self, APP_NAME, f"The update didn't download: {err}"))
        self.dl_job.start()

    def install_nosuspend(self):
        write_nosuspend_conf()
        run_quiet("systemctl", "--user", "restart", "wireplumber", timeout=15)
        QtCore.QTimer.singleShot(2500, fix_default_sink)
        self.refresh_health()
        self.toast("The Mbox now stays awake — recordings start instantly.", 8000)

    def restart_app(self):
        me = Path(__file__).resolve()
        self.quitting = True
        self.close()
        if getattr(self, "ipc", None):
            self.ipc.close()
        os.execv(sys.executable, [sys.executable, str(me)])

    def apply_update(self, path):
        me = Path(__file__).resolve()
        if not os.access(me, os.W_OK):
            QtWidgets.QMessageBox.information(self, APP_NAME, "Mbox Control was installed as a package, "
                                              "so update it with your package manager instead.")
            return
        try:
            shutil.copy2(me, me.with_suffix(".py.bak"))
            shutil.copy2(path, me)
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, APP_NAME, f"Update failed: {e}")
            return
        self.quitting = True
        self.close()
        if self.ipc:
            self.ipc.close()
        os.execv(sys.executable, [sys.executable, str(me)])

    # ---------------------------------------------------------------- plumbing
    def toast(self, msg, ms=6000):
        self.statusBar().showMessage(msg, ms)

    def announce(self, icon, msg, ms=6000):
        """A message about something that happened in the background: on screen even when the app isn't."""
        self.toast(msg, ms)
        if not self.isActiveWindow():
            self.osd.text(icon, msg)

    def ctrl(self, name, index=0):
        return self.by_name.get((name, index))

    def ctrl_item(self, name):
        c = self.ctrl(name)
        if c and c.values and isinstance(c.values[0], int) and 0 <= c.values[0] < len(c.items):
            return c.items[c.values[0]]
        return None

    def reload_state(self):
        try:
            fresh = self.backend.read_all()
        except Exception as e:  # noqa: BLE001
            self.toast(f"Can't read the Mbox: {e}", 0)
            return False
        self.ctrls = fresh
        self.by_name = {(c.name, c.index): c for c in fresh.values()}
        return True

    def run_refreshers(self):
        for fn in self.refreshers:
            fn()

    def _refresh_all(self):
        if QtWidgets.QApplication.mouseButtons() != Qt.NoButton:
            self.refresh_soon()
            return
        self.run_refreshers()

    def save_app_state(self):
        save_json(STATE_FILE, self.app_state)

    def write(self, c, values):
        if c.name in MIX_HISTORY_NAMES and not getattr(self, "hist_applying", False) and \
                hasattr(self, "hist_timer"):
            self.hist_timer.start(700)                  # a mixer change: remember it once it settles
        try:
            self.backend.write(c, values)
        except Exception as e:  # noqa: BLE001
            self.toast(f"Write failed ({c.name}): {e}", 8000)

    def write_element(self, name, index, element, value):
        c = self.ctrl(name, index)
        if c is None or element >= len(c.values) or c.values[element] == value:
            return
        vals = list(c.values)
        vals[element] = value
        self.write(c, vals)

    def ensure_metering(self):
        c = self.ctrl("metering")
        if c and not c.values[0]:
            self.write(c, [True])

    # ---------------------------------------------------------------- live data
    def on_events_alive(self, ok):
        self.events_alive = ok
        if self.poller:                                  # None while paused for sleep / recovery
            self.poller.events_ok = ok
        if hasattr(self, "events_lbl"):
            self.events_lbl.setText("connected ✓" if ok else "not available — polling instead")

    def on_event(self, numid):
        c = self.ctrls.get(numid)
        if c is None:
            self.refresh_soon()
            return
        if c.name not in METER_CTRLS:
            self.poller.request(numid)

    def on_values(self, numid, values):
        c = self.ctrls.get(numid)
        if c is None:
            return
        changed = c.values != values
        c.values = values
        if c.name in METER_CTRLS:
            self.dirty_ui = True
            if c.name == "mixer-source-meter":
                if changed or not any(values):
                    self.meters_frozen_since = 0.0
                elif not self.meters_frozen_since:
                    self.meters_frozen_since = time.time()
            return
        if not changed:
            return
        if c.name in ("master-knob-value", "spkr-button"):
            self.osd_value(c.name, values)
        if c.name == "spkr-button":
            it = self.ctrl_item("spkr-button")
            if self.skip_unused_set(it):
                return
            self.on_speaker_change(it)
            if it and it.split("-")[0] in SPEAKERS and self.app_state.get("last_spk") != it.split("-")[0]:
                self.app_state["last_spk"] = it.split("-")[0]
                self.save_app_state()
        if c.name in FAST_CTRLS:
            self.dirty_ui = True
        else:
            self.refresh_soon()

    def poll_state(self):
        if QtWidgets.QApplication.mouseButtons() != Qt.NoButton:
            return
        if not self.events_alive:
            if self.reload_state():
                self.run_refreshers()
        self.push_remote_state()

    def ui_update(self):
        if not self.dirty_ui:
            return
        self.dirty_ui = False
        for name, i, w in self.meters:
            c = self.ctrl(name)
            if c and i < len(c.values):
                (w.setLevel if isinstance(w, Fader) else w.set_level)(c.values[i])
        self.update_status()
        hv = getattr(self, "hw_view", None)
        if hv is not None and hv.isVisible():
            hv.update()
        if hasattr(self, "mini"):
            self.mini.update()
        if hasattr(self, "now"):
            self.now.refresh()
        self.tick_n += 1
        if self.tray:
            spk_now = SPEAKERS.get((self.ctrl_item("spkr-button") or "Off").split("-")[0])
            kv = self.ctrl("master-knob-value")
            st = (spk_now[1] if spk_now else "#8b93a1", bool(self.app_state.get("spk_mute")),
                  round((kv.values[0] / 255 if kv else 0.75) * 8) / 8)
            if st != getattr(self, "tray_state", None):
                self.tray_state = st
                self.tray.setIcon(knob_icon(st[0], st[1], max(0.1, st[2])))
        if self.tray and self.tick_n % 15 == 0:
            spk = SPEAKERS.get((self.ctrl_item("spkr-button") or "Off").split("-")[0])
            knob = self.ctrl("master-knob-value")
            self.tray.setToolTip(f"{APP_NAME} — " + (f"Speakers {spk[0]}" if spk else "speakers off")
                                 + (f" · knob {int(knob.values[0]) * 100 // 255}%" if knob else "")
                                 + (" · MUTED" if self.app_state.get("spk_mute") else ""))

    # ---------------------------------------------------------------- labels
    def label(self, key, default=""):
        return self.labels.get(key, default)

    def set_label(self, key, value):
        self.labels[key] = value
        m = re.fullmatch(r"in([0-3])(?::([ab]))?", key)
        if m:                                   # inputs 1–4 have two jacks each: keep "the name in use" in step
            r, side = int(m.group(1)), m.group(2)
            if side is None:
                self.labels[f"in{r}:{self.input_side(r)}"] = value
            elif side == self.input_side(r):
                self.labels[f"in{r}"] = value
        save_json(LABELS_FILE, self.labels)
        self.run_refreshers()

    def input_side(self, r):
        """Which jack feeds input r+1: "a" = front (1–2) or mic XLR (3–4), "b" = rear line jack. Set by hand —
        the Mbox doesn't report its F/R and Mic/Ln buttons."""
        return self.app_state.get("input_side", {}).get(str(r), "a")

    def toggle_input_side(self, r):
        self.set_input_side(r, "a" if self.input_side(r) == "b" else "b")

    def set_input_side(self, r, side):
        self.app_state.setdefault("input_side", {})[str(r)] = side
        self.save_app_state()
        self.labels[f"in{r}"] = self.labels.get(f"in{r}:{side}", "")
        save_json(LABELS_FILE, self.labels)
        jack = (("front", "rear") if r < 2 else ("mic (XLR)", "line (¼\")"))[side == "b"]
        name = self.labels[f"in{r}"]
        self.toast(f"Input {r + 1} now on its {jack} jack" + (f" — {name}." if name else "."), 5000)
        if hasattr(self, "hw_view"):
            self.hw_view.update()
        self.run_refreshers()

    def migrate_input_names(self):
        changed = False
        for r in range(4):
            if self.labels.get(f"in{r}") and f"in{r}:a" not in self.labels and f"in{r}:b" not in self.labels:
                self.labels[f"in{r}:{self.input_side(r)}"] = self.labels[f"in{r}"]
                changed = True
        if changed:
            save_json(LABELS_FILE, self.labels)

    def label_edit(self, key, default="", placeholder=""):
        e = QtWidgets.QLineEdit(self.label(key, default))
        e.setPlaceholderText(placeholder)
        e.editingFinished.connect(lambda: self.set_label(key, e.text()))
        return e

    def input_level(self, item):
        slot = self.slot_of(item)
        c = self.ctrl("mixer-source-meter")
        return c.values[slot] if c and slot is not None and slot < len(c.values) else 0

    # ---------------------------------------------------------------- mixer model
    def slot_of(self, item):
        c = self.ctrl("mixer-source")
        if not c:
            return None
        for slot, v in enumerate(c.values):
            if 0 <= v < len(c.items) and c.items[v] == item:
                return slot
        return None

    def mix_lr(self, k, slot):
        lb, rb = STEREO_MIX[k]
        cl, cr = self.ctrl("mixer-source-gain", lb), self.ctrl("mixer-source-gain", rb)
        return (cl.values[slot], cr.values[slot]) if cl and cr else (0, 0)

    def mix_set_lr(self, k, slot, l, r, buses=None):
        lb, rb = buses or STEREO_MIX[k]
        self.write_element("mixer-source-gain", lb, slot, l)
        self.write_element("mixer-source-gain", rb, slot, r)

    def mix_clear(self, k):
        for b in STEREO_MIX[k]:
            c = self.ctrl("mixer-source-gain", b)
            if c and any(c.values):
                self.write(c, [0] * len(c.values))

    def mix_put(self, k, item, l, r):
        slot = self.slot_of(item)
        if slot is not None:
            self.mix_set_lr(k, slot, l, r)

    def pair_raw(self, first):
        c = self.ctrl("output-source")
        return list(c.values[first:first + 2]) if c else [0, 0]

    def pair_source(self, first):
        c = self.ctrl("output-source")
        if not c or first + 1 >= len(c.values):
            return ("other", None, False)
        a, b = c.items[c.values[first]], c.items[c.values[first + 1]]
        ma, mb = re.fullmatch(r"Mixer-(\d+)", a), re.fullmatch(r"Mixer-(\d+)", b)
        if ma and mb:
            na, nb = int(ma.group(1)), int(mb.group(1))
            lo, hi = min(na, nb), max(na, nb)
            if lo % 2 == 1 and hi == lo + 1 and lo <= 11:
                return ("mix", (lo + 1) // 2, na > nb)
        if a == "None" and b == "None":
            return ("none", None, False)
        return ("direct", (a, b), False)

    def pair_text(self, first):
        kind, val, swapped = self.pair_source(first)
        if kind == "mix":
            return f"Stereo Mix {val}" + (" (swapped)" if swapped else "")
        if kind == "direct":
            return "Direct: " + " / ".join(friendly(x) for x in val)
        if kind == "none":
            return "Silent"
        return "Custom"

    def route_pair(self, first, left, right):
        c = self.ctrl("output-source")
        if not c:
            return
        vals = list(c.values)
        vals[first], vals[first + 1] = c.items.index(left), c.items.index(right)
        if vals != c.values:
            self.write(c, vals)

    def route_pair_raw(self, first, pair):
        c = self.ctrl("output-source")
        if c:
            vals = list(c.values)
            vals[first], vals[first + 1] = pair
            if vals != c.values:
                self.write(c, vals)

    def route_pair_to_mix(self, first, k, swapped=False):
        lb, rb = STEREO_MIX[k]
        a, b = f"Mixer-{lb + 1}", f"Mixer-{rb + 1}"
        self.route_pair(first, *((b, a) if swapped else (a, b)))

    def set_stream(self, aux, item):
        c = self.ctrl("stream-source")
        if c and item in c.items:
            self.write_element("stream-source", 0, aux, c.items.index(item))

    def rate(self):
        r = self.ctrl_item("clock-rate")
        return int(r) if r and str(r).isdigit() else 48000

    # ---------------------------------------------------------------- bound widgets
    def bind_combo(self, name, index=0, element=0, names=None):
        c = self.ctrl(name, index)
        w = QtWidgets.QComboBox()
        if c is None:
            w.setEnabled(False)
            return w
        w.addItems(names or [friendly(i) for i in c.items])

        def refresh():
            c2 = self.ctrl(name, index)
            if c2 and element < len(c2.values) and not w.view().isVisible():
                w.blockSignals(True)
                w.setCurrentIndex(int(c2.values[element]))
                w.blockSignals(False)

        w.currentIndexChanged.connect(lambda i: self.write_element(name, index, element, i))
        self.refreshers.append(refresh)
        return w

    def bind_check(self, name, text, index=0, element=0, widget=None):
        w = widget or QtWidgets.QCheckBox(text)
        if self.ctrl(name, index) is None:
            w.setEnabled(False)
            return w

        def refresh():
            c2 = self.ctrl(name, index)
            if c2 and element < len(c2.values):
                w.blockSignals(True)
                w.setChecked(bool(c2.values[element]))
                w.blockSignals(False)

        w.toggled.connect(lambda on: self.write_element(name, index, element, bool(on)))
        self.refreshers.append(refresh)
        return w

    def bind_slider(self, name, index=0, element=0, fmt=str):
        c = self.ctrl(name, index)
        box = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        s = QtWidgets.QSlider(Qt.Horizontal)
        val = QtWidgets.QLabel()
        val.setMinimumWidth(56)
        if c is None:
            s.setEnabled(False)
        else:
            s.setRange(c.min, c.max)
            send = Debounced(lambda v: self.write_element(name, index, element, v))

            def moved(v):
                val.setText(fmt(v))
                send(v)

            def refresh():
                c2 = self.ctrl(name, index)
                if c2 and element < len(c2.values) and not s.isSliderDown():
                    s.blockSignals(True)
                    s.setValue(int(c2.values[element]))
                    s.blockSignals(False)
                    val.setText(fmt(s.value()))

            s.valueChanged.connect(moved)
            self.refreshers.append(refresh)
        lay.addWidget(s, 1)
        lay.addWidget(val)
        return box

    # ================================================================ HOME
    def _build_home(self):
        w, lay = page("Home")
        self.health_box, hb = card()
        self.health_title = QtWidgets.QLabel("Suggestions")
        self.health_title.setObjectName("title")
        hb.addWidget(self.health_title)
        self.health_box.setVisible(False)
        lay.addWidget(self.health_box)
        hero, hl = card()
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        self.chip_spk, self.chip_dim, self.chip_mono, self.chip_rate = Chip(), Chip(), Chip(), Chip()
        for c in (self.chip_spk, self.chip_dim, self.chip_mono, self.chip_rate):
            row.addWidget(c)
        self.chip_spk.make_clickable("Click to switch speakers, turn them off, or use the headphone EQ")
        self.chip_spk.clicked.connect(self.speaker_menu)
        row.addStretch(1)
        self.phantom_btn = QtWidgets.QPushButton("+48V off")
        self.phantom_btn.setObjectName("phantom")
        self.phantom_btn.setCheckable(True)
        self.phantom_btn.setMinimumHeight(34)
        self.phantom_btn.clicked.connect(lambda: self.toggle_phantom())
        row.addWidget(self.phantom_btn)
        hl.addLayout(row)
        kr = QtWidgets.QHBoxLayout()
        kr.addWidget(muted("Big volume knob"))
        self.knob_bar = ValueBar(255)
        kr.addWidget(self.knob_bar, 1)
        hl.addLayout(kr)
        lay.addWidget(hero)

        two = ResponsiveBox(1000)
        oc, ol = card("Where sound goes", "What each output is playing right now.")
        og = QtWidgets.QGridLayout()
        og.setHorizontalSpacing(14)
        self.route_rows = []
        for r, (name, first, _) in enumerate(OUTPUT_PAIRS):
            nl = QtWidgets.QLabel(f"<b>{name}</b>")
            src = muted("")
            m = Meter(thick=8)
            self.meters.append(("output-source-meter", first, m))
            og.addWidget(nl, r, 0)
            og.addWidget(src, r, 1)
            og.addWidget(m, r, 2)
            self.route_rows.append((first, nl, src))
        og.setColumnStretch(2, 1)
        ol.addLayout(og)
        two.addWidget(oc, 1)

        ic, il = card("Inputs", "Name them in Settings → Your setup.")
        ig = QtWidgets.QGridLayout()
        ig.setHorizontalSpacing(14)
        for r, (name, item) in enumerate([("Input 1", "Analog-A-1"), ("Input 2", "Analog-A-2"),
                                          ("Input 3", "Analog-A-3"), ("Input 4", "Analog-A-4"),
                                          ("Aux 5", "Analog-A-5"), ("Aux 6", "Analog-A-6")]):
            ig.addWidget(QtWidgets.QLabel(f"<b>{name}</b>"), r, 0)
            tag = muted("")
            ig.addWidget(tag, r, 1)
            m = Meter(thick=8)
            slot = self.slot_of(item)
            if slot is not None:
                self.meters.append(("mixer-source-meter", slot, m))
            ig.addWidget(m, r, 2)
            self.refreshers.append(lambda tag=tag, r=r: tag.setText(self.label(f"in{r}", "—")[:28]))
        ig.setColumnStretch(2, 1)
        il.addLayout(ig)
        two.addWidget(ic, 1)
        lay.addWidget(two)

        def refresh_routes():
            for first, nl, src in self.route_rows:
                name = self.label(f"out{first}", "")
                src.setText(self.pair_text(first) + (f" · <b>{name[:22]}</b>" if name else ""))
        self.refreshers.append(refresh_routes)

        sc, sl = card("Scenes", "One click sets up the Mbox for a job and highlights the cables on "
                                "the Hardware page. Undo puts everything back exactly.")
        self.scene_flow = FlowGrid(330)
        sl.addWidget(self.scene_flow)
        brow = QtWidgets.QHBoxLayout()
        undo = QtWidgets.QPushButton("↶  Undo last scene")
        undo.clicked.connect(self.undo_scene)
        save = QtWidgets.QPushButton("＋  Save current setup as a scene…")
        save.clicked.connect(self.save_user_scene)
        brow.addWidget(undo)
        brow.addWidget(save)
        brow.addStretch(1)
        sl.addLayout(brow)
        lay.addWidget(sc)
        lay.addStretch(1)
        self.rebuild_scene_grid()
        return scroll(w)

    def user_scenes(self):
        if not SCENES_DIR.exists():
            return []
        return sorted(p.stem for p in SCENES_DIR.glob("*.json"))

    def rebuild_scene_grid(self):
        items = [(v["name"], v["short"], v["what"], lambda _=False, s=s: self.ask_scene(s), None)
                 for s in SCENES for v in [self.scene_view(s)]]
        for name in self.user_scenes():
            items.append((f"⭐  {name}", "Your saved scene", "Restores a setup you saved.",
                          lambda _=False, n=name: self.apply_user_scene(n), name))
        boxes = []
        for i, (title, short, tip, fn, user) in enumerate(items):
            box = QtWidgets.QWidget()
            hb = QtWidgets.QHBoxLayout(box)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(4)
            b = QtWidgets.QPushButton(f"{title}\n{short}")
            b.setObjectName("scene")
            b.setMinimumHeight(62)
            b.setMinimumWidth(10)
            b.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            b.setToolTip(tip + (f"   (Ctrl+{i + 1})" if i < 9 else ""))
            b.clicked.connect(fn)
            hb.addWidget(b, 1)
            if user:
                x = QtWidgets.QPushButton("✕")
                x.setFixedWidth(34)
                x.setToolTip("Delete this scene")
                x.clicked.connect(lambda _=False, n=user: self.delete_user_scene(n))
                hb.addWidget(x)
            boxes.append(box)
        self.scene_flow.set_widgets(boxes)
        self.push_remote_state()
        self.fill_tray_scenes()
        if hasattr(self, "refresh_startup_box"):
            self.refresh_startup_box()

    def update_status(self):
        if not hasattr(self, "chip_spk"):
            return
        spk = self.ctrl_item("spkr-button") or "Off"
        sc = SPEAKERS.get(spk.split("-")[0])
        if sc:
            name = self.label(f"spk{sc[0]}", "")
            hold = " · Aux" if spk.endswith("Blink") else ""
            hp = " · 🎧 EQ" if self.app_state.get("hp_listen") else ""
            self.chip_spk.set(f"Speakers {sc[0]}" + (f" · {name}" if name else "") + hold + hp + "  ▾", sc[1])
        else:
            self.chip_spk.set("Speakers off · 🎧  ▾")
        mute = self.ctrl_item("mute-button")
        self.chip_dim.set({"On": "DIMMED", "Blink": "MUTED"}.get(mute, "Dim/Mute off"),
                          "#e3a52a" if mute in ("On", "Blink") else None)
        mono = self.ctrl_item("mono-button")
        self.chip_mono.set("MONO" if mono == "On" else "Mono off", "#e3a52a" if mono == "On" else None)
        self.chip_rate.set(f"{self.rate() / 1000:g} kHz")
        ph = self.ctrl("phantom-powering")
        on = bool(ph and ph.values[0])
        self.phantom_btn.blockSignals(True)
        self.phantom_btn.setChecked(on)
        self.phantom_btn.blockSignals(False)
        self.phantom_btn.setText("+48V ON — all 4 mics" if on else "+48V off")
        knob = self.ctrl("master-knob-value")
        if knob:
            v = int(knob.values[0])
            self.knob_bar.setValue(v)
            if hasattr(self, "knob_bar2"):
                self.knob_bar2.setValue(v)
            if hasattr(self, "knob_bar3"):
                self.knob_bar3.setValue(v)
        if hasattr(self, "spk_rows"):
            self.refresh_speaker_rows()

    def toggle_phantom(self):
        ph = self.ctrl("phantom-powering")
        if not ph:
            return
        on = not bool(ph.values[0])
        if on and QtWidgets.QMessageBox.question(
                self, "Phantom power", "Turn on +48V for all four mic inputs?\n\nOnly condenser mics "
                "need it. Ribbon mics can be damaged — unplug them first.") != QtWidgets.QMessageBox.Yes:
            self.update_status()
            return
        self.write(ph, [on])
        self.update_status()
        if hasattr(self, "hw_view"):
            self.hw_view.update()

    # ---------------------------------------------------------------- scenes
    def ask_scene(self, s):
        s = self.scene_view(s)
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(s["name"].strip())
        box.setText(f"<b>{s['name'].strip()}</b><br><br>{s['what']}")
        box.setInformativeText("Before you start:\n• " + "\n• ".join(s["check"]))
        box.setStandardButtons(QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel)
        box.button(QtWidgets.QMessageBox.Ok).setText("Apply")
        if box.exec() == QtWidgets.QMessageBox.Ok:
            self.run_scene(s)

    def run_scene(self, s):
        if s["id"] == "guitar" and not self.guitar_on and hasattr(self, "guitar_btn"):
            self.toggle_guitar()                  # full guitar mode: latency, effects app, wiring
            return
        self._run_scene(s)
        if s["id"] == "rip":
            if self.app_state["setup"].get("rip_app") == "audacity":
                self.open_audacity()
            else:
                self.nav.setCurrentRow(self.page_index("Record"))

    def page_index(self, name):
        return next((i for i in range(self.nav.count()) if self.nav.item(i).text().strip().endswith(name)), 0)

    def open_audacity(self, path=None):
        if not shutil.which("audacity"):
            if QtWidgets.QMessageBox.question(self, APP_NAME, "Audacity isn't installed. Install it now? "
                                              "(Asks for your password once.)") != QtWidgets.QMessageBox.Yes:
                return
            if not shutil.which("pkexec") or not install_argv("audacity"):
                QtWidgets.QMessageBox.information(self, APP_NAME, "Install Audacity with your package manager.")
                return

            def done(ok, _msg):
                if ok and shutil.which("audacity"):
                    self.open_audacity(path)
                else:
                    QtWidgets.QMessageBox.warning(self, APP_NAME, "The install didn't finish. Try:  "
                                                  + install_hint("audacity"))
            self.run_root(install_argv("audacity"), "Installing Audacity…", done)
            return
        env = dict(os.environ)
        if pw_node_exists("mbox_tape_in"):
            self.route_rip_input()
            env["PULSE_SOURCE"] = "mbox_tape_in"      # Audacity's “default” input = your rip input
        subprocess.Popen(["audacity"] + ([str(path)] if path else []), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.toast("Opening Audacity. Recording device: “default” (or “pulse”) = your rip input.", 9000)

    def _run_scene(self, s):
        self.apply_scene(s["id"])
        if hasattr(self, "hw_view"):
            self.hw_view.highlight = set(s["jacks"])
            self.hw_view.update()
        self.toast(f"Applied {s['name'].strip()} — cables are highlighted on the Hardware page.", 8000)

    def apply_scene(self, sid):
        self.reload_state()
        save_json(UNDO_SNAPSHOT, self.snapshot())
        U = UNITY
        if sid == "listen":
            self.route_pair_to_mix(0, 1)
            self.mix_clear(1)
            self.mix_put(1, PC_LEFT, U, 0)
            self.mix_put(1, PC_RIGHT, 0, U)
            self.restore_always_on()
        elif sid == "rip":
            self.set_rec_source(f"pair:{self.app_state['setup']['rip']}")
            self.route_rip_input()
            self.route_pair_to_mix(0, 1)
            self.mix_put(1, PC_LEFT, U, 0)
            self.mix_put(1, PC_RIGHT, 0, U)
            st = self.app_state["setup"]
            left = st["rip"]
            on = st.get("rip_monitor") or left in st.get("always_on", [])
            self.mix_put(1, left, U if on else 0, 0)
            self.mix_put(1, pair_right(left), 0, U if on else 0)
        elif sid == "tape":
            self.route_pair_to_mix(2, 2)
            self.mix_clear(2)
            self.mix_put(2, PC_LEFT, U, 0)
            self.mix_put(2, PC_RIGHT, 0, U)
        elif sid == "record":
            self.set_rec_source("mono:Analog-A-1")
            for aux in range(4):
                self.set_stream(aux, f"Analog-A-{aux + 1}")
            self.route_pair_to_mix(6, 5)
            self.mix_clear(5)
            for n in range(1, 5):
                self.mix_put(5, f"Analog-A-{n}", *from_pan(U, 0))
            self.mix_put(5, PC_LEFT, U, 0)
            self.mix_put(5, PC_RIGHT, 0, U)
        elif sid == "guitar":
            for item in self.guitar_inputs():
                n = int(item[-1])
                self.set_stream(n - 1, item)              # Input n → capture AUX(n-1)
                slot = self.slot_of(item)
                if slot is not None:                      # dry signal off: you only hear the amp
                    for k in STEREO_MIX:
                        self.mix_set_lr(k, slot, 0, 0)
            for k, first in ((1, 0), (5, 6)):
                self.route_pair_to_mix(first, k)
                self.mix_put(k, PC_LEFT, U, 0)
                self.mix_put(k, PC_RIGHT, 0, U)
            pw_set("clock.force-quantum", 128)
        self.reload_state()
        self.run_refreshers()

    def undo_scene(self):
        if not UNDO_SNAPSHOT.exists():
            self.toast("No scene to undo yet.")
            return
        self.apply_snapshot(load_json(UNDO_SNAPSHOT, {}))
        if hasattr(self, "hw_view"):
            self.hw_view.highlight = set()
        self.toast("Restored the settings from before the last scene.")

    def save_user_scene(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Save scene", "Name for this setup:")
        name = safe_name(name) if ok and name.strip() else None
        if not name:
            return
        self.reload_state()
        save_json(SCENES_DIR / f"{name}.json", self.snapshot())
        self.rebuild_scene_grid()
        self.toast(f"Saved scene “{name}”.")

    def apply_user_scene(self, name):
        self.reload_state()
        save_json(UNDO_SNAPSHOT, self.snapshot())
        self.apply_snapshot(load_json(SCENES_DIR / f"{name}.json", {}))
        self.toast(f"Applied your scene “{name}”.")

    def delete_user_scene(self, name):
        if QtWidgets.QMessageBox.question(self, "Delete scene", f"Delete “{name}”?") == QtWidgets.QMessageBox.Yes:
            (SCENES_DIR / f"{name}.json").unlink(missing_ok=True)
            self.rebuild_scene_grid()

    # ================================================================ HARDWARE
    def _build_hardware(self):
        mw = QtWidgets.QWidget()
        ml = QtWidgets.QVBoxLayout(mw)
        ml.setContentsMargins(0, 8, 0, 0)
        self.sig_map = SignalMap(self)
        self.sig_map.boxClicked.connect(self.map_clicked)
        ml.addWidget(self.sig_map, 1)
        ml.addWidget(muted("Lit green cables are carrying sound right now. Grey ones are connected but "
                           "silent. Dashed cables show inputs being sent to the computer right now. Names come from "
                           "Settings → Your setup — click any box to name it."))
        self.map_timer = QtCore.QTimer(self)
        self.map_timer.timeout.connect(lambda: self.sig_map.isVisible() and self.sig_map.update())
        self.map_timer.start(200)
        return self.wrap_tabs("Hardware", "Your Mbox, live: where sound comes in, where it goes, and the box "
                                          "itself.",
                              [("Signal map", mw), ("The box", self._build_box())])

    def map_clicked(self, kind, key):
        if kind == "in":
            r = [j for j, _ in INPUT_JACKS].index(key)
            jack = dict(INPUT_JACKS)[key]
            text, ok = QtWidgets.QInputDialog.getText(self, "Name this input", f"What's plugged into {jack}?",
                                                      text=self.label(f"in{r}", ""))
            if ok:
                self.set_label(f"in{r}", text.strip())
                self.names_changed()
        elif kind == "out":
            name = next(n for n, f, _ in OUTPUT_PAIRS if str(f) == key)
            text, ok = QtWidgets.QInputDialog.getText(self, "Name this output", f"What does {name} feed?",
                                                      text=self.label(f"out{key}", ""))
            if ok:
                self.set_label(f"out{key}", text.strip())
                self.names_changed()
        elif kind == "mix":
            self.nav.setCurrentRow(self.page_index("Mixer"))
        elif kind == "pc":
            self.nav.setCurrentRow(self.page_index("Speakers"))
            t = self.stack.currentWidget().findChild(QtWidgets.QTabWidget)
            if t:
                t.setCurrentIndex(1)
        elif kind == "cap":
            self.nav.setCurrentRow(self.page_index("Record"))
        self.sig_map.update()

    def _build_box(self):
        w, lay = page("Hardware", "Your Mbox, live — the lights follow the real box. Click a jack to "
                                  "note what's plugged in; scenes highlight the cables they need. "
                                  "Click +48V to switch phantom power.")
        self.hw_view = HardwareView(self)
        self.hw_view.jackClicked.connect(self.tag_jack)
        self.hw_view.phantomClicked.connect(self.toggle_phantom)
        lay.addWidget(self.hw_view, 1)
        cc, cl = card("Connections", "Jacks with a blue dot have a note.")
        self.conn_list = QtWidgets.QLabel()
        self.conn_list.setWordWrap(True)
        cl.addWidget(self.conn_list)
        lay.addWidget(cc)

        def refresh_conn():
            seen, rows = set(), []
            for jid, _, _, label in self.hw_view.shapes:
                key = self.hw_view.jack_tag_key(jid)
                tag = self.label(key, "")
                if tag and key not in seen:
                    seen.add(key)
                    rows.append(f"<b>{re.sub(r' [(].*[)]', '', label)}</b> — {tag}")
            self.conn_list.setText("<br>".join(rows) if rows else "Nothing noted yet.")

        self.refreshers.append(refresh_conn)
        lay.addWidget(muted("Not visible to software (set by hand): Front/Rear, Mic/Line, Pad (pull "
                            "the gain knob), Soft-limit, +4/−10, gain and headphone knobs, Multi."))
        return w

    def tag_jack(self, jid):
        key = self.hw_view.jack_tag_key(jid)
        text, ok = QtWidgets.QInputDialog.getText(self, "What's plugged in?",
                                                  f"{self.hw_view.label_for(jid)}:", text=self.label(key, ""))
        if ok:
            self.set_label(key, text.strip())
            self.hw_view.update()

    # ================================================================ MIXER
    def _build_mixer(self):
        w = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(w)
        outer.setContentsMargins(26, 20, 26, 14)
        outer.setSpacing(10)
        head = QtWidgets.QLabel("Mixer")
        head.setObjectName("h1")
        hrow = QtWidgets.QHBoxLayout()
        hrow.addWidget(head)
        hrow.addStretch(1)
        self.undo_btn = QtWidgets.QToolButton()
        self.undo_btn.setText("↶ Undo")
        self.undo_btn.setToolTip("Undo the last mixer change (Ctrl+Z)")
        self.undo_btn.clicked.connect(lambda: self.mix_history_step(-1))
        self.redo_btn = QtWidgets.QToolButton()
        self.redo_btn.setText("↷ Redo")
        self.redo_btn.setToolTip("Redo (Ctrl+Shift+Z)")
        self.redo_btn.clicked.connect(lambda: self.mix_history_step(+1))
        hrow.addWidget(self.undo_btn)
        hrow.addWidget(self.redo_btn)
        hrow.addSpacing(14)
        self.ab_btns = {}
        for L in "AB":
            b = QtWidgets.QToolButton()
            b.setText(L)
            b.setCheckable(True)
            b.setMinimumWidth(34)
            b.setToolTip("A/B compare: tweak a mix on A, switch to B and try something else, then flick between "
                         "them to hear which is better. Whatever you change goes into the lit one.")
            b.clicked.connect(lambda _=False, L=L: self.ab_switch(L))
            b.setStyleSheet(f"QToolButton:checked {{ background:{ACCENT}; color:white; border-radius:6px; }}")
            hrow.addWidget(b)
            self.ab_btns[L] = b
        self.ab_btns["A"].setChecked(True)
        hrow.addSpacing(14)
        cm = QtWidgets.QToolButton()
        cm.setText("⧉ Compact mixer")
        cm.setToolTip("A small always-on-top mixer to keep beside your DAW or game.")
        cm.clicked.connect(self.open_compact_mixer)
        hrow.addWidget(cm)
        outer.addLayout(hrow)
        for keys, step in (("Ctrl+Z", -1), ("Ctrl+Shift+Z", +1), ("Ctrl+Y", +1)):
            QtGui.QShortcut(QtGui.QKeySequence(keys), w, activated=lambda s=step: self.mix_history_step(s),
                            context=Qt.WidgetWithChildrenShortcut)

        self.pair_tabs = QtWidgets.QTabBar()
        self.pair_tabs.setExpanding(False)
        self.pair_tabs.setDrawBase(False)
        for name, _, _ in OUTPUT_PAIRS:
            self.pair_tabs.addTab(name)
        outer.addWidget(self.pair_tabs)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("<b>This output plays</b>"))
        self.pair_src = QtWidgets.QComboBox()
        self.pair_src.setMinimumWidth(230)
        self.pair_choices = [("mix", k, f"Stereo Mix {k}") for k in range(1, 7)]
        self.pair_choices += [("direct", (f"Stream-{n}", f"Stream-{n + 1}"), f"Direct: PC {n}/{n + 1}")
                              for n in (1, 3, 5, 7)]
        self.pair_choices += [("direct", ("Analog-A-1", "Analog-A-2"), "Direct: Inputs 1/2"),
                              ("direct", ("Analog-A-3", "Analog-A-4"), "Direct: Inputs 3/4"),
                              ("direct", ("Analog-A-5", "Analog-A-6"), "Direct: Aux in 5/6"),
                              ("direct", ("S/PDIF-1", "S/PDIF-2"), "Direct: S/PDIF in"),
                              ("none", None, "Nothing (silent)")]
        for _, _, t in self.pair_choices:
            self.pair_src.addItem(t)
        top.addWidget(self.pair_src)
        self.swap_btn = QtWidgets.QPushButton("⇄  Swap L/R")
        self.swap_btn.setCheckable(True)
        top.addWidget(self.swap_btn)
        self.mix_hint = muted("")
        top.addWidget(self.mix_hint, 1)
        self.only_used = QtWidgets.QCheckBox("Only channels in use")
        self.only_used.setChecked(bool(self.app_state["only_used"]))
        self.only_used.toggled.connect(lambda on: (self.app_state.__setitem__("only_used", on),
                                                   self.save_app_state(), self.refresh_mixer()))
        top.addWidget(self.only_used)
        outer.addLayout(top)

        self.strips = []
        self.mix_groups = []
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        groups_w = QtWidgets.QWidget()
        gl = QtWidgets.QHBoxLayout(groups_w)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(12)
        groups = [("Hardware inputs", ["Analog-A-1", "Analog-A-2", "Analog-A-3", "Analog-A-4",
                                       "Analog-A-5", "Analog-A-6", "S/PDIF-1", "S/PDIF-2"]),
                  ("From the computer", [f"Stream-{n}" for n in range(1, 9)]),
                  ("FX return", ["Reverb-1", "Reverb-2"])]
        for gname, items in groups:
            gf, glay = card(gname)
            glay.setContentsMargins(10, 10, 10, 10)
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(6)
            for item in items:
                row.addWidget(self._make_strip(item))
            row.addStretch(1)
            glay.addLayout(row, 1)
            gl.addWidget(gf)
            self.mix_groups.append((gf, items))
        gl.addStretch(1)
        body.addWidget(scroll(groups_w), 1)
        body.addWidget(self._make_master())
        outer.addLayout(body, 1)

        self.pair_tabs.currentChanged.connect(lambda _: self.refresh_mixer())
        self.pair_src.currentIndexChanged.connect(self.pair_source_chosen)
        self.swap_btn.toggled.connect(self.swap_toggled)
        self.refreshers.append(self.refresh_mixer)
        return w

    # ---------------------------------------------------------------- 4.18: mixer history, A/B, compact, colours
    def mix_state(self):
        return {"ctrls": {c.key: list(c.values) for c in self.ctrls.values() if c.name in MIX_HISTORY_NAMES},
                "mutes": json.loads(json.dumps(self.app_state.get("mutes", {}))),
                "solo": json.loads(json.dumps(self.app_state.get("solo", {})))}

    def record_mix_state(self):
        st = self.mix_state()
        hist = self.mix_hist
        if hist and hist[self.mix_pos] == st:
            return
        del hist[self.mix_pos + 1:]
        hist.append(st)
        del hist[:-100]                                 # keep the last 100 changes
        self.mix_pos = len(hist) - 1
        self.refresh_history_buttons()

    def apply_mix_state(self, st):
        keyed = {c.key: c for c in self.ctrls.values()}
        self.hist_applying = True
        try:
            for key, vals in st["ctrls"].items():
                c = keyed.get(key)
                if c is not None and c.values != vals:
                    try:
                        self.backend.write(c, vals)
                        c.values = list(vals)
                    except Exception:  # noqa: BLE001
                        pass
            self.app_state["mutes"], self.app_state["solo"] = st["mutes"], st["solo"]
            self.save_app_state()
        finally:
            self.hist_applying = False
        self.run_refreshers()

    def mix_history_step(self, step):
        if self.hist_timer.isActive():                  # a change still settling: record it first
            self.hist_timer.stop()
            self.record_mix_state()
        new = self.mix_pos + step
        if not 0 <= new < len(self.mix_hist):
            self.toast("Nothing to undo." if step < 0 else "Nothing to redo.", 2000)
            return
        self.mix_pos = new
        self.apply_mix_state(self.mix_hist[new])
        self.refresh_history_buttons()

    def refresh_history_buttons(self):
        if hasattr(self, "undo_btn"):
            self.undo_btn.setEnabled(self.mix_pos > 0)
            self.redo_btn.setEnabled(self.mix_pos < len(self.mix_hist) - 1)

    def ab_switch(self, letter):
        cur = self.ab_active
        for L, b in self.ab_btns.items():
            b.setChecked(L == letter)
        if letter == cur:
            return
        self.ab_slots[cur] = self.mix_state()
        target = self.ab_slots.get(letter)
        self.ab_active = letter
        if target is None:                              # first visit: B starts as a copy of A
            self.ab_slots[letter] = self.mix_state()
            self.toast(f"{letter} starts as a copy — change things, then flick back to compare.", 5000)
            return
        self.apply_mix_state(target)
        self.record_mix_state()

    def open_compact_mixer(self):
        if getattr(self, "compact", None) is None:
            self.compact = CompactMixer(self)
        self.compact.rebuild()
        self.compact.show()
        self.compact.raise_()

    def set_strip_mute(self, k, item, on):
        slot = self.slot_of(item)
        if k is None or slot is None:
            return
        key = f"{k}:{item}"
        mutes = self.app_state["mutes"]
        if on and key not in mutes:
            mutes[key] = list(self.mix_lr(k, slot))
            self.mix_set_lr(k, slot, 0, 0)
        elif not on and key in mutes:
            l_, r_ = mutes.pop(key)
            if not self.is_soloed_out(k, item):
                self.mix_set_lr(k, slot, l_, r_)
        self.save_app_state()
        self.run_refreshers()

    def item_colour(self, item):
        col = self.label(f"stripcol:{item}", "")
        if not col:
            m = re.fullmatch(r"(Analog-A-|Stream-|S/PDIF-|Reverb-)(\d+)", item)
            if m:
                n = int(m.group(2))
                col = self.label(f"stripcol:{m.group(1)}{n + 1 if n % 2 else n - 1}", "")
        return col

    def item_title(self, item):
        custom = self.label(f"strip:{item}", "")
        m = re.fullmatch(r"Analog-A-(\d)", item)
        tag = self.label(f"in{int(m.group(1)) - 1}", "") if m else ""
        return custom or tag or friendly(item)

    def pick_colour(self, item, button=None):
        cur = QtGui.QColor(self.item_colour(item) or ACCENT)
        c = QtWidgets.QColorDialog.getColor(cur, self, f"Colour for {friendly(item)}")
        if c.isValid():
            self.set_label(f"stripcol:{item}", c.name())
            if button:
                button.setStyleSheet(f"background:{c.name()}; border-radius:9px;")
            self.run_refreshers()

    # ---------------------------------------------------------------- 4.18: automatic scenes
    def scene_choices(self):
        out = [(s["id"], s["name"].strip()) for s in SCENES]
        out += [(f"user:{n}", f"⭐ {n}") for n in self.user_scenes()]
        return out

    def check_auto_scenes(self):
        rules = self.app_state.get("auto_scenes", [])
        if not rules or self.disconnected:
            return
        running = running_programs()
        active = self.auto_active
        for rule in rules:
            proc = rule["proc"].lower()
            up = proc in running
            if up and proc not in active:
                active[proc] = self.snapshot() if rule.get("revert", True) else None
                sid = rule["scene"]
                if sid.startswith("user:"):
                    self.apply_snapshot(load_json(SCENES_DIR / f"{sid[5:]}.json", {}))
                else:
                    self.apply_scene(sid)
                    self.run_refreshers()
                name = dict(self.scene_choices()).get(sid, sid)
                self.announce("media-playback-start", f"{rule.get('label') or proc} opened → {name}")
            elif not up and proc in active:
                snap = active.pop(proc)
                if snap and not active:                 # only put things back when no other rule is active
                    self.apply_snapshot(snap)
                    self.announce("edit-undo", f"{rule.get('label') or proc} closed — settings put back")

    def build_auto_scenes_card(self, lay):
        c, l = card("Automatic scenes", "Switch scenes by themselves when an app opens — and put everything back "
                                        "when it closes.")
        self.auto_rows = QtWidgets.QGridLayout()
        self.auto_rows.setColumnStretch(1, 1)
        l.addLayout(self.auto_rows)
        add = QtWidgets.QHBoxLayout()
        self.auto_app = QtWidgets.QComboBox()
        self.auto_app.setEditable(True)
        self.auto_app.setToolTip("Pick an app or type its program name (the name you'd see in a task manager).")
        for proc, label in AUTO_SCENE_HINTS:
            self.auto_app.addItem(label, proc)
        add.addWidget(QtWidgets.QLabel("When"))
        add.addWidget(self.auto_app, 1)
        add.addWidget(QtWidgets.QLabel("opens, use"))
        self.auto_scene = QtWidgets.QComboBox()
        for sid, name in self.scene_choices():
            self.auto_scene.addItem(name, sid)
        add.addWidget(self.auto_scene, 1)
        b = QtWidgets.QPushButton("Add")
        b.clicked.connect(self.add_auto_scene)
        add.addWidget(b)
        l.addLayout(add)
        lay.addWidget(c)
        self.refresh_auto_scenes()

    def add_auto_scene(self):
        i = self.auto_app.currentIndex()
        text = self.auto_app.currentText().strip()
        proc = self.auto_app.itemData(i) if i >= 0 and self.auto_app.itemText(i) == text else text
        if not proc:
            return
        rules = self.app_state.setdefault("auto_scenes", [])
        sid = self.auto_scene.currentData()
        old = next((r for r in rules if r["proc"].lower() == proc.lower()), None)
        scene = self.auto_scene.currentText().strip()
        if old and old["scene"] == sid:
            self.toast(f"Already set: {text} → {scene}.", 4000)
            return
        if old:
            old["scene"] = sid
            self.toast(f"Changed: {text} now uses {scene}.", 4000)
        else:
            rules.append({"proc": proc.lower(), "label": text, "scene": sid, "revert": True})
            self.toast(f"Added: when {text} opens, use {scene}.", 4000)
        self.save_app_state()
        self.refresh_auto_scenes()

    def refresh_auto_scenes(self):
        while self.auto_rows.count():
            it = self.auto_rows.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        names = dict(self.scene_choices())
        for r, rule in enumerate(self.app_state.get("auto_scenes", [])):
            self.auto_rows.addWidget(QtWidgets.QLabel(f"<b>{html_escape(rule.get('label') or rule['proc'])}</b>"), r, 0)
            self.auto_rows.addWidget(QtWidgets.QLabel("→ " + names.get(rule["scene"], rule["scene"])), r, 1)
            rv = QtWidgets.QCheckBox("put back when it closes")
            rv.setChecked(rule.get("revert", True))
            rv.toggled.connect(lambda on, rule=rule: (rule.__setitem__("revert", on), self.save_app_state()))
            self.auto_rows.addWidget(rv, r, 2)
            x = QtWidgets.QToolButton()
            x.setText("✕")
            x.clicked.connect(lambda _=False, rule=rule: (self.app_state["auto_scenes"].remove(rule),
                                                         self.save_app_state(), self.refresh_auto_scenes()))
            self.auto_rows.addWidget(x, r, 3)

    def current_pair(self):
        return OUTPUT_PAIRS[self.pair_tabs.currentIndex()]

    def current_mix(self):
        kind, val, _ = self.pair_source(self.current_pair()[1])
        return val if kind == "mix" else None

    def pair_source_chosen(self, i):
        if i >= len(self.pair_choices):
            return
        _, first, _ = self.current_pair()
        kind, val, _ = self.pair_choices[i]
        if kind == "mix":
            if self.rate() > 96000 and val > 2:
                QtWidgets.QMessageBox.warning(self, "Sample rate too high",
                                              "Above 96 kHz the Mbox only has Stereo Mix 1 and 2. "
                                              "Lower the sample rate in Settings to use Mixes 3–6.")
                self.refresh_mixer()
                return
            self.route_pair_to_mix(first, val)
        elif kind == "direct":
            self.route_pair(first, *val)
        else:
            self.route_pair(first, "None", "None")
        self.refresh_mixer()

    def swap_toggled(self, on):
        _, first, _ = self.current_pair()
        k = self.current_mix()
        if k:
            self.route_pair_to_mix(first, k, swapped=on)

    def fader_max(self):
        return GAIN_MAX if self.app_state.get("boost") else UNITY

    def _make_strip(self, item):
        f = QtWidgets.QFrame()
        f.setObjectName("strip")
        f.setFixedWidth(82)
        v = QtWidgets.QVBoxLayout(f)
        v.setContentsMargins(4, 8, 4, 8)
        v.setSpacing(4)
        name = QtWidgets.QLabel()
        name.setAlignment(Qt.AlignCenter)
        name.setWordWrap(True)
        name.setFixedHeight(34)
        v.addWidget(name)
        send = Knob(0, UNITY, 0, size=34, colour="#b48cff", label="FX")
        send.setToolTip("Effect send (shared by all mixes). Double-click to zero.")
        pan = Knob(-100, 100, 0, size=42, bipolar=True, label="Pan")
        pan.setToolTip("Pan — drag up/down, double-click to centre")
        if item.startswith("Reverb"):
            send.setVisible(False)
        v.addWidget(send, 0, Qt.AlignHCenter)
        v.addWidget(pan, 0, Qt.AlignHCenter)
        pan_lbl = QtWidgets.QLabel("C")
        pan_lbl.setAlignment(Qt.AlignCenter)
        pan_lbl.setObjectName("muted")
        v.addWidget(pan_lbl)
        fader = Fader(self.fader_max())
        fader.setToolTip("Drag, scroll, or double-click for 0 dB")
        v.addWidget(fader, 1)
        db = QtWidgets.QLabel("−∞")
        db.setAlignment(Qt.AlignCenter)
        v.addWidget(db)
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(3)
        solo, mute = QtWidgets.QPushButton("S"), QtWidgets.QPushButton("M")
        solo.setObjectName("solo")
        mute.setObjectName("mute")
        for b in (solo, mute):
            b.setCheckable(True)
            b.setFixedSize(32, 26)
            btns.addWidget(b)
        v.addLayout(btns)
        link, partner = None, None
        m = re.fullmatch(r"(Analog-A-|Stream-|S/PDIF-|Reverb-)(\d+)", item)
        if m:
            n = int(m.group(2))
            partner = f"{m.group(1)}{n + 1 if n % 2 else n - 1}"
            if n % 2:
                link = QtWidgets.QPushButton("🔗 link")
                link.setObjectName("link")
                link.setCheckable(True)
                link.setToolTip(f"Link fader, mute and solo with {friendly(partner)}")
                v.addWidget(link)
            else:
                v.addSpacing(30)
        name.setContextMenuPolicy(Qt.CustomContextMenu)
        name.customContextMenuRequested.connect(lambda pos, it=item, nm=name: self.strip_menu(it, nm.mapToGlobal(pos)))
        st = {"item": item, "frame": f, "name": name, "send": send, "pan": pan, "pan_lbl": pan_lbl,
              "fader": fader, "db": db, "mute": mute, "solo": solo, "link": link, "partner": partner}
        self.strips.append(st)

        def write_gain():
            k = self.current_mix()
            if k is None or mute.isChecked() or self.is_soloed_out(k, item):
                return
            slot = self.slot_of(item)
            if slot is not None:
                self.app_state["pans"][f"{k}:{item}"] = pan.value()
                self.save_app_state()
                self.mix_set_lr(k, slot, *from_pan(fader.gain, pan.value()))

        deb = Debounced(write_gain, 30)

        def on_fader(val):
            db.setText(gain_text(val))
            deb()
            p = self.strip_partner_linked(item)
            if p:
                ps = self.strip(p)
                if ps and ps["fader"].gain != val:
                    ps["fader"].gain = val
                    ps["fader"].update()
                    ps["fader"].gainChanged.emit(val)

        def on_pan(val):
            pan_lbl.setText(pan_text(val))
            deb()

        send_deb = Debounced(lambda: self.write_send(item, send.value(), pan.value()), 40)
        send.valueChanged.connect(lambda _: send_deb())
        fader.gainChanged.connect(on_fader)
        pan.valueChanged.connect(on_pan)
        mute.toggled.connect(lambda on: self.set_mute(item, on, propagate=True))
        solo.toggled.connect(lambda on: self.set_solo(item, on, propagate=True))
        if link is not None:
            def on_link(on):
                self.app_state["links"][item] = on
                self.save_app_state()
            link.toggled.connect(on_link)
        return f

    def strip(self, item):
        for s in self.strips:
            if s["item"] == item:
                return s
        return None

    def strip_partner_linked(self, item):
        s = self.strip(item)
        if not s or not s["partner"]:
            return None
        key = item if s["link"] is not None else s["partner"]
        return s["partner"] if self.app_state["links"].get(key) else None

    def write_send(self, item, level, pan):
        slot = self.slot_of(item)
        if slot is not None:
            self.mix_set_lr(None, slot, *from_pan(level, pan), buses=FX_BUS)

    def set_mute(self, item, on, propagate=False):
        k = self.current_mix()
        slot = self.slot_of(item)
        if k is None or slot is None:
            return
        key = f"{k}:{item}"
        mutes = self.app_state["mutes"]
        if on and key not in mutes:
            mutes[key] = list(self.mix_lr(k, slot))
            self.mix_set_lr(k, slot, 0, 0)
        elif not on and key in mutes:
            l, r = mutes.pop(key)
            if not self.is_soloed_out(k, item):
                self.mix_set_lr(k, slot, l, r)
        self.save_app_state()
        p = self.strip_partner_linked(item) if propagate else None
        if p:
            ps = self.strip(p)
            ps["mute"].blockSignals(True)
            ps["mute"].setChecked(on)
            ps["mute"].blockSignals(False)
            self.set_mute(p, on)

    def is_soloed_out(self, k, item):
        solo = self.app_state["solo"].get(str(k), [])
        return bool(solo) and item not in solo

    def set_solo(self, item, on, propagate=False):
        k = self.current_mix()
        if k is None:
            return
        solo = self.app_state["solo"].setdefault(str(k), [])
        saved = self.app_state.setdefault("solo_saved", {}).setdefault(str(k), {})
        was_any = bool(solo)
        targets = [item] + ([self.strip_partner_linked(item)] if propagate and self.strip_partner_linked(item) else [])
        for t in targets:
            if on and t not in solo:
                solo.append(t)
            elif not on and t in solo:
                solo.remove(t)
        all_items = [s["item"] for s in self.strips]
        if solo and not was_any:
            for it in all_items:
                slot = self.slot_of(it)
                if slot is not None:
                    saved[it] = list(self.mix_lr(k, slot))
        for it in all_items:
            slot = self.slot_of(it)
            if slot is None or it not in saved:
                continue
            if solo and it not in solo:
                self.mix_set_lr(k, slot, 0, 0)
            elif f"{k}:{it}" not in self.app_state["mutes"]:
                self.mix_set_lr(k, slot, *saved[it])
        if not solo:
            self.app_state["solo_saved"].pop(str(k), None)
        self.save_app_state()
        self.refresh_mixer()

    def _make_master(self):
        f, lay = card("Master")
        f.setFixedWidth(210)
        self.master_note = muted("")
        lay.addWidget(self.master_note)
        row = QtWidgets.QHBoxLayout()
        self.trim_l, self.trim_r = Fader(UNITY), Fader(UNITY)
        for fd, lbl in ((self.trim_l, "L"), (self.trim_r, "R")):
            col = QtWidgets.QVBoxLayout()
            col.addWidget(fd, 1)
            l = QtWidgets.QLabel(lbl)
            l.setAlignment(Qt.AlignCenter)
            col.addWidget(l)
            row.addLayout(col)
        lay.addLayout(row, 1)
        self.master_link = QtWidgets.QCheckBox("Move together")
        self.master_link.setChecked(True)
        lay.addWidget(self.master_link)
        self.master_val = QtWidgets.QLabel()
        self.master_val.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.master_val)
        self.knob_assign = QtWidgets.QCheckBox("Big knob controls this")
        lay.addWidget(self.knob_assign)
        lay.addWidget(muted("Big knob"))
        self.knob_bar2 = ValueBar(255)
        lay.addWidget(self.knob_bar2)

        # trims are 0..255; the fader works in gain units, so map 255 ↔ UNITY
        def to_trim(g):
            return max(0, min(255, round(255 * g / UNITY)))

        def write_trims():
            pair = self.current_pair()[2]
            c = self.ctrl("output-trim")
            if pair and c:
                vals = list(c.values)
                vals[pair[0] - 1], vals[pair[1] - 1] = to_trim(self.trim_l.gain), to_trim(self.trim_r.gain)
                if vals != c.values:
                    self.write(c, vals)

        send = Debounced(write_trims, 40)

        def moved(side, g):
            if self.master_link.isChecked():
                other = self.trim_r if side == 0 else self.trim_l
                other.gain = g
                other.update()
            self.master_val.setText(f"{to_trim(self.trim_l.gain) * 100 // 255}%  /  "
                                    f"{to_trim(self.trim_r.gain) * 100 // 255}%")
            send()

        self.trim_l.gainChanged.connect(lambda g: moved(0, g))
        self.trim_r.gainChanged.connect(lambda g: moved(1, g))

        def knob_toggled(on):
            pair = self.current_pair()[2]
            c = self.ctrl("master-knob-assign")
            if pair and c:
                vals = list(c.values)
                vals[pair[0] - 1] = vals[pair[1] - 1] = bool(on)
                self.write(c, vals)

        self.knob_assign.toggled.connect(knob_toggled)
        return f

    def refresh_mixer(self):
        if not hasattr(self, "pair_tabs"):
            return
        name, first, analog = self.current_pair()
        kind, val, swapped = self.pair_source(first)
        idx = next((i for i, (ck, cv, _) in enumerate(self.pair_choices) if ck == kind and cv == val), -1)
        self.pair_src.blockSignals(True)
        if idx < 0:
            if self.pair_src.count() == len(self.pair_choices):
                self.pair_src.addItem("Custom (see Advanced)")
            idx = self.pair_src.count() - 1
        self.pair_src.setCurrentIndex(idx)
        self.pair_src.blockSignals(False)
        self.swap_btn.blockSignals(True)
        self.swap_btn.setChecked(swapped)
        self.swap_btn.setEnabled(kind == "mix")
        self.swap_btn.blockSignals(False)

        k = val if kind == "mix" else None
        if k is None:
            self.mix_hint.setText("Direct feed — no mixer. Pick a Stereo Mix to mix sources.")
        else:
            others = [n for n, f2, _ in OUTPUT_PAIRS if f2 != first and self.pair_source(f2)[:2] == ("mix", k)]
            self.mix_hint.setText(f"Stereo Mix {k}" + (f" · also heard on {', '.join(others)}" if others else ""))

        mutes, solo = self.app_state["mutes"], self.app_state["solo"].get(str(k), [])
        saved = self.app_state.get("solo_saved", {}).get(str(k), {})
        fmax = self.fader_max()
        fx = self.ctrl("mixer-source-gain", FX_BUS[0]), self.ctrl("mixer-source-gain", FX_BUS[1])
        used = set()
        if k is not None:
            for st in self.strips:
                it, sl = st["item"], self.slot_of(st["item"])
                if sl is None:
                    continue
                if any(self.mix_lr(k, sl)) or f"{k}:{it}" in mutes or it in solo:
                    used.add(it)
                    if st["partner"]:
                        used.add(st["partner"])
        filt = bool(self.app_state["only_used"]) and k is not None and bool(used)
        for st in self.strips:
            st["frame"].setVisible(not filt or st["item"] in used)
        for gf, items in getattr(self, "mix_groups", []):
            gf.setVisible(not filt or any(i in used for i in items))
        for st in self.strips:
            item = st["item"]
            slot = self.slot_of(item)
            tag = ""
            m = re.fullmatch(r"Analog-A-(\d)", item)
            if m:
                tag = self.label(f"in{int(m.group(1)) - 1}", "")
            custom = self.label(f"strip:{item}", "")
            title = custom or friendly(item)
            sub = friendly(item) if custom else tag
            st["name"].setText(f"<b>{title[:14]}</b>" + (f"<br><span style='color:{SUBTLE}'>"
                                                          f"<small>{sub[:12]}</small></span>" if sub else ""))
            st["name"].setToolTip((tag + "\n" if tag else "") + "Right-click to rename, colour or MIDI-learn")
            col = self.label(f"stripcol:{item}", "")
            st["frame"].setStyleSheet(f"QFrame#strip {{ border-top: 3px solid {col}; }}" if col else "")
            st["frame"].setEnabled(k is not None and slot is not None)
            st["fader"].setMax(fmax)
            if slot is not None:
                self.meters = [x for x in self.meters if x[2] is not st["fader"]]
                self.meters.append(("mixer-source-meter", slot, st["fader"]))
            if st["link"] is not None:
                st["link"].blockSignals(True)
                st["link"].setChecked(bool(self.app_state["links"].get(item)))
                st["link"].blockSignals(False)
            if k is None or slot is None or st["fader"].dragging:
                continue
            key = f"{k}:{item}"
            if key in mutes:
                l, r = mutes[key]
            elif solo and item not in solo and item in saved:
                l, r = saved[item]
            else:
                l, r = self.mix_lr(k, slot)
            level, pan = to_pan(l, r)
            if level == 0:
                pan = self.app_state["pans"].get(key, pan)
            else:
                self.app_state["pans"][key] = pan
            send_level = to_pan(fx[0].values[slot], fx[1].values[slot])[0] if all(fx) else 0
            for wdg in (st["pan"], st["mute"], st["solo"], st["send"]):
                wdg.blockSignals(True)
            if level > fmax:
                st["fader"].setMax(level)
            st["fader"].setGain(level)
            st["pan"].setValue(pan)
            st["send"].setValue(min(UNITY, send_level))
            st["mute"].setChecked(key in mutes)
            st["solo"].setChecked(item in solo)
            for wdg in (st["pan"], st["mute"], st["solo"], st["send"]):
                wdg.blockSignals(False)
            st["db"].setText(gain_text(level))
            st["pan_lbl"].setText(pan_text(pan))

        trims = self.ctrl("output-trim")
        enabled = bool(analog and trims)
        for wdg in (self.trim_l, self.trim_r, self.master_link, self.knob_assign):
            wdg.setEnabled(enabled)
        if enabled:
            self.master_note.setText(f"Output level for {name}. Uneven = balance.")
            for fd, n in ((self.trim_l, analog[0]), (self.trim_r, analog[1])):
                fd.setGain(round(trims.values[n - 1] / 255 * UNITY))
            self.master_val.setText(f"{trims.values[analog[0] - 1] * 100 // 255}%  /  "
                                    f"{trims.values[analog[1] - 1] * 100 // 255}%")
            ka = self.ctrl("master-knob-assign")
            if ka:
                self.knob_assign.blockSignals(True)
                self.knob_assign.setChecked(bool(ka.values[analog[0] - 1]))
                self.knob_assign.blockSignals(False)
        else:
            self.master_note.setText("Headphones use their own front-panel knobs; S/PDIF is fixed level.")
            self.master_val.setText("")

    # ================================================================ SPEAKERS
    def _build_speakers(self):
        return self.wrap_tabs("Speakers & apps", "Which speakers play, and where each app's sound goes.",
                              [("Speaker sets", self._build_speaker_sets()), ("Apps", self._build_apps()),
                               ("EQ", self._build_eq())])

    # ---------------------------------------------------------------- 4.15: EQ
    def _build_eq(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(14)
        c, l = card("EQ for each speaker set", "Correct your headphones or speakers. The EQ follows the Spkr "
                                               "button. The Mbox's headphone jack hears the same thing as the "
                                               "speakers, so headphones get their EQ when you tick 'Listening on "
                                               "headphones' below or switch the speakers off.")
        top = QtWidgets.QHBoxLayout()
        self.eq_on_btn = QtWidgets.QPushButton()
        self.eq_on_btn.clicked.connect(self.toggle_eq)
        top.addWidget(self.eq_on_btn)
        self.eq_state = muted("")
        top.addWidget(self.eq_state, 1)
        l.addLayout(top)
        self.eq_rows = QtWidgets.QGridLayout()
        self.eq_rows.setColumnStretch(1, 1)
        self.eq_rows.setHorizontalSpacing(14)
        l.addLayout(self.eq_rows)
        brow = QtWidgets.QHBoxLayout()
        find = QtWidgets.QPushButton("Find my headphones (AutoEQ)…")
        find.setToolTip("Searches AutoEQ's free database of measured corrections for over 5,000 headphones.")
        find.clicked.connect(self.autoeq_dialog)
        imp = QtWidgets.QPushButton("Import ParametricEQ.txt…")
        imp.setToolTip("A file from autoeq.app, REW (room measurement) or Equalizer APO.")
        imp.clicked.connect(self.import_eq_file)
        brow.addWidget(find)
        brow.addWidget(imp)
        brow.addStretch(1)
        l.addLayout(brow)
        l.addWidget(muted("EQ changes computer sound only — things the Mbox mixes itself (like a source on Aux 5/6) "
                          "aren't touched. Switching profiles is instant and silent."))
        lay.addWidget(c)
        tc, tl = card("Tone", "Quick bass and treble on top of whatever profile is playing. The line shows what "
                              "you hear: above the middle = louder, below = quieter. Dashed = the profile alone.")
        self.eq_graph = EqGraph()
        tl.addWidget(self.eq_graph)
        self.tone = {}
        for key, label, freq in (("bass", "Bass", 105), ("treble", "Treble", 8000)):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(label), 0)
            sl = QtWidgets.QSlider(Qt.Horizontal)
            sl.setRange(-12, 12)
            sl.setValue(int(self.app_state.get("eq_tone", {}).get(key, 0)))
            val = QtWidgets.QLabel()
            val.setMinimumWidth(56)
            sl.valueChanged.connect(lambda v, val=val: val.setText(f"{v:+d} dB"))
            sl.valueChanged.connect(lambda _: self.tone_debounce.start(150))
            val.setText(f"{sl.value():+d} dB")
            row.addWidget(sl, 1)
            row.addWidget(val)
            tl.addLayout(row)
            self.tone[key] = sl
        self.tone_debounce = QtCore.QTimer(self)
        self.tone_debounce.setSingleShot(True)
        self.tone_debounce.timeout.connect(self.tone_changed)
        lay.addWidget(tc)
        lay.addStretch(1)
        self.refresh_eq_ui()
        self.draw_eq()
        return scroll(w)

    def eq_profiles(self):
        return {"Off (flat)": EQ_FLAT, **self.app_state.get("eq_profiles", {})}

    def refresh_eq_ui(self):
        on = bool(self.app_state.get("eq_on"))
        self.eq_on_btn.setText("Turn EQ off" if on else "Turn EQ on")
        self.eq_on_btn.setToolTip("Puts an EQ between the computer and your main output (PipeWire restarts for a "
                                  "second, once). Flat = exactly the same sound as without it.")
        self.eq_state.setText("On — it follows the Spkr button." if on else "Off.")
        while self.eq_rows.count():
            it = self.eq_rows.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        names = list(self.eq_profiles())
        assign = self.app_state.get("eq_sets", {})
        for r, L in enumerate("ABCH"):
            if L == "H":
                self.eq_rows.addWidget(QtWidgets.QLabel("🎧 <b>Headphones</b>"), r, 0)
            else:
                own = self.label(f"spk{L}", "")
                dot = LETTER_COLOUR[L][1]
                self.eq_rows.addWidget(QtWidgets.QLabel(f"<span style='color:{dot}'>●</span> <b>Speakers {L}</b>"
                                                        + (f" — {html_escape(own)}" if own else "")), r, 0)
            cb = QtWidgets.QComboBox()
            cb.addItems(names)
            cb.setCurrentText(assign.get(L, "Off (flat)") if assign.get(L) in names else "Off (flat)")
            cb.setEnabled(on)
            cb.activated.connect(lambda _, L=L, cb=cb: self.assign_eq(L, cb.currentText()))
            self.eq_rows.addWidget(cb, r, 1)
            sub = QtWidgets.QCheckBox("Subsonic filter")
            sub.setChecked(bool(self.app_state.get("subsonic_sets", {}).get(L)))
            sub.setToolTip(f"Cuts everything below {SUBSONIC_HZ:.0f} Hz (24 dB/octave) — rumble and the thumps of a "
                           "hard bass pluck that make small speakers pop. Music doesn't go that low (a bass "
                           "guitar's lowest note is 41 Hz). Turn it off for a subwoofer or full-range speakers "
                           "you want to hear everything on. Computer sound only.")
            sub.toggled.connect(lambda on, L=L: self.set_subsonic(L, on))
            self.eq_rows.addWidget(sub, r, 2)
        hp = QtWidgets.QCheckBox("Listening on headphones — use the Headphones EQ (also automatic when the "
                                 "speakers are switched off)")
        hp.setChecked(bool(self.app_state.get("hp_listen")))
        hp.setToolTip("The speakers, if they're on, get the headphone EQ too while this is ticked.")
        hp.toggled.connect(self.set_hp_listen)
        self.eq_rows.addWidget(hp, 4, 0, 1, 3)
        t = self.eq_target() if hasattr(self, "by_name") else "A"
        now = "Headphones" if t == "H" else f"Speakers {t}"
        self.eq_rows.addWidget(muted(f"Playing now: {now} — "
                                     f"{self.app_state.get('eq_sets', {}).get(t, 'Off (flat)')}"), 5, 0, 1, 3)

    def set_hp_listen(self, on):
        self.app_state["hp_listen"] = bool(on)
        self.save_app_state()
        self.apply_eq()
        QtCore.QTimer.singleShot(0, self.refresh_eq_ui)
        self.update_status()
        self.toast("Headphone EQ on." if on else "EQ follows the Spkr button again.", 4000)

    def set_subsonic(self, letter, on):
        before = chain_wanted(self.app_state)
        self.app_state.setdefault("subsonic_sets", {})[letter] = bool(on)
        self.subsonic_changed(before)
        who = "the headphones" if letter == "H" else f"Speakers {letter}"
        self.toast(f"Subsonic filter {'on' if on else 'off'} for {who}.", 4000)

    def enable_subsonic_everywhere(self):
        before = chain_wanted(self.app_state)
        self.app_state["subsonic_sets"] = {L: True for L in "ABCH"}
        self.subsonic_changed(before)
        self.refresh_eq_ui()
        self.toast("Subsonic filter on — turn it off per speaker set in Speakers → EQ.", 6000)

    def subsonic_changed(self, before):
        """Live if the chain is already there with the filter in it; otherwise (re)build it — one short
        PipeWire restart."""
        self.app_state["subsonic_asked"] = True
        self.save_app_state()
        after = chain_wanted(self.app_state)
        sink = mbox_pw_node("sink")
        has_filter = MAIN_CONF.exists() and "name = sub1 " in MAIN_CONF.read_text()
        if sink and (after != before or (after and not has_filter)):
            write_main_conf(sink, self.main_output_name(), eq=after)
            restart_pipewire()
            QtCore.QTimer.singleShot(2500, lambda: (fix_default_sink(), self.apply_eq()))
        else:
            self.apply_eq()
        self.refresh_health()

    def assign_eq(self, letter, name):
        self.app_state.setdefault("eq_sets", {})[letter] = name
        self.save_app_state()
        self.apply_eq()
        QtCore.QTimer.singleShot(0, self.refresh_eq_ui)

    def current_set(self):
        it = (self.ctrl_item("spkr-button") or "").split("-")[0]
        return SPEAKERS.get(it, ("A",))[0]

    def eq_target(self):
        """Whose EQ plays: 'H' (headphones) when 'Listening on headphones' is ticked or the speakers are
        switched off — the Mbox's headphone jack hears the same mix as the speakers — else the speaker set."""
        if self.app_state.get("hp_listen") or (self.ctrl_item("spkr-button") or "Off") == "Off":
            return "H"
        return self.current_set()

    def eq_profile_now(self, tone=True):
        target = self.eq_target()
        name = self.app_state.get("eq_sets", {}).get(target, "Off (flat)")
        prof = dict(self.eq_profiles().get(name, EQ_FLAT))
        tone = self.app_state.get("eq_tone", {}) if tone else {}
        extra = [{"type": "LSC", "f": 105.0, "q": 0.7, "g": float(tone.get("bass", 0))},
                 {"type": "HSC", "f": 8000.0, "q": 0.7, "g": float(tone.get("treble", 0))}]
        base = list(prof.get("bands", []))
        prof["bands"] = base + [b for b in extra if b["g"]]
        # drop the level only as far as the combined curve rises above the profile's own peak: never clips
        extra_peak = eq_peak_db(prof["bands"]) - eq_peak_db(base)
        prof["preamp"] = float(prof.get("preamp", 0.0)) - max(0.0, extra_peak)
        if not self.app_state.get("eq_on"):
            prof = dict(EQ_FLAT)                         # chain only there for the subsonic filter
        prof["subsonic"] = bool(self.app_state.get("subsonic_sets", {}).get(target))
        return prof

    def apply_eq(self):
        self.draw_eq()
        if not chain_wanted(self.app_state) or os.environ.get("MBOX_AMIXER"):
            return False
        ok = set_eq_live(MAIN_SINK, self.eq_profile_now())
        if not ok:
            self.toast("Couldn't change the EQ — is the main output there? (Settings → Sound)", 6000)
        return ok

    def draw_eq(self):
        if not hasattr(self, "eq_graph"):
            return
        t = self.eq_target()
        who = "Headphones" if t == "H" else f"Speakers {t}"
        if not self.app_state.get("eq_on"):
            name = "EQ off"
        else:
            name = self.app_state.get("eq_sets", {}).get(t, "Off (flat)")
        self.eq_graph.show_profile(self.eq_profile_now(), self.eq_profile_now(tone=False), f"{who}: {name}")

    def tone_changed(self):
        self.app_state["eq_tone"] = {k: s.value() for k, s in self.tone.items()}
        self.save_app_state()
        self.apply_eq()

    def toggle_eq(self):
        on = not self.app_state.get("eq_on")
        sink = mbox_pw_node("sink")
        if not sink and not os.environ.get("MBOX_AMIXER"):
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't find the Mbox in PipeWire.")
            return
        before = chain_wanted(self.app_state)
        self.app_state["eq_on"] = on
        self.save_app_state()
        if sink and chain_wanted(self.app_state) != before:
            write_main_conf(sink, self.main_output_name(), eq=chain_wanted(self.app_state))
            restart_pipewire()
            QtCore.QTimer.singleShot(2500, lambda: (fix_default_sink(), self.apply_eq()))
        else:
            self.apply_eq()                              # chain already there (subsonic filter): switch live
        self.refresh_eq_ui()
        self.toast("EQ on — pick a profile for each speaker set." if on else "EQ off.", 6000)

    def save_eq_profile(self, prof, assign_to=None):
        self.app_state.setdefault("eq_profiles", {})[prof["name"]] = prof
        if assign_to:
            self.app_state.setdefault("eq_sets", {})[assign_to] = prof["name"]
        self.save_app_state()
        self.refresh_eq_ui()
        self.apply_eq()

    def import_eq_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "ParametricEQ.txt", str(HOME / "Downloads"),
                                                        "EQ files (*.txt);;All files (*)")
        if not path:
            return
        try:
            prof = parse_parametric_eq(Path(path).read_text(errors="replace"),
                                       Path(path).stem.replace(" ParametricEQ", ""))
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.save_eq_profile(prof)
        self.toast(f"Added “{prof['name']}” — pick it for a speaker set.", 6000)

    def autoeq_dialog(self):
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Find your headphones")
        d.resize(560, 480)
        v = QtWidgets.QVBoxLayout(d)
        q = QtWidgets.QLineEdit()
        q.setPlaceholderText("Type the model, e.g. HD 280 Pro, DT 770, ATH-M50x…")
        v.addWidget(q)
        lst = QtWidgets.QListWidget()
        v.addWidget(lst, 1)
        note = muted("Profiles from AutoEQ (github.com/jaakkopasanen/AutoEq). oratory1990's measurements are "
                     "listed first when there's a choice.")
        v.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Use it for"))
        target = QtWidgets.QComboBox()
        target.addItem("Headphones", "H")
        for L in "ABC":
            own = self.label(f"spk{L}", "")
            target.addItem(f"Speakers {L}" + (f" — {own}" if own else ""), L)
        target.setCurrentIndex(0)                                   # AutoEQ only has headphones
        row.addWidget(target, 1)
        use = QtWidgets.QPushButton("Use this one")
        use.setObjectName("primary")
        row.addWidget(use)
        v.addLayout(row)
        timer = QtCore.QTimer(d)
        timer.setSingleShot(True)

        def search():
            if len(q.text().strip()) < 2:
                return
            note.setText("Searching…")
            d.job = AutoEqSearch(q.text().strip())
            d.job.listed.connect(show)
            d.job.start()

        def show(rows, err):
            lst.clear()
            if err:
                note.setText(f"Couldn't reach AutoEQ: {err}")
                return
            for name, by, path in rows:
                it = QtWidgets.QListWidgetItem(f"{name}    — measured by {by}")
                it.setData(Qt.UserRole, (name, path))
                lst.addItem(it)
            note.setText(f"{len(rows)} found." if rows else "Nothing found — try fewer words.")

        def pick():
            it = lst.currentItem()
            if not it:
                return
            name, folder = it.data(Qt.UserRole)          # folder = results/<source>/<rig>/<model>
            note.setText("Downloading…")
            d.job = AutoEqSearch(path=folder, name=name)
            d.job.fetched.connect(lambda prof, err: (
                note.setText(f"Couldn't download it: {err}") if err else
                (self.save_eq_profile(prof, target.currentData()), d.accept(),
                 self.toast(f"{prof['name']} EQ is on Speakers {target.currentData()}.", 7000))))
            d.job.start()
        q.textChanged.connect(lambda _: timer.start(350))
        timer.timeout.connect(search)
        use.clicked.connect(pick)
        lst.itemDoubleClicked.connect(lambda _: pick())
        d.exec()

    # ---------------------------------------------------------------- 4.15: voice chain
    def build_voice_card(self, lay):
        c, l = card("Clean mic (voice chain)", "A cleaned-up version of a mic input for calls, streams and "
                                               "voice-overs: rumble cut, background noise reduced, levels evened out.")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Mic on"))
        self.voice_in = QtWidgets.QComboBox()
        for item, jack in INPUT_JACKS[:4]:
            self.voice_in.addItem(jack, item)
        self.voice_in.setCurrentIndex(max(0, self.voice_in.findData(self.app_state.get("voice_in", "Analog-A-1"))))
        row.addWidget(self.voice_in)
        self.voice_btn = QtWidgets.QPushButton()
        self.voice_btn.clicked.connect(self.toggle_voice)
        row.addWidget(self.voice_btn)
        row.addStretch(1)
        l.addLayout(row)
        self.voice_note = muted("")
        l.addWidget(self.voice_note)
        prow = QtWidgets.QHBoxLayout()
        self.voice_comp_btn = QtWidgets.QPushButton("Get the compressor")
        self.voice_comp_btn.setToolTip("Installs the SWH audio plugins (one password prompt).")
        self.voice_comp_btn.clicked.connect(lambda: self.run_root(install_argv("compressor"),
                                                                  "Installing the compressor…",
                                                                  lambda ok, _m: self.refresh_voice()))
        prow.addWidget(self.voice_comp_btn)
        self.voice_nr_link = QtWidgets.QLabel("<a href='https://github.com/werman/noise-suppression-for-voice'>"
                                              "Get noise suppression (RNNoise)</a>")
        self.voice_nr_link.setOpenExternalLinks(True)
        prow.addWidget(self.voice_nr_link)
        prow.addStretch(1)
        l.addLayout(prow)
        lay.addWidget(c)
        self.refresh_voice()

    VOICE_CONF = "mbox-voice.conf"

    def refresh_voice(self):
        on = (PW_CONF_DIR / self.VOICE_CONF).exists()
        rnn, sc4 = bool(find_ladspa("librnnoise_ladspa.so")), bool(find_ladspa("sc4m_1916.so"))
        self.voice_btn.setText("Turn off" if on else "Turn on")
        self.voice_comp_btn.setVisible(not sc4 and bool(install_argv("compressor")))
        self.voice_nr_link.setVisible(not rnn)
        parts = ["low-cut"] + (["noise reduction"] if rnn else []) + (["compressor"] if sc4 else [])
        self.voice_note.setText((f"On: apps can pick “Mic — clean (Mbox)”. Using {', '.join(parts)}."
                                 if on else f"Off. Ready: {', '.join(parts)}.")
                                + ("" if rnn and sc4 else "  Add the missing pieces below for the full chain."))

    def toggle_voice(self):
        path = PW_CONF_DIR / self.VOICE_CONF
        if path.exists():
            path.unlink()
            restart_pipewire()
            self.refresh_voice()
            return
        src = mbox_pw_node("source")
        if not src:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't find the Mbox input in PipeWire.")
            return
        item = self.voice_in.currentData()
        self.app_state["voice_in"] = item
        self.save_app_state()
        n = int(item[-1])
        self.set_stream(n - 1, item)                       # Input n → capture AUX(n-1)
        conf, rnn, _ = voice_chain_conf(src, f"AUX{n - 1}")
        PW_CONF_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(conf)
        restart_pipewire()

        def check():
            if pw_node_exists("mbox_voice"):
                self.toast("“Mic — clean (Mbox)” is ready — pick it in your call or streaming app.", 8000)
            elif rnn:                                      # the noise plugin wouldn't load: go without it
                path.write_text(voice_chain_conf(src, f"AUX{n - 1}", denoise=False)[0])
                restart_pipewire()
                self.toast("Noise reduction wouldn't load here, so the clean mic runs without it.", 9000)
            else:
                self.toast("The clean mic didn't start — Copy diagnostics and send it over.", 9000)
            QtCore.QTimer.singleShot(2500, fix_default_sink)
            self.refresh_voice()
        QtCore.QTimer.singleShot(3000, check)
        self.refresh_voice()

    def _build_speaker_sets(self):
        w, lay = page("Speakers", "The Spkr button on the front cycles speaker sets A (green), B (red) "
                                  "and C (amber). Assign an output to each set and the Mbox switches "
                                  "between them — only the active set plays. Hold Spkr to listen to "
                                  "Aux In 5/6 on the active set.")
        c, l = card()
        self.cr_enable = QtWidgets.QCheckBox("Let the Spkr button switch outputs")
        self.cr_enable.setChecked(bool(self.app_state["cr_enabled"]))
        self.cr_enable.toggled.connect(self.set_cr_enabled)
        l.addWidget(self.cr_enable)
        self.cr_hold = QtWidgets.QCheckBox("Holding Spkr plays Aux In 5/6 on the active set")
        self.cr_hold.setChecked(bool(self.app_state["cr_aux_hold"]))
        self.cr_hold.toggled.connect(lambda on: (self.app_state.__setitem__("cr_aux_hold", on),
                                                 self.save_app_state()))
        l.addWidget(self.cr_hold)
        lay.addWidget(c)

        self.spk_rows = {}
        grid_card, gl = card("Speaker sets")
        g = QtWidgets.QGridLayout()
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(10)
        for col, h in enumerate(["", "Set", "Output", "Name", "", ""]):
            g.addWidget(muted(h), 0, col)
        analog_pairs = [(n, f) for n, f, a in OUTPUT_PAIRS if a]
        for r, letter in enumerate("ABC", start=1):
            colour = LETTER_COLOUR[letter][1]
            dot = QtWidgets.QLabel("●")
            dot.setStyleSheet(f"color:{colour}; font-size:22pt;")
            g.addWidget(dot, r, 0)
            g.addWidget(QtWidgets.QLabel(f"<b>Speakers {letter}</b>"), r, 1)
            combo = QtWidgets.QComboBox()
            combo.addItem("Not used", None)
            combo.addItem("Mute speakers (headphones only)", -1)
            for n, f in analog_pairs:
                combo.addItem(n, f)
            cur = self.app_state["cr"].get(letter)
            combo.setCurrentIndex(max(0, combo.findData(cur)))
            combo.currentIndexChanged.connect(lambda _, L=letter, cb=combo: self.assign_cr(L, cb.currentData()))
            g.addWidget(combo, r, 2)
            g.addWidget(self.label_edit(f"spk{letter}", "", "e.g. Hi-fi / Desk monitors"), r, 3)
            go = QtWidgets.QPushButton("Switch to this set")
            go.clicked.connect(lambda _=False, L=letter: self.switch_speakers(L))
            g.addWidget(go, r, 4)
            state = muted("")
            g.addWidget(state, r, 5)
            self.spk_rows[letter] = (combo, state)
        g.setColumnStretch(3, 1)
        gl.addLayout(g)
        lay.addWidget(grid_card)

        kc, kl = card("Big knob", "Tick which outputs the big front knob turns up and down.")
        row = QtWidgets.QHBoxLayout()
        for name, first, analog in OUTPUT_PAIRS[:3]:
            row.addWidget(self.bind_check("master-knob-assign", name, 0, analog[0] - 1))
        row.addStretch(1)
        kl.addLayout(row)
        self.knob_bar3 = ValueBar(255)
        kl.addWidget(self.knob_bar3)
        lay.addWidget(kc)

        dc, dl = card("Dim, Mute & Mono", "Press Dim/Mute for a quick volume drop; hold it to mute. "
                                          "These are handled by the Mbox itself.")
        lay.addWidget(dc)
        lay.addStretch(1)
        return scroll(w)

    def refresh_speaker_rows(self):
        spk = self.ctrl_item("spkr-button") or "Off"
        active = SPEAKERS.get(spk.split("-")[0], (None,))[0]
        for letter, (combo, state) in self.spk_rows.items():
            first = self.app_state["cr"].get(letter)
            if letter == active:
                state.setText("<b style='color:#3fb950'>● active</b>" + (" · Aux" if spk.endswith("Blink") else ""))
            elif first is not None and first >= 0 and self.app_state["cr_enabled"]:
                state.setText("muted while another set plays")
            else:
                state.setText("")

    def assign_cr(self, letter, first):
        # a pair can belong to only one set
        for L in "ABC":
            if L != letter and self.app_state["cr"].get(L) == first and first is not None and first >= 0:
                self.app_state["cr"][L] = None
                combo = self.spk_rows[L][0]
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
        self.app_state["cr"][letter] = first
        self.save_app_state()
        if self.app_state["cr_enabled"]:
            self.on_speaker_change(self.ctrl_item("spkr-button"), force=True)

    def speaker_menu(self):
        """The Spkr button, from the screen: pick a set, switch the speakers off, or the headphone EQ."""
        m = QtWidgets.QMenu(self)
        cur = (self.ctrl_item("spkr-button") or "Off").split("-")[0]
        for L in "ABC":
            own = self.label(f"spk{L}", "")
            a = m.addAction(f"Speakers {L}" + (f" — {own}" if own else ""))
            a.setCheckable(True)
            a.setChecked(SPEAKERS.get(cur, (None,))[0] == L)
            a.triggered.connect(lambda _=False, L=L: self.switch_speakers(L))
        a = m.addAction("Speakers off (headphones only)")
        a.setCheckable(True)
        a.setChecked(cur == "Off")
        a.triggered.connect(lambda: self.switch_speakers("off"))
        m.addSeparator()
        hp = m.addAction("Use the headphone EQ")
        hp.setCheckable(True)
        hp.setChecked(bool(self.app_state.get("hp_listen")))
        hp.triggered.connect(lambda on: self.set_hp_listen(on))
        self.spk_menu = m                                     # kept alive while it's open
        m.popup(self.chip_spk.mapToGlobal(QtCore.QPoint(0, self.chip_spk.height())))

    def switch_speakers(self, letter):
        c = self.ctrl("spkr-button")
        if c:
            item = "Off" if letter == "off" else LETTER_COLOUR[letter][0]
            self.off_wanted_until = time.monotonic() + 5 if letter == "off" else 0
            self.write(c, [c.items.index(item)])
            self.on_speaker_change(item, force=True)
            self.update_status()

    def set_cr_enabled(self, on):
        self.app_state["cr_enabled"] = on
        self.save_app_state()
        if on:
            self.on_speaker_change(self.ctrl_item("spkr-button"), force=True)
        else:
            self.restore_all_cr()
        self.update_status()

    def restore_all_cr(self):
        saved = self.app_state["cr_saved"]
        for key, pair in list(saved.items()):
            self.route_pair_raw(int(key.split(":")[0]), pair)
        saved.clear()
        self.save_app_state()
        self.refresh_soon()

    def on_speaker_change(self, item, force=False):
        QtCore.QTimer.singleShot(0, lambda: (self.apply_eq(), self.refresh_eq_ui()))   # each set has its own EQ
        if not item or not self.app_state["cr_enabled"] or self.app_state.get("spk_mute"):
            self.last_spk = item
            return
        if item == self.last_spk and not force:
            return
        self.last_spk = item
        base, blink = item.split("-")[0], item.endswith("Blink")
        letter = SPEAKERS.get(base, (None,))[0]
        assigned = {L: f for L, f in self.app_state["cr"].items() if f is not None}
        if letter not in assigned:
            return
        saved = self.app_state["cr_saved"]
        aux = [self.ctrl("output-source").items.index("Analog-A-5"),
               self.ctrl("output-source").items.index("Analog-A-6")]
        for L, first in assigned.items():
            if first < 0:
                continue
            mute_key, aux_key = f"{first}:muted", f"{first}:aux"
            cur = self.pair_raw(first)
            if L == letter:
                if mute_key in saved:
                    self.route_pair_raw(first, saved.pop(mute_key))
                    cur = self.pair_raw(first)
                if blink and self.app_state["cr_aux_hold"]:
                    if aux_key not in saved:
                        saved[aux_key] = cur
                    self.route_pair_raw(first, aux)
                elif aux_key in saved:
                    self.route_pair_raw(first, saved.pop(aux_key))
            else:
                if aux_key in saved:
                    self.route_pair_raw(first, saved.pop(aux_key))
                    cur = self.pair_raw(first)
                if mute_key not in saved and cur != [0, 0]:
                    saved[mute_key] = cur
                    self.route_pair_raw(first, [0, 0])
        self.save_app_state()
        self.refresh_soon()
        silent = assigned.get(letter, 0) < 0
        self.toast(f"Speakers {letter}" + (" — speakers muted, headphones only" if silent else "")
                   + (" — listening to Aux In 5/6" if blink and not silent else ""), 3000)

    # ================================================================ RECORD
    def _build_record(self):
        w, lay = page("Record", "Rip tapes and records straight to FLAC, split them into tracks, and "
                                "tag them — or tune up before you play.")
        self.recorder = Recorder()
        self.recorder.finished.connect(self.rec_finished)
        self.rec_file = None

        self.build_take_card(lay)
        self.build_retro_card(lay)
        self.build_multitrack_card(lay)
        self.build_level_card(lay)
        self.build_practice_card(lay)
        rc, rl = card("Rip tape / vinyl")
        top = QtWidgets.QHBoxLayout()
        self.rec_btn = QtWidgets.QPushButton("●  Record")
        self.rec_btn.setObjectName("danger")
        self.rec_btn.setMinimumSize(150, 48)
        self.rec_btn.clicked.connect(self.toggle_record)
        QtCore.QTimer.singleShot(0, lambda: self.auto_rec.isChecked() and self.rec_btn.setText("●  Arm"))
        top.addWidget(self.rec_btn)
        tbox = QtWidgets.QVBoxLayout()
        self.rec_time = QtWidgets.QLabel("0:00")
        self.rec_time.setObjectName("big")
        self.rec_info = muted("")
        self.refreshers.append(lambda: (not self.recorder.running()) and
                               (not self.rec_info.text() or self.rec_info.text().startswith("Source:")) and
                               self.rec_info.setText("Source: " + self.rip_name()))
        tbox.addWidget(self.rec_time)
        tbox.addWidget(self.rec_info)
        self.rec_src_box = QtWidgets.QComboBox()
        for left, _, name in INPUT_PAIRS:
            self.rec_src_box.addItem(f"Stereo: {name}", f"pair:{left}")
        for item, jack in INPUT_JACKS[:4]:
            self.rec_src_box.addItem(f"Mono: {jack} (mic / instrument, centred)", f"mono:{item}")
        self.rec_src_box.setToolTip("What the Record button records. One mic or guitar = Mono, so it plays "
                                    "back in the middle instead of only on the left.")
        self.rec_src_box.setCurrentIndex(max(0, self.rec_src_box.findData(
            self.app_state.get("rec_src") or f"pair:{self.app_state['setup']['rip']}")))
        self.rec_src_box.currentIndexChanged.connect(lambda _: self.set_rec_source(self.rec_src_box.currentData()))
        tbox.addWidget(self.rec_src_box)
        top.addLayout(tbox)
        top.addStretch(1)
        setup_b = QtWidgets.QPushButton("Set up the Mbox for ripping")
        setup_b.setToolTip("Applies the Rip tape/vinyl scene")
        setup_b.clicked.connect(lambda: self.run_scene(self.scene_view(SCENES[1])))
        top.addWidget(setup_b)
        rl.addLayout(top)
        arow = QtWidgets.QHBoxLayout()
        self.auto_rec = QtWidgets.QCheckBox("Hands-free: start when sound begins, stop after")
        self.auto_rec.setToolTip("Arm it, press play on the deck and walk away. Recording starts with the "
                                 "music, stops when the side ends, and the silence before and after is "
                                 "trimmed off.")
        self.auto_rec.setChecked(bool(self.app_state.get("auto_rec", True)))
        arow.addWidget(self.auto_rec)
        self.auto_quiet = QtWidgets.QSpinBox()
        self.auto_quiet.setRange(5, 120)
        self.auto_quiet.setSuffix(" s of silence")
        self.auto_quiet.setValue(int(self.app_state.get("auto_quiet", 20)))
        self.auto_quiet.setToolTip("Longer than the longest quiet bit inside a song or between songs.")
        arow.addWidget(self.auto_quiet)
        arow.addWidget(QtWidgets.QLabel("  Sound means louder than"))
        self.auto_thr = QtWidgets.QSpinBox()
        self.auto_thr.setRange(-70, -10)
        self.auto_thr.setSuffix(" dB")
        self.auto_thr.setValue(int(self.app_state.get("auto_thr", -40)))
        self.auto_thr.setToolTip("Tape hiss on blank leader should stay below this. Raise it if hiss "
                                 "keeps the recording going; lower it for very quiet music.")
        arow.addWidget(self.auto_thr)
        arow.addStretch(1)
        for wdg in (self.auto_rec, self.auto_quiet, self.auto_thr):
            sig = wdg.toggled if wdg is self.auto_rec else wdg.valueChanged
            sig.connect(lambda *_: (self.app_state.update(auto_rec=self.auto_rec.isChecked(),
                                                          auto_quiet=self.auto_quiet.value(),
                                                          auto_thr=self.auto_thr.value()),
                                    self.save_app_state(),
                                    self.rec_btn.setText("●  Arm" if self.auto_rec.isChecked() and
                                                         not self.recorder.running() else self.rec_btn.text())))
        rl.addLayout(arow)
        self.auto_state, self.auto_t0, self.auto_t1 = None, 0.0, 0.0

        mg = QtWidgets.QGridLayout()
        self.rec_meters = []
        for r, lbl in enumerate(("L", "R")):
            mg.addWidget(QtWidgets.QLabel(f"<b>{lbl}</b>"), r, 0)
            m = Meter(thick=16)
            mg.addWidget(m, r, 1)
            self.rec_meters.append([None, m])
        self.rewire_rec_meters()
        mg.setColumnStretch(1, 1)
        rl.addLayout(mg)
        self.level_hint = muted("Play the loudest part first and aim for peaks in the amber zone (−12 to −1 dB). "
                                "Red = clipping: turn the source down.")
        rl.addWidget(self.level_hint)
        self.clip_lbl = QtWidgets.QLabel("")
        rl.addWidget(self.clip_lbl)

        fr = QtWidgets.QHBoxLayout()
        fr.addWidget(QtWidgets.QLabel("Save to"))
        self.rip_dir = QtWidgets.QLineEdit(self.app_state["rip_dir"])
        self.rip_dir.editingFinished.connect(lambda: (self.app_state.__setitem__("rip_dir", self.rip_dir.text()),
                                                      self.save_app_state()))
        fr.addWidget(self.rip_dir, 1)
        pick = QtWidgets.QPushButton("Browse…")
        pick.clicked.connect(self.pick_rip_dir)
        fr.addWidget(pick)
        rl.addLayout(fr)
        lay.addWidget(rc)

        # split & tag
        tc, tl = card("Split & tag", "After recording, find the gaps between songs, name the tracks "
                                     "(or look them up on MusicBrainz), and save FLAC files tagged and "
                                     "sorted as Artist / Album.")
        ff = QtWidgets.QHBoxLayout()
        self.split_src = QtWidgets.QLineEdit()
        self.split_src.setPlaceholderText("Recording to split…")
        self.split_shown = ""
        self.split_src.textChanged.connect(lambda _: QtCore.QTimer.singleShot(0, self.split_src_changed))
        ff.addWidget(self.split_src, 1)
        openb = QtWidgets.QPushButton("Open…")
        openb.clicked.connect(self.pick_split_src)
        ff.addWidget(openb)
        findb = QtWidgets.QPushButton("Find tracks")
        findb.setObjectName("primary")
        findb.clicked.connect(self.find_tracks)
        ff.addWidget(findb)
        tl.addLayout(ff)
        meta = QtWidgets.QHBoxLayout()
        self.tag_artist = QtWidgets.QLineEdit()
        self.tag_artist.setPlaceholderText("Artist")
        self.tag_album = QtWidgets.QLineEdit()
        self.tag_album.setPlaceholderText("Album")
        self.tag_year = QtWidgets.QLineEdit()
        self.tag_year.setPlaceholderText("Year")
        self.tag_year.setMaximumWidth(80)
        for e in (self.tag_artist, self.tag_album, self.tag_year):
            meta.addWidget(e)
        self.first_no = QtWidgets.QSpinBox()
        self.first_no.setRange(1, 99)
        self.first_no.setPrefix("First # ")
        self.first_no.setToolTip("Track number of the first song in this recording. For side B, this "
                                 "continues after side A automatically.")
        self.first_no.valueChanged.connect(self.renumber_tracks)
        meta.addWidget(self.first_no)
        self.cover_path = None
        self.cover_btn = QtWidgets.QPushButton("Cover…")
        self.cover_btn.setToolTip("Pick a photo or scan of the cover / J-card. Saved as folder.jpg and "
                                  "embedded in every track.")
        self.cover_btn.clicked.connect(self.pick_cover)
        meta.addWidget(self.cover_btn)
        mb = QtWidgets.QPushButton("MusicBrainz")
        mb.setToolTip("Look up the track list on MusicBrainz")
        mb.clicked.connect(self.mb_lookup)
        meta.addWidget(mb)
        dg = QtWidgets.QPushButton("Discogs")
        dg.setToolTip("Look up the track list on Discogs (needs a free token — Settings)")
        dg.clicked.connect(self.discogs_lookup)
        meta.addWidget(dg)
        tl.addLayout(meta)
        sens = QtWidgets.QHBoxLayout()
        sens.addWidget(muted("Gap detection: silence below"))
        self.sil_db = QtWidgets.QSpinBox()
        self.sil_db.setRange(-70, -20)
        self.sil_db.setValue(-45)
        self.sil_db.setSuffix(" dB")
        sens.addWidget(self.sil_db)
        sens.addWidget(muted("for at least"))
        self.sil_len = QtWidgets.QDoubleSpinBox()
        self.sil_len.setRange(0.5, 10)
        self.sil_len.setValue(2.0)
        self.sil_len.setSuffix(" s")
        sens.addWidget(self.sil_len)
        sens.addStretch(1)
        tl.addLayout(sens)
        self.wave = WaveformView()
        self.wave.splitsChanged.connect(self.apply_splits)
        self.wave.previewAt.connect(lambda t: self.preview.play(
            self.split_src.text().strip(), t, af=speed_filter(self.split_src.text().strip(), self.speed_pct())))
        self.preview = PreviewPlayer()
        self.wave_env = None
        tl.addWidget(self.wave)
        wrow = QtWidgets.QHBoxLayout()
        wrow.addWidget(QtWidgets.QLabel("Songs in this recording:"))
        self.song_count = QtWidgets.QSpinBox()
        self.song_count.setRange(1, 60)
        self.song_count.setValue(10)
        self.song_count.setToolTip("If you know how many songs there are (from the cover), the app puts "
                                   "the splits at the quietest moments — works even for medleys.")
        wrow.addWidget(self.song_count)
        sb = QtWidgets.QPushButton("Split into that many")
        sb.clicked.connect(self.split_by_count)
        wrow.addWidget(sb)
        stopb = QtWidgets.QPushButton("■ Stop listening")
        stopb.clicked.connect(lambda: self.preview.stop())
        wrow.addWidget(stopb)
        wrow.addStretch(1)
        wrow.addWidget(muted("Click the picture to listen · drag the amber lines · double-click to add a "
                             "split · right-click a line to remove it"))
        tl.addLayout(wrow)
        self.track_table = QtWidgets.QTableWidget(0, 5)
        self.track_table.setHorizontalHeaderLabels(["#", "Title", "Start", "End", "Original-script title"])
        self.track_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.track_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        self.track_table.horizontalHeaderItem(4).setToolTip(
            "Optional: the title in its own script (e.g. Hebrew). Saved in the files as ORIGINALTITLE, "
            "while the main title stays romanized for your player.")
        self.track_table.itemChanged.connect(self.table_edited)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.setMinimumHeight(180)
        self.track_table.setStyleSheet(f"QTableWidget {{ background:{BG2}; border-radius:8px; }}"
                                       f"QHeaderView::section {{ background:{BG1}; border:none; padding:6px; }}")
        tl.addWidget(self.track_table)
        er = QtWidgets.QHBoxLayout()
        addr = QtWidgets.QPushButton("＋ Row")
        addr.clicked.connect(lambda: self.add_track_row(len(self._tracks()) + 1, "", 0, 0))
        delr = QtWidgets.QPushButton("− Row")
        delr.clicked.connect(lambda: self.track_table.removeRow(self.track_table.currentRow())
                             if self.track_table.currentRow() >= 0 else None)
        er.addWidget(addr)
        er.addWidget(delr)
        er.addStretch(1)
        spec = QtWidgets.QPushButton("Spectrogram")
        spec.clicked.connect(self.show_spectrogram)
        er.addWidget(spec)
        idb = QtWidgets.QPushButton("Identify songs")
        idb.setToolTip("Recognises each track by its sound (AcoustID) and fills in titles you haven't typed. "
                       "Needs a free AcoustID key in Settings → App.")
        idb.clicked.connect(self.identify_songs)
        er.addWidget(idb)
        tlb = QtWidgets.QPushButton("Copy tracklist")
        tlb.setToolTip("Copies the track list with times, ready to paste into a new Discogs or "
                       "MusicBrainz entry.")
        tlb.clicked.connect(self.copy_tracklist)
        er.addWidget(tlb)
        aud = QtWidgets.QPushButton("Open in Audacity")
        aud.setToolTip("Opens this recording in Audacity for hand editing (installs it if needed).")
        aud.clicked.connect(lambda: self.open_audacity(self.split_src.text().strip() or None))
        er.addWidget(aud)
        self.cleanup_boxes = {}
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(QtWidgets.QLabel("Clean-up when saving:"))
        tl.addLayout(crow)
        for key, (label, _) in CLEANUP.items():
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(key in self.app_state["cleanup"])
            cb.setToolTip({"declick": "Repairs short clicks and crackle from dust and scratches. "
                                      "Leave off for tapes.",
                           "dehiss": "Gently lowers steady background hiss. Too much makes cymbals "
                                     "swishy, so it's set mild.",
                           "rumble": "Removes deep turntable rumble you can't hear but that eats "
                                     "headroom."}[key])
            cb.toggled.connect(self.save_cleanup)
            crow.addWidget(cb)
            self.cleanup_boxes[key] = cb
        sprow = QtWidgets.QHBoxLayout()
        sprow.addWidget(QtWidgets.QLabel("Tape speed:"))
        self.speed_spin = QtWidgets.QDoubleSpinBox()
        self.speed_spin.setRange(-8.0, 8.0)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setSuffix(" %")
        self.speed_spin.setToolTip("Cassette decks drift: a fast deck makes everything higher and quicker. "
                                   "+1 % speeds the saved tracks up by 1 %, −1 % slows them down — pitch and "
                                   "tempo together, exactly like fixing the deck. Click the waveform to hear it.")
        self.speed_spin.valueChanged.connect(lambda _: self.speed_note.setText(self.speed_text()))
        sprow.addWidget(self.speed_spin)
        mb = QtWidgets.QPushButton("Measure")
        mb.setToolTip("Listens to the whole recording and works out how far the notes sit from normal "
                      "(A = 440 Hz) tuning. Most music is tuned there, so the difference is the deck's speed "
                      "error.")
        mb.clicked.connect(self.measure_speed)
        self.measure_btn = mb
        sprow.addWidget(mb)
        self.speed_note = muted("")
        sprow.addWidget(self.speed_note, 1)
        tl.addLayout(sprow)
        rg = QtWidgets.QCheckBox("Loudness tags (ReplayGain)")
        rg.setChecked(bool(self.app_state.get("replaygain", True)))
        rg.setToolTip("Measures each track and the whole album so players that support ReplayGain play "
                      "your rips at the same volume as the rest of your library. Doesn't change the audio.")
        rg.toggled.connect(lambda on: (self.app_state.__setitem__("replaygain", on), self.save_app_state()))
        crow.addWidget(rg)
        crow.addStretch(1)
        self.export_btn = QtWidgets.QPushButton("Save tracks as FLAC")
        self.export_btn.setObjectName("primary")
        self.export_btn.clicked.connect(self.export_tracks)
        er.addWidget(self.export_btn)
        tl.addLayout(er)
        self.split_status = muted("")
        tl.addWidget(self.split_status)
        lay.addWidget(tc)

        nc, nl = card("Named inputs for your apps",
                      "Adds inputs with plain names — “Rip input (Mbox)”, “Mbox Input 1 "
                      "(mic/guitar)”, etc. — so Audacity, Guitarix and the ripper above find them.")
        self.named_status = muted("")
        nl.addWidget(self.named_status)
        b = QtWidgets.QPushButton("Install / update named inputs")
        b.clicked.connect(self.install_named_inputs)
        nl.addWidget(b, 0, Qt.AlignLeft)
        lay.addWidget(nc)
        self.refreshers.append(lambda: self.named_status.setText(
            "Installed ✓" if (PW_CONF_DIR / "mbox-inputs.conf").exists() else "Not installed yet."))
        self.build_metronome_card(lay)
        self.build_convert_card(lay)
        self.build_loopback_card(lay)
        self.build_voice_card(lay)

        gc, gl = card("Mic & instrument guide", "Pick what you're recording for placement tips and the "
                                                "right settings.")
        grow = QtWidgets.QHBoxLayout()
        self.guide_src = QtWidgets.QComboBox()
        self.guide_src.addItems(list(SOURCE_GUIDE))
        self.guide_in = QtWidgets.QComboBox()
        for item, jack in INPUT_JACKS[:4]:
            self.guide_in.addItem(jack, item)
        gbtn = QtWidgets.QPushButton("Set it up")
        gbtn.setObjectName("primary")
        gbtn.setToolTip("Records this input in mono (centred), and sets its 75 Hz low-cut to suit.")
        gbtn.clicked.connect(self.apply_guide)
        grow.addWidget(self.guide_src, 1)
        grow.addWidget(QtWidgets.QLabel("on"))
        grow.addWidget(self.guide_in)
        grow.addWidget(gbtn)
        gl.addLayout(grow)
        self.guide_text = QtWidgets.QLabel()
        self.guide_text.setWordWrap(True)
        gl.addWidget(self.guide_text)
        self.guide_src.currentIndexChanged.connect(lambda _: self.show_guide())
        self.show_guide()
        lay.addWidget(gc)

        qc, ql = card("Rip queue", "Line up a stack of tapes or records. Pick one, rip its sides, and "
                                   "the next one is ready when you are.")
        self.queue_list = QtWidgets.QListWidget()
        self.queue_list.setMaximumHeight(150)
        self.queue_list.itemDoubleClicked.connect(lambda _: self.queue_use())
        ql.addWidget(self.queue_list)
        qrow = QtWidgets.QHBoxLayout()
        for label, fn, tip in (("＋ Add", self.queue_add, "Add a tape or record to the queue"),
                               ("Use this one", self.queue_use, "Fills in artist/album and starts at side A"),
                               ("Remove", self.queue_remove, "Take it off the queue")):
            b = QtWidgets.QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            qrow.addWidget(b)
        qrow.addStretch(1)
        ql.addLayout(qrow)
        lay.addWidget(qc)
        QtCore.QTimer.singleShot(0, self.queue_refresh)

        ac2, al2 = card("Live analysis", "Loudness (LUFS), spectrum and stereo phase of what you're "
                                         "ripping. Phase near +1 is healthy; below 0 means a wiring or "
                                         "cartridge problem.")
        arow = QtWidgets.QHBoxLayout()
        self.an_btn = QtWidgets.QPushButton("Start analysis")
        self.an_btn.setCheckable(True)
        self.an_btn.toggled.connect(self.toggle_analyzer)
        arow.addWidget(self.an_btn)
        an_reset = QtWidgets.QPushButton("Reset integrated")
        an_reset.clicked.connect(lambda: self.analyzer and self.analyzer.reset_integrated())
        arow.addWidget(an_reset)
        arow.addStretch(1)
        al2.addLayout(arow)
        self.an_view = AnalyzerView()
        al2.addWidget(self.an_view)
        lay.addWidget(ac2)

        self._build_tape_tools(lay)

        tc2, tl2 = card("Tuner", "For guitar, bass, or anything plugged into the Mbox.")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Listen to"))
        self.tuner_ch = QtWidgets.QComboBox()
        self.tuner_ch.addItems(["Input 1", "Input 2", "Input 3", "Input 4", "Aux 5", "Aux 6"])
        row.addWidget(self.tuner_ch)
        self.tuner_btn = QtWidgets.QPushButton("Start tuner")
        self.tuner_btn.setCheckable(True)
        self.tuner_btn.toggled.connect(self.toggle_tuner)
        row.addWidget(self.tuner_btn)
        row.addStretch(1)
        tl2.addLayout(row)
        self.tuner_view = TunerDisplay()
        tl2.addWidget(self.tuner_view)
        lay.addWidget(tc2)
        lay.addStretch(1)

        self.rec_timer = QtCore.QTimer(self)
        self.rec_timer.timeout.connect(self.rec_tick)
        self.rec_timer.start(200)
        self.clips = 0
        self.rec_page = w
        return self.tabify(w, lay, [("Rip", ["Rip tape / vinyl", "Mic & instrument guide", "Rip queue",
                                             "Live analysis"]),
                                    ("Quick take", ["Quick take", "Capture what I just played", "Multitrack",
                                                    "Level check"]),
                                    ("Practice", ["Practice player"]),
                                    ("Split && tag", ["Split & tag"]),
                                    ("Tools", ["Metronome", "Convert audio", "Loopback", "Clean mic (voice chain)", "Make a tape", "Tuner",
                                               "Named inputs for your apps"])])

    def pick_rip_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Save rips to", self.rip_dir.text())
        if d:
            self.rip_dir.setText(d)
            self.app_state["rip_dir"] = d
            self.save_app_state()

    def toggle_record(self):
        if self.recorder.running():
            self.recorder.stop()
            return
        if not pw_node_exists("mbox_tape_in"):
            if QtWidgets.QMessageBox.question(
                    self, "One-time setup", "Recording uses the “Rip input (Mbox)” input, "
                    "which isn't installed yet. Install it now? (PipeWire restarts for a second.)") \
                    != QtWidgets.QMessageBox.Yes:
                return
            if not self.install_named_inputs(ask=False):
                return
            QtCore.QThread.msleep(2500)
        self.route_rip_input()
        folder = Path(self.rip_dir.text() or self.app_state["rip_dir"]) / "_recordings"
        path = folder / f"rip-{datetime.now():%Y-%m-%d_%H%M%S}.flac"
        try:
            self.recorder.start("mbox_tape_in", path, self.rate())
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.clips = 0
        self.auto_state = "armed" if self.auto_rec.isChecked() else None
        self.auto_t0 = self.auto_t1 = 0.0
        if not self.an_btn.isChecked():
            self.an_btn.setChecked(True)
        elif self.analyzer:
            self.analyzer.reset_integrated()
        self.rec_btn.setText("■  Disarm" if self.auto_state else "■  Stop")
        self.rec_live, self.rec_offset = False, 0.0
        self.rec_info.setText("Armed — press play on the deck. Recording starts with the music."
                              if self.auto_state else "Starting… wait for “go”.")

    def rec_tick(self):
        if self.recorder.running() and self.auto_state:
            t = self.recorder.elapsed()
            lvl = max(meter_db(self.input_level(item)) for item, _ in self.rec_meters if item)
            if lvl > self.auto_thr.value():
                if self.auto_state == "armed":
                    self.auto_state, self.auto_t0 = "rec", t
                    self.rec_btn.setText("■  Stop")
                    self.rec_info.setText("Recording — stops by itself when the side ends.")
                self.auto_t1 = t
            if self.auto_state == "armed":
                self.rec_time.setText("waiting…")
                return
            if t - self.auto_t1 > self.auto_quiet.value():
                self.auto_state = "done"
                self.recorder.stop()
                return
            self.rec_time.setText(fmt_time(t - self.auto_t0))
            return
        if self.recorder.running() and getattr(self, "take_mode", False):
            QtCore.QTimer.singleShot(0, self.take_tick)
        if self.recorder.running():
            if not getattr(self, "rec_live", True):
                try:
                    grown = Path(self.recorder.path).stat().st_size > 48000
                except OSError:
                    grown = False
                if not grown:
                    self.rec_time.setText("…")
                    return
                self.rec_live, self.rec_offset = True, self.recorder.elapsed()
                self.rec_info.setText("● Recording — go ahead.")
            self.rec_time.setText(fmt_time(self.recorder.elapsed() - getattr(self, "rec_offset", 0.0)))
            for item, m in self.rec_meters:
                lvl = self.input_level(item)
                if lvl >= METER_MAX - 4:
                    self.clips += 1
            self.clip_lbl.setText(f"<b style='color:#f85149'>Clipped {self.clips}×</b> — turn the source down"
                                  if self.clips else "<span style='color:#3fb950'>No clipping</span>")
        elif self.rec_btn.text().startswith("■"):
            self.recorder.stop()

    # ---------------------------------------------------------------- 4.10: quick takes
    # ---------------------------------------------------------------- 4.16: musician tools
    def input_choices(self, box, key, default="mono:Analog-A-1", pairs=True):
        for item, jack in INPUT_JACKS[:4]:
            own = self.label(f"in{int(item[-1]) - 1}", "")
            box.addItem(jack + (f" ({own})" if own else ""), f"mono:{item}")
        if pairs:                                       # plain stereo pairs — never “whatever the Rip page uses”
            for left, _, name in INPUT_PAIRS:
                box.addItem(f"Stereo: {name}", f"pair:{left}")
        saved = self.app_state.get(key, default)
        if saved == "rip":                              # before 4.19.1: meant the rip pair
            saved = self.app_state[key] = f"pair:{self.app_state['setup'].get('rip', 'Analog-A-5')}"
        box.setCurrentIndex(max(0, box.findData(saved)))
        box.currentIndexChanged.connect(lambda _: (self.app_state.__setitem__(key, box.currentData()),
                                                   self.save_app_state()))

    def source_items(self, choice):
        """'mono:Analog-A-2' / 'pair:Analog-A-5' / 'rip' → the Mbox inputs to show levels for."""
        choice = choice or ""
        if choice == "rip":
            left = self.app_state["setup"].get("rip", "Analog-A-5")
            return [left, pair_right(left)]
        if choice.startswith("pair:"):
            left = choice[5:]
            return [left, pair_right(left)]
        if choice.startswith("multi:"):
            return [i for i in choice[6:].split(",") if i]
        return [choice[5:]] if choice.startswith("mono:") else []

    def capture_for(self, choice):
        """'mono:Analog-A-2' → (pipewire source, ['AUX1'], label), making sure the Mbox sends it."""
        src = mbox_pw_node("source")
        if choice == "rip":
            self.route_rip_input()
            return src, ["AUX4", "AUX5"], self.rip_name()
        item = choice.split(":", 1)[1]
        n = int(item[-1])
        if choice.startswith("pair:"):                    # Inputs n/n+1 → capture AUX(n-1)/AUX(n), their own slots
            right = pair_right(item)
            m = int(right[-1])
            self.set_stream(n - 1, item)
            self.set_stream(m - 1, right)
            own = self.label(f"in{n - 1}", "")
            return src, [f"AUX{n - 1}", f"AUX{m - 1}"], pair_label(item) + (f" — {own}" if own else "")
        self.set_stream(n - 1, item)                      # Input n → capture AUX(n-1)
        own = self.label(f"in{n - 1}", "")
        return src, [f"AUX{n - 1}"], own or dict(INPUT_JACKS).get(item, item)

    def build_retro_card(self, lay):
        c, l = card("Capture what I just played", "Keeps the last few minutes in memory while you noodle. "
                                                 "Played something good? Save it — no need to have pressed record.")
        row = QtWidgets.QHBoxLayout()
        self.retro_btn = QtWidgets.QPushButton()
        self.retro_btn.setCheckable(True)
        self.retro_btn.toggled.connect(self.toggle_retro)
        row.addWidget(self.retro_btn)
        row.addWidget(QtWidgets.QLabel("from"))
        self.retro_src = QtWidgets.QComboBox()
        self.input_choices(self.retro_src, "retro_src")
        row.addWidget(self.retro_src, 1)
        l.addLayout(row)
        self.retro_hear = QtWidgets.QCheckBox("Hear it through the speakers while listening")
        self.retro_hear.setChecked(self.app_state.get("retro_hear", True))
        self.retro_hear.setToolTip("Plays the input through Outputs 1/2 straight from the Mbox's own mixer — "
                                   "no delay. Put back the way it was when you stop listening.")
        self.retro_hear.toggled.connect(self.retro_hear_toggled)
        l.addWidget(self.retro_hear)
        self.retro_strip = LevelStrip(self, lambda: self.source_items(self.retro_src.currentData()),
                                      lambda: min(30, self.retro.held()) if self.retro else 0)
        l.addWidget(self.retro_strip)
        srow = QtWidgets.QHBoxLayout()
        for secs, text in ((30, "Save last 30 s"), (60, "Save last minute"), (300, "Save last 5 min")):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, s=secs: self.save_retro(s))
            srow.addWidget(b)
            setattr(self, f"retro_save_{secs}", b)
        srow.addStretch(1)
        l.addLayout(srow)
        self.retro_note = muted("")
        l.addWidget(self.retro_note)
        lay.addWidget(c)
        self.retro = None
        self.retro_timer = QtCore.QTimer(self)
        self.retro_timer.timeout.connect(self.refresh_retro)
        self.retro_timer.start(1000)
        self.refresh_retro()
        if self.app_state.get("retro_on"):
            QtCore.QTimer.singleShot(4000, lambda: self.retro_btn.setChecked(True))

    def toggle_retro(self, on):
        if on and not self.retro:
            src, auxes, name = self.capture_for(self.retro_src.currentData())
            if not src:
                self.retro_failed("Couldn't find the Mbox input in PipeWire.")
                return
            self.retro = RetroBuffer(src, auxes, self.rate(), seconds=300)
            self.retro.label = name
            self.retro.failed.connect(self.retro_failed)
            self.retro.start()
            self.retro_choice = self.retro_src.currentData()
            if self.retro_hear.isChecked():
                self.retro_monitor(True)
        elif not on and self.retro:
            self.retro.stop()
            self.retro = None
            self.retro_monitor(False)
        self.app_state["retro_on"] = on
        self.save_app_state()
        self.refresh_retro()

    def retro_hear_toggled(self, on):
        self.app_state["retro_hear"] = on
        self.save_app_state()
        if self.retro:
            self.retro_monitor(on)

    def retro_monitor(self, on):
        self.input_monitor("retro", getattr(self, "retro_choice", ""), on)

    def input_monitor(self, owner, choice, on):
        """While `owner` records, play the input through Stereo Mix 1 (Outputs 1/2); put it back afterwards."""
        mons = self.__dict__.setdefault("monitors", {})
        prev = mons.get(owner)
        if not on:
            for k, slot, l_, r_ in reversed(prev or []):
                self.mix_set_lr(k, slot, l_, r_)
            mons.pop(owner, None)
            return
        if prev:
            return
        choice = choice or ""
        items = self.source_items(choice)
        if choice.startswith("multi:") or len(items) == 1:
            want = [(i, UNITY, UNITY) for i in items]   # instruments/mics: each centred, both sides
        else:
            want = [(items[0], UNITY, 0), (items[1], 0, UNITY)]     # a stereo source: left and right
        slots = [(item, self.slot_of(item), l, r) for item, l, r in want]
        if not slots or any(sl is None for _, sl, _, _ in slots):
            self.toast("That input isn't in the Mbox mixer — can't play it through the speakers.", 6000)
            return
        mixes, direct = self.listening_mixes()
        if direct and not self.app_state.get("hp_direct_told"):
            self.app_state["hp_direct_told"] = True
            self.save_app_state()
            self.hint.offer(f"<b>{' and '.join(direct)} play{'s' if len(direct) == 1 else ''} the computer "
                            "directly</b>, so you won't hear your inputs there while recording. To hear them, "
                            "set the headphones to a mix on the Mixer page (e.g. Stereo Mix 1, same as the "
                            "speakers).", [("Open the Mixer", lambda: self.nav.setCurrentRow(
                                self.page_index("Mixer")))])
        mons[owner] = []
        for k in mixes:
            if all(max(self.mix_lr(k, sl)) >= UNITY // 2 for _, sl, _, _ in slots):
                continue                                # already audible in this mix: leave it alone
            for item, sl, l, r in slots:
                mons[owner].append((k, sl, *self.mix_lr(k, sl)))
                self.mix_set_lr(k, sl, l, r)

    def listening_mixes(self):
        """The stereo mixes the speakers (Outputs 1/2) and headphones play, and any output playing the computer
        straight (where an input can't be added)."""
        mixes, direct = [], []
        for name, first in (("Outputs 1/2", 0), ("Headphone A", 6), ("Headphone B", 8)):
            kind, k, _ = self.pair_source(first)
            if kind == "mix" and k not in mixes:
                mixes.append(k)
            elif kind == "direct" and name.startswith("Headphone"):
                direct.append(name)
        return (mixes or [1]), direct

    def retro_failed(self, msg):
        self.retro_error = time.time()
        self.retro_btn.setChecked(False)
        self.toast(f"Stopped listening: {msg}", 10000)
        self.retro_note.setText(f"<b style='color:#f85149'>Couldn't listen:</b> {html_escape(msg)}")

    def refresh_retro(self):
        on = bool(self.retro)
        self.retro_btn.blockSignals(True)
        self.retro_btn.setChecked(on)
        self.retro_btn.blockSignals(False)
        self.retro_btn.setText("● Listening — stop" if on else "Start listening")
        self.retro_btn.setStyleSheet("background:#f85149; color:white;" if on else "")
        self.retro_src.setEnabled(not on)
        held = self.retro.held() if on else 0
        for secs in (30, 60, 300):
            getattr(self, f"retro_save_{secs}").setEnabled(on and held > 1)
        if not on and time.time() - getattr(self, "retro_error", 0) < 30:
            return                                      # leave the error on screen for a while
        self.retro_note.setText(f"Holding the last {int(held // 60)}:{int(held % 60):02d} of {self.retro.label} — "
                                "memory only; nothing is saved unless you press save." if on else
                                "Off. When it's on, it never writes anything to disk until you save.")
        if hasattr(self, "q_retro"):
            self.q_retro.setVisible(on)

    def save_retro(self, seconds):
        if not self.retro:
            self.toast("Turn on “Capture what I just played” first (Record → Quick take).", 6000)
            return
        data = self.retro.snapshot(seconds)
        if not data:
            return
        folder = self.takes_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"caught-{datetime.now():%Y-%m-%d_%H%M%S}.flac"
        try:
            save_raw(data, path, self.retro.rate, len(self.retro.auxes))
        except RuntimeError as e:
            QtWidgets.QMessageBox.warning(self, APP_NAME, f"Couldn't save it: {e}")
            return
        path = self.finish_format(path, polish=True)
        self.app_state["last_take"] = str(path)
        self.save_app_state()
        self.show_last_take()
        self.toast(f"Saved — {path.name}", 6000)

    def build_multitrack_card(self, lay):
        c, l = card("Multitrack", "Record several inputs at once, each to its own file — perfect for a jam, "
                                  "or mic + instrument together. Files line up exactly in any editor.")
        row = QtWidgets.QHBoxLayout()
        self.mt_boxes = {}
        for item, jack in INPUT_JACKS[:4]:
            own = self.label(f"in{int(item[-1]) - 1}", "")
            cb = QtWidgets.QCheckBox(jack + (f" ({own})" if own else ""))
            cb.setChecked(item in self.app_state.get("mt_inputs", ["Analog-A-1", "Analog-A-2"]))
            cb.toggled.connect(lambda _: (self.app_state.__setitem__(
                "mt_inputs", [i for i, b in self.mt_boxes.items() if b.isChecked()]), self.save_app_state(),
                self.refresh_mt_strips()))
            row.addWidget(cb)
            self.mt_boxes[item] = cb
        row.addStretch(1)
        l.addLayout(row)
        self.mt_strips = {}
        self.mt_strip_box = QtWidgets.QVBoxLayout()
        self.mt_strip_box.setSpacing(4)
        for item, jack in INPUT_JACKS[:4]:              # one level strip per ticked input
            st = LevelStrip(self, lambda item=item: [item], spectrum=False, compact=True,
                            title=jack)
            self.mt_strips[item] = st
            self.mt_strip_box.addWidget(st)
        l.addLayout(self.mt_strip_box)
        self.mt_hear = QtWidgets.QCheckBox("Hear them while recording (speakers and headphones)")
        self.mt_hear.setChecked(self.app_state.get("mt_hear", True))
        self.mt_hear.setToolTip("Plays the ticked inputs from the Mbox's own mixer — no delay, exactly what's "
                                "being recorded. Put back the way it was when you stop.")
        self.mt_hear.toggled.connect(self.mt_hear_toggled)
        l.addWidget(self.mt_hear)
        self.mt_norm = QtWidgets.QCheckBox("Normalize each track after recording")
        self.mt_norm.setChecked(self.app_state.get("mt_norm", False))
        self.mt_norm.setToolTip("Turns each track up (or down) on its own so its loudest point sits at −1 dB — "
                                "a quiet mic track comes up next to the bass. Volume only; the sound isn't "
                                "changed. Leave it off if you'll set levels in a mixing program anyway.")
        self.mt_norm.toggled.connect(lambda on: (self.app_state.__setitem__("mt_norm", on),
                                                 self.save_app_state()))
        l.addWidget(self.mt_norm)
        l.addWidget(self.lowcut_row())
        self.refresh_mt_strips()
        self.refreshers.append(self.refresh_mt_strips)      # names change (e.g. the F/R button flipped)
        brow = QtWidgets.QHBoxLayout()
        self.mt_btn = QtWidgets.QPushButton("●  Record multitrack")
        self.mt_btn.clicked.connect(self.toggle_multitrack)
        brow.addWidget(self.mt_btn)
        self.mt_note = muted("")
        brow.addWidget(self.mt_note, 1)
        l.addLayout(brow)
        lay.addWidget(c)
        self.mt_proc = None

    def lowcut_row(self):
        """75 Hz low-cut switches for Inputs 1–4 — a setting in the Mbox itself, so it applies to every
        recording. On for mics (rumble, desk thumps, a bass leaking into the mic); off for bass."""
        box = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QtWidgets.QLabel("Low-cut (75 Hz)")
        lab.setToolTip("Removes rumble, thumps and the low end leaking into a mic. Use it on mics, not on "
                       "bass or kick drum. It's the Mbox's own filter, so it applies to every recording "
                       "and to what you hear.")
        h.addWidget(lab)
        boxes = []
        for i in range(4):
            cb = self.bind_check("input-hp-filter", f"Input {i + 1}", 0, i)
            cb.setToolTip(lab.toolTip())
            h.addWidget(cb)
            boxes.append(cb)
        h.addStretch(1)

        def names():
            for i, cb in enumerate(boxes):
                own = self.label(f"in{i}", "")
                cb.setText(f"Input {i + 1}" + (f" ({own})" if own else ""))
        names()
        self.refreshers.append(names)
        return box

    def show_takes_folder(self):
        folder = self.takes_dir()
        folder.mkdir(parents=True, exist_ok=True)
        last = Path(self.app_state.get("last_take") or "")
        reveal_in_files(last if last.is_file() and last.parent == folder else folder)

    def mt_choice(self):
        return "multi:" + ",".join(self.app_state.get("mt_inputs", []))

    def mt_hear_toggled(self, on):
        self.app_state["mt_hear"] = on
        self.save_app_state()
        if self.mt_proc:
            self.input_monitor("multitrack", self.mt_choice(), on)

    def refresh_mt_strips(self):
        for item, st in self.mt_strips.items():
            own = self.label(f"in{int(item[-1]) - 1}", "")
            st.title = f"Input {item[-1]}" + (f" — {own}" if own else "")
            self.mt_boxes[item].setText(f"Input {item[-1]}" + (f" ({own})" if own else ""))
            st.setVisible(self.mt_boxes[item].isChecked())

    def toggle_multitrack(self):
        if self.mt_proc:
            self.mt_proc.terminate()
            self.mt_proc.wait(3000)
            self.mt_proc = None
            self.input_monitor("multitrack", "", False)
            self.mt_timer.stop()
            self.mt_btn.setText("●  Record multitrack")
            self.mt_btn.setStyleSheet("")
            try:
                outs = split_multitrack(self.mt_file, self.mt_names, self.mt_folder)
                self.mt_file.unlink()
                if self.mt_norm.isChecked():
                    key = self.app_state.get("save_fmt", "flac")
                    lossy = key.startswith("mp3") or key in ("aac", "opus", "ogg")
                    for o in outs:
                        try:
                            polish_take(o, "normalize", target=-2.0 if lossy else -1.0)
                        except (RuntimeError, OSError) as e:
                            self.toast(f"Couldn't normalize {o.name}: {e}", 8000)
                outs = [self.finish_format(o) for o in outs]
                self.mt_note.setText(f"Saved {len(outs)} tracks in {self.mt_folder.name}.")
                self.toast(f"Saved {len(outs)} tracks — {self.mt_folder}", 8000)
            except (RuntimeError, OSError) as e:
                self.mt_note.setText(f"Couldn't split the tracks: {e}. The combined file is kept: {self.mt_file}")
            return
        items = [i for i, b in self.mt_boxes.items() if b.isChecked()]
        if not items:
            self.mt_note.setText("Tick at least one input.")
            return
        src = mbox_pw_node("source")
        if not src:
            self.mt_note.setText("Couldn't find the Mbox input in PipeWire.")
            return
        auxes, names = [], []
        for item in items:
            _, a, name = self.capture_for(f"mono:{item}")
            auxes += a
            names.append(name)
        self.mt_folder = self.takes_dir() / f"session-{datetime.now():%Y-%m-%d_%H%M%S}"
        self.mt_folder.mkdir(parents=True, exist_ok=True)
        self.mt_file, self.mt_names = self.mt_folder / "all-inputs.wav", names
        self.mt_proc = subprocess.Popen(capture_cmd(src, auxes, self.rate(), out=str(self.mt_file), fmt="s24"),
                                        stderr=subprocess.DEVNULL)
        self.mt_started = time.time()
        if self.mt_hear.isChecked():
            self.input_monitor("multitrack", self.mt_choice(), True)
        self.mt_btn.setText("■  Stop")
        self.mt_btn.setStyleSheet("background:#f85149; color:white;")
        self.mt_timer = QtCore.QTimer(self)
        self.mt_timer.timeout.connect(lambda: self.mt_note.setText(
            f"● Recording {len(names)} tracks — {int((time.time() - self.mt_started) // 60)}:"
            f"{int((time.time() - self.mt_started) % 60):02d}"))
        self.mt_timer.start(500)

    def build_level_card(self, lay):
        c, l = card("Level check", "Play or sing your loudest part for 10 seconds and the app tells you which way "
                                   "to turn the gain knob.")
        row = QtWidgets.QHBoxLayout()
        self.lv_src = QtWidgets.QComboBox()
        self.input_choices(self.lv_src, "level_src", pairs=False)
        row.addWidget(self.lv_src, 1)
        self.lv_btn = QtWidgets.QPushButton("Check my level")
        self.lv_btn.clicked.connect(self.start_level_check)
        row.addWidget(self.lv_btn)
        l.addLayout(row)
        self.lv_note = QtWidgets.QLabel("")
        self.lv_note.setWordWrap(True)
        l.addWidget(self.lv_note)
        lay.addWidget(c)

    def start_level_check(self):
        item = self.lv_src.currentData().split(":", 1)[1]
        self.capture_for(self.lv_src.currentData())
        self.lv_item, self.lv_peak, self.lv_clips, self.lv_end = item, METER_FLOOR_DB, 0, time.time() + 10
        self.lv_btn.setEnabled(False)
        self.lv_timer = QtCore.QTimer(self)
        self.lv_timer.timeout.connect(self.level_tick)
        self.lv_timer.start(50)

    def level_tick(self):
        db = self.item_db(self.lv_item)
        self.lv_peak = max(self.lv_peak, db)
        if db > -0.5:
            self.lv_clips += 1
        left = self.lv_end - time.time()
        if left > 0:
            self.lv_note.setText(f"Play now… {left:.0f} s   (loudest so far {self.lv_peak:.0f} dB)")
            return
        self.lv_timer.stop()
        self.lv_btn.setEnabled(True)
        kind, text = level_advice(self.lv_peak, self.lv_clips)
        colour = {"good": "#3fb950", "hot": "#d29922", "low": "#d29922"}.get(kind, "#f85149")
        self.lv_note.setText(f"<b style='color:{colour}'>●</b> {text}")

    def build_metronome_card(self, lay):
        c, l = card("Metronome", "Clicks in your headphones only (when “Headphones (Mbox)” is installed), so "
                                 "they never end up on a recording from the speakers.")
        row = QtWidgets.QHBoxLayout()
        self.met_bpm = QtWidgets.QSpinBox()
        self.met_bpm.setRange(20, 300)
        self.met_bpm.setSuffix(" bpm")
        self.met_bpm.setValue(int(self.app_state.get("met_bpm", 100)))
        self.met_bpm.valueChanged.connect(self.metronome_changed)
        row.addWidget(self.met_bpm)
        tap = QtWidgets.QPushButton("Tap")
        tap.setToolTip("Tap along a few times to set the tempo.")
        tap.clicked.connect(self.tap_tempo)
        row.addWidget(tap)
        row.addWidget(QtWidgets.QLabel("Time"))
        self.met_sig = QtWidgets.QComboBox()
        for key, label in METER_LABELS:
            self.met_sig.addItem(label, key)
        old = {2: "2/4", 3: "3/4", 4: "4/4", 5: "5/4", 6: "6/8", 1: "none"}.get(self.app_state.get("met_beats"))
        self.met_sig.setCurrentIndex(max(0, self.met_sig.findData(self.app_state.get("met_sig") or old or "4/4")))
        self.met_sig.setToolTip("The accented click is the one. In 6/8, 9/8 and 12/8 the tempo counts the "
                                "dotted-quarter beat and the eighths tick softly in between.")
        self.met_sig.currentIndexChanged.connect(self.metronome_changed)
        row.addWidget(self.met_sig)
        self.met_vol = QtWidgets.QSlider(Qt.Horizontal)
        self.met_vol.setRange(5, 100)
        self.met_vol.setValue(int(self.app_state.get("met_vol", 60)))
        self.met_vol.valueChanged.connect(self.metronome_changed)
        row.addWidget(self.met_vol, 1)
        self.met_btn = QtWidgets.QPushButton("▶  Start")
        self.met_btn.clicked.connect(self.toggle_metronome)
        row.addWidget(self.met_btn)
        l.addLayout(row)
        lay.addWidget(c)
        self.metro, self.taps = None, []

    def tap_tempo(self):
        now = time.time()
        self.taps = [t for t in self.taps if now - t < 3] + [now]
        if len(self.taps) >= 3:
            gaps = [b - a for a, b in zip(self.taps, self.taps[1:])]
            self.met_bpm.setValue(round(60 / (sum(gaps) / len(gaps))))

    def metronome_changed(self):
        self.app_state.update(met_bpm=self.met_bpm.value(), met_sig=self.met_sig.currentData(),
                              met_vol=self.met_vol.value())
        if self.metro:
            self.metro.bpm, self.metro.meter = self.met_bpm.value(), self.met_sig.currentData()
            self.metro.volume = self.met_vol.value() / 100

    def toggle_metronome(self):
        if self.metro:
            self.metro.stop()
            self.metro = None
            self.metro_route(False)
            self.met_btn.setText("▶  Start")
            self.save_app_state()
            return
        target = "mbox_headphones" if pw_node_exists("mbox_headphones", "sink") else None
        if target:
            reached = self.metro_route(True)
            if reached:
                self.toast(f"Metronome → {' and '.join(reached)}.", 4000)
            else:
                self.hint.offer("<b>The metronome can't reach your headphones.</b> Both headphone jacks play the "
                                "same mix as the speakers, so the clicks would end up on the speakers (and on a "
                                "recording). Give the headphones their own mix — the Headphone A jack then "
                                "plays computer sound for headphones, the metronome and anything you monitor.",
                                [("Give headphones their own mix", self.install_headphones)])
                return
        else:
            self.hint.offer("<b>Playing the metronome on your normal output.</b> Add a “Headphones (Mbox)” "
                            "output and it clicks in the headphones only, never on the speakers.",
                            [("Add Headphones (Mbox)", self.install_headphones)])
        self.metro = Metronome(target, self.met_bpm.value(), self.met_sig.currentData(), self.met_vol.value() / 100,
                               rate=self.rate())
        self.metro.failed.connect(self.metronome_failed)
        self.metro.start()
        self.met_btn.setText("■  Stop")

    def metronome_failed(self, err):
        if self.metro:
            self.metro = None
            self.metro_route(False)
            self.met_btn.setText("▶  Start")
        self.hint.offer(f"<b>The metronome couldn't play.</b> {html_escape(err)}", [])

    def metro_route(self, on):
        """Make sure the clicks (computer 1/2, the "Headphones (Mbox)" channels) reach whatever each headphone
        jack is playing — never the speakers' mix. Put it back on stop. → names of the jacks reached."""
        saved = getattr(self, "metro_saved", None)
        if not on:
            for k, slot, l_, r_ in reversed(saved or []):
                self.mix_set_lr(k, slot, l_, r_)
            self.metro_saved = None
            return []
        if saved:
            return []
        kind0, spk_mix, _ = self.pair_source(0)
        spk_mix = spk_mix if kind0 == "mix" else None
        sl, sr = self.slot_of(PC_HP_LEFT), self.slot_of(PC_HP_RIGHT)
        reached, self.metro_saved = [], []
        for name, first in (("Headphone A", 6), ("Headphone B", 8)):
            kind, val, _ = self.pair_source(first)
            if kind == "direct" and tuple(val) == (PC_HP_LEFT, PC_HP_RIGHT):
                reached.append(name)
            elif kind == "mix" and val != spk_mix and None not in (sl, sr):
                for slot, want in ((sl, (UNITY, 0)), (sr, (0, UNITY))):
                    cur = self.mix_lr(val, slot)
                    if max(cur) < UNITY // 2:
                        self.metro_saved.append((val, slot, *cur))
                        self.mix_set_lr(val, slot, *want)
                reached.append(name)
        return reached

    # ---------------------------------------------------------------- 4.17: practice player
    def build_practice_card(self, lay):
        c, l = card("Practice player", "Learn a tune by ear: slow it down without changing the pitch, loop the "
                                       "tricky bit, or move it into your instrument's key.")
        top = QtWidgets.QHBoxLayout()
        ob = QtWidgets.QPushButton("Open a song…")
        ob.clicked.connect(self.practice_open_dialog)
        top.addWidget(ob)
        self.pr_name = QtWidgets.QLabel("")
        top.addWidget(self.pr_name, 1)
        self.pr_key = muted("")
        top.addWidget(self.pr_key)
        l.addLayout(top)
        self.pr_wave = PracticeWave()
        self.pr_wave.setMinimumHeight(110)
        self.pr_wave.setToolTip("Click to jump there. Drag across a bit to loop it.")
        self.pr_wave.seek.connect(self.practice_seek)
        self.pr_wave.loopChosen.connect(self.practice_set_loop)
        l.addWidget(self.pr_wave)
        row = QtWidgets.QHBoxLayout()
        self.pr_play = QtWidgets.QPushButton("▶  Play")
        self.pr_play.setObjectName("primary")
        self.pr_play.clicked.connect(self.practice_toggle)
        row.addWidget(self.pr_play)
        self.pr_time = QtWidgets.QLabel("0:00")
        self.pr_time.setStyleSheet("font: 600 14pt monospace;")
        row.addWidget(self.pr_time)
        self.pr_loop = QtWidgets.QCheckBox("Loop")
        self.pr_loop.setToolTip("Drag across the waveform to choose what to loop.")
        self.pr_loop.toggled.connect(lambda _: self.practice_restart())
        row.addWidget(self.pr_loop)
        self.pr_loop_lbl = muted("drag on the waveform to pick a section")
        row.addWidget(self.pr_loop_lbl, 1)
        clr = QtWidgets.QPushButton("Clear loop")
        clr.clicked.connect(lambda: self.practice_set_loop(None, None))
        row.addWidget(clr)
        l.addLayout(row)
        g = QtWidgets.QGridLayout()
        g.addWidget(QtWidgets.QLabel("Speed"), 0, 0)
        self.pr_speed = QtWidgets.QSlider(Qt.Horizontal)
        self.pr_speed.setRange(40, 125)
        self.pr_speed.setValue(100)
        self.pr_speed.setToolTip("Slower or faster without changing the pitch.")
        self.pr_speed_lbl = QtWidgets.QLabel("100%")
        self.pr_speed.valueChanged.connect(lambda v: (self.pr_speed_lbl.setText(f"{v}%"), self.practice_debounce()))
        g.addWidget(self.pr_speed, 0, 1)
        g.addWidget(self.pr_speed_lbl, 0, 2)
        g.addWidget(QtWidgets.QLabel("Transpose"), 1, 0)
        trow = QtWidgets.QHBoxLayout()
        self.pr_semi = QtWidgets.QSpinBox()
        self.pr_semi.setRange(-12, 12)
        self.pr_semi.setSuffix(" semitones")
        self.pr_semi.valueChanged.connect(lambda _: self.practice_debounce())
        trow.addWidget(self.pr_semi)
        trow.addWidget(QtWidgets.QLabel("  or play it in my instrument's key:"))
        self.pr_inst = QtWidgets.QComboBox()
        self.pr_inst.addItem("—", "")
        for n in ["D", "C", "Bb", "Eb", "F", "G", "A", "B", "E", "Ab", "C#", "F#"]:
            self.pr_inst.addItem(n, n)
        self.pr_inst.setToolTip("A D whistle → D. A C harmonica played normally → C; in 2nd position "
                                "(cross harp) → G. Minor tunes go to the matching minor (D whistle → B minor).")
        self.pr_inst.setCurrentIndex(max(0, self.pr_inst.findData(self.app_state.get("pr_inst", ""))))
        self.pr_inst.activated.connect(lambda _: self.practice_match_key())
        trow.addWidget(self.pr_inst)
        trow.addStretch(1)
        g.addLayout(trow, 1, 1, 1, 2)
        g.setColumnStretch(1, 1)
        l.addLayout(g)
        brow = QtWidgets.QHBoxLayout()
        self.pr_ramp = QtWidgets.QCheckBox("Speed up 5% each time round the loop (up to 100%)")
        brow.addWidget(self.pr_ramp)
        brow.addStretch(1)
        brow.addWidget(QtWidgets.QLabel("Play on"))
        self.pr_out = QtWidgets.QComboBox()
        self.pr_out.addItem("Default output", "")
        self.pr_out.addItem("Headphones (Mbox)", "mbox_headphones")
        brow.addWidget(self.pr_out)
        sv = QtWidgets.QPushButton("Save this version…")
        sv.setToolTip("Saves the song (or just the loop) slowed / transposed as you hear it, e.g. for your phone.")
        sv.clicked.connect(self.practice_save)
        brow.addWidget(sv)
        l.addLayout(brow)
        lay.addWidget(c)
        c.setAcceptDrops(True)
        c.dragEnterEvent = lambda e: e.acceptProposedAction() if e.mimeData().hasUrls() else None
        c.dropEvent = lambda e: self.practice_open(e.mimeData().urls()[0].toLocalFile())
        self.pr_path, self.pr_pos, self.pr_ab, self.pr_job, self.pr_player = None, 0.0, None, None, None
        self.pr_key_info = None
        self.pr_timer = QtCore.QTimer(self)
        self.pr_timer.setSingleShot(True)
        self.pr_timer.timeout.connect(self.practice_restart)

    def practice_open_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open a song", str(HOME / "Music"),
                                                        "Audio (*.flac *.mp3 *.wav *.ogg *.opus *.m4a *.aac *.aiff "
                                                        "*.wma *.mp4 *.mkv *.webm);;All files (*)")
        if path:
            self.practice_open(path)

    def practice_open(self, path):
        self.practice_stop()
        self.pr_path, self.pr_pos, self.pr_ab = path, 0.0, None
        self.pr_wave.loop_ab = None
        self.pr_name.setText(f"<b>{html_escape(Path(path).name)}</b>")
        self.pr_key.setText("Listening for the key…")
        self.pr_loop_lbl.setText("drag on the waveform to pick a section")
        self.pr_env_job = EnvelopeJob(path)
        self.pr_env_job.done.connect(lambda env, dur, err: (
            self.pr_wave.set_data(env[0], env[1], dur) if not err and env is not None else
            self.pr_name.setText(f"Couldn't open it: {err}")))
        self.pr_env_job.start()
        self.pr_key_job = KeyJob(path)
        self.pr_key_job.done.connect(self.practice_key_found)
        self.pr_key_job.start()

    def practice_key_found(self, k):
        self.pr_key_info = k
        if not k:
            self.pr_key.setText("")
            return
        tonic, mode, conf = k
        sure = "" if conf > 0.6 else " (not sure)"
        self.pr_key.setText(f"Sounds like {NOTE_NAMES[tonic]} {mode}{sure}")
        if self.pr_inst.currentData():
            self.practice_match_key()

    def practice_match_key(self):
        inst = self.pr_inst.currentData()
        self.app_state["pr_inst"] = inst
        self.save_app_state()
        if not inst or not self.pr_key_info:
            return
        tonic, mode, _ = self.pr_key_info
        shift = shift_to_key(tonic, mode, inst)
        self.pr_semi.setValue(shift)
        target = NOTE_NAMES[(tonic + shift) % 12]
        self.toast(f"Moved {shift:+d} semitones: now in {target} {mode}.", 6000)

    def practice_set_loop(self, a, b):
        if a is None:
            self.pr_ab = None
            self.pr_wave.loop_ab = None
            self.pr_loop.setChecked(False)
            self.pr_loop_lbl.setText("drag on the waveform to pick a section")
        else:
            self.pr_ab = (a, b)
            self.pr_wave.loop_ab = (a, b)
            self.pr_loop_lbl.setText(f"A {fmt_time(a)} – B {fmt_time(b)}  ({b - a:.1f} s)")
            self.pr_loop.blockSignals(True)
            self.pr_loop.setChecked(True)
            self.pr_loop.blockSignals(False)
            self.pr_pos = a
        self.pr_wave.update()
        self.practice_restart()

    def practice_seek(self, t):
        self.pr_pos = t
        self.pr_wave.play_t = t
        self.pr_wave.update()
        self.practice_restart()

    def practice_debounce(self):
        if self.pr_player:
            self.pr_timer.start(350)

    def practice_toggle(self):
        if self.pr_player:
            self.practice_stop()
        else:
            self.practice_play()

    def practice_stop(self):
        if self.pr_player:
            self.pr_player.stop()
            self.pr_player = None
        self.pr_play.setText("▶  Play")

    def practice_restart(self):
        if self.pr_player:
            self.practice_stop()
            self.practice_play()

    def practice_play(self):
        if not self.pr_path:
            self.practice_open_dialog()
            return
        speed, semis, rate = self.pr_speed.value() / 100, self.pr_semi.value(), self.rate()
        target = self.pr_out.currentData() or None
        if target and not pw_node_exists(target, "sink"):
            target = None
        if self.pr_loop.isChecked() and self.pr_ab:
            a, b = self.pr_ab
            self.pr_play.setText("…")
            job = PracticeRender(self.pr_path, a, b, speed, semis, rate)

            def go(data, err):
                if err or not data:
                    self.pr_play.setText("▶  Play")
                    self.toast(f"Couldn't play it: {err}", 6000)
                    return
                self.pr_player = PracticePlayer(data, rate, target, True, a, speed)
                self.pr_player.position.connect(self.practice_pos)
                self.pr_player.looped.connect(self.practice_looped)
                self.pr_player.failed.connect(self.practice_failed)
                self.pr_player.start()
                self.pr_play.setText("❚❚  Pause")
            job.ready.connect(go)
            self.pr_job = job
            job.start()
        else:
            self.pr_player = PracticeStream(self.pr_path, self.pr_pos, speed, semis, rate, target)
            self.pr_player.position.connect(self.practice_pos)
            self.pr_player.finished_play.connect(lambda: (self.practice_stop(), setattr(self, "pr_pos", 0.0)))
            self.pr_player.failed.connect(self.practice_failed)
            self.pr_player.start()
            self.pr_play.setText("❚❚  Pause")

    def practice_failed(self, err):
        self.practice_stop()
        self.toast(f"Couldn't play it: {err}", 10000)

    def practice_pos(self, t):
        self.pr_pos = t
        self.pr_time.setText(fmt_time(t))
        self.pr_wave.play_t = t
        self.pr_wave.update()

    def practice_looped(self):
        if not self.pr_ramp.isChecked() or self.pr_speed.value() >= 100 or not self.pr_player:
            return
        new = min(100, self.pr_speed.value() + 5)
        self.pr_speed.blockSignals(True)
        self.pr_speed.setValue(new)
        self.pr_speed.blockSignals(False)
        self.pr_speed_lbl.setText(f"{new}%")
        a, b = self.pr_ab
        job = PracticeRender(self.pr_path, a, b, new / 100, self.pr_semi.value(), self.rate())
        player = self.pr_player
        job.ready.connect(lambda data, err: (setattr(player, "next_data", data),
                                             setattr(player, "speed", new / 100)) if data else None)
        self.pr_ramp_job = job
        job.start()

    def practice_save(self):
        if not self.pr_path:
            return
        speed, semis = self.pr_speed.value() / 100, self.pr_semi.value()
        tag = (f" {int(speed * 100)}%" if speed != 1 else "") + (f" {semis:+d}st" if semis else "")
        dest, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save this version",
                                                        str(Path(self.pr_path).with_name(
                                                            Path(self.pr_path).stem + (tag or " copy") + ".flac")),
                                                        "FLAC (*.flac);;MP3 (*.mp3)")
        if not dest:
            return
        af = practice_filter(speed, semis)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if self.pr_loop.isChecked() and self.pr_ab:
            cmd += ["-ss", f"{self.pr_ab[0]:.3f}", "-t", f"{self.pr_ab[1] - self.pr_ab[0]:.3f}"]
        cmd += ["-i", self.pr_path, *(["-af", af] if af else []), "-map_metadata", "0", dest]
        self.toast("Saving…", 0)
        proc = QtCore.QProcess(self)
        proc.finished.connect(lambda code, _s: self.toast(f"Saved {Path(dest).name}" if code == 0 else
                                                          "Couldn't save it.", 6000))
        proc.start(cmd[0], cmd[1:])
        self.pr_save_proc = proc

    def takes_dir(self):
        return Path(self.app_state.get("takes_dir") or (Path.home() / "Music" / "Mbox takes"))

    def build_take_card(self, lay):
        c, l = card("Quick take", "A voice memo for your instruments: one button, saved as FLAC in "
                                  f"{self.takes_dir()}. For anything you'll edit or mix, use Audacity.")
        row = QtWidgets.QHBoxLayout()
        self.take_btn = QtWidgets.QPushButton("🎙  Quick take")
        self.take_btn.setObjectName("primary")
        self.take_btn.setMinimumHeight(44)
        self.take_btn.setToolTip("Starts recording right away; press again to stop. Also on the tray "
                                 "panel and the phone remote.")
        self.take_btn.clicked.connect(self.toggle_take)
        row.addWidget(self.take_btn)
        self.take_time = QtWidgets.QLabel("0:00")
        self.take_time.setStyleSheet("font: 600 18pt monospace;")
        row.addWidget(self.take_time)
        row.addWidget(QtWidgets.QLabel("from"))
        self.take_src_box = QtWidgets.QComboBox()
        for left, _, name in INPUT_PAIRS:
            self.take_src_box.addItem(f"Stereo: {name}", f"pair:{left}")
        for n, (item, jack) in enumerate(INPUT_JACKS[:4]):
            own = self.label(f"in{n}", "")
            self.take_src_box.addItem(f"Mono: {jack}" + (f" ({own})" if own else ""), f"mono:{item}")
        self.take_src_box.setCurrentIndex(max(0, self.take_src_box.findData(
            self.app_state.get("take_src", "mono:Analog-A-1"))))
        self.take_src_box.currentIndexChanged.connect(lambda _: (
            self.app_state.__setitem__("take_src", self.take_src_box.currentData()), self.save_app_state()))
        self.take_src_box.setToolTip("One mic or guitar = Mono (plays back in the middle).")
        row.addWidget(self.take_src_box, 1)
        l.addLayout(row)
        self.take_strip = LevelStrip(self, lambda: self.source_items(self.take_src_box.currentData()))
        l.addWidget(self.take_strip)
        self.take_level = muted("")
        l.addWidget(self.take_level)
        self.take_hear = QtWidgets.QCheckBox("Hear it through the speakers while recording")
        self.take_hear.setChecked(self.app_state.get("take_hear", True))
        self.take_hear.setToolTip("Plays the input through Outputs 1/2 from the Mbox's own mixer — no delay. "
                                  "Put back the way it was when the take stops.")
        self.take_hear.toggled.connect(self.take_hear_toggled)
        l.addWidget(self.take_hear)
        l.addWidget(self.lowcut_row())
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel("After recording"))
        self.take_polish = QtWidgets.QComboBox()
        for key, label in TAKE_POLISH:
            self.take_polish.addItem(label, key)
        self.take_polish.setCurrentIndex(max(0, self.take_polish.findData(self.app_state.get("take_polish",
                                                                                              "none"))))
        self.take_polish.setToolTip("For quick takes and “Capture what I just played” (not multitrack — those "
                                    "are for mixing). Normalize only changes the volume; “even out” also brings "
                                    "quiet and loud notes closer together. Neither can undo clipping — set the "
                                    "gain knob so peaks sit around −6 dB.")
        self.take_polish.currentIndexChanged.connect(lambda _: (self.app_state.__setitem__(
            "take_polish", self.take_polish.currentData()), self.save_app_state()))
        prow.addWidget(self.take_polish)
        prow.addStretch(1)
        l.addLayout(prow)
        frow = QtWidgets.QHBoxLayout()
        frow.addWidget(QtWidgets.QLabel("Save takes as"))
        self.save_fmt = QtWidgets.QComboBox()
        for key, label, *_ in AUDIO_FORMATS:
            self.save_fmt.addItem(label, key)
        self.save_fmt.setCurrentIndex(max(0, self.save_fmt.findData(self.app_state.get("save_fmt", "flac"))))
        self.save_fmt.setToolTip("For quick takes, “Capture what I just played” and multitrack. Rips stay FLAC. "
                                 "Anything already saved can be converted in Record → Tools → Convert audio.")
        self.save_fmt.currentIndexChanged.connect(lambda _: (self.app_state.__setitem__(
            "save_fmt", self.save_fmt.currentData()), self.save_app_state()))
        frow.addWidget(self.save_fmt)
        frow.addStretch(1)
        l.addLayout(frow)
        last = QtWidgets.QHBoxLayout()
        self.take_last = muted("No takes yet.")
        last.addWidget(self.take_last, 1)
        self.take_play = QtWidgets.QPushButton("▶ Play")
        self.take_play.clicked.connect(self.play_take)
        self.take_aud = QtWidgets.QPushButton("Open in Audacity")
        self.take_aud.clicked.connect(lambda: self.open_audacity(self.app_state.get("last_take")))
        self.take_dir_btn = QtWidgets.QPushButton("Show folder")
        self.take_dir_btn.clicked.connect(self.show_takes_folder)
        for b in (self.take_play, self.take_aud, self.take_dir_btn):
            last.addWidget(b)
        l.addLayout(last)
        lay.addWidget(c)
        self.take_mode, self.take_proc = False, None
        self.show_last_take()

    def watch_dead_input(self):
        """A take or capture running on a silent input for 4 s: say why (usually +48V or the F/R button)."""
        runs = []                                       # each input watched on its own: a live bass can't hide a dead mic
        if getattr(self, "take_mode", False):
            runs += [("take", i) for i in self.source_items(self.app_state.get("take_src", "mono:Analog-A-1"))]
        if getattr(self, "retro", None):
            runs += [("retro", i) for i in self.source_items(getattr(self, "retro_choice", ""))]
        if getattr(self, "mt_proc", None):
            runs += [("multitrack", i) for i in self.app_state.get("mt_inputs", [])]
        keys = {f"{o}:{i}" for o, i in runs}
        live = self.__dict__.setdefault("dead_watch", {})
        for k in [k for k in live if k not in keys]:
            live.pop(k)
        warned_now = set()
        for owner, item in runs:
            st = live.setdefault(f"{owner}:{item}", {"t": time.time(), "max": -90.0, "warned": None})
            st["max"] = max(st["max"], self.item_db(item))
            age = time.time() - st["t"]
            if item in warned_now:
                continue
            if st["warned"] is None and age >= 4 and st["max"] <= -55:
                st["warned"] = "dead"
                warned_now.add(item)
                self.warn_dead_input([item])
            elif st["warned"] is None and age >= 6 and st["max"] < -30:     # a voice peaking at −37 is too quiet
                st["warned"] = "quiet"
                warned_now.add(item)
                self.warn_dead_input([item], quiet=st["max"])

    def warn_dead_input(self, items, quiet=None):
        n = int(items[0][-1])
        own = self.label(f"in{n - 1}", "")
        name = f"Input {n}" + (f" ({own})" if own else "")
        ph = self.ctrl("phantom-powering")
        phantom_off = n <= 4 and ph and not ph.values[0]
        if quiet is not None:
            self.hint.offer(f"<b>{name} is very quiet</b> (peaks around {quiet:.0f} dB) — turn its gain knob up "
                            "until the loudest parts reach about −6 dB." +
                            (" If it's a condenser mic, it also needs +48V (it's off)." if phantom_off else ""),
                            [("Turn on +48V", self.toggle_phantom)] if phantom_off else [])
            self.announce("dialog-warning", f"{name} is very quiet — gain up", 8000)
            return
        if phantom_off:
            self.hint.offer(f"<b>Nothing is coming in on {name}.</b> A condenser mic (like an AT2020) needs "
                            "+48V phantom power. Turn your speakers down first — switching it on can pop.",
                            [("Turn on +48V", self.toggle_phantom)])
        else:
            jack = "F/R" if n <= 2 else "Mic/Ln" if n <= 4 else ""
            self.hint.offer(f"<b>Nothing is coming in on {name}.</b> Check the cable, the gain knob" +
                            (f" and the {jack} button (Hardware → The box shows which jack the app thinks "
                             "is in use)." if jack else "."), [])
        self.announce("dialog-warning", f"Nothing coming in on {name}", 8000)

    def take_hear_toggled(self, on):
        self.app_state["take_hear"] = on
        self.save_app_state()
        if getattr(self, "take_mode", False):
            self.input_monitor("take", self.app_state.get("take_src", "mono:Analog-A-1"), on)

    def finish_format(self, path, polish=False):
        """Takes are recorded as FLAC; polish them if asked, then turn them into the chosen format."""
        key = self.app_state.get("save_fmt", "flac")
        path = Path(path)
        mode = self.app_state.get("take_polish", "none")
        if polish and mode != "none" and path.exists():
            lossy = key.startswith("mp3") or key in ("aac", "opus", "ogg")
            try:
                polish_take(path, mode, target=-2.0 if lossy else -1.0)    # lossy encoders overshoot a little
            except (RuntimeError, OSError) as e:
                self.toast(f"Saved as recorded — couldn't {mode} it: {e}", 8000)
        if key == "flac" or not path.exists():
            return path
        dst = path.with_suffix("." + audio_format(key)[2])
        try:
            transcode(path, dst, key)
        except (RuntimeError, OSError) as e:
            self.toast(f"Kept the FLAC — couldn't convert it: {e}", 8000)
            return path
        path.unlink(missing_ok=True)
        return dst

    def build_convert_card(self, lay):
        c, l = card("Convert audio", "Turn audio files into another format. Tags and cover art come along; "
                                     "the originals are never touched. Drop files or folders on the list.")
        brow = QtWidgets.QHBoxLayout()
        for text, fn in (("Add files…", self.convert_add_files), ("Add folder…", self.convert_add_folder),
                         ("Remove", self.convert_remove), ("Clear", lambda: (self.conv_list.clear(),
                                                                             self.convert_refresh()))):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            brow.addWidget(b)
        brow.addStretch(1)
        l.addLayout(brow)
        self.conv_list = DropList()
        self.conv_list.setMaximumHeight(150)
        self.conv_list.dropped.connect(self.convert_add)
        l.addWidget(self.conv_list)
        f = QtWidgets.QFormLayout()
        self.conv_fmt = QtWidgets.QComboBox()
        for key, label, *_ in AUDIO_FORMATS:
            self.conv_fmt.addItem(label, key)
        self.conv_fmt.setCurrentIndex(max(0, self.conv_fmt.findData(self.app_state.get("conv_fmt", "mp3_v0"))))
        self.conv_fmt.currentIndexChanged.connect(lambda _: (self.app_state.__setitem__(
            "conv_fmt", self.conv_fmt.currentData()), self.save_app_state(), self.convert_refresh()))
        f.addRow("Convert to", self.conv_fmt)
        orow = QtWidgets.QHBoxLayout()
        self.conv_where = QtWidgets.QComboBox()
        self.conv_where.addItem("A “Converted” folder beside each file", "")
        self.conv_where.addItem("Choose a folder…", "pick")
        out = self.app_state.get("conv_dir", "")
        if out:
            self.conv_where.insertItem(1, out, out)
            self.conv_where.setCurrentIndex(1)
        self.conv_where.activated.connect(self.convert_where_changed)
        orow.addWidget(self.conv_where, 1)
        f.addRow("Save to", orow)
        l.addLayout(f)
        grow = QtWidgets.QHBoxLayout()
        self.conv_btn = QtWidgets.QPushButton("Convert")
        self.conv_btn.setObjectName("primary")
        self.conv_btn.clicked.connect(self.convert_start)
        grow.addWidget(self.conv_btn)
        self.conv_bar = QtWidgets.QProgressBar()
        self.conv_bar.setVisible(False)
        grow.addWidget(self.conv_bar, 1)
        self.conv_note = muted("")
        grow.addWidget(self.conv_note, 2)
        l.addLayout(grow)
        lay.addWidget(c)
        self.conv_job = None
        self.convert_refresh()

    def convert_add(self, paths):
        have = {self.conv_list.item(i).data(Qt.UserRole) for i in range(self.conv_list.count())}
        added = 0
        for p in paths:
            p = Path(p)
            files = sorted(x for x in p.rglob("*") if x.suffix.lower() in AUDIO_EXTS) if p.is_dir() else \
                ([p] if p.suffix.lower() in AUDIO_EXTS else [])
            for x in files:
                if "Converted (" in str(x.parent) or str(x) in have:
                    continue
                it = QtWidgets.QListWidgetItem(x.name)
                it.setToolTip(str(x))
                it.setData(Qt.UserRole, str(x))
                self.conv_list.addItem(it)
                have.add(str(x))
                added += 1
        if paths and not added:
            self.conv_note.setText("No audio files there.")
        else:
            self.convert_refresh()

    def convert_add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Audio files to convert", str(self.takes_dir()),
            "Audio (" + " ".join(f"*{e}" for e in sorted(AUDIO_EXTS)) + ")")
        self.convert_add(files)

    def convert_add_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder to convert (includes subfolders)",
                                                       str(self.takes_dir()))
        if d:
            self.convert_add([d])

    def convert_remove(self):
        for it in self.conv_list.selectedItems():
            self.conv_list.takeItem(self.conv_list.row(it))
        self.convert_refresh()

    def convert_where_changed(self, i):
        if self.conv_where.itemData(i) == "pick":
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "Save converted files in",
                                                           self.app_state.get("conv_dir") or str(HOME))
            if not d:
                self.conv_where.setCurrentIndex(0)
                return
            if self.conv_where.findData(d) < 0:
                self.conv_where.insertItem(1, d, d)
            self.conv_where.setCurrentIndex(self.conv_where.findData(d))
        self.app_state["conv_dir"] = self.conv_where.currentData() or ""
        self.save_app_state()

    def convert_refresh(self):
        n = self.conv_list.count()
        busy = self.conv_job is not None
        self.conv_btn.setText("Stop" if busy else f"Convert {n} file{'s' if n != 1 else ''}" if n else "Convert")
        self.conv_btn.setEnabled(busy or n > 0)
        if not busy and not self.conv_note.text().startswith(("Done", "No audio")):
            self.conv_note.setText("" if n else "Add files or drop them here.")

    def convert_start(self):
        if self.conv_job:
            self.conv_job.cancelled = True
            self.conv_note.setText("Stopping after this file…")
            return
        files = [self.conv_list.item(i).data(Qt.UserRole) for i in range(self.conv_list.count())]
        if not files:
            return
        self.conv_job = ConvertJob(files, self.conv_fmt.currentData(), self.conv_where.currentData() or None)
        self.conv_bar.setRange(0, len(files))
        self.conv_bar.setValue(0)
        self.conv_bar.setVisible(True)
        self.conv_job.progress.connect(lambda i, n, name: (self.conv_bar.setValue(i),
                                                           self.conv_note.setText(f"{i + 1}/{n}: {name}")))
        self.conv_job.done.connect(self.convert_done)
        self.conv_job.start()
        self.convert_refresh()

    def convert_done(self, ok, errors):
        job, self.conv_job = self.conv_job, None
        if job:
            job.wait(2000)
        self.conv_bar.setVisible(False)
        where = self.conv_where.currentData() or "a “Converted” folder beside the originals"
        msg = f"Done: {ok} converted into {where}."
        if errors:
            msg += f" {len(errors)} failed — hover for why."
            self.conv_note.setToolTip("\n".join(errors[:20]))
        self.conv_note.setText(msg)
        self.toast(msg, 8000)
        self.convert_refresh()

    def show_last_take(self):
        p = self.app_state.get("last_take")
        have = bool(p and Path(p).exists())
        self.take_last.setText(f"Last take: {Path(p).name}" if have else "No takes yet.")
        for b in (self.take_play, self.take_aud):
            b.setEnabled(have)

    def play_take(self):
        if self.take_proc and self.take_proc.poll() is None:
            self.take_proc.terminate()
            self.take_play.setText("▶ Play")
            return
        p = self.app_state.get("last_take")
        if not p or not shutil.which("pw-play"):
            return
        target = ["--target", MAIN_SINK] if pw_node_exists(MAIN_SINK, "sink") else []
        self.take_proc = subprocess.Popen(["pw-play", *target, p], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        self.take_play.setText("■ Stop")
        QtCore.QTimer.singleShot(500, self.watch_take_play)

    def watch_take_play(self):
        if self.take_proc and self.take_proc.poll() is None:
            QtCore.QTimer.singleShot(500, self.watch_take_play)
        else:
            self.take_play.setText("▶ Play")

    def toggle_take(self):
        if self.recorder.running():
            if self.take_mode:
                self.recorder.stop()
            else:
                self.toast("A rip is recording — stop it first.", 5000)
            return
        if not pw_node_exists("mbox_tape_in"):
            if QtWidgets.QMessageBox.question(
                    self, "One-time setup", "Recording uses the “Rip input (Mbox)” input, "
                    "which isn't installed yet. Install it now? (PipeWire restarts for a second.)") \
                    != QtWidgets.QMessageBox.Yes:
                return
            if not self.install_named_inputs(ask=False):
                return
            QtCore.QThread.msleep(2500)
        self.take_prev = self.app_state.get("rec_src")
        self.app_state["take_restore"] = self.take_prev or ""   # survives a crash mid-take (see startup)
        self.app_state["rec_src"] = self.app_state.get("take_src", "mono:Analog-A-1")
        self.route_rip_input()
        self.rewire_rec_meters()
        folder = self.takes_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"take-{datetime.now():%Y-%m-%d_%H%M%S}.flac"
        try:
            self.recorder.start("mbox_tape_in", path, self.rate())
        except Exception as e:  # noqa: BLE001
            self.app_state["rec_src"] = self.take_prev
            self.app_state.pop("take_restore", None)
            self.route_rip_input()
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.take_mode, self.auto_state, self.clips = True, None, 0
        if self.take_hear.isChecked():
            self.input_monitor("take", self.app_state.get("take_src", "mono:Analog-A-1"), True)
        self.rec_live, self.rec_offset = False, 0.0
        self.take_btn.setText("■  Stop take")
        self.take_btn.setStyleSheet("background:#f85149; color:white;")
        self.take_level.setText("Starting… wait a moment before you play.")

    def take_tick(self):
        """Called from rec_tick while a take is running."""
        if not self.take_mode:
            return
        self.take_time.setText(self.rec_time.text())
        if not getattr(self, "rec_live", False):
            return
        lvl = max((meter_db(self.input_level(item)) for item, _ in self.rec_meters if item), default=-60)
        self.take_level.setText(f"● Recording — level {lvl:.0f} dB" +
                                (f"  ·  <b style='color:#f85149'>clipped {self.clips}×</b>" if self.clips else ""))

    def take_finished(self, path, ok):
        self.take_mode = False
        self.input_monitor("take", "", False)
        self.app_state["rec_src"] = self.take_prev
        self.app_state.pop("take_restore", None)
        self.route_rip_input()
        self.rewire_rec_meters()
        self.take_btn.setText("🎙  Quick take")
        self.take_btn.setStyleSheet("")
        self.take_time.setText("0:00")
        self.rec_btn.setText("●  Arm" if self.auto_rec.isChecked() else "●  Record")
        self.rec_info.setText("Source: " + self.rip_name())
        if ok and Path(path).exists():
            path = self.finish_format(path, polish=True)
            self.app_state["last_take"] = str(path)
            self.save_app_state()
            self.take_level.setText(f"Saved {Path(path).name}")
            self.toast(f"Take saved — {Path(path).name}", 6000)
        else:
            self.take_level.setText("The take failed — is the Mbox input available?")
        self.show_last_take()

    def rec_finished(self, path, ok):
        if getattr(self, "take_mode", False):
            self.take_finished(path, ok)
            return
        self.rec_btn.setText("●  Arm" if self.auto_rec.isChecked() else "●  Record")
        state, self.auto_state = self.auto_state, None
        if state == "armed":                       # disarmed before any music: throw it away
            Path(path).unlink(missing_ok=True)
            self.rec_info.setText("Disarmed — nothing was recorded.")
            self.rec_time.setText("0:00")
            return
        if ok and state in ("rec", "done"):
            start, end = max(0.0, self.auto_t0 - 1.0), self.auto_t1 + 1.5
            self.rec_info.setText("Trimming the silence…")
            self.trimmer = TrimJob(path, start, end)
            self.trimmer.done.connect(lambda p, e: self.rec_finished_auto(p or path, e, end - start))
            self.trimmer.start()
            return
        if ok:
            self.rec_info.setText(f"Saved {path}")
            self.split_src.setText(path)
            self.focus_split_tab()
            self.toast("Recording saved — use “Find tracks” to split it.", 8000)
        else:
            self.rec_info.setText("Recording failed — is the Mbox input available?")

    def show_guide(self):
        g = SOURCE_GUIDE[self.guide_src.currentText()]
        rows = [("Distance", g["dist"]), ("Aim", g["aim"]), ("Level", g["level"]),
                ("+48V", "On (condenser mics like the AT2020). Plug in first, then switch on."
                 if g["phantom"] else "Not needed."),
                ("75 Hz low-cut", "On — removes rumble and thumps." if g["lowcut"] else "Off — keeps the low end.")]
        self.guide_text.setText("<table cellspacing=4>" + "".join(
            f"<tr><td style='color:{SUBTLE}; padding-right:12px'>{k}</td><td>{html_escape(v)}</td></tr>"
            for k, v in rows) + "</table>")

    def apply_guide(self):
        g = SOURCE_GUIDE[self.guide_src.currentText()]
        item = self.guide_in.currentData()
        idx = [j for j, _ in INPUT_JACKS].index(item)
        self.write_element("input-hp-filter", 0, idx, bool(g["lowcut"]))
        self.set_rec_source(f"mono:{item}")
        self.reload_state()
        self.run_refreshers()
        self.toast(f"Set up for {self.guide_src.currentText().lower()} on {self.guide_in.currentText()}: "
                   f"recording in mono, low-cut {'on' if g['lowcut'] else 'off'}.", 8000)

    def queue_refresh(self):
        if not hasattr(self, "queue_list"):
            return
        self.queue_list.clear()
        cur = self.app_state.get("queue_cur", -1)
        for i, it in enumerate(self.app_state["queue"]):
            sides = it.get("sides", 2)
            done = it.get("done", 0)
            marks = " ".join(("✓" if k < done else "·") + chr(65 + k) for k in range(sides))
            pre = "▶ " if i == cur else ("✔ " if done >= sides else "   ")
            li = QtWidgets.QListWidgetItem(f"{pre}{it.get('artist') or '?'} — {it['album']}"
                                           f"{' (' + it['year'] + ')' if it.get('year') else ''}    {marks}")
            if done >= sides:
                li.setForeground(QtGui.QColor(SUBTLE))
            self.queue_list.addItem(li)

    def queue_add(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Add to rip queue")
        f = QtWidgets.QFormLayout(dlg)
        a, b, y = QtWidgets.QLineEdit(), QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        sides = QtWidgets.QSpinBox()
        sides.setRange(1, 8)
        sides.setValue(2)
        for lbl, wdg in (("Artist", a), ("Album", b), ("Year", y), ("Sides", sides)):
            f.addRow(lbl, wdg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() == QtWidgets.QDialog.Accepted and b.text().strip():
            self.app_state["queue"].append({"artist": a.text().strip(), "album": b.text().strip(),
                                            "year": y.text().strip(), "sides": sides.value(), "done": 0})
            self.save_app_state()
            self.queue_refresh()

    def queue_use(self):
        i = self.queue_list.currentRow()
        q = self.app_state["queue"]
        if not 0 <= i < len(q):
            return
        it = q[i]
        self.app_state["queue_cur"] = i
        self.save_app_state()
        self.tag_artist.setText(it.get("artist", ""))
        self.tag_album.setText(it["album"])
        self.tag_year.setText(it.get("year", ""))
        self.first_no.setValue(1)
        self.cover_path = None
        self.cover_btn.setIcon(QtGui.QIcon())
        self.cover_btn.setText("Cover…")
        self.queue_refresh()
        self.toast(f"Ready for “{it['album']}” side {chr(65 + it.get('done', 0))} — press Arm.", 8000)

    def queue_remove(self):
        i = self.queue_list.currentRow()
        q = self.app_state["queue"]
        if 0 <= i < len(q):
            del q[i]
            cur = self.app_state.get("queue_cur", -1)
            self.app_state["queue_cur"] = -1 if cur == i else cur - (cur > i)
            self.save_app_state()
            self.queue_refresh()

    def focus_split_tab(self):
        t = self.rec_page.findChild(QtWidgets.QTabWidget) if getattr(self, "rec_page", None) else None
        if t:
            t.setCurrentIndex(1)

    def rec_finished_auto(self, path, err, secs):
        self.rec_info.setText(f"Saved {path}" + (f"  (couldn't trim: {err})" if err else ""))
        self.rec_time.setText(fmt_time(secs))
        self.split_src.setText(path)
        self.focus_split_tab()
        msg = f"Side recorded ({fmt_time(secs)}). Next: Find tracks, or flip the tape and Arm again."
        q = self.app_state["queue"]
        cur = self.app_state.get("queue_cur", -1)
        if 0 <= cur < len(q):
            it = q[cur]
            it["done"] = it.get("done", 0) + 1
            if it["done"] < it.get("sides", 2):
                msg = f"Side {chr(64 + it['done'])} of “{it['album']}” done ({fmt_time(secs)}). Flip it and Arm again."
            else:
                nxt = next((i for i, x in enumerate(q) if x.get("done", 0) < x.get("sides", 2)), -1)
                msg = f"“{it['album']}” is fully ripped." + (f" Next up: “{q[nxt]['album']}”." if nxt >= 0 else "")
                self.app_state["queue_cur"] = -1
            self.save_app_state()
            self.queue_refresh()
        self.toast(msg, 15000)
        if self.tray and not self.isVisible():
            self.tray.showMessage(APP_NAME, msg, app_icon(), 10000)

    def _orig_titles(self):
        out = []
        for r in range(self.track_table.rowCount()):
            it = self.track_table.item(r, 4)
            out.append(it.text().strip() if it else "")
        return out

    def table_edited(self, item):
        if item.column() in (2, 3):
            self.sync_wave_from_table()

    def sync_wave_from_table(self):
        rows = self._tracks()
        self.wave.first = self.first_no.value()
        self.wave.set_splits([a for a, b, t in rows[1:]])

    def load_wave(self, path):
        self.wave_env = None
        self.wave.clear()
        if not path or not Path(path).exists():
            return
        self.envjob = EnvelopeJob(path)
        self.envjob.done.connect(self.wave_loaded)
        self.envjob.start()

    def wave_loaded(self, data, dur, err):
        if data is None:
            self.split_status.setText(err)
            return
        self.wave_env = data
        self.wave.set_data(data[0], data[1], dur)
        self.sync_wave_from_table()

    def apply_splits(self, splits):
        """Rebuild the track rows from the split lines, keeping each title with its music."""
        old = self._tracks()
        origs = self._orig_titles()
        start = old[0][0] if old else 0.0
        end = old[-1][1] if old and old[-1][1] > 0 else self.wave.dur
        bounds = [start] + [x for x in sorted(splits) if start < x < end] + [end]
        used, rows = set(), []
        for a, b in zip(bounds[:-1], bounds[1:]):
            title, orig = "", ""
            cand = [j for j, (oa, ob, ot) in enumerate(old) if oa < b - 0.05 and ob > a + 0.05 and j not in used]
            pick = next((j for j in cand if old[j][2]), cand[0] if cand else None)
            if pick is not None:
                used.update(cand if len(cand) > 1 and b - a > 0 and all(
                    old[j][1] <= b + 0.05 for j in cand) else [pick])
                title, orig = old[pick][2], origs[pick] if pick < len(origs) else ""
            rows.append((a, b, title, orig))
        self.track_table.blockSignals(True)
        self.track_table.setRowCount(0)
        self.track_table.blockSignals(False)
        for i, (a, b, t, o) in enumerate(rows):
            self.add_track_row(self.first_no.value() + i, t, a, b, o)
        self.save_track_sidecar()
        self.split_status.setText(f"{len(rows)} track(s). Click near a line to hear the cut.")

    def split_by_count(self):
        if self.wave_env is None:
            self.split_status.setText("Open a recording first.")
            return
        n = self.song_count.value()
        dur = self.wave.dur
        if not self._tracks():
            self.track_table.setRowCount(0)
            self.add_track_row(self.first_no.value(), "", 0.0, dur)
        cuts = deepest_dips(self.wave_env[0], EnvelopeJob.HOP, n - 1, min_gap=min(25.0, dur / (n * 2)))
        self.apply_splits(cuts)

    def set_cover(self, path):
        self.cover_path = str(path)
        pm = QtGui.QPixmap(self.cover_path)
        if not pm.isNull():
            self.cover_btn.setIcon(QtGui.QIcon(pm.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            self.cover_btn.setIconSize(QtCore.QSize(28, 28))
            self.cover_btn.setText("Cover ✓")

    def pick_cover(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Cover picture", str(HOME / "Pictures"),
                                                     "Images (*.jpg *.jpeg *.png *.webp *.heic *.bmp)")
        if f:
            self.set_cover(f)
            self.save_track_sidecar()

    def identify_songs(self):
        key = self.app_state.get("acoustid_key", "").strip()
        if not key:
            QtWidgets.QMessageBox.information(
                self, APP_NAME, "Song identification needs a free AcoustID key.\n\n1. Go to "
                "acoustid.org/new-application and register (any name, e.g. “Mbox Control”).\n"
                "2. Paste the key into Settings → App → Song identification.")
            return
        if not shutil.which("fpcalc"):
            if QtWidgets.QMessageBox.question(self, APP_NAME, "This needs Chromaprint (fpcalc). Install it now?"
                                              ) != QtWidgets.QMessageBox.Yes:
                return
            if not install_argv("chromaprint"):
                self.split_status.setText("Install Chromaprint (fpcalc) with your package manager.")
                return
            self.run_root(install_argv("chromaprint"), "Installing Chromaprint…", lambda ok, _m: (
                self.identify_songs() if shutil.which("fpcalc") else
                self.split_status.setText("Couldn't install Chromaprint. Try:  " + install_hint("chromaprint"))))
            return
        src = self.split_src.text().strip()
        rows = [(r, a, b) for r, (a, b, t) in enumerate(self._tracks()) if b > a]
        if not src or not rows:
            self.split_status.setText("Find tracks first.")
            return
        self.split_status.setText(f"Listening to {len(rows)} track(s) and asking AcoustID…")
        self.ident = IdentifyJob(src, rows, key)
        self.ident.found.connect(self.song_identified)
        self.ident.done.connect(lambda n, err: self.split_status.setText(
            err or (f"Identified {n} of {len(rows)} track(s). Titles you'd already typed were kept; "
                    "matches are in the tooltips." if n else "No matches — this one's not in the database.")))
        self.ident.start()

    def song_identified(self, row, title, artist):
        it = self.track_table.item(row, 1)
        if it is None:
            return
        if not it.text().strip():
            it.setText(title)
        it.setToolTip(f"AcoustID match: {title} — {artist}")
        if artist and not self.tag_artist.text().strip():
            self.tag_artist.setText(artist)

    def copy_tracklist(self):
        rows = [(a, b, t, o) for (a, b, t), o in zip(self._tracks(), self._orig_titles())]
        if not rows:
            self.split_status.setText("No tracks yet.")
            return
        QtWidgets.QApplication.clipboard().setText(tracklist_text(rows, self.first_no.value()))
        self.toast("Tracklist copied — paste it into Discogs' or MusicBrainz's “add release” form.", 8000)

    def sidecar(self, src):
        return Path(src).with_suffix(".tracks.json") if src else None

    def save_track_sidecar(self):
        src = self.split_shown
        if src and Path(src).exists():
            save_json(self.sidecar(src), {"first": self.first_no.value(), "cover": self.cover_path or "",
                                          "speed": self.speed_pct(),
                                          "rows": [[t, a, b, o] for (a, b, t), o in
                                                   zip(self._tracks(), self._orig_titles())]})

    def renumber_tracks(self):
        for r in range(self.track_table.rowCount()):
            it = self.track_table.item(r, 0)
            if it:
                it.setText(str(self.first_no.value() + r))
        self.wave.first = self.first_no.value()
        self.wave.update()

    def split_src_changed(self):
        new = self.split_src.text().strip()
        if new == self.split_shown:
            return
        self.save_track_sidecar()                 # keep the old recording's titles with it
        self.split_shown = new
        self.track_table.setRowCount(0)
        self.load_wave(new)
        data = load_json(self.sidecar(new), {}) if new else {}
        self.speed_spin.setValue(float(data.get("speed", 0.0)) if data else 0.0)
        if data:
            self.first_no.setValue(int(data.get("first", 1)))
            for i, row in enumerate(data.get("rows", [])):
                self.add_track_row(self.first_no.value() + i, *row)
            if data.get("cover") and Path(data["cover"]).exists():
                self.set_cover(data["cover"])
            self.split_status.setText("Loaded the track list you made for this recording.")
        else:
            last = getattr(self, "last_export", None)
            same_album = last and last[0] and last[0] == self.tag_album.text().strip()
            self.first_no.setValue(last[1] if same_album else 1)
            self.split_status.setText(f"New recording — numbering continues at {last[1]} (side B)."
                                      if same_album else "New recording — click Find tracks.")

    def speed_pct(self):
        return round(self.speed_spin.value(), 1) if hasattr(self, "speed_spin") else 0.0

    def speed_text(self):
        pct = self.speed_pct()
        if not pct:
            return "Saved exactly as recorded."
        cents = 1200 * math.log2(1 + pct / 100)
        return (f"Saved {abs(pct):.1f} % {'faster' if pct > 0 else 'slower'} — about {abs(cents):.0f} cents "
                f"{'higher' if pct > 0 else 'lower'}; a {len_change(pct)}.")

    def measure_speed(self):
        src = self.split_src.text().strip()
        if not src or not Path(src).exists():
            self.speed_note.setText("Pick a recording first.")
            return
        self.measure_btn.setEnabled(False)
        self.speed_note.setText("Listening to the whole recording…")
        self.tune_job = TuningJob(src)
        self.tune_job.done.connect(self.speed_measured)
        self.tune_job.start()

    def speed_measured(self, cents, conf, err):
        self.measure_btn.setEnabled(True)
        if err:
            self.speed_note.setText(err)
            return
        pct = round((2 ** (-cents / 1200) - 1) * 100, 1)
        if conf < 0.25:
            self.speed_note.setText("Couldn't tell — too few clear notes (speech, drums or very noisy). "
                                    "Set it by ear instead.")
            return
        if abs(pct) < 0.2:
            self.speed_spin.setValue(0.0)
            self.speed_note.setText(f"Spot on — within {abs(cents):.0f} cents of normal tuning. Nothing to fix.")
            return
        self.speed_spin.setValue(pct)
        sure = "fairly sure" if conf > 0.45 else "best guess"
        self.speed_note.setText(f"Music sits {abs(cents):.0f} cents {'sharp' if cents > 0 else 'flat'} → set "
                                f"{pct:+.1f} % ({sure}). Click the waveform to check by ear. Old recordings "
                                f"weren't always tuned to A440, so trust your ears over this.")

    def pick_split_src(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Recording to split", self.rip_dir.text(),
                                                     "Audio (*.flac *.wav)")
        if f:
            self.split_src.setText(f)

    def _tracks(self):
        rows = []
        for r in range(self.track_table.rowCount()):
            def cell(c):
                it = self.track_table.item(r, c)
                return it.text() if it else ""

            def secs(t):
                try:
                    parts = [float(x) for x in t.split(":")]
                    return sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
                except ValueError:
                    return 0.0
            rows.append((secs(cell(2)), secs(cell(3)), cell(1)))
        return rows

    def add_track_row(self, n, title, start, end, orig=""):
        r = self.track_table.rowCount()
        self.track_table.blockSignals(True)
        self.track_table.insertRow(r)
        def tm(x):
            x = round(float(x or 0), 1)
            return fmt_time(x) + (f".{int(round(x * 10)) % 10}" if x % 1 else "")
        for c, v in enumerate((str(n), title, tm(start), tm(end), orig)):
            it = QtWidgets.QTableWidgetItem(v)
            if c == 0:
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.track_table.setItem(r, c, it)
        self.track_table.blockSignals(False)
        self.sync_wave_from_table()

    def find_tracks(self):
        src = self.split_src.text().strip()
        if not src or not Path(src).exists():
            self.split_status.setText("Pick a recording first.")
            return
        self.split_status.setText("Listening for the gaps between songs…")
        self.splitter = SilenceSplitter(src, self.sil_db.value(), self.sil_len.value())
        self.splitter.done.connect(self.tracks_found)
        self.splitter.start()

    def tracks_found(self, tracks, dur, err):
        if err:
            self.split_status.setText(err)
            return
        same = self.split_shown == self.split_src.text().strip()
        old_titles = [t[2] for t in self._tracks()] if same else []
        old_orig = self._orig_titles() if same else []
        self.track_table.setRowCount(0)
        for i, (a, b) in enumerate(tracks, 1):
            self.add_track_row(self.first_no.value() + i - 1,
                               old_titles[i - 1] if i - 1 < len(old_titles) else "", a, b,
                               old_orig[i - 1] if i - 1 < len(old_orig) else "")
        self.split_shown = self.split_src.text().strip()
        self.save_track_sidecar()
        if self.wave_env is None:
            self.load_wave(self.split_shown)
        self.split_status.setText(f"Found {len(tracks)} track(s) in {fmt_time(dur)}. Edit titles or "
                                  "times (m:ss) as needed, then save.")

    def mb_lookup(self):
        if not self.tag_album.text().strip():
            self.split_status.setText("Type at least the album title, then look it up.")
            return
        self.split_status.setText("Searching MusicBrainz…")
        self.mb = MusicBrainzLookup(self.tag_artist.text().strip(), self.tag_album.text().strip())
        self.mb.done.connect(self.mb_results)
        self.mb.start()

    def mb_results(self, results, err):
        if not results:
            self.split_status.setText(err)
            return
        items = [f"{r['artist']} — {r['title']}  ({r['date'] or '?'}, {r['country'] or '?'}, "
                 f"{r['format'] or '?'}, {r['tracks']} tracks)" for r in results]
        choice, ok = QtWidgets.QInputDialog.getItem(self, "Pick the release", "Matches:", items, 0, False)
        if not ok:
            return
        rel = results[items.index(choice)]
        self.tag_artist.setText(rel["artist"])
        self.tag_album.setText(rel["title"])
        self.split_status.setText("Fetching track list…")
        self.mbt = MusicBrainzTracks(rel["id"])
        self.mbt.done.connect(self.mb_tracks)
        self.mbt.start()

    def mb_tracks(self, titles, year, err):
        if err:
            self.split_status.setText(err)
            return
        if year:
            self.tag_year.setText(year)
        while self.track_table.rowCount() < len(titles):
            self.add_track_row(self.track_table.rowCount() + 1, "", 0, 0)
        for r, t in enumerate(titles):
            self.track_table.item(r, 1).setText(t)
        found = self.track_table.rowCount()
        note = "" if found == len(titles) else f" Note: {len(titles)} titles but {found} rows — check the splits."
        self.split_status.setText(f"Filled {len(titles)} track titles.{note}")

    def export_tracks(self):
        src = self.split_src.text().strip()
        tracks = [t for t in self._tracks() if t[1] > t[0]]
        if not src or not tracks:
            self.split_status.setText("Find tracks first (or add rows with start and end times).")
            return
        self.export_btn.setEnabled(False)
        self.split_status.setText("Saving…")
        af = ",".join(f for f in [speed_filter(src, self.speed_pct())] +
                      [CLEANUP[k][1] for k in CLEANUP if k in self.app_state["cleanup"]] if f)
        self.save_track_sidecar()
        self.last_export = (self.tag_album.text().strip(), self.first_no.value() + len(tracks))
        self.exporter = Exporter(src, tracks, self.rip_dir.text(), self.tag_artist.text().strip(),
                                 self.tag_album.text().strip(), self.tag_year.text().strip(), af,
                                 self.first_no.value(), self.app_state.get("layout", "artist"),
                                 self.cover_path, self._orig_titles(), bool(self.app_state.get("replaygain")))
        self.exporter.progress.connect(lambda i, n: self.split_status.setText(f"Saving track {i} of {n}…"))
        self.exporter.done.connect(self.export_done)
        self.exporter.start()

    def export_done(self, folder, err):
        self.export_btn.setEnabled(True)
        self.split_status.setText(f"Couldn't save: {err}" if err else f"Done — tracks saved in {folder}")
        if err or not folder:
            return
        lib = self.app_state.get("library_dir", "").strip()
        if lib:
            self.libjob = LibraryJob(folder, lib, self.app_state.get("library_cmd", ""), self.rip_dir.text())
            self.libjob.done.connect(lambda msg, e: self.split_status.setText(
                self.split_status.text() + ("  ·  " + (msg or e))))
            self.libjob.start()
        if self.discogs_release and self.app_state.get("discogs_mark") and self.app_state.get("discogs_token"):
            self.dmark = DiscogsMarkRipped(self.app_state["discogs_token"], self.discogs_release)
            self.dmark.done.connect(lambda m: self.toast(m, 8000))
            self.dmark.start()

    # ---------------------------------------------------------------- 4.14: loopback
    def build_loopback_card(self, lay):
        c, l = card("Loopback", "Record exactly what one of the outputs plays — mic and computer together, "
                                "mixed by the Mbox itself. Apps see it as “Loopback (Mbox)”.")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Record what this plays:"))
        self.loop_box = QtWidgets.QComboBox()
        self.loop_box.addItem("Off", -1)
        for name, first, _ in OUTPUT_PAIRS:
            self.loop_box.addItem(name, first)
        self.loop_box.setToolTip("Pick the output whose mix you want to record or stream. Tip: build a "
                                 "separate mix on a spare output (e.g. Outputs 5/6) for streaming, so your "
                                 "own speakers' mix stays as it is.")
        self.loop_box.activated.connect(lambda _: self.set_loopback(self.loop_box.currentData()))
        row.addWidget(self.loop_box, 1)
        self.loop_mix_btn = QtWidgets.QPushButton("Make it a stream mix…")
        self.loop_mix_btn.setToolTip("Turns the chosen output's mix into: your mic (centred) + computer sound. "
                                     "Fine-tune it on the Mixer page.")
        self.loop_mix_btn.clicked.connect(self.make_stream_mix)
        row.addWidget(self.loop_mix_btn)
        l.addLayout(row)
        self.loop_note = muted("")
        l.addWidget(self.loop_note)
        self.loop_install = QtWidgets.QPushButton("Add “Loopback (Mbox)” to your sound settings")
        self.loop_install.clicked.connect(self.install_named_inputs)
        l.addWidget(self.loop_install, 0, Qt.AlignLeft)
        lay.addWidget(c)
        self.refreshers.append(self.refresh_loopback)

    def refresh_loopback(self):
        first = self.app_state.get("loopback", -1)
        self.loop_box.blockSignals(True)
        self.loop_box.setCurrentIndex(max(0, self.loop_box.findData(first)))
        self.loop_box.blockSignals(False)
        self.loop_mix_btn.setEnabled(first >= 0)
        have = pw_node_exists("mbox_loopback")
        self.loop_install.setVisible(first >= 0 and not have and not os.environ.get("MBOX_AMIXER"))
        if first < 0:
            self.loop_note.setText("Off.")
            return
        want = self.loopback_items(first)
        if want is None:
            self.loop_note.setText("That output plays the computer directly — pick one that plays a mix.")
            return
        ss = self.ctrl("stream-source")
        cur = [ss.items[ss.values[a]] for a in LOOP_AUX] if ss else []
        if cur != list(want) and getattr(self, "loop_tried", None) != (first, tuple(want)):
            self.loop_tried = (first, tuple(want))   # one try per routing change — never a retry loop
            for a, item in zip(LOOP_AUX, want):
                self.set_stream(a, item)
        elif cur == list(want):
            self.loop_tried = None
        name = next(n for n, f, _ in OUTPUT_PAIRS if f == first)
        own = self.label(f"out{first}", "")
        self.loop_note.setText(f"“Loopback (Mbox)” = what {own or name} plays ({self.pair_text(first)})."
                               + ("" if have else " Add it to your sound settings below."))

    def loopback_items(self, first):
        c = self.ctrl("output-source")
        ss = self.ctrl("stream-source")
        if not c or not ss:
            return None
        items = (c.items[c.values[first]], c.items[c.values[first + 1]])
        return items if all(i in ss.items for i in items) else None

    def set_loopback(self, first):
        self.loop_tried = None
        ss = self.ctrl("stream-source")
        if first >= 0 and self.app_state.get("loopback", -1) < 0 and ss:
            self.app_state["loopback_prev"] = [ss.items[ss.values[a]] for a in LOOP_AUX]
        self.app_state["loopback"] = first
        if first < 0:
            for a, item in zip(LOOP_AUX, self.app_state.pop("loopback_prev", ["S/PDIF-1", "S/PDIF-2"])):
                self.set_stream(a, item)
        self.save_app_state()
        self.reload_state()
        self.refresh_loopback()

    def make_stream_mix(self):
        first = self.app_state.get("loopback", -1)
        if first < 0:
            return
        name = next(n for n, f, _ in OUTPUT_PAIRS if f == first)
        own = self.label(f"out{first}", "")
        if own and QtWidgets.QMessageBox.question(
                self, APP_NAME, f"{name} feeds “{own}”. Turning it into a stream mix changes what that "
                "plays too. Go ahead?") != QtWidgets.QMessageBox.Yes:
            return
        kind, k, _ = self.pair_source(first)
        if kind != "mix":
            used = {self.pair_source(f)[1] for _, f, _ in OUTPUT_PAIRS if self.pair_source(f)[0] == "mix"}
            free = [m for m in STEREO_MIX if m not in used and (self.rate() <= 96000 or m <= 2)]
            if not free:
                QtWidgets.QMessageBox.information(self, APP_NAME, "Every Stereo Mix is already in use — pick an "
                                                  "output that already plays one.")
                return
            k = free[0]
            self.route_pair_to_mix(first, k)
        self.mix_clear(k)
        g = int(UNITY * 0.7071)
        mics = self.guitar_inputs() if self.app_state.get("guitar_inputs") else ["Analog-A-1"]
        for m in mics:
            self.mix_put(k, m, g, g)
        self.mix_put(k, PC_LEFT, UNITY, 0)
        self.mix_put(k, PC_RIGHT, 0, UNITY)
        self.reload_state()
        self.run_refreshers()
        self.toast(f"Stream mix ready on {name}: mic + computer. Balance it on the Mixer page.", 8000)

    # ---------------------------------------------------------------- 4.14: pop-up, app rules, backup
    def osd_value(self, name, values):
        if not self.app_state.get("osd", True) or not values:
            return
        if name == "master-knob-value":
            spk = (self.ctrl_item("spkr-button") or "").split("-")[0]
            L = SPEAKERS.get(spk, ("",))[0]
            title = "Mbox" + (f" — Speakers {L}" if L else "")
            self.osd.volume(round(int(values[0]) * 100 / 255), title)
        elif name == "spkr-button":
            it = self.ctrl_item("spkr-button") or "Off"
            sc = SPEAKERS.get(it.split("-")[0])
            if sc:
                own = self.label(f"spk{sc[0]}", "") or self.label(f"cr_name{sc[0]}", "")
                self.osd.text("audio-speakers", f"Speakers {sc[0]}" + (f" — {own}" if own else ""))
            else:
                self.osd.text("audio-volume-muted", "Speakers off")

    def start_rules_watch(self):
        if not shutil.which("pactl") or getattr(self, "rules_proc", None):
            return
        self.rules_proc = QtCore.QProcess(self)
        self.rules_proc.setProgram("pactl")
        self.rules_proc.setArguments(["subscribe"])
        self.rules_proc.readyReadStandardOutput.connect(self.rules_event)
        self.rules_proc.start()
        QtCore.QTimer.singleShot(1500, self.apply_app_rules)

    def rules_event(self):
        data = bytes(self.rules_proc.readAllStandardOutput()).decode(errors="ignore")
        if "'new' on sink-input" in data and self.app_state.get("app_rules"):
            QtCore.QTimer.singleShot(250, self.apply_app_rules)

    def apply_app_rules(self):
        rules = self.app_state.get("app_rules", {})
        if not rules:
            return
        sinks = {k["name"]: k["id"] for k in list_sinks()}
        for st in list_app_streams():
            want = rules.get(st["app"])
            if want and want in sinks and st["sink"] != sinks[want]:
                run_quiet("pactl", "move-sink-input", str(st["id"]), want)

    def set_app_rule(self, app, sink):
        rules = self.app_state.setdefault("app_rules", {})
        if sink:
            rules[app] = sink
        else:
            rules.pop(app, None)
        self.save_app_state()
        self.apps_sig = None
        self.refresh_apps()

    def export_settings_dialog(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export settings",
                                                        str(HOME / f"mbox-control-{date.today()}.mboxbackup"),
                                                        "Mbox Control backup (*.mboxbackup)")
        if not path:
            return
        keys = QtWidgets.QMessageBox.question(
            self, APP_NAME, "Include your Discogs and AcoustID keys?\n\nSay No if you'll share this file.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes
        self.save_app_state()
        try:
            export_settings(path, keys)
            self.toast(f"Settings saved to {path}", 8000)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, APP_NAME, f"Couldn't save: {e}")

    def import_settings_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import settings", str(HOME),
                                                        "Mbox Control backup (*.mboxbackup);;All files (*)")
        if not path:
            return
        if QtWidgets.QMessageBox.question(self, APP_NAME, "Replace your current names, scenes and settings with "
                                          "the ones in this backup? The app restarts afterwards.") \
                != QtWidgets.QMessageBox.Yes:
            return
        ok, msg, pw = import_settings(path)
        if not ok:
            QtWidgets.QMessageBox.warning(self, APP_NAME, msg)
            return
        if pw:
            restart_pipewire()
        self.restart_app()

    def install_named_inputs(self, ask=True):
        src = mbox_pw_node("source")
        if not src:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't find the Mbox in PipeWire. "
                                                          "Is its profile set to Pro Audio?")
            return False
        if ask and QtWidgets.QMessageBox.question(
                self, APP_NAME, "This writes ~/.config/pipewire/pipewire.conf.d/mbox-inputs.conf and "
                                "restarts PipeWire — sound cuts out for a second and playing apps may need "
                                "restarting. Continue?") != QtWidgets.QMessageBox.Yes:
            return False
        write_named_inputs_conf(src)
        restart_pipewire()
        QtCore.QTimer.singleShot(2500, fix_default_sink)
        self.run_refreshers()
        self.toast("Named inputs installed. Restart apps like Audacity to see them.", 8000)
        return True

    def toggle_tuner(self, on):
        if on:
            src = mbox_pw_node("source")
            if not src:
                self.tuner_btn.setChecked(False)
                QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't find the Mbox input in PipeWire.")
                return
            self.tuner = TunerWorker(src, self.tuner_ch.currentIndex(), self.rate())
            self.tuner.pitch.connect(self.tuner_view.set_freq)
            self.tuner.failed.connect(self.tuner_failed)
            self.tuner.start()
            self.tuner_btn.setText("Stop tuner")
        else:
            if self.tuner:
                self.tuner.stop()
                self.tuner.wait(1500)
                self.tuner = None
            self.tuner_view.set_freq(0)
            self.tuner_btn.setText("Start tuner")

    def tuner_failed(self, msg):
        self.tuner_btn.setChecked(False)
        QtWidgets.QMessageBox.warning(self, APP_NAME, msg)

    # ================================================================ EFFECTS
    def _build_effects(self):
        w, lay = page("Effects", "The Mbox's built-in effect. Turn up the purple FX knob on any mixer "
                                 "strip to send to it; hear it through the FX return strips. Not "
                                 "available above 96 kHz.")
        c, l = card("Effect")
        form = QtWidgets.QFormLayout()
        form.setSpacing(12)
        form.addRow("Type", self.bind_combo("reverb-type", names=[
            "Room 1", "Room 2", "Room 3", "Hall 1", "Hall 2", "Plate", "Echo", "Delay"]))
        pct = lambda v: f"{v * 100 // 255}%"  # noqa: E731
        form.addRow("Duration / delay time", self.bind_slider("reverb-duration", fmt=pct))
        form.addRow("Feedback (echo and delay)", self.bind_slider("reverb-feedback", fmt=pct))
        form.addRow("Effect level", self.bind_slider("reverb-output-volume", fmt=pct))
        l.addLayout(form)
        lay.addWidget(c)
        rc, rl = card("Wiring", "The effect's input should be fed by the FX send bus — this is the "
                                "normal setup. Use the button if the effect seems silent.")
        b = QtWidgets.QPushButton("Wire effect input to the FX sends")
        b.clicked.connect(lambda: self.route_pair(10, f"Mixer-{FX_BUS[0] + 1}", f"Mixer-{FX_BUS[1] + 1}"))
        rl.addWidget(b, 0, Qt.AlignLeft)
        lay.addWidget(rc)
        self._build_guitar_card(lay)
        lay.addStretch(1)
        return scroll(w)

    # ================================================================ REMOTE
    def _build_remote(self):
        w, lay = page("Remote", "Control the Mbox from your phone on the couch: scenes, speaker sets, "
                                "volume and live levels.")
        c, l = card()
        self.remote_btn = QtWidgets.QPushButton("Start phone remote")
        self.remote_btn.setObjectName("primary")
        self.remote_btn.setCheckable(True)
        self.remote_btn.setMinimumHeight(44)
        self.remote_btn.toggled.connect(lambda on: self.start_remote() if on else self.stop_remote())
        l.addWidget(self.remote_btn, 0, Qt.AlignLeft)
        self.remote_url = QtWidgets.QLabel("")
        self.remote_url.setObjectName("big")
        self.remote_url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l.addWidget(self.remote_url)
        self.remote_qr = QtWidgets.QLabel()
        self.remote_qr.setToolTip("Point your phone's camera at this to open the remote.")
        self.remote_qr.setVisible(False)
        l.addWidget(self.remote_qr)
        self.fw_btn = QtWidgets.QPushButton("Let phones through the firewall")
        self.fw_btn.setToolTip("Your computer's firewall blocks phones from reaching the remote. This allows "
                               "it for devices on your home network only. Asks for your password once.")
        self.fw_btn.clicked.connect(lambda: self.allow_remote_firewall(ask=False))
        self.fw_btn.setVisible(False)
        l.addWidget(self.fw_btn, 0, Qt.AlignLeft)
        l.addWidget(muted("Scan with your phone's camera on the same Wi-Fi, then “Add to Home screen”. "
                          "Only phones that scanned this code can use it; +48V is never exposed."))
        unpair = QtWidgets.QPushButton("Unpair all phones")
        unpair.setToolTip("Makes a new code. Every phone has to scan again — use it if a phone is lost or "
                          "you shared the code by mistake.")
        unpair.clicked.connect(self.unpair_phones)
        l.addWidget(unpair, 0, Qt.AlignLeft)
        self.remote_auto = QtWidgets.QCheckBox("Start the remote whenever Mbox Control opens")
        self.remote_auto.setChecked(bool(self.app_state.get("remote_on")))
        self.remote_auto.toggled.connect(lambda on: (self.app_state.__setitem__("remote_on", on),
                                                     self.save_app_state()))
        l.addWidget(self.remote_auto)
        lay.addWidget(c)
        lay.addStretch(1)
        return scroll(w)

    def start_remote(self, quiet=False):
        if self.remote.running():
            return
        if not self.remote.files:
            self.remote.files = {"/manifest.webmanifest": (REMOTE_MANIFEST.encode(), "application/manifest+json"),
                                 "/sw.js": (REMOTE_SW.encode(), "text/javascript"),
                                 "/icon-192.png": (remote_icon_png(192), "image/png"),
                                 "/icon-512.png": (remote_icon_png(512), "image/png")}
        if not self.app_state.get("remote_token"):
            import secrets
            self.app_state["remote_token"] = secrets.token_urlsafe(9)
            self.save_app_state()
        self.remote.token = self.app_state["remote_token"]
        try:
            self.remote.start()
        except OSError as e:
            if not quiet:
                QtWidgets.QMessageBox.warning(self, APP_NAME, f"Couldn't start the remote: {e}")
            self.remote_btn.setChecked(False)
            return
        self.push_remote_state()
        if not getattr(self, "remote_timer", None):
            self.remote_timer = QtCore.QTimer(self)          # live meters / rip time for the phone
            self.remote_timer.timeout.connect(self.push_remote_state)
        self.remote_timer.start(400)
        self.remote_btn.blockSignals(True)
        self.remote_btn.setChecked(True)
        self.remote_btn.setText("Stop phone remote")
        self.remote_btn.blockSignals(False)
        url = f"http://{lan_ip()}:{REMOTE_PORT}/?k={self.remote.token}"
        self.remote_url.setText(f"http://{lan_ip()}:{REMOTE_PORT} — scan the code; it has the key in it")
        if not self.app_state.get("fw_open") and firewall_kind():
            self.fw_btn.setVisible(True)
            if not quiet:
                QtCore.QTimer.singleShot(300, lambda: self.allow_remote_firewall(ask=True))
        try:
            self.remote_qr.setPixmap(qr_pixmap(url, 240))
            self.remote_qr.setVisible(True)
        except ValueError:
            self.remote_qr.setVisible(False)

    def unpair_phones(self):
        import secrets
        self.app_state["remote_token"] = secrets.token_urlsafe(9)
        self.save_app_state()
        if self.remote.running():
            self.stop_remote()
            self.start_remote(quiet=True)
        self.toast("New code made — phones need to scan it again.", 6000)

    def allow_remote_firewall(self, ask=True):
        if ask and QtWidgets.QMessageBox.question(
                self, APP_NAME, "Your computer's firewall will block your phone from reaching the remote.\n\n"
                "Allow it for devices on your home network? (Asks for your password once.)") \
                != QtWidgets.QMessageBox.Yes:
            return
        def done(ok, msg):
            if ok:
                self.app_state["fw_open"] = True
                self.save_app_state()
                self.fw_btn.setVisible(False)
                self.toast("Phones on your home network can now reach the remote.", 8000)
            else:
                QtWidgets.QMessageBox.warning(self, APP_NAME, "Couldn't change the firewall." +
                                              (f"\n\n{msg}" if msg else ""))
        cmd = firewall_open_cmd(REMOTE_PORT)
        if cmd:
            self.run_root(cmd, "Opening the firewall for the phone remote…", done)
        else:
            done(True, "")

    def stop_remote(self):
        if getattr(self, "remote_timer", None):
            self.remote_timer.stop()
        self.remote.stop()
        self.remote_btn.setText("Start phone remote")
        self.remote_url.setText("")
        self.remote_qr.setVisible(False)

    def push_remote_state(self):
        if not getattr(self, "remote", None) or not self.remote.running():
            return
        spk = self.ctrl_item("spkr-button") or "Off"
        sc = SPEAKERS.get(spk.split("-")[0])
        trims = self.ctrl("output-trim")
        knob = self.ctrl("master-knob-value")
        mute = self.ctrl_item("mute-button")

        def pct(item):
            return round(max(0.0, (meter_db(self.input_level(item)) - METER_FLOOR_DB) / -METER_FLOOR_DB) * 100) \
                if self.input_level(item) else 0

        scenes = [{"id": s["id"], "name": s["name"].strip(), "short": self.scene_view(s)["short"]} for s in SCENES]
        on = self.app_state["setup"].get("always_on") or []
        if on:
            aux_l, aux_r = on[0], pair_right(on[0])
            idx = [j for j, _ in INPUT_JACKS].index(aux_l)
            aux_label = self.label(f"in{idx}", "") or pair_label(aux_l)
        else:
            aux_l, aux_r, aux_label = (*self.rec_source()[:2], self.rip_name())
        scenes += [{"id": "user:" + n, "name": "⭐ " + n, "short": "Your scene"} for n in self.user_scenes()]
        rec = {}
        if hasattr(self, "recorder"):
            running = self.recorder.running()
            state = ("armed" if self.auto_state == "armed" else "rec") if running else "idle"
            lvl = max((self.input_level(item) for item, _ in getattr(self, "rec_meters", []) if item), default=0)
            q, cur = self.app_state.get("queue", []), self.app_state.get("queue_cur", -1)
            qtxt = ""
            if 0 <= cur < len(q):
                it = q[cur]
                qtxt = f"Queue {cur + 1}/{len(q)}: {it.get('album') or it.get('artist') or ''}"
            saving = ""
            if getattr(self, "exporter", None) and self.exporter.isRunning():
                saving = self.split_status.text()
            if getattr(self, "take_mode", False):
                state = "take"
            rec_retro = bool(getattr(self, "retro", None))
            rec = {"retro": rec_retro, "state": state, "auto": self.auto_rec.isChecked(), "src": self.rec_source()[2],
                   "time": self.rec_time.text() if running else "0:00",
                   "level": round(max(0.0, (meter_db(lvl) - METER_FLOOR_DB) / -METER_FLOOR_DB) * 100) if lvl else 0,
                   "info": self.rec_info.text() if running else
                   ("Arm, then press play — it starts and stops with the music." if self.auto_rec.isChecked()
                    else "Starts recording right away."),
                   "queue": qtxt, "saving": saving}
        self.remote.set_state({
            "mute": bool(self.app_state.get("spk_mute")), "rec": rec,
            "spk_names": {L: (self.label(f"spk{L}", "") or self.label(f"cr_name{L}", "")) for L in "ABC"},
            "speaker": sc[0] if sc else "", "speaker_colour": sc[1] if sc else "",
            "dim": {"On": "Dimmed", "Blink": "Muted"}.get(mute, "Dim off"), "rate": f"{self.rate() / 1000:g} kHz",
            "knob": int(knob.values[0]) if knob else 0,
            "volume": int(trims.values[0]) if trims else 255,
            "m_pc": max(pct(PC_LEFT), pct(PC_RIGHT)), "m_aux": max(pct(aux_l), pct(aux_r)), "aux_label": aux_label,
            "scenes": scenes})

    def on_remote(self, action, arg):
        if action == "scene":
            if arg.startswith("user:"):
                self.apply_user_scene(arg[5:])
            else:
                s = next((x for x in SCENES if x["id"] == arg), None)
                if s:
                    self.run_scene(s)
        elif action == "speakers" and arg in "ABC" and arg:
            self.switch_speakers(arg)
        elif action == "mute":
            self.mute_speakers(arg == "1")
        elif action == "retro":
            self.save_retro(60)
        elif action == "take":
            if not self.recorder.running() and not pw_node_exists("mbox_tape_in"):
                self.toast("Set up the rip input on the computer once before the phone can record.", 8000)
            else:
                self.toggle_take()
        elif action == "record":
            if not self.recorder.running() and not pw_node_exists("mbox_tape_in"):
                self.toast("The phone can't start the first recording — set up the rip input on the computer once.",
                           8000)
            else:
                self.toggle_record()
        elif action == "volume":
            c = self.ctrl("output-trim")
            if c and arg.isdigit():
                v = max(0, min(255, int(arg)))
                vals = list(c.values)
                vals[0] = vals[1] = v
                self.write(c, vals)
                self.refresh_soon()
        self.push_remote_state()

    # ================================================================ SETTINGS
    def _build_settings(self):
        w, lay = page("Settings")
        ac, al = card("Audio engine", "Sample rate and latency are set through PipeWire, which then sets "
                                      "the Mbox. Changes apply now and are remembered.")
        form = QtWidgets.QFormLayout()
        form.setSpacing(10)
        self.rate_box = QtWidgets.QComboBox()
        for r in RATES:
            self.rate_box.addItem(f"{r / 1000:g} kHz" + ("   (2 mixes, no effect)" if r > 96000 else ""), r)
        form.addRow("Sample rate", self.rate_box)
        self.quant_box = QtWidgets.QComboBox()
        form.addRow("Buffer (latency)", self.quant_box)
        form.addRow("Clock source", self.bind_combo("clock-source", names=[
            "S/PDIF (breakout)", "Word clock (breakout)", "Internal"]))
        al.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        apply_b = QtWidgets.QPushButton("Apply")
        apply_b.setObjectName("primary")
        apply_b.clicked.connect(self.apply_engine)
        reset_b = QtWidgets.QPushButton("Back to automatic")
        reset_b.clicked.connect(self.reset_engine)
        row.addWidget(apply_b)
        row.addWidget(reset_b)
        row.addStretch(1)
        al.addLayout(row)
        self.engine_note = muted("")
        al.addWidget(self.engine_note)
        lay.addWidget(ac)

        def fill_quanta():
            rate = self.rate_box.currentData() or 48000
            cur = self.quant_box.currentData()
            self.quant_box.blockSignals(True)
            self.quant_box.clear()
            for q in QUANTA:
                self.quant_box.addItem(f"{q} samples  ≈ {q / rate * 1000:.1f} ms", q)
            i = self.quant_box.findData(cur or self.app_state.get("quantum", 1024))
            self.quant_box.setCurrentIndex(max(i, 0))
            self.quant_box.blockSignals(False)

        self.rate_box.currentIndexChanged.connect(lambda _: fill_quanta())

        def refresh_engine():
            if not self.rate_box.view().isVisible():
                i = self.rate_box.findData(self.rate())
                self.rate_box.blockSignals(True)
                self.rate_box.setCurrentIndex(max(i, 0))
                self.rate_box.blockSignals(False)
            fill_quanta()
            self.engine_note.setText(f"The Mbox is running at {self.rate()} Hz.")

        self.refreshers.append(refresh_engine)

        ic, il = card("Inputs")
        row = QtWidgets.QGridLayout()
        for i in range(4):
            row.addWidget(self.bind_check("input-hp-filter", f"75 Hz low-cut on input {i + 1}", 0, i), i // 2, i % 2)
        row.setColumnStretch(2, 1)
        il.addLayout(row)
        lay.addWidget(ic)

        oc, ol = card("Output trims", "Fine level for each analog output (Avid's Left/Right Trim).")
        grid = QtWidgets.QGridLayout()
        pct = lambda v: f"{v * 100 // 255}%"  # noqa: E731
        for r, (name, first, analog) in enumerate(OUTPUT_PAIRS[:3]):
            grid.addWidget(QtWidgets.QLabel(name), r, 0)
            grid.addWidget(self.bind_slider("output-trim", 0, analog[0] - 1, fmt=pct), r, 1)
            grid.addWidget(self.bind_slider("output-trim", 0, analog[1] - 1, fmt=pct), r, 2)
        ol.addLayout(grid)
        lay.addWidget(oc)

        bc, bl = card("Front-panel buttons")
        form = QtWidgets.QFormLayout()
        hold = QtWidgets.QComboBox()
        for ms in (250, 500, 750, 1000):
            hold.addItem(f"{ms} ms", ms // 10)

        def refresh_hold():
            c = self.ctrl("hold-duration")
            if c:
                i = hold.findData(c.values[0])
                hold.blockSignals(True)
                hold.setCurrentIndex(i if i >= 0 else 1)
                hold.blockSignals(False)

        hold.currentIndexChanged.connect(lambda i: self.write_element("hold-duration", 0, 0, hold.itemData(i)))
        hold.setEnabled(self.ctrl("hold-duration") is not None)
        self.refreshers.append(refresh_hold)
        form.addRow("Press-and-hold time", hold)
        form.addRow("", self.bind_check("dim-led", "Dim the button lights when off (instead of dark)"))
        bl.addLayout(form)
        lay.addWidget(bc)

        hc, hl = card("Headphones output for apps",
                      "Adds “Headphones (Mbox)” next to your main Mbox output in your sound settings. Anything "
                      "sent there plays only in Headphone A — great for a game or call while music stays "
                      "on the speakers.")
        hb_row = QtWidgets.QHBoxLayout()
        hbtn = QtWidgets.QPushButton("Install Headphones (Mbox) output")
        hbtn.clicked.connect(self.install_headphones)
        hb_row.addWidget(hbtn)
        self.hp_status = muted("")
        hb_row.addWidget(self.hp_status, 1)
        hl.addLayout(hb_row)
        self.refreshers.append(lambda: self.hp_status.setText(
            "Installed ✓" if HEADPHONES_CONF.exists() else "Not installed"))
        lay.addWidget(hc)

        self.build_auto_scenes_card(lay)
        kc, kl = card("Keyboard shortcuts",
                      "In the app: Ctrl+M mute speakers · Ctrl+1…9 scenes · Ctrl+Shift+A/B/C speaker sets · "
                      "Ctrl+↑/↓ speaker volume. For shortcuts that work anywhere (even with the window "
                      "hidden), add these commands in KDE System Settings → Keyboard → Shortcuts → "
                      "Add New → Command or Script:")
        g = QtWidgets.QGridLayout()
        me = Path(__file__).resolve()
        for r, (cmd, desc) in enumerate(COMMANDS.items()):
            g.addWidget(QtWidgets.QLabel(desc), r, 0)
            full = f"{shlex.quote(sys.executable)} {me} --cmd {cmd}"
            e = QtWidgets.QLineEdit(full)
            e.setReadOnly(True)
            g.addWidget(e, r, 1)
            cp = QtWidgets.QPushButton("Copy")
            cp.clicked.connect(lambda _=False, t=full: (QtWidgets.QApplication.clipboard().setText(t),
                                                        self.toast("Copied — paste it into KDE's shortcut settings.")))
            g.addWidget(cp, r, 2)
        g.setColumnStretch(1, 1)
        kl.addLayout(g)
        lay.addWidget(kc)

        mdc, mdl = card("MIDI controller", "Use knobs, faders and pads on a MIDI controller. Pick the "
                                           "port, tick Enable, then press Learn and move a control. You can "
                                           "also right-click any mixer channel name → MIDI learn.")
        prow = QtWidgets.QHBoxLayout()
        self.midi_port = QtWidgets.QComboBox()
        self.midi_port.setMinimumWidth(260)
        prow.addWidget(self.midi_port)
        rf = QtWidgets.QPushButton("Refresh")
        rf.clicked.connect(self.fill_midi_ports)
        prow.addWidget(rf)
        self.midi_on = QtWidgets.QCheckBox("Enable")
        self.midi_on.setChecked(bool(self.app_state["midi"].get("on")))
        self.midi_on.toggled.connect(self.midi_enable)
        prow.addWidget(self.midi_on)
        prow.addStretch(1)
        mdl.addLayout(prow)
        lrow = QtWidgets.QHBoxLayout()
        for label, target in (("Speaker volume", "trim:0"), ("Mute speakers", "mute"),
                              ("Listening scene", "scene:listen"), ("Rip scene", "scene:rip")):
            b = QtWidgets.QPushButton(f"Learn: {label}")
            b.clicked.connect(lambda _=False, t=target, l=label: self.midi_learn(t, l))
            lrow.addWidget(b)
        lrow.addStretch(1)
        mdl.addLayout(lrow)
        self.midi_list = muted("")
        mdl.addWidget(self.midi_list)
        clr = QtWidgets.QPushButton("Clear all MIDI mappings")
        clr.clicked.connect(lambda: (self.app_state["midi"].__setitem__("map", {}), self.save_app_state(),
                                     self.refresh_midi_list()))
        mdl.addWidget(clr, 0, Qt.AlignLeft)
        lay.addWidget(mdc)
        self.fill_midi_ports()
        self.refresh_midi_list()

        apc, apl = card("Appearance", "Applies after a restart.")
        f = QtWidgets.QFormLayout()
        th = QtWidgets.QComboBox()
        for lbl, val in (("Follow KDE (light or dark)", "system"), ("Dark", "dark"), ("Light", "light")):
            th.addItem(lbl, val)
        th.setCurrentIndex(max(0, th.findData(self.app_state.get("theme", "system"))))
        ac = QtWidgets.QComboBox()
        for lbl, val in (("Follow KDE accent colour", "system"), ("Mbox blue", "blue")):
            ac.addItem(lbl, val)
        ac.setCurrentIndex(max(0, ac.findData(self.app_state.get("accent", "system"))))
        f.addRow("Theme", th)
        f.addRow("Accent", ac)
        apl.addLayout(f)
        rb = QtWidgets.QPushButton("Restart to apply")
        rb.clicked.connect(lambda: (self.app_state.update(theme=th.currentData(), accent=ac.currentData()),
                                    self.save_app_state(), self.restart_app()))
        apl.addWidget(rb, 0, Qt.AlignLeft)
        lay.addWidget(apc)

        idc, idl = card("Song identification", "Recognises songs by their sound when the album isn't "
                                               "listed anywhere. Free key: acoustid.org/new-application")
        ak = QtWidgets.QLineEdit(self.app_state.get("acoustid_key", ""))
        ak.setPlaceholderText("AcoustID application key")
        ak.editingFinished.connect(lambda: (self.app_state.__setitem__("acoustid_key", ak.text().strip()),
                                            self.save_app_state()))
        idl.addWidget(ak)
        lay.addWidget(idc)

        dc, dl = card("Discogs", "Optional: paste a free personal token from discogs.com → Settings → "
                                 "Developers to look up releases on Discogs when ripping.")
        tok = QtWidgets.QLineEdit(self.app_state.get("discogs_token", ""))
        tok.setPlaceholderText("Discogs personal access token")
        tok.setEchoMode(QtWidgets.QLineEdit.PasswordEchoOnEdit)
        tok.editingFinished.connect(lambda: (self.app_state.__setitem__("discogs_token", tok.text().strip()),
                                             self.save_app_state()))
        dl.addWidget(tok)
        lay.addWidget(dc)

        mc, ml = card("Mixer")
        boost = QtWidgets.QCheckBox("Allow faders above 0 dB (up to +12 dB) — Avid never allows this; "
                                    "it can clip")
        boost.setChecked(bool(self.app_state.get("boost")))
        boost.toggled.connect(lambda on: (self.app_state.__setitem__("boost", on), self.save_app_state(),
                                          self.refresh_mixer()))
        ml.addWidget(boost)
        lay.addWidget(mc)

        stc, stl = card("Standalone mode", "What the Mbox does with no computer connected.")
        form = QtWidgets.QFormLayout()
        form.addRow("Mode", self.bind_combo("standalone-usecase", names=[
            "Mixer — everything summed to outputs 1/2", "AD/DA converter — inputs 1/2 ↔ S/PDIF",
            "Preamp — each input to its matching output", "Undefined"]))
        form.addRow("Clock source", self.bind_combo("standalone-clock-source", names=["S/PDIF", "Word clock", "Internal"]))
        form.addRow("Sample rate", self.bind_combo("standalone-internal-clock-rate"))
        stl.addLayout(form)
        lay.addWidget(stc)

        tc, tl = card("Startup & tray")
        form = QtWidgets.QFormLayout()
        self.startup_box = QtWidgets.QComboBox()

        def refresh_startup_box():
            cur = self.app_state.get("startup_scene", "")
            self.startup_box.blockSignals(True)
            self.startup_box.clear()
            self.startup_box.addItem("None — leave the Mbox as it is", "")
            for n in self.user_scenes():
                self.startup_box.addItem(n, n)
            self.startup_box.setCurrentIndex(max(0, self.startup_box.findData(cur)))
            self.startup_box.blockSignals(False)

        self.refresh_startup_box = refresh_startup_box
        refresh_startup_box()
        self.startup_box.currentIndexChanged.connect(lambda _: (
            self.app_state.__setitem__("startup_scene", self.startup_box.currentData() or ""),
            self.save_app_state(), self.refresh_health()))
        form.addRow("Startup scene", self.startup_box)
        tl.addLayout(form)
        tl.addWidget(muted("When the Mbox has been switched off or used in Windows, its mixer is wiped. "
                           "The app notices and restores this scene automatically. Save one on Home first "
                           "(“＋ Save current setup as a scene…”)."))
        auto = QtWidgets.QCheckBox("Start Mbox Control in the tray when I log in")
        auto.setChecked(AUTOSTART_FILE.exists())

        def set_auto(on):
            if on:
                AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
                AUTOSTART_FILE.write_text(desktop_entry(" --tray"))
            else:
                AUTOSTART_FILE.unlink(missing_ok=True)

        auto.toggled.connect(set_auto)
        tl.addWidget(auto)
        ctt = QtWidgets.QCheckBox("Closing the window hides it to the tray (Quit from the tray icon)")
        ctt.setChecked(bool(self.app_state["close_to_tray"]))
        ctt.toggled.connect(lambda on: (self.app_state.__setitem__("close_to_tray", on), self.save_app_state()))
        tl.addWidget(ctt)
        lay.addWidget(tc)

        sv, svl = card("Background service & launcher")
        svl.addWidget(muted("The control service runs in the background (mbox-ctl.service) and restarts "
                            "itself. Use these if the Mbox stops responding."))
        row = QtWidgets.QHBoxLayout()
        rs = QtWidgets.QPushButton("Restart control service")
        rs.clicked.connect(self.restart_service)
        row.addWidget(rs)
        rp = QtWidgets.QPushButton("Repair background service")
        rp.setToolTip("Reinstalls the service so it finds the Mbox by name and restarts itself")
        rp.clicked.connect(self.repair_service)
        row.addWidget(rp)
        ln = QtWidgets.QPushButton("Install app launcher")
        ln.clicked.connect(self.install_launcher)
        row.addWidget(ln)
        row.addStretch(1)
        svl.addLayout(row)
        lay.addWidget(sv)
        self.build_sleep_card(lay)
        lay.addStretch(1)
        out = self.tabify(w, lay, [("Sound", ["Audio engine", "Inputs", "Output trims", "Mixer",
                                               "Headphones output for apps"]),
                                    ("Mbox", ["Front-panel buttons", "Standalone mode"]),
                                    ("Controls", ["Automatic scenes", "Keyboard shortcuts", "MIDI controller"]),
                                    ("App", [])])
        t = out.findChild(QtWidgets.QTabWidget)
        setup = self._build_setup()
        for h in setup.findChildren(QtWidgets.QLabel, "h1"):
            h.setVisible(False)
        t.insertTab(0, setup, "Your setup")
        t.setCurrentIndex(0)
        self.settings_tabs = t
        return out

    def apply_engine(self):
        rate, q = self.rate_box.currentData(), self.quant_box.currentData()
        if rate > 96000 and QtWidgets.QMessageBox.question(
                self, APP_NAME, "Above 96 kHz the Mbox drops to 2 stereo mixes and turns the effect off. "
                                "Mixes 3–6 stop working. Continue?") != QtWidgets.QMessageBox.Yes:
            return
        ok = pw_set("clock.force-rate", rate) and pw_set("clock.force-quantum", q)
        write_clock_conf(rate, q)
        self.app_state.update({"rate": rate, "quantum": q})
        self.save_app_state()
        self.toast("Applied." if ok else "Saved for next start (pw-metadata isn't available right now).")
        QtCore.QTimer.singleShot(1500, lambda: self.reload_state() and self.run_refreshers())

    def reset_engine(self):
        pw_set("clock.force-rate", 0)
        pw_set("clock.force-quantum", 0)
        (PW_CONF_DIR / "mbox-clock.conf").unlink(missing_ok=True)
        self.app_state.pop("rate", None)
        self.app_state.pop("quantum", None)
        self.save_app_state()
        self.toast("PipeWire picks the sample rate and buffer automatically again.")

    def restart_service(self):
        if run_quiet("systemctl", "--user", "restart", "mbox-ctl.service") is None:
            start_service(self.card_no)
        QtCore.QTimer.singleShot(2500, lambda: (self.reload_state(), self.run_refreshers(),
                                                self.toast("Control service restarted.")))

    def install_launcher(self):
        install_icon_files()
        LAUNCHER_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHER_FILE.write_text(desktop_entry())
        if AUTOSTART_FILE.exists():
            AUTOSTART_FILE.write_text(desktop_entry(" --tray"))
        run_quiet("update-desktop-database", str(LAUNCHER_FILE.parent))
        self.toast("Launcher and icon installed — find Mbox Control in your app menu.")

    # ================================================================ ADVANCED
    def _build_advanced(self):
        w, lay = page("Advanced", "The raw controls behind everything else — for troubleshooting.")
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(scroll(self._adv_outputs()), "Output router")
        tabs.addTab(scroll(self._adv_streams()), "To computer")
        tabs.addTab(self._adv_buses(), "Raw buses")
        tabs.addTab(scroll(self._adv_misc()), "LEDs && clock")
        lay.addWidget(tabs, 1)
        return w

    def _adv_outputs(self):
        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        c = self.ctrl("output-source")
        for col, h in enumerate(["Slot", "Feeds", "Source", "Level"]):
            g.addWidget(QtWidgets.QLabel(f"<b>{h}</b>"), 0, col)
        for i in range(len(c.values) if c else 0):
            g.addWidget(QtWidgets.QLabel(str(i + 1)), i + 1, 0)
            g.addWidget(QtWidgets.QLabel(SLOT_NAMES.get(i, "")), i + 1, 1)
            g.addWidget(self.bind_combo("output-source", 0, i), i + 1, 2)
            m = Meter()
            self.meters.append(("output-source-meter", i, m))
            g.addWidget(m, i + 1, 3)
        g.setColumnStretch(3, 1)
        g.setRowStretch(17, 1)
        return w

    def _adv_streams(self):
        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        c = self.ctrl("stream-source")
        for col, h in enumerate(["Linux channel", "Source", "Level"]):
            g.addWidget(QtWidgets.QLabel(f"<b>{h}</b>"), 0, col)
        for i in range(len(c.values) if c else 0):
            g.addWidget(QtWidgets.QLabel(f"capture_AUX{i}"), i + 1, 0)
            g.addWidget(self.bind_combo("stream-source", 0, i), i + 1, 1)
            m = Meter()
            self.meters.append(("stream-source-meter", i, m))
            g.addWidget(m, i + 1, 2)
        g.setColumnStretch(2, 1)
        g.setRowStretch(9, 1)
        return w

    def _adv_buses(self):
        w = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(w)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("<b>Bus</b>"))
        bus_box = QtWidgets.QComboBox()
        top.addWidget(bus_box)
        top.addStretch(1)
        outer.addLayout(top)
        buses = sorted(c.index for c in self.ctrls.values() if c.name == "mixer-source-gain")
        for b in buses:
            bus_box.addItem(f"Bus {b + 1} — {friendly(f'Mixer-{b + 1}')}", b)
        self.raw_bus = buses[0] if buses else 0
        src = self.ctrl("mixer-source")
        sw = QtWidgets.QWidget()
        strips = QtWidgets.QHBoxLayout(sw)
        faders = []
        for slot in range(len(src.values) if src else 0):
            col = QtWidgets.QVBoxLayout()
            combo = self.bind_combo("mixer-source", 0, slot)
            combo.setMaximumWidth(110)
            col.addWidget(combo)
            fader = Fader(GAIN_MAX)
            col.addWidget(fader, 1)
            val = QtWidgets.QLabel("−∞")
            val.setAlignment(Qt.AlignCenter)
            col.addWidget(val)
            strips.addLayout(col)
            send = Debounced(lambda v, s=slot: self.write_element("mixer-source-gain", self.raw_bus, s, v))

            def moved(v, val=val, send=send):
                val.setText(gain_text(v))
                send(v)

            fader.gainChanged.connect(moved)
            faders.append((fader, val))

        def refresh():
            c = self.ctrl("mixer-source-gain", self.raw_bus)
            if not c:
                return
            for slot, (fader, val) in enumerate(faders):
                if slot < len(c.values):
                    fader.setGain(int(c.values[slot]))
                    val.setText(gain_text(fader.gain))

        self.refreshers.append(refresh)

        def changed(i):
            self.raw_bus = bus_box.itemData(i)
            refresh()

        bus_box.currentIndexChanged.connect(changed)
        outer.addWidget(scroll(sw), 1)
        return w

    def _adv_misc(self):
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        f.addRow("Speaker button LED", self.bind_combo("spkr-button"))
        f.addRow("Dim/Mute LED", self.bind_combo("mute-button"))
        f.addRow("Mono LED", self.bind_combo("mono-button"))
        f.addRow("", self.bind_check("metering", "Metering enabled"))
        f.addRow("Mbox clock rate (direct)", self.bind_combo("clock-rate"))
        f.addRow("Standalone S/PDIF high rate", self.bind_check("standalone-spdif-high-rate", ""))
        f.addRow("Standalone word clock mode", self.bind_combo("standalone-word-clock-mode"))
        nick = self.ctrl("nickname")
        if nick:
            f.addRow("Device name", QtWidgets.QLabel(bytes(v for v in nick.values if v).decode(errors="replace")))
        self.events_lbl = QtWidgets.QLabel()
        f.addRow("Live hardware events", self.events_lbl)
        self.refreshers.append(lambda: self.events_lbl.setText("connected ✓" if self.events_alive
                                                               else "not available — polling instead"))
        f.addRow("App version", QtWidgets.QLabel(VERSION))
        uc = QtWidgets.QCheckBox("Check for new versions once a day")
        uc.setChecked(bool(self.app_state.get("update_check")) and bool(UPDATE_REPO))
        uc.setEnabled(bool(UPDATE_REPO))
        uc.setToolTip("Asks GitHub whether there's a newer release. Nothing else is sent." if UPDATE_REPO else
                      "Available once Mbox Control is published on GitHub.")
        uc.toggled.connect(lambda on: (self.app_state.__setitem__("update_check", on),
                                       self.app_state.__setitem__("update_checked_at", 0),
                                       self.save_app_state(), self.check_update()))
        f.addRow("Updates", uc)
        tu = QtWidgets.QCheckBox("Tester mode: offer new versions I save to Downloads")
        tu.setChecked(bool(self.app_state.get("tester_updates")))
        tu.setToolTip("For trying test builds: a newer mbox_control.py in ~/Downloads shows up as an update.")
        tu.toggled.connect(lambda on: (self.app_state.__setitem__("tester_updates", on), self.save_app_state(),
                                       self.check_update()))
        f.addRow("", tu)
        dm = QtWidgets.QCheckBox("Fast meter reading (reads the Mbox directly)")
        dm.setChecked(self.app_state.get("direct_meters", True) is not False)
        dm.setToolTip("Much lighter on the CPU. If meters misbehave or the app stalls, switch it off — "
                      "the app goes back to the slower amixer way after a restart.")
        dm.toggled.connect(lambda on: (self.app_state.__setitem__("direct_meters", on), self.save_app_state(),
                                       self.toast("Takes effect after restarting the app.", 5000)))
        f.addRow("Meters", dm)
        osd = QtWidgets.QCheckBox("Show a pop-up when the big knob or Spkr button changes")
        osd.setChecked(bool(self.app_state.get("osd", True)))
        osd.toggled.connect(lambda on: (self.app_state.__setitem__("osd", on), self.save_app_state()))
        f.addRow("Pop-ups", osd)
        brow = QtWidgets.QHBoxLayout()
        for text, fn in (("Export settings…", self.export_settings_dialog), ("Import settings…", self.import_settings_dialog)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            brow.addWidget(b)
        brow.addStretch(1)
        f.addRow("Backup", brow)
        diag = QtWidgets.QPushButton("Copy diagnostics")
        diag.setToolTip("Copies your system details (no personal info) — paste them into a bug report.")
        diag.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(diagnostics()),
                                      self.toast("Diagnostics copied — paste them into your bug report.")))
        f.addRow("", diag)
        f.addRow("", muted("Unofficial. Not made or supported by Avid; “Mbox” is Avid's trademark. "
                           "Free software under the GNU GPL v3 or later. Built on the open-source snd-dice driver and "
                           "snd-firewire-ctl-services."))
        return w

    # ================================================================ snapshots
    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        m.addAction("Save snapshot…").triggered.connect(self.save_snapshot_dialog)
        m.addAction("Load snapshot…").triggered.connect(self.load_snapshot_dialog)
        m.addSeparator()
        m.addAction("Export settings…").triggered.connect(self.export_settings_dialog)
        m.addAction("Import settings…").triggered.connect(self.import_settings_dialog)
        m.addSeparator()
        m.addAction("Re-read from Mbox").triggered.connect(lambda: self.reload_state() and self.run_refreshers())
        m.addSeparator()
        m.addAction("Quit").triggered.connect(self.close)

    def snapshot(self):
        return {c.key: c.values for c in self.ctrls.values()
                if c.name not in VOLATILE and "w" in c.access[:2]}

    def apply_snapshot(self, data):
        keyed = {c.key: c for c in self.ctrls.values()}
        failed = []
        for key, vals in data.items():
            c = keyed.get(key)
            if c is None or c.name in VOLATILE or c.values == vals:
                continue
            try:
                self.backend.write(c, vals)
            except Exception:  # noqa: BLE001
                failed.append(key)
        self.app_state["mutes"], self.app_state["solo"] = {}, {}
        self.app_state.pop("solo_saved", None)
        self.app_state["spk_mute"] = {}
        self.save_app_state()
        self.reload_state()
        self.ensure_marker()
        self.run_refreshers()
        return failed

    def save_snapshot_dialog(self):
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save snapshot", str(CONF_DIR / "snapshot.json"),
                                                        "JSON (*.json)")
        if path:
            self.reload_state()
            save_json(path, self.snapshot())
            self.toast(f"Saved {path}")

    def load_snapshot_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load snapshot", str(CONF_DIR), "JSON (*.json)")
        if path:
            failed = self.apply_snapshot(load_json(path, {}))
            self.toast(f"Loaded {path}" + (f" ({len(failed)} failed)" if failed else ""), 8000)

    def closeEvent(self, e):
        if self.tray and not self.quitting and self.app_state.get("close_to_tray", True):
            e.ignore()
            self.hide()
            if not self.app_state.get("tray_hint"):
                self.tray.showMessage(APP_NAME, "Still running in the tray. Right-click the icon to quit.",
                                      app_icon(), 5000)
                self.app_state["tray_hint"] = True
                self.save_app_state()
            return
        for t in (getattr(self, "poller", None), getattr(self, "events", None), self.tuner):
            if t:
                t.stop()
        if getattr(self, "recorder", None) and self.recorder.running():
            self.recorder.stop()
        if self.analyzer:
            self.analyzer.stop()
        self.stop_midi()
        if hasattr(self, "track_table"):
            self.save_track_sidecar()
        if getattr(self, "remote", None):
            self.remote.stop()
        for k in list(getattr(self, "mirrors", {})):
            self.stop_mirror(k)
        for job in (getattr(self, "retro", None), getattr(self, "metro", None), getattr(self, "pr_player", None)):
            if job:
                job.stop()
        if getattr(self, "mt_proc", None):
            self.mt_proc.terminate()
        if getattr(self, "rules_proc", None):
            self.rules_proc.kill()
            self.rules_proc.waitForFinished(500)
        for t in (getattr(self, "poller", None), getattr(self, "events", None), self.tuner):
            if t:
                t.wait(1500)
        super().closeEvent(e)


# ================================================================== startup

def start_service(card_no):
    if not SERVICE_PATH.exists():
        return False
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    log = open(CONF_DIR / "service.log", "ab")
    subprocess.Popen([str(SERVICE_PATH), str(card_no)], stdout=log, stderr=log, start_new_session=True)
    return True


def mbox_playing():
    """True when PipeWire is actively sending sound to one of the Mbox's outputs."""
    out = run_quiet("pactl", "list", "short", "sinks", timeout=3) or ""
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) >= 5 and f[4].strip().upper() == "RUNNING" and \
                re.search(r"mbox|sa600|dice|firewire|avid", f[1], re.I):
            return True
    return False


def service_running():
    out = run_quiet("pgrep", "-f", "snd-dice-ctl-service")
    return bool(out and out.strip())


def ensure_service(card_no):
    if service_running():
        return True
    unit = HOME / ".config/systemd/user/mbox-ctl.service"
    if unit.exists() and run_quiet("systemctl", "--user", "restart", "mbox-ctl.service") is not None:
        return True
    return start_service(card_no)


def fix_default_sink():
    cur = (run_quiet("pactl", "get-default-sink") or "").strip()
    sinks = run_quiet("pactl", "list", "sinks", "short") or ""
    if MAIN_SINK in sinks and "firewire" in cur and "pro-output" in cur:
        run_quiet("pactl", "set-default-sink", MAIN_SINK)
        return True
    return False


def service_ready(backend):
    try:
        return "output-source" in {c.name for c in backend.read_all().values()}
    except Exception:  # noqa: BLE001
        return False


CRASH_LOG = CONF_DIR / "crash.log"


# ================================================================== 4.20: sleep/wake survival & recovery
#
# What goes wrong (seen on a TI XIO2213 + CachyOS + PipeWire): after resume the FireWire unit comes back
# (fw1.0 in /sys/bus/firewire/devices) but its ALSA card doesn't. The old card lingers as a zombie because
# PipeWire/WirePlumber, the ctl daemon and this app still hold its (deleted) device nodes, so the kernel
# module can't be reloaded ("in use"). The recovery that works: let go of every handle, stop the audio
# stack (sockets too, so nothing grabs the card again), reload the *sound* driver, start everything again.
# NEVER touch firewire_ohci: unbinding or reloading the controller wedges the whole FireWire stack until
# a reboot.

# One place for the kernel driver ↔ control daemon mapping (snd-firewire-ctl-services binaries), so the
# ProFire 610 (BeBoB), Fireworks, MOTU… apps can reuse it.
FW_DAEMONS = {
    "snd_dice": "snd-dice-ctl-service",
    "snd_bebob": "snd-bebob-ctl-service",
    "snd_fireworks": "snd-fireworks-ctl-service",
    "snd_oxfw": "snd-oxfw-ctl-service",
    "snd_firewire_motu": "snd-firewire-motu-ctl-service",
    "snd_fireface": "snd-fireface-ctl-service",
    "snd_firewire_digi00x": "snd-firewire-digi00x-ctl-service",
    "snd_firewire_tascam": "snd-firewire-tascam-ctl-service",
}
SYSFS = Path(os.environ.get("MBOX_SYSFS", "/sys"))
PROCFS = Path(os.environ.get("MBOX_PROCFS", "/proc"))
RECOVERY_LOG = CONF_DIR / "recovery.log"
WAKE_LOG = Path("/var/log/mbox-control-wake.log")
PIPEWIRE_UNITS = ["pipewire.socket", "pipewire-pulse.socket", "pipewire.service", "pipewire-pulse.service",
                  "wireplumber.service"]
PULSE_UNITS = ["pulseaudio.socket", "pulseaudio.service"]
SYSTEM_DIRS = (Path("/usr/lib/mbox-control"), Path("/usr/local/lib/mbox-control"))  # packaged, app-installed
SLEEP_HOOK_NAME = "mbox-control"
SLEEP_HOOK_DIR = Path("/usr/lib/systemd/system-sleep")
POLKIT_ACTION = "org.fancybutler.mboxcontrol.reload-driver"


def rlog(msg):
    """Every recovery step goes here, so setups that behave differently can send a useful report."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        with open(RECOVERY_LOG, "a") as f:
            f.write(line + "\n")
        if RECOVERY_LOG.stat().st_size > 400_000:            # keep it small: last ~half
            text = RECOVERY_LOG.read_text(errors="ignore")
            RECOVERY_LOG.write_text(text[len(text) // 2:])
    except OSError:
        pass
    return line


def _driver_of(unit):
    """Kernel driver of a FireWire unit: its sysfs driver link, or — if no driver is bound (a failed probe
    after wake) — whatever module its modalias resolves to."""
    link = unit / "driver"
    if link.exists():
        return Path(os.path.realpath(link)).name
    try:
        alias = (unit / "modalias").read_text().strip()
    except OSError:
        return None
    out = run_quiet("modprobe", "-R", alias, timeout=4) or ""
    for mod in out.split():
        if mod.replace("-", "_") in FW_DAEMONS:
            return mod.replace("-", "_")
    return None


def fw_audio_units():
    """[{unit: 'fw1.0', driver: 'snd_dice', daemon: 'snd-dice-ctl-service', card: 1 | None}] for every
    FireWire audio unit present. card is None when the unit is there but its ALSA card isn't."""
    base = SYSFS / "bus/firewire/devices"
    try:
        cards_txt = (PROCFS / "asound/cards").read_text()
    except OSError:
        cards_txt = ""
    out = []
    for unit in sorted(base.glob("fw*.*")) if base.exists() else []:
        drv = _driver_of(unit)
        if drv not in FW_DAEMONS:
            continue
        card = None
        m = re.search(r"^\s*(\d+)\s+\[[^\]]*\]:.*\n.*\bat " + re.escape(unit.name) + r"\b", cards_txt, re.M)
        if m:
            card = int(m.group(1))
        else:
            for c in sorted(unit.glob("sound/card*")):          # the card's device lives under the unit
                n = c.name[4:]
                if n.isdigit() and re.search(rf"^\s*{n}\s+\[", cards_txt, re.M):
                    card = int(n)
                    break
        out.append({"unit": unit.name, "driver": drv, "daemon": FW_DAEMONS[drv], "card": card})
    return out


def mbox_unit():
    """The Mbox's FireWire unit (or the first FireWire audio unit), whether or not its card is up."""
    units = fw_audio_units()
    card = find_mbox_card()
    for u in units:
        if card is not None and u["card"] == card:
            return u
    dice = [u for u in units if u["driver"] == "snd_dice"]
    return (dice or units or [None])[0]


def card_missing_after_wake():
    """True only when a FireWire audio unit is present but its ALSA card isn't — the one case the driver
    reload is for. Never true without a unit (nothing plugged in / Mbox off)."""
    u = mbox_unit()
    return bool(u and u["card"] is None)


def systemd_user(*args, timeout=15):
    return run_quiet("systemctl", "--user", *args, timeout=timeout)


def unit_active(name):
    return (systemd_user("is-active", name, timeout=5) or "").strip() == "active"


def existing_user_units(names):
    out = systemd_user("list-unit-files", "--no-legend", *names, timeout=8) or ""
    have = {line.split()[0] for line in out.splitlines() if line.strip()}
    return [n for n in names if n in have]


def audio_stack():
    """('pipewire' | 'pulseaudio' | None, [units to stop/start])."""
    if unit_active("pipewire.service") or unit_active("pipewire.socket"):
        return "pipewire", existing_user_units(PIPEWIRE_UNITS)
    if unit_active("pulseaudio.service") or unit_active("pulseaudio.socket"):
        return "pulseaudio", existing_user_units(PULSE_UNITS)
    return None, []


def card_holders():
    """Processes with a sound device open — including deleted nodes of a zombie card — as {pid: name}."""
    found = {}
    for p in PROCFS.glob("[0-9]*"):
        try:
            for fd in (p / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("/dev/snd/") or "/dev/snd/" in target and "(deleted)" in target:
                    found[int(p.name)] = (p / "comm").read_text().strip()
                    break
        except (OSError, ValueError):
            continue
    return found


def helper_path(name):
    for d in SYSTEM_DIRS:
        if (d / name).exists():
            return d / name
    return None


def sleep_fix_installed():
    return (SLEEP_HOOK_DIR / SLEEP_HOOK_NAME).exists()


RELOAD_HELPER = """#!/bin/sh
# Mbox Control: reload ONE FireWire *sound* driver (never the FireWire controller, firewire_ohci).
# Run through pkexec (polkit action org.fancybutler.mboxcontrol.reload-driver) or by the sleep hook.
set -u
case "${1:-}" in
    %(drivers)s) ;;
    *) echo "mbox-reload-driver: refusing unknown driver '${1:-}'" >&2; exit 2 ;;
esac
logger -t mbox-control "reloading $1" 2>/dev/null || true
if ! modprobe -r "$1"; then
    echo "mbox-reload-driver: $1 is still in use" >&2
    exit 3
fi
modprobe "$1" || exit 4
echo "mbox-reload-driver: $1 reloaded"
""" % {"drivers": "|".join(FW_DAEMONS)}

WAKE_RECOVER = r"""#!/bin/sh
# Mbox Control: after the PC wakes up, bring back a FireWire audio card that didn't return by itself.
# Conditional: does nothing unless a FireWire audio unit is present but its ALSA card is missing.
# Never touches firewire_ohci. Logs every step to /var/log/mbox-control-wake.log and the journal.
SYS=${MBOX_SYSFS:-/sys}; PROC=${MBOX_PROCFS:-/proc}; RUN=${MBOX_RUNDIR:-/run}   # overridable for tests
LOG=${MBOX_WAKE_LOG:-/var/log/mbox-control-wake.log}
WAIT=${MBOX_WAKE_WAIT:-8}
DRIVERS=" %(drivers)s "
log() { echo "$(date '+%%F %%T') $*" >>"$LOG"; logger -t mbox-control "$*" 2>/dev/null || true; }
daemon_for() {
    case "$1" in
%(daemons)s
    esac
}
driver_of() {  # sysfs driver link, else the module the unit's modalias resolves to
    if [ -e "$1/driver" ]; then basename "$(readlink -f "$1/driver")"; return; fi
    [ -r "$1/modalias" ] || return
    for m in $(modprobe -R "$(cat "$1/modalias")" 2>/dev/null); do
        m=$(echo "$m" | tr - _)
        case "$DRIVERS" in *" $m "*) echo "$m"; return ;; esac
    done
}
missing() {  # prints "unit driver" for the first FireWire audio unit whose card isn't in /proc/asound/cards
    for u in "$SYS"/bus/firewire/devices/fw*.*; do
        [ -e "$u" ] || continue
        d=$(driver_of "$u")
        case "$DRIVERS" in *" $d "*) ;; *) continue ;; esac
        grep -q " at $(basename "$u")\b" "$PROC"/asound/cards 2>/dev/null && continue
        echo "$(basename "$u") $d"; return
    done
}
as_user() {  # as_user UID cmd...  — run in that user's session (systemd --user)
    uid=$1; shift
    name=$(getent passwd "$uid" | cut -d: -f1)
    [ -n "$name" ] || return 1
    runuser -u "$name" -- env XDG_RUNTIME_DIR="$RUN/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN/user/$uid/bus" "$@"
}
holds_snd() {  # does process dir $1 have a sound device open (deleted nodes of a zombie card included)?
    for f in "$1"/fd/*; do
        case "$(readlink "$f" 2>/dev/null)" in /dev/snd/*) return 0 ;; esac
    done
    return 1
}
users() {  # logged-in users with a systemd --user instance
    for d in "$RUN"/user/*; do
        u=$(basename "$d")
        [ "$u" -ge 1000 ] 2>/dev/null && [ -S "$d/bus" ] && echo "$u"
    done
}

log "wake: checking FireWire audio"
n=0
while [ $n -lt "$WAIT" ]; do               # give it a moment to come back by itself
    m=$(missing)
    [ -z "$m" ] && { log "wake: all FireWire audio cards are present - nothing to do"; exit 0; }
    sleep 1; n=$((n + 1))
done
unit=${m%% *}; drv=${m#* }; daemon=$(daemon_for "$drv")
log "wake: $unit is present but its sound card is missing (driver $drv) - recovering"

for uid in $(users); do                    # let go of the card in every session
    if as_user "$uid" systemctl --user is-active --quiet mbox-ctl.service; then
        as_user "$uid" systemctl --user stop mbox-ctl.service && echo "$uid" >>"$RUN"/mbox-control.ctl-restart
        log "uid $uid: stopped mbox-ctl.service"
    fi
    if as_user "$uid" systemctl --user is-active --quiet pipewire.service || \
       as_user "$uid" systemctl --user is-active --quiet pipewire.socket; then
        as_user "$uid" systemctl --user stop pipewire.socket pipewire-pulse.socket pipewire.service \
            pipewire-pulse.service wireplumber.service 2>>"$LOG"
        echo "$uid pipewire" >>"$RUN"/mbox-control.restart
        log "uid $uid: stopped PipeWire (and its sockets)"
    elif as_user "$uid" systemctl --user is-active --quiet pulseaudio.service; then
        as_user "$uid" systemctl --user stop pulseaudio.socket pulseaudio.service 2>>"$LOG"
        echo "$uid pulseaudio" >>"$RUN"/mbox-control.restart
        log "uid $uid: stopped PulseAudio"
    else
        log "uid $uid: no PipeWire/PulseAudio user units active - skipping"
    fi
done
if [ -n "$daemon" ] && pkill -x "$daemon"; then log "stopped $daemon"; fi
sleep 1
for p in "$PROC"/[0-9]*; do
    holds_snd "$p" && log "note: still open on a sound device: $(basename "$p") $(cat "$p/comm" 2>/dev/null)"
done

if modprobe -r "$drv" 2>>"$LOG"; then
    if modprobe "$drv" 2>>"$LOG"; then log "reloaded $drv"; else log "ERROR: modprobe $drv failed"; fi
else
    log "ERROR: $drv still in use (see the holders above)"
fi

n=0
while [ $n -lt 10 ] && [ -n "$(missing)" ]; do sleep 1; n=$((n + 1)); done
if [ -z "$(missing)" ]; then
    log "card is back: $(grep -B1 " at $unit" "$PROC"/asound/cards | head -1 | sed 's/^ *//')"
else
    log "ERROR: card still missing after reload"
fi

if [ -f "$RUN"/mbox-control.restart ]; then
    while read -r uid stack; do
        if [ "$stack" = pipewire ]; then
            as_user "$uid" systemctl --user start pipewire.socket pipewire-pulse.socket pipewire.service \
                pipewire-pulse.service wireplumber.service 2>>"$LOG"
        else
            as_user "$uid" systemctl --user start pulseaudio.socket pulseaudio.service 2>>"$LOG"
        fi
        log "uid $uid: started $stack again"
    done <"$RUN"/mbox-control.restart
    rm -f "$RUN"/mbox-control.restart
fi
if [ -f "$RUN"/mbox-control.ctl-restart ]; then
    while read -r uid; do
        as_user "$uid" systemctl --user start mbox-ctl.service && log "uid $uid: started mbox-ctl.service"
    done <"$RUN"/mbox-control.ctl-restart
    rm -f "$RUN"/mbox-control.ctl-restart
fi
log "wake: done"
""" % {"drivers": " ".join(FW_DAEMONS),
       "daemons": "\n".join(f"        {d}) echo {s} ;;" for d, s in FW_DAEMONS.items())}


def sleep_hook_text(recover):
    return ("#!/bin/sh\n# Mbox Control sleep/wake fix (optional). Remove it in Mbox Control: Settings → App →\n"
            "# Sleep & wake, or delete this file. Runs the recovery in the background so resume isn't held up.\n"
            '[ "$1" = post ] || exit 0\n'
            f"{recover} </dev/null >/dev/null 2>&1 &\nexit 0\n")


def polkit_policy(helper):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>Mbox Control</vendor>
  <action id="{POLKIT_ACTION}">
    <description>Reload the sound driver of a FireWire audio interface</description>
    <message>Mbox Control needs to reload the Mbox's sound driver to bring it back</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{helper}</annotate>
  </action>
</policyconfig>
"""


class RecoveryJob(QtCore.QThread):
    """The user-side recovery: stop the ctl daemon and the audio stack, reload the sound driver through the
    root helper (one password prompt), start everything again, wait for the card. Every step is logged."""
    step = QtCore.Signal(str)
    done = QtCore.Signal(bool, str)

    def __init__(self, unit, force=False):
        super().__init__()
        self.unit, self.force = unit, force

    def say(self, msg):
        rlog(msg)
        self.step.emit(msg)

    def run(self):
        try:
            ok, msg = self._run()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"recovery crashed: {e}"
            rlog(msg)
        self.done.emit(ok, msg)

    def _run(self):
        u = self.unit
        if not u:
            return False, "No FireWire audio unit found — is the Mbox switched on and plugged in?"
        drv, daemon = u["driver"], u["daemon"]
        self.say(f"recover: unit {u['unit']}, driver {drv}, daemon {daemon}, card {u['card']}")
        restart_ctl = unit_active("mbox-ctl.service")
        if restart_ctl:
            systemd_user("stop", "mbox-ctl.service")
            self.say("stopped mbox-ctl.service")
        if run_quiet("pkill", "-x", daemon) is not None:
            self.say(f"stopped {daemon}")
        stack, units = audio_stack()
        if units:
            systemd_user("stop", *units, timeout=30)
            self.say(f"stopped {stack}: {' '.join(units)}")
        else:
            self.say("no PipeWire/PulseAudio user units active — not stopping an audio server")
        time.sleep(1)
        held = card_holders()
        if held:
            self.say("still holding a sound device: " + ", ".join(f"{n} ({p})" for p, n in sorted(held.items())))
        helper = helper_path("mbox-reload-driver")
        if helper:
            cmd = ["pkexec", str(helper), drv]
        else:                                                   # not installed: run the same script once
            tmp = CONF_DIR / "mbox-reload-driver.sh"
            tmp.write_text(RELOAD_HELPER)
            cmd = ["pkexec", "/bin/sh", str(tmp), drv]
        self.say("reloading the driver: " + " ".join(cmd).replace(str(HOME), "~"))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if out:
            self.say("helper: " + out.replace("\n", " | "))
        reloaded = r.returncode == 0
        if not reloaded:
            self.say(f"driver reload failed (exit {r.returncode})" +
                     (" — cancelled at the password prompt" if r.returncode in (126, 127) else ""))
        if units:
            systemd_user("start", *units, timeout=30)
            self.say(f"started {stack} again")
        card = None
        for _ in range(30):
            fresh = next((x for x in fw_audio_units() if x["unit"] == u["unit"]), None)
            if fresh and fresh["card"] is not None:
                card = fresh["card"]
                break
            time.sleep(0.5)
        if card is None:
            held = card_holders()
            msg = "The card didn't come back." + (
                " Still holding a sound device: " + ", ".join(sorted(set(held.values()))) if held and not
                reloaded else "")
            self.say(msg)
            return False, msg
        self.say(f"card is back as card {card}")
        if restart_ctl:
            systemd_user("start", "mbox-ctl.service")
            self.say("started mbox-ctl.service again")
        return True, f"card {card}"


class UdevWatch(QtCore.QThread):
    """Card/unit appear and disappear events from udev (sound + firewire). Without pyudev the app falls back
    to its slow watchdog poll."""
    changed = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    @staticmethod
    def available():
        import importlib.util
        return importlib.util.find_spec("pyudev") is not None

    def run(self):
        try:
            import pyudev
            ctx = pyudev.Context()
            mon = pyudev.Monitor.from_netlink(ctx)
            mon.filter_by("sound")
            mon.filter_by("firewire")
            mon.start()
            while self.running:
                dev = mon.poll(timeout=1.0)
                if dev is not None:
                    self.changed.emit(f"{dev.action} {dev.subsystem} {dev.sys_name}")
        except Exception as e:  # noqa: BLE001
            rlog(f"udev watch stopped: {e}")

    def stop(self):
        self.running = False


class SleepWatch(QtCore.QObject):
    """logind's PrepareForSleep(true/false), plus a short 'delay' inhibitor so the app gets a moment to let
    go of the card before the PC actually sleeps."""
    sleeping = QtCore.Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.inhibitor = None
        self.connected = False
        try:
            from PySide6 import QtDBus
            bus = QtDBus.QDBusConnection.sessionBus() if os.environ.get("MBOX_SLEEP_BUS") == "session" else \
                QtDBus.QDBusConnection.systemBus()
            self.connected = bus.connect("", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                                         "PrepareForSleep", self, QtCore.SLOT("on_prepare(bool)"))
        except Exception as e:  # noqa: BLE001
            rlog(f"sleep watch unavailable: {e}")
        self.take_inhibitor()

    @QtCore.Slot(bool)
    def on_prepare(self, going):
        rlog("logind: going to sleep" if going else "logind: woke up")
        self.sleeping.emit(bool(going))

    def take_inhibitor(self):
        if self.inhibitor or not shutil.which("systemd-inhibit") or os.environ.get("MBOX_AMIXER"):
            return
        try:
            self.inhibitor = subprocess.Popen(
                ["systemd-inhibit", "--what=sleep", "--mode=delay", "--who=Mbox Control",
                 "--why=Let go of the Mbox before sleep", "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            self.inhibitor = None

    def release_inhibitor(self):
        if self.inhibitor:
            try:
                os.killpg(self.inhibitor.pid, signal.SIGTERM)
            except OSError:
                pass
            self.inhibitor = None




KWIN_RAISE_JS = """
var want = %s;
var list = (typeof workspace.windowList === "function") ? workspace.windowList() : workspace.clientList();
for (var i = 0; i < list.length; i++) {
    var w = list[i];
    var cls = String(w.resourceClass || "").toLowerCase();
    var file = /dolphin|nautilus|nemo|thunar|caja|pcmanfm/.test(cls);
    if (file && String(w.caption).indexOf(want) >= 0) {
        if (w.minimized) { w.minimized = false; }
        if ("activeWindow" in workspace) { workspace.activeWindow = w; } else { workspace.activeClient = w; }
        break;
    }
}
"""


def kwin_raise(folder_name):
    """Ask KWin (Plasma 5 or 6) to bring the file-manager window showing `folder_name` to the front."""
    try:
        from PySide6 import QtDBus
        js = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"mbox-raise-{os.getuid()}.js"
        js.write_text(KWIN_RAISE_JS % json.dumps(folder_name))
        bus = QtDBus.QDBusConnection.sessionBus()
        sc = QtDBus.QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus)
        if not sc.isValid():
            return False
        name = "mbox-control-raise"
        sc.call("unloadScript", name)
        r = sc.call("loadScript", str(js), name)
        args = r.arguments()
        if r.type() == QtDBus.QDBusMessage.ErrorMessage or not args:
            return False
        sid = int(args[0])
        for obj in (f"/Scripting/Script{sid}", f"/{sid}"):      # Plasma 6 / Plasma 5 object paths
            run = QtDBus.QDBusInterface("org.kde.KWin", obj, "org.kde.kwin.Script", bus)
            if run.isValid() and run.call("run").type() != QtDBus.QDBusMessage.ErrorMessage:
                QtCore.QTimer.singleShot(1500, lambda: sc.call("unloadScript", name))
                return True
        sc.call("unloadScript", name)
    except Exception:  # noqa: BLE001
        pass
    return False


def reveal_in_files(path):
    """Show a file (selected) or folder in the file manager and bring its window to the front — even when
    that folder is already open behind other windows. Uses the freedesktop FileManager1 service (Dolphin,
    Nautilus, Nemo, Thunar…), which raises the existing window; falls back to opening the folder."""
    path = Path(path)
    try:
        from PySide6 import QtDBus
        bus = QtDBus.QDBusConnection.sessionBus()
        iface = QtDBus.QDBusInterface("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                                      "org.freedesktop.FileManager1", bus)
        if iface.isValid():
            uri = QtCore.QUrl.fromLocalFile(str(path)).toString()
            method = "ShowItems" if path.is_file() else "ShowFolders"
            token = os.environ.get("XDG_ACTIVATION_TOKEN", "")
            reply = iface.call(method, [uri], token)
            if reply.type() != QtDBus.QDBusMessage.ErrorMessage:
                if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
                    # Plasma on Wayland keeps an already-open window behind ("focus stealing prevention")
                    # unless asked properly: a tiny KWin script raises the file-manager window for us.
                    folder = path.parent if path.is_file() else path
                    QtCore.QTimer.singleShot(400, lambda: kwin_raise(folder.name))
                return True
    except Exception:  # noqa: BLE001
        pass
    return QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))


class WheelGuard(QtCore.QObject):
    """Scrolling the page must never change a setting: drop-downs, number boxes and sliders only take the
    wheel/trackpad once you've clicked them; otherwise the scroll goes to the page underneath."""
    TYPES = (QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QAbstractSlider)

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Wheel and isinstance(obj, self.TYPES) and not obj.hasFocus() \
                and not isinstance(obj, QtWidgets.QScrollBar):
            p = obj.parentWidget()
            while p is not None and not isinstance(p, QtWidgets.QAbstractScrollArea):
                p = p.parentWidget()
            if p is not None:
                QtWidgets.QApplication.sendEvent(p.verticalScrollBar() if ev.angleDelta().y() else
                                                 p.horizontalScrollBar(), ev)
            return True
        if ev.type() == QtCore.QEvent.Polish and isinstance(obj, self.TYPES) \
                and obj.focusPolicy() == Qt.WheelFocus:
            obj.setFocusPolicy(Qt.StrongFocus)           # a wheel turn mustn't give them focus either
        return False


_WHEEL_GUARD = None


def install_wheel_guard():
    global _WHEEL_GUARD
    app = QtWidgets.QApplication.instance()
    if app is not None and _WHEEL_GUARD is None:
        _WHEEL_GUARD = WheelGuard(app)
        app.installEventFilter(_WHEEL_GUARD)


class FreezeWatch:
    """If the window stops responding for 5 s, write where every thread is stuck to crash.log.
    Errors and hard crashes go there too — so "it stalled" turns into a line number."""

    def __init__(self):
        import faulthandler
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        if CRASH_LOG.exists() and CRASH_LOG.stat().st_size > 400_000:
            CRASH_LOG.replace(CRASH_LOG.with_suffix(".old.log"))
        self.f = open(CRASH_LOG, "a", buffering=1)
        self.f.write(f"\n=== Mbox Control {VERSION} started {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        faulthandler.enable(file=self.f, all_threads=True)
        self.beat = time.monotonic()                     # stands still while the PC sleeps
        self.reported = False
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        threading.Thread(target=self._watch, daemon=True).start()
        old = sys.excepthook

        def hook(t, v, tb):
            import traceback
            self.f.write(f"--- error {datetime.now():%H:%M:%S} ---\n" + "".join(traceback.format_exception(t, v, tb)))
            old(t, v, tb)
        sys.excepthook = hook

    def _tick(self):
        self.beat = time.monotonic()
        self.reported = False

    def _watch(self):
        import faulthandler
        while True:
            time.sleep(1)
            stuck = time.monotonic() - self.beat
            if stuck > 5 and not self.reported:
                self.reported = True
                self.f.write(f"--- window not responding for {stuck:.0f} s at {datetime.now():%H:%M:%S} ---\n")
                faulthandler.dump_traceback(file=self.f, all_threads=True)


def crash_log_tail(lines=60):
    try:
        return "\n".join(CRASH_LOG.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def diagnostics():
    """Plain-text facts for a bug report. Nothing personal: no names, paths in home are shortened."""
    def first(*cmd):
        out = run_quiet(*cmd) or ""
        return out.strip().splitlines()[0] if out.strip() else "not found"
    try:
        osname = re.search(r'^PRETTY_NAME="?([^"\n]+)', Path("/etc/os-release").read_text(), re.M).group(1)
    except (OSError, AttributeError):
        osname = "unknown"
    try:
        from PySide6 import __version__ as pyside
    except ImportError:
        pyside = "missing"
    card = find_mbox_card()
    rows = [("Mbox Control", VERSION), ("System", osname), ("Kernel", os.uname().release),
            ("Python", sys.version.split()[0]), ("PySide6", pyside),
            ("PipeWire", (re.findall(r"libpipewire ([\d.]+)", run_quiet("pipewire", "--version") or "") or
                          ["not found"])[-1]),
            ("Package manager", package_manager() or "unknown"),
            ("Mbox card", f"card {card}" if card is not None else "not found"),
            ("Control service", str(SERVICE_PATH).replace(str(HOME), "~") + (" (found)" if SERVICE_PATH.exists()
                                                                             else " (MISSING)")),
            ("Service running", "yes" if service_running() else "no"),
            ("Meter reading", ("direct (fast)" if card is not None and Backend(card).enable_direct()
                               else "amixer (slow)") if card is not None else "n/a"),
            ("snd-dice module", "loaded" if Path("/sys/module/snd_dice").exists() else "not loaded"),
            ("FireWire audio", "; ".join(f"{u['unit']} {u['driver']} card "
                                         f"{u['card'] if u['card'] is not None else 'MISSING'}"
                                         for u in fw_audio_units()) or "none found"),
            ("Audio server", audio_stack()[0] or "none detected"),
            ("Sleep/wake fix", "installed" if sleep_fix_installed() else
             ("OLD mbox-rebind hook" if WAKE_HOOK.exists() else "not installed")),
            ("udev (pyudev)", "yes" if UdevWatch.available() else "no"),
            ("Firewall", firewall_kind() or "none active"),
            ("ffmpeg", "yes" if shutil.which("ffmpeg") else "no"), ("pw-jack", "yes" if shutil.which("pw-jack") else "no")]
    out = "\n".join(f"{k}: {v}" for k, v in rows)
    for title, path in (("Recovery log", RECOVERY_LOG), ("Wake hook log", WAKE_LOG)):
        try:
            lines = path.read_text(errors="replace").splitlines()[-40:]
        except OSError:
            continue
        if lines:
            out += f"\n\n{title} (last {len(lines)} lines):\n" + "\n".join(lines).replace(str(HOME), "~")
    tail = crash_log_tail()
    if "---" in tail:
        out += "\n\nRecent problems (crash.log):\n" + tail.replace(str(HOME), "~")
    return out


def main():
    if "--version" in sys.argv:
        print(f"Mbox Control {VERSION}")
        return 0
    if "--diagnostics" in sys.argv:
        print(diagnostics())
        return 0
    if "--install" in sys.argv:
        app = QtGui.QGuiApplication(sys.argv[:1] + ["-platform", "offscreen"])
        cli_install()
        return 0
    cmd = None
    if "--cmd" in sys.argv:
        i = sys.argv.index("--cmd")
        cmd = sys.argv[i + 1] if i + 1 < len(sys.argv) else "show"
    app = QtWidgets.QApplication(sys.argv)
    if send_command(cmd or ("noop" if "--tray" in sys.argv else "show")):
        return 0                      # already running: handed over
    if cmd:
        print("Mbox Control isn't running.", file=sys.stderr)
        return 1
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("mbox-control")
    st = load_json(STATE_FILE, {})
    set_theme(st.get("theme", "system"), st.get("accent", "system"))
    ensure_icon_theme()
    apply_theme(app)
    card_no = os.environ.get("MBOX_CARD") or find_mbox_card()
    if card_no is None:
        QtWidgets.QMessageBox.critical(None, APP_NAME, "No Mbox found. Is it switched on and connected over FireWire?")
        return 1
    backend = Backend(card_no)
    if not service_running() and not os.environ.get("MBOX_AMIXER"):
        if ensure_service(card_no):
            QtCore.QThread.msleep(1500)
            deadline = time.time() + 6
            while time.time() < deadline and not service_ready(backend):
                QtCore.QThread.msleep(200)
    if not service_ready(backend):
        QtWidgets.QMessageBox.critical(None, APP_NAME, "The Mbox control service isn't running and couldn't "
                                                       f"be started.\n\nExpected it at:\n{SERVICE_PATH}")
        return 1
    app.setWindowIcon(app_icon())
    install_icon_files()
    refresh_launcher_if_old()
    fixed = fix_default_sink()
    if not os.environ.get("MBOX_AMIXER"):
        main.freeze = FreezeWatch()
    win = MainWindow(backend)
    if fixed:
        win.toast(f"Default output was the raw Mbox — switched it back to {win.main_output_name()}.", 10000)
    if "--tray" in sys.argv and win.tray:
        pass
    else:
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
