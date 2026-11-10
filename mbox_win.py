# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Fancy Butler
"""
Mbox Control on Windows: talks to Avid's own driver service (DriverInterface.exe) over its named pipes and
presents the Mbox to the rest of the app as the same set of controls the Linux driver offers, so every page,
scene and feature works unchanged. See docs/windows-protocol.md for how the pipe protocol was worked out.

Layers:
  * encode()/decode()      — the pipe's binary format
  * WinPipe                — one message-mode named-pipe connection (Win32 via ctypes)
  * DriverLink             — handshake, the full parameter store, notifications, meter polling
  * AlsaView               — the parameter store seen as Linux-style controls (Control objects)
  * WinBackend             — what mbox_control.Backend is on Linux: read_all / read / write
"""
import math
import struct
import threading
import time

# ------------------------------------------------------------------------------------------------ protocol
NULL, BOOL, INT, INT64, FLOAT, DOUBLE, STRING, ARRAY = 0, 1, 2, 3, 4, 5, 9, 10
CMD_HELLO, CMD_READY, CMD_DUMP, CMD_VALUES, CMD_POLL = -3, -4, -5, -6, -7
KIND_SET, KIND_QUERY, KIND_HANDSHAKE = 0, 1, 2
LAST_PARAM = 1177


def _el(t, payload=b""):
    return struct.pack("<IIi", 12 + len(payload), 0, t) + payload


def encode_value(v):
    if v is None:
        return _el(NULL)
    if isinstance(v, bool):
        return _el(BOOL, bytes([v]))
    if isinstance(v, int):
        return _el(INT, struct.pack("<i", v))
    if isinstance(v, float):
        return _el(DOUBLE, struct.pack("<d", v))
    if isinstance(v, str):
        return _el(STRING, v.encode("latin1") + b"\0")
    return _el(ARRAY, b"".join(encode_value(x) for x in v))


def encode(kind, cmd, body=None):
    e = encode_value(body) if body is not None else _el(NULL)
    return struct.pack("<IIi", 12 + len(e), kind, cmd) + e


def decode_value(b, o=0):
    ln, _key, t = struct.unpack_from("<IIi", b, o)
    p = b[o + 12:o + ln]
    if t == NULL:
        v = None
    elif t == BOOL:
        v = bool(p[0]) if p else False
    elif t == INT:
        v = struct.unpack("<i", p[:4])[0]
    elif t == INT64:
        v = struct.unpack("<q", p[:8])[0]
    elif t == FLOAT:
        v = struct.unpack("<f", p[:4])[0]
    elif t == DOUBLE:
        v = struct.unpack("<d", p[:8])[0]
    elif t == STRING:
        v = p.split(b"\0")[0].decode("latin1")
    elif t == ARRAY:
        v, q = [], o + 12
        while q < o + ln:
            c, q = decode_value(b, q)
            v.append(c)
    else:
        v = ("opaque", t, p.hex())
    return v, o + ln


def decode(msg):
    """→ (kind, cmd, body)"""
    ln, kind, cmd = struct.unpack_from("<IIi", msg, 0)
    body = decode_value(msg, 12)[0] if ln > 12 else None
    return kind, cmd, body


def updates(body):
    """[[value, id, extra], …] → {id: value} (ids ≥ 0 only; negative ids are replies to commands)."""
    out = {}
    for e in body or []:
        if isinstance(e, list) and len(e) >= 2 and isinstance(e[1], int) and e[1] >= 0:
            out[e[1]] = e[0]
    return out


