"""Mbox Pipe Test — proves Mbox Control can drive the Mbox on Windows without Avid's panel.

Talks straight to Avid's background driver service (DriverInterface.exe) over its named pipe, reads a few
settings, then mutes Hardware Input 1 in Mix 1 for three seconds and unmutes it. Avid's panel should be
CLOSED while this runs.
"""
import os
import struct
import sys
import time

NULL, BOOL, INT, DOUBLE, STRING, ARRAY = 0, 1, 2, 5, 9, 10


def el(t, payload=b""):
    return struct.pack("<IIi", 12 + len(payload), 0, t) + payload


def val(v):
    if v is None:
        return el(NULL)
    if isinstance(v, bool):
        return el(BOOL, bytes([v]))
    if isinstance(v, int):
        return el(INT, struct.pack("<i", v))
    if isinstance(v, float):
        return el(DOUBLE, struct.pack("<d", v))
    if isinstance(v, str):
        return el(STRING, v.encode("latin1") + b"\0")
    return el(ARRAY, b"".join(val(x) for x in v))


def message(kind, cmd, body=None):
    e = val(body) if body is not None else el(NULL)
    return struct.pack("<IIi", 12 + len(e), kind, cmd) + e


def parse(b, o=0):
    ln, _k, t = struct.unpack_from("<IIi", b, o)
    p = b[o + 12:o + ln]
    if t == NULL:
        v = None
    elif t == BOOL:
        v = bool(p[0])
    elif t == INT:
        v = struct.unpack("<i", p[:4])[0]
    elif t == DOUBLE:
        v = struct.unpack("<d", p[:8])[0]
    elif t == STRING:
        v = p.split(b"\0")[0].decode("latin1")
    elif t == ARRAY:
        v, q = [], o + 12
        while q < o + ln:
            c, q = parse(b, q)
            v.append(c)
    else:
        v = ("type%d" % t, p.hex())
    return v, o + ln


class Pipe:
    def __init__(self, path):
        for _ in range(50):
            try:
                self.f = open(path, "r+b", buffering=0)
                return
            except OSError:
                time.sleep(0.1)                         # busy: the service hands it over in a moment
        raise OSError(f"couldn't open {path}")

    def send(self, data):
        self.f.write(data)

    def recv(self):
        head = self.f.read(12)
        ln = struct.unpack_from("<I", head)[0]
        rest = b""
        while len(rest) < ln - 12:
            rest += self.f.read(ln - 12 - len(rest))
        kind, cmd = struct.unpack_from("<Ii", head, 4)
        body, _ = parse(rest) if rest else (None, 0)
        return kind, cmd, body


def main():
    print(__doc__)
    root = "\\\\.\\pipe\\"
    pipes = [root + n for n in os.listdir(root) if n.startswith("SDeviceID")]
    if not pipes:
        print("No Mbox found: is the Mbox on and Avid's driver installed?")
        return 1
    s_path = pipes[0]
    n_path = s_path.replace("\\SDeviceID", "\\NDeviceID")
    print("Device pipe:", s_path)
    s = Pipe(s_path)
    s.send(message(2, -3))
    print("handshake 1:", s.recv())
    s.send(message(2, -4))
    print("handshake 2:", s.recv())
    n = Pipe(n_path)
    s.send(message(1, -5, [[0, 1177]]))                 # ask for every value
    values = {}
    deadline = time.time() + 5
    while time.time() < deadline and len(values) < 1000:
        _kind, cmd, body = n.recv()
        if cmd == -6 and isinstance(body, list):
            for e in body:
                if isinstance(e, list) and len(e) >= 2 and isinstance(e[1], int):
                    values[e[1]] = e[0]
    print(f"Read {len(values)} settings. Sample rate: {values.get(4)}  Clock: {values.get(1175)}  "
          f"Out 1/2 source: {values.get(57)}  Input 1 mute: {values.get(125)}")
    print("\nMuting Hardware Input 1 (Mix 1) for 3 seconds — watch Avid's panel or listen to Input 1…")
    s.send(message(0, -6, [[True, 125, None], [True, 126, None]]))
    time.sleep(3)
    s.send(message(0, -6, [[False, 125, None], [False, 126, None]]))
    print("…and unmuted. If the mute happened, Mbox Control can drive the Mbox on Windows.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"\nStopped: {e}")
        code = 1
    input("\nPress Enter to close.")
    sys.exit(code)
