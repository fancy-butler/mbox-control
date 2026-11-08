import json, struct, sys
def el(b, o):
    ln, k, t = struct.unpack_from("<IIi", b, o)
    p = b[o+12:o+ln]
    if t == 0: v = None
    elif t == 1: v = bool(p[0]) if p else None
    elif t == 2: v = struct.unpack("<i", p[:4])[0]
    elif t == 3: v = struct.unpack("<q", p[:8])[0]
    elif t == 4: v = struct.unpack("<f", p[:4])[0]
    elif t == 5: v = struct.unpack("<d", p[:8])[0]
    elif t == 9: v = p.split(b"\0")[0].decode("latin1")
    elif t == 0xa:
        v = []; q = o+12
        while q < o+ln:
            c, q = el(b, q); v.append(c)
    else: v = ("t%d" % t, p.hex())
    return (v if k == 0 else {"k": k, "v": v}), o+ln
def msg(b):
    ln, kind, cmd = struct.unpack_from("<IIi", b, 0)
    body, _ = el(b, 12) if ln > 12 else (None, 0)
    return {"kind": kind, "cmd": cmd, "body": body}
def stream(path):
    """yield (t, dir, handle, decoded msg) with pipe-in chunks reassembled."""
    buf = {}
    for line in open(path):
        r = json.loads(line)
        if r["kind"] in ("step", "step-done"):
            yield (r["t"], r["kind"], r["n"], r.get("text") or r.get("answer")); continue
        if r["kind"] not in ("pipe-out", "pipe-in"): continue
        d = r["data"]; key = (r["kind"], d["handle"])
        b = buf.get(key, b"") + bytes.fromhex(d["bytes"] or "")
        while len(b) >= 4:
            ln = struct.unpack_from("<I", b)[0]
            if len(b) < ln: break
            m, b = b[:ln], b[ln:]
            try: yield (r["t"], r["kind"], d["handle"], msg(m))
            except Exception as e: yield (r["t"], r["kind"], d["handle"], {"err": str(e), "hex": m[:64].hex()})
        buf[key] = b
if __name__ == "__main__":
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    for i, x in enumerate(stream(sys.argv[1])):
        if i >= n: break
        s = json.dumps(x[3]); print(x[1], x[2], s[:300])