# ------------------------------------------------------------------------------------------------ transport
class WinPipe:
    """A message-mode named-pipe client, done the way Avid's DeviceInterface.dll does it."""

    def __init__(self, path, wait=5.0):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.k32 = k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        for fn in ("ReadFile", "WriteFile"):
            getattr(k32, fn).argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                                ctypes.c_void_p, ctypes.c_void_p]
        k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        invalid = wintypes.HANDLE(-1).value
        deadline = time.time() + wait
        while True:
            h = k32.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
            if h and h != invalid:
                break
            if time.time() > deadline:
                raise OSError(f"couldn't open {path} (error {ctypes.get_last_error()})")
            k32.WaitNamedPipeW(path, 200)               # returns at once if the pipe doesn't exist yet
            time.sleep(0.05)
        self.h, self.buf, self.lock = h, b"", threading.Lock()
        mode = wintypes.DWORD(2)                         # PIPE_READMODE_MESSAGE
        k32.SetNamedPipeHandleState(h, ctypes.byref(mode), None, None)

    def send(self, data):
        n = self.wt.DWORD()
        with self.lock:
            if not self.k32.WriteFile(self.h, data, len(data), self.ct.byref(n), None):
                raise OSError(f"pipe write failed (error {self.ct.get_last_error()})")

    def available(self):
        n = self.wt.DWORD()
        if not self.k32.PeekNamedPipe(self.h, None, 0, None, self.ct.byref(n), None):
            raise OSError(f"pipe closed (error {self.ct.get_last_error()})")
        return n.value + len(self.buf)

    def _read_some(self):
        out = b""
        while True:
            buf = self.ct.create_string_buffer(65536)
            n = self.wt.DWORD()
            ok = self.k32.ReadFile(self.h, buf, 65536, self.ct.byref(n), None)
            out += buf.raw[:n.value]
            if ok:
                return out
            err = self.ct.get_last_error()
            if err != 234:                               # ERROR_MORE_DATA: the rest of a long message follows
                raise OSError(f"pipe read failed (error {err})")

    def recv(self):
        """One whole message (blocks)."""
        while len(self.buf) < 4 or len(self.buf) < struct.unpack_from("<I", self.buf)[0]:
            self.buf += self._read_some()
        ln = struct.unpack_from("<I", self.buf)[0]
        m, self.buf = self.buf[:ln], self.buf[ln:]
        return m

    def close(self):
        try:
            self.k32.CloseHandle(self.h)
        except Exception:  # noqa: BLE001
            pass


def find_device_pipes():
    """[(request pipe path, notification pipe path)] for every Mbox the driver knows."""
    import os
    root = "\\\\.\\pipe\\"
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return [(root + n, root + "N" + n[1:]) for n in sorted(names) if n.startswith("SDeviceID")]


