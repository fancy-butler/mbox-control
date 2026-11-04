#!/usr/bin/env python3
"""Fake amixer: serves a real Mbox Pro 3 control dump and applies csets to a JSON state file."""
import json, os, re, sys

DUMP = os.environ["FAKE_DUMP"]
STATE = os.environ["FAKE_STATE"]
LOG = os.environ.get("FAKE_LOG")


def load_blocks():
    blocks, cur = {}, None
    for line in open(DUMP).read().splitlines():
        if line.startswith("numid="):
            cur = int(re.match(r"numid=(\d+)", line).group(1))
            blocks[cur] = [line]
        elif cur is not None:
            blocks[cur].append(line)
    return blocks


def render(blocks, state, numid):
    out = []
    for line in blocks[numid]:
        if line.strip().startswith(": values=") and str(numid) in state:
            out.append("  : values=" + state[str(numid)])
        else:
            out.append(line)
    return "\n".join(out)


args = sys.argv[1:]
if args[:1] == ["-c"]:
    args = args[2:]
blocks = load_blocks()
state = json.load(open(STATE)) if os.path.exists(STATE) else {}
if args[0] == "contents":
    print("\n".join(render(blocks, state, n) for n in blocks))
elif args[0] == "cget":
    n = int(args[1].split("=")[1])
    print(render(blocks, state, n))
elif args[0] == "cset":
    n = int(args[1].split("=")[1])
    state[str(n)] = args[2]
    json.dump(state, open(STATE, "w"))
    if LOG:
        open(LOG, "a").write(f"cset numid={n} {args[2]}\n")
    print(render(blocks, state, n))
else:
    sys.exit(1)