# ------------------------------------------------------------------------------------------------ link
class DriverLink:
    """A connection to the driver: holds every parameter, follows notifications, polls meters.

    `request_pipe` / `note_pipe` are anything with send/recv/available/close (WinPipe, or a fake in tests).
    `on_change(ids)` is called from the reader thread with the set of parameter ids that changed."""

    def __init__(self, request_pipe, note_pipe, on_change=None):
        self.req, self.note = request_pipe, note_pipe
        self.params, self.lock = {}, threading.Lock()
        self.on_change = on_change
        self.meter_ids, self.running, self.thread = [], False, None
        self.alive = True

    def connect(self, timeout=5.0):
        self.req.send(encode(KIND_HANDSHAKE, CMD_HELLO))
        decode(self.req.recv())
        self.req.send(encode(KIND_HANDSHAKE, CMD_READY))
        decode(self.req.recv())
        self.req.send(encode(KIND_QUERY, CMD_DUMP, [[0, LAST_PARAM]]))
        deadline, quiet_since = time.time() + timeout, None
        while time.time() < deadline and len(self.params) <= LAST_PARAM:
            if not self.note.available():
                if len(self.params) > 100:               # the dump comes in a few large messages, then stops
                    quiet_since = quiet_since or time.time()
                    if time.time() - quiet_since > 0.3:
                        break
                time.sleep(0.02)
                continue
            quiet_since = None
            self._take(self.note.recv())
        if len(self.params) < 100:
            raise OSError("the Mbox driver didn't send its settings")
        return self

    def _take(self, msg):
        _kind, cmd, body = decode(msg)
        if cmd != CMD_VALUES:
            return set()
        up = updates(body)
        changed = set()
        with self.lock:
            for i, v in up.items():
                if i not in self.params or self.params[i] != v:
                    self.params[i] = v
                    changed.add(i)
        return changed

    def start(self, meter_interval=0.1):
        """Follow notifications (and poll meters) in the background.

        The driver writes every change to our notification pipe and waits if nobody reads it, so this loop
        must never stop draining while we're connected: a stalled reader stalls Avid's driver service, and
        with it all sound on the PC. Anything unexpected is logged and skipped; on a broken pipe both pipes
        are closed at once so the driver can let go of us."""
        self.running = True
        self.meter_interval = meter_interval

        def loop():
            last_poll = 0.0
            self.last_rx = time.time()
            while self.running:
                try:
                    if self.meter_ids and self.meters_on and time.time() - self.last_rx > self.stale_after:
                        # we keep asking for meters and nothing comes back: after sleep the driver can leave
                        # our old pipe open but dead. Treat it as lost so the app reconnects.
                        raise OSError(f"no reply from the driver for {self.stale_after:.0f} s")
                    busy = False
                    if self.meter_ids and self.meters_on and time.time() - last_poll >= self.meter_interval:
                        last_poll = time.time()
                        self.req.send(encode(KIND_QUERY, CMD_POLL, [[i, None] for i in self.meter_ids]))
                    while self.req.available():          # replies on the request pipe: drain them
                        self._dispatch(self.req.recv())
                        busy = True
                    while self.note.available():
                        self._dispatch(self.note.recv())
                        busy = True
                    if not busy:
                        time.sleep(0.005)
                except OSError as e:
                    self._log(f"driver link lost: {e}")
                    self.alive = False
                    self.running = False
                    for p in (self.req, self.note):
                        try:
                            p.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self._notify(None)
                    return
                except Exception as e:  # noqa: BLE001 — never stop reading
                    self._log(f"driver link: skipped a message ({type(e).__name__}: {e})")
                    time.sleep(0.01)
        self.thread = threading.Thread(target=loop, name="mbox-driver-link", daemon=True)
        self.thread.start()
        return self

    meters_on = True
    log = None
    stale_after = 4.0
    last_rx = 0.0

    def _log(self, msg):
        if self.log:
            try:
                self.log(msg)
            except Exception:  # noqa: BLE001
                pass

    def _notify(self, ids):
        cb = self.on_change
        if cb:
            try:
                cb(ids)
            except Exception as e:  # noqa: BLE001 — a UI problem must not stop the reader
                self._log(f"change handler failed: {type(e).__name__}: {e}")

    def _dispatch(self, msg):
        self.last_rx = time.time()
        changed = self._take(msg)
        if changed:
            self._notify(changed)

    def get(self, i, default=None):
        with self.lock:
            return self.params.get(i, default)

    def set(self, values):
        """{id: value} → one message to the driver. The local copy follows at once."""
        if not values:
            return
        body = [[v, i, None] for i, v in values.items()]
        self.req.send(encode(KIND_SET, CMD_VALUES, body))
        with self.lock:
            self.params.update(values)

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(1.0)
        for p in (self.req, self.note):
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------------------------------------ layout
MIX_BASE, MIX_STRIDE, MIXES = 116, 171, 6      # six stereo mixes, identical blocks
HW_OFF, SW_OFF, CH_STRIDE = 4, 80, 9           # channel strips inside a mix block
MASTER_FX_RET = (163, 164)                      # FX return level L/R inside a mix block (dB)
FX_RET_METERS = (169, 170)
OUT_BASE, OUT_STRIDE = 56, 10                   # outputs: name, source, speaker, master-vol, …
OUT_ORDER = ["Outputs 1/2", "Outputs 3/4", "Outputs 5/6", "S/PDIF Out L/R", "Headphone A", "Headphone B"]
FX_SEND_HW, FX_SEND_SW = 15, 31                 # name/send-dB pairs, 2 ids per source
P_RATE, P_RATES, P_CLOCK = 4, 5, 1175
P_HPF = 46
P_SPKR, P_DIM, P_MONO = 52, 54, 55
P_FX_TYPE = 1143
SILENT_DB = -150.0                              # at or below: −∞ (the driver uses −200 and lower)
UNITY = 16384                                   # Linux driver's 0 dB


def mix_base(m):
    return MIX_BASE + MIX_STRIDE * m


def strip(m, src):
    """Gain param of mixer source `src` (0–7 hardware inputs, 8–15 software returns) in mix m."""
    off = HW_OFF + CH_STRIDE * src if src < 8 else SW_OFF + CH_STRIDE * (src - 8)
    return mix_base(m) + off


def out_param(pair, field):
    """field: 1 source, 2 speaker assignment, 3 master-volume flag, 5/8 trim L/R, 6/9 meter L/R."""
    return OUT_BASE + OUT_STRIDE * pair + field


def db_to_lin(db):
    if db is None or db <= SILENT_DB:
        return 0
    return max(0, min(65535, round(UNITY * 10 ** (db / 20))))


def lin_to_db(v):
    return -200.0 if v <= 0 else 20 * math.log10(v / UNITY)


def trim255(db):
    """Output trim: driver dB (0 = full) → Linux driver's 0–255 (linear, 255 = full)."""
    if db is None or db <= SILENT_DB:
        return 0
    return max(0, min(255, round(255 * 10 ** (db / 20))))


def trim_db(v):
    return -200.0 if v <= 0 else 20 * math.log10(v / 255)


def meter12(v):
    """Driver meter [level 0–1, timestamp] → Linux driver's 0–4095."""
    lvl = v[0] if isinstance(v, list) and v else 0.0
    return max(0, min(4095, round((lvl or 0.0) * 4095)))


# ------------------------------------------------------------------------------------------------ view
# Linux names for sources and the Windows output-pair choices that match them.
SRC_ITEMS = ["None", "Analog-A-1", "Analog-A-2", "Analog-A-3", "Analog-A-4", "Analog-A-5", "Analog-A-6",
             "Reverb-1", "Reverb-2", "S/PDIF-1", "S/PDIF-2"] + [f"Stream-{i}" for i in range(1, 9)] + \
            [f"Mixer-{i}" for i in range(1, 17)]
HW_ITEMS = ["Analog-A-1", "Analog-A-2", "Analog-A-3", "Analog-A-4", "Analog-A-5", "Analog-A-6",
            "S/PDIF-1", "S/PDIF-2"]


def pair_items(choice):
    """Windows output-pair source (0–5 mixes, 6–9 software returns, 10–13 hardware inputs) → two items."""
    if 0 <= choice <= 5:
        return [f"Mixer-{2 * choice + 1}", f"Mixer-{2 * choice + 2}"]
    if 6 <= choice <= 9:
        k = 2 * (choice - 6) + 1
        return [f"Stream-{k}", f"Stream-{k + 1}"]
    if 10 <= choice <= 13:
        k = 2 * (choice - 10)
        return HW_ITEMS[k:k + 2]
    return ["None", "None"]


def items_choice(a, b):
    for c in range(14):
        if pair_items(c) == [a, b]:
            return c
    return None


# output-source slots on Linux → Windows output pair (None = fixed/unused)
SLOT_PAIRS = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2, 6: 4, 7: 4, 8: 5, 9: 5, 12: 3, 13: 3}
RATES = [44100, 48000, 88200, 96000, 176400, 192000]


class Ctl:
    """Same shape as mbox_control.Control."""

    def __init__(self, numid, name, index, ctype, count, items=None, lo=0, hi=0, access="rw------"):
        self.numid, self.name, self.index = numid, name, index
        self.type, self.items, self.min, self.max, self.access = ctype, list(items or []), lo, hi, access
        self.values = [0] * count

    @property
    def key(self):
        return f"{self.name}#{self.index}"


class AlsaView:
    """The driver's parameters as the Linux controls the app knows. Only what both sides really have."""

    def __init__(self, link):
        self.link = link
        self.ctrls = {}
        n = 0

        def add(name, ctype, count, items=None, lo=0, hi=0, index=0, access="rw------"):
            nonlocal n
            n += 1
            self.ctrls[n] = Ctl(n, name, index, ctype, count, items, lo, hi, access)
            return n
        self.id_rate = add("clock-rate", "ENUMERATED", 1, [str(r) for r in RATES])
        self.id_clock = add("clock-source", "ENUMERATED", 1, ["SPDIF", "Word Clock", "Internal"])
        self.id_gain = [add("mixer-source-gain", "INTEGER", 18, lo=0, hi=65535, index=b) for b in range(16)]
        self.id_out = add("output-source", "ENUMERATED", 16, SRC_ITEMS)
        self.id_stream = add("stream-source", "ENUMERATED", 8, SRC_ITEMS[:11] + SRC_ITEMS[19:], access="r-------")
        self.id_msrc = add("mixer-source", "ENUMERATED", 18, SRC_ITEMS[:19], access="r-------")
        self.id_out_m = add("output-source-meter", "INTEGER", 16, hi=4095, access="r-------")
        self.id_str_m = add("stream-source-meter", "INTEGER", 8, hi=4095, access="r-------")
        self.id_mix_m = add("mixer-source-meter", "INTEGER", 18, hi=4095, access="r-------")
        self.id_assign = add("master-knob-assign", "BOOLEAN", 6)
        self.id_dim = add("mute-button", "ENUMERATED", 1, ["Off", "Blink", "On"])
        self.id_mono = add("mono-button", "ENUMERATED", 1, ["Off", "On"])
        self.id_spkr = add("spkr-button", "ENUMERATED", 1,
                           ["Off", "Green", "Green-Blink", "Red", "Red-Blink", "Orange", "Orange-Blink"])
        self.id_hpf = add("input-hp-filter", "BOOLEAN", 4)
        self.id_trim = add("output-trim", "INTEGER", 6, lo=0, hi=255)
        self.id_rtype = add("reverb-type", "ENUMERATED", 1,
                            ["Room-1", "Room-2", "Room-3", "Hall-1", "Hall-2", "Plate", "Echo", "Delay"])
        self.refresh()

    # -- which controls hold meters, for the poller
    def meter_ids(self):
        return [self.id_out_m, self.id_str_m, self.id_mix_m]

    def meter_params(self):
        ids = [out_param(p, f) for p in range(6) for f in (6, 9)]
        ids += [strip(0, s) + 1 for s in range(16)]
        ids += [mix_base(0) + FX_RET_METERS[0], mix_base(0) + FX_RET_METERS[1]]
        return ids

    # -- params → controls
    def refresh(self, numids=None):
        g = self.link.get
        c = self.ctrls
        want = set(numids) if numids else set(c)
        if self.id_rate in want:
            r = g(P_RATE, 48000.0)
            c[self.id_rate].values = [RATES.index(int(r)) if int(r or 0) in RATES else 1]
        if self.id_clock in want:
            c[self.id_clock].values = [0 if g(P_CLOCK, 0) == 1 else 2]
        for b, nid in enumerate(self.id_gain):
            if nid in want:
                c[nid].values = self._bus_gains(b)
        if self.id_out in want:
            vals = [0] * 16
            for slot, pair in SLOT_PAIRS.items():
                items = pair_items(g(out_param(pair, 1), -1))
                vals[slot] = SRC_ITEMS.index(items[slot % 2])
            vals[10], vals[11] = SRC_ITEMS.index("Mixer-13"), SRC_ITEMS.index("Mixer-14")   # effect input
            c[self.id_out].values = vals
        if self.id_stream in want:
            items = c[self.id_stream].items
            c[self.id_stream].values = [items.index(x) for x in HW_ITEMS]
        if self.id_msrc in want:
            items = c[self.id_msrc].items
            c[self.id_msrc].values = [items.index(x) for x in HW_ITEMS] + \
                [items.index(f"Stream-{i}") for i in range(1, 9)] + [items.index("Reverb-1"), items.index("Reverb-2")]
        if self.id_out_m in want:
            vals = [0] * 16
            for slot, pair in SLOT_PAIRS.items():
                vals[slot] = meter12(g(out_param(pair, 6 if slot % 2 == 0 else 9)))
            c[self.id_out_m].values = vals
        if self.id_str_m in want:
            c[self.id_str_m].values = [meter12(g(strip(0, s) + 1)) for s in range(8)]
        if self.id_mix_m in want:
            c[self.id_mix_m].values = [meter12(g(strip(0, s) + 1)) for s in range(16)] + \
                [meter12(g(mix_base(0) + FX_RET_METERS[k])) for k in (0, 1)]
        if self.id_assign in want:
            c[self.id_assign].values = [bool(g(out_param(p // 2, 3), False)) for p in range(6)]
        if self.id_dim in want:
            c[self.id_dim].values = [2 if g(P_DIM) else 0]
        if self.id_mono in want:
            c[self.id_mono].values = [1 if g(P_MONO) else 0]
        if self.id_spkr in want:
            c[self.id_spkr].values = [{1: 1, 2: 3, 3: 5}.get(g(P_SPKR, 0), 0)]
        if self.id_hpf in want:
            c[self.id_hpf].values = [bool(g(P_HPF + k, False)) for k in range(4)]
        if self.id_trim in want:
            c[self.id_trim].values = [trim255(g(out_param(o // 2, 5 if o % 2 == 0 else 8))) for o in range(6)]
        if self.id_rtype in want:
            c[self.id_rtype].values = [max(0, min(7, int(g(P_FX_TYPE, 0) or 0)))]

    def _bus_gains(self, b):
        g = self.link.get
        if b >= 14:
            return [0] * 18
        if b >= 12:                                      # FX send bus: one send level per source
            sends = [g(FX_SEND_HW + 2 * s) for s in range(8)] + [g(FX_SEND_SW + 2 * s) for s in range(8)]
            return [db_to_lin(v) for v in sends] + [0, 0]
        m, side = b // 2, b % 2
        out = []
        for s in range(16):
            p = strip(m, s)
            pair = g(p) or [-200.0, -200.0]
            muted = g(p + 5 + side, False)
            out.append(0 if muted else db_to_lin(pair[side] if len(pair) > side else -200.0))
        base = mix_base(m)
        out.append(db_to_lin(g(base + MASTER_FX_RET[0]) if side == 0 else -200.0))
        out.append(db_to_lin(g(base + MASTER_FX_RET[1]) if side == 1 else -200.0))
        return out

    def param_owners(self, ids):
        """Which controls a set of changed parameter ids affects."""
        if ids is None:
            return set(self.ctrls)
        hit = set()
        for i in ids:
            if i == P_RATE:
                hit.add(self.id_rate)
            elif i == P_CLOCK:
                hit.add(self.id_clock)
            elif P_HPF <= i < P_HPF + 4:
                hit.add(self.id_hpf)
            elif i == P_SPKR:
                hit.add(self.id_spkr)
            elif i == P_DIM:
                hit.add(self.id_dim)
            elif i == P_MONO:
                hit.add(self.id_mono)
            elif i == P_FX_TYPE:
                hit.add(self.id_rtype)
            elif OUT_BASE <= i < OUT_BASE + 6 * OUT_STRIDE:
                f = (i - OUT_BASE) % OUT_STRIDE
                hit.add({1: self.id_out, 3: self.id_assign, 5: self.id_trim, 8: self.id_trim,
                         6: self.id_out_m, 9: self.id_out_m}.get(f, self.id_out))
            elif FX_SEND_HW <= i < FX_SEND_SW + 16:
                hit.update(self.id_gain[12:14])
            elif mix_base(0) <= i < mix_base(MIXES):
                m = (i - MIX_BASE) // MIX_STRIDE
                hit.update(self.id_gain[2 * m:2 * m + 2])
                if m == 0:
                    hit.update((self.id_mix_m, self.id_str_m))
        return hit

    # -- controls → params
    def write(self, ctrl, values):
        """→ {param id: value} to send. Raises ValueError for what the Windows driver can't do."""
        out = {}
        g = self.link.get
        if ctrl.numid == self.id_rate:
            out[P_RATE] = float(RATES[values[0]])
        elif ctrl.numid == self.id_clock:
            if values[0] == 1:
                raise ValueError("The Windows driver has no word clock")
            out[P_CLOCK] = 1 if values[0] == 0 else 0
        elif ctrl.numid in self.id_gain:
            b = self.id_gain.index(ctrl.numid)
            if b >= 14:
                return {}
            if b >= 12:
                for s, v in enumerate(values[:16]):
                    p = (FX_SEND_HW + 2 * s) if s < 8 else (FX_SEND_SW + 2 * (s - 8))
                    db = lin_to_db(v)
                    if db_to_lin(g(p)) != v:
                        out[p] = db
            else:
                m, side = b // 2, b % 2
                for s, v in enumerate(values[:16]):
                    p = strip(m, s)
                    pair = list(g(p) or [-200.0, -200.0])
                    cur = 0 if g(p + 5 + side, False) else db_to_lin(pair[side])
                    if cur == v:
                        continue
                    pair[side] = lin_to_db(v)
                    out[p] = pair
                    if v > 0 and g(p + 5 + side, False):
                        out[p + 5 + side] = False            # the app has no mute: a level means "on"
                base = mix_base(m)
                for k in (0, 1):
                    if k == side:
                        p = base + MASTER_FX_RET[k]
                        v = values[16 + k] if len(values) > 16 + k else 0
                        if db_to_lin(g(p)) != v:
                            out[p] = lin_to_db(v)
        elif ctrl.numid == self.id_out:
            for slot in (0, 2, 4, 6, 8, 12):
                pair = SLOT_PAIRS[slot]
                a, b2 = ctrl.items[values[slot]], ctrl.items[values[slot + 1]]
                choice = items_choice(a, b2)
                if choice is None:
                    raise ValueError(f"The Windows driver can only send whole pairs to {OUT_ORDER[pair]}; "
                                     f"{a} + {b2} isn't one")
                if g(out_param(pair, 1)) != choice:
                    out[out_param(pair, 1)] = choice
        elif ctrl.numid == self.id_assign:
            for pair in range(3):
                v = bool(values[2 * pair] or values[2 * pair + 1])
                if bool(g(out_param(pair, 3))) != v:
                    out[out_param(pair, 3)] = v
        elif ctrl.numid == self.id_dim:
            out[P_DIM] = values[0] != 0
        elif ctrl.numid == self.id_mono:
            out[P_MONO] = values[0] != 0
        elif ctrl.numid == self.id_spkr:
            out[P_SPKR] = {0: 0, 1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3}[values[0]]
        elif ctrl.numid == self.id_hpf:
            for k in range(4):
                if bool(g(P_HPF + k)) != bool(values[k]):
                    out[P_HPF + k] = bool(values[k])
        elif ctrl.numid == self.id_rtype:
            out[P_FX_TYPE] = int(values[0])
        elif ctrl.numid == self.id_trim:
            for o in range(6):
                p = out_param(o // 2, 5 if o % 2 == 0 else 8)
                if trim255(g(p)) != int(values[o]):
                    out[p] = trim_db(int(values[o]))
        else:
            raise ValueError(f"{ctrl.name} can't be changed on Windows")
        return out


# ------------------------------------------------------------------------------------------------ backend
class WinBackend:
    """Drop-in for mbox_control.Backend on Windows."""

    def __init__(self, link):
        self.link = link
        self.view = AlsaView(link)
        self.card, self.direct, self.direct_tried, self.shapes = "win", None, True, {}
        link.meter_ids = self.view.meter_params()

    @classmethod
    def open(cls, on_change=None):
        pipes = find_device_pipes()
        if not pipes:
            raise OSError("No Mbox found. Is it switched on, and is Avid's Mbox Pro driver installed?")
        req = WinPipe(pipes[0][0])
        note = WinPipe(pipes[0][1])
        link = DriverLink(req, note, on_change).connect()
        return cls(link)

    def enable_direct(self):
        return False

    def read_all(self):
        self.view.refresh()
        return {n: self._copy(c) for n, c in self.view.ctrls.items()}

    def read(self, numid):
        if numid not in self.view.ctrls:
            return None
        self.view.refresh([numid])
        return self._copy(self.view.ctrls[numid])

    @staticmethod
    def _copy(c):
        d = Ctl(c.numid, c.name, c.index, c.type, 0, c.items, c.min, c.max, c.access)
        d.values = list(c.values)
        return d

    def write(self, ctrl, values):
        mine = self.view.ctrls.get(ctrl.numid)
        if mine is None:
            raise RuntimeError(f"{ctrl.name} isn't available on Windows")
        try:
            sets = self.view.write(mine, list(values))
        except ValueError as e:
            raise RuntimeError(str(e)) from None
        self.link.set(sets)
        self.view.refresh([ctrl.numid])
        ctrl.values = list(values)

    def changed_controls(self, param_ids):
        return self.view.param_owners(param_ids)


# ------------------------------------------------------------------------------------------------ simulation
def _fixture_dump():
    import json
    from pathlib import Path
    for p in (Path(__file__).parent / "tests/fixtures/win-driver-dump.json",
              Path(getattr(__import__("sys"), "_MEIPASS", ".")) / "win-driver-dump.json"):
        if p.exists():
            return {int(k): v for k, v in json.loads(p.read_text()).items()}
    return {}


class FakeDriver:
    """DriverInterface.exe in miniature: two pipes, a parameter store, the same replies."""

    def __init__(self):
        self.params = _fixture_dump()
        self.req_out, self.note_out = __import__('queue').Queue(), __import__('queue').Queue()
        self.sets = []

        driver = self

        class End:
            def __init__(self, outq):
                self.q, self.pending = outq, []

            def available(self):
                return self.q.qsize()

            def recv(self):
                return self.q.get(timeout=2)

            def close(self):
                pass

        class Req(End):
            def send(self, data):
                driver.handle(data)

        self.req, self.note = Req(self.req_out), End(self.note_out)

    def handle(self, data):
        kind, cmd, body = decode(data)
        if cmd in (CMD_HELLO, CMD_READY):
            self.req_out.put(encode(0, CMD_VALUES, [[True, cmd, None]]))
        elif cmd == CMD_DUMP:
            items = [[v, i, None] for i, v in sorted(self.params.items(), reverse=True)]
            for k in range(0, len(items), 300):
                self.note_out.put(encode(0, CMD_VALUES, items[k:k + 300]))
        elif cmd == CMD_VALUES and kind == KIND_SET:
            up = updates(body)
            self.sets.append(up)
            self.params.update(up)
            self.note_out.put(encode(0, CMD_VALUES, [[v, i, None] for i, v in up.items()]))
        elif cmd == CMD_POLL:
            self.note_out.put(encode(0, CMD_VALUES, [[self.params.get(e[0]), e[0], None] for e in body]))

    def press(self, i, v):
        """Something changed on the box itself (a front-panel button)."""
        self.params[i] = v
        self.note_out.put(encode(0, CMD_VALUES, [[v, i, None]]))


