# Mbox Pro (3rd gen) on Windows: how Avid's panel talks to the driver

Found with `tools/windows-probe` (Frida inside Avid's `Panel.exe`, driver 1.1.10, Windows 11), 2026-11-08.

## Transport
`DriverInterface.exe` (installed with Avid's driver, runs in the background) owns the Mbox. Clients connect to
two named pipes per device:

- `\\.\pipe\SDeviceID<id>` — requests and their replies
- `\\.\pipe\NDeviceID<id>` — notifications (parameter changes, meters), pushed by the driver

`<id>` seen: `010203047EA0000004000000DECAFBAD00000000000000000000000000000001`.
Avid's `DeviceInterface.dll` (`AADI_*` exports) is only a wrapper around these pipes; we can speak them directly.

## Message format (little-endian)
```
message  = u32 total_len | u32 kind | i32 cmd | element
element  = u32 len | u32 key (0) | i32 type | payload
types    0 null · 1 bool (1 byte) · 2 int32 · 3 int64 · 4 float · 5 double · 9 string (NUL-terminated)
         8 opaque · 10 array (payload = elements)
```
A parameter update is an array of `[value, param_id, null]` triples.

| cmd | dir | meaning |
|----:|-----|---------|
| -3, -4 | → / ← | handshake (kind 2) |
| -5 | → | request all values, body `[[0, 1177]]` (first, last id); reply is a `-6` dump on the N pipe |
| -6 | → | **set** values (kind 0), body `[[value, id, null], …]` |
| -6 | ← | values / notifications |
| -7 | → | poll values (meters), body `[[id, null], …]`; meter values are `[level, timestamp]` |
| -248 | ↔ | panel window size (ignore) |

## Parameters seen (1177 in total)
| id | what | values |
|---:|------|--------|
| 4 | sample rate | 44100.0 / 48000.0 / … |
| 5 | supported rates + lock | `[n, locked, rates…]` |
| 1175 / 1174 | clock source | 0 internal, 1 S/PDIF |
| 46 | high-pass filter In 1 (47–49: In 2–4, to confirm) | bool |
| 57, 67, 77?, 87?, 97, 107 | output source: Out 1/2, 3/4, 5/6?, S/PDIF?, Headphone A, Headphone B | enum |
| 52, 54, 55 | front-panel buttons (speaker select, dim, mono …) | int/bool |
| 68, 69, 78, 79 | speaker select | — |
| 120 | Mix 1 · Hardware In 1 fader+pan as `[L dB, R dB]` | doubles, −200 = −∞ |
| 125/126, 127/128 | Hardware In 1 mute L/R, solo L/R | bool |
| 129 | Hardware In 2 fader (channel stride 9) | |
| 196 / 205 | Software Return 1 / 2 fader | |
| 278 | Master fader | |
| 15 | FX send, Hardware In 1 | dB |
| 1143 | effect type | 0 = Room 1 … |
| 1173 | tuner on/off | bool |
| 121, 130, …, 286 | meters (polled with -7) | `[level 0–1, timestamp]` |

Still to map: the other mixes (one per output), pans vs. fader encoding, phantom (front-panel only?),
FX parameters, and names of the remaining ids — the full start-up dump makes the structure visible.

## Layout of the 1,178 parameters (from the start-up dump, 2026-11-08)
| ids | block |
|----:|-------|
| 0–1 | driver version `5.10.0.6019`, firmware `1.0.4` |
| 2–3 | buffer size, `[min, max, step]` |
| 4–5 | sample rate, supported rates (+ locked flag) |
| 14–29 / 30–45 | FX sends, Hardware Inputs 1–8 / Software Returns 1–8: `name, send dB` pairs |
| 46–49 | high-pass filter In 1–4 |
| 52–55 | front-panel state (speaker select, dim, mono …) |
| 56–115 | six outputs × 10: `name, source, …, flag, "1", trim, meter, "2", trim, meter` — Outputs 1/2, 3/4, 5/6, S/PDIF, Headphone A, Headphone B. Sources seen: 0–2 Stereo Mix 1–3, 4 Mix 5, 7 SW Returns 3/4, 12 HW Inputs 5/6 |
| 116–286 | **Mix 1** (171 ids): 4 header values, 8 Hardware Inputs × 9 (`[L dB, R dB]` gain incl. pan, meter, 3 names, mute L, mute R, solo L, solo R), 4 values, 8 Software Returns × 9, then master (`"L/R"`, mutes, meters, balance/width/fader as id 278 `[LL, LR, RL, RR]` matrix …) |
| 287–457, 458–628, 629–799, 800–970 | Mixes 2–5, same layout (stride 171) |
| 971–1141 | FX mix (the reverb bus), same layout |
| 1143 | effect type; 1144–1167 effect parameters |
| 1168–1172 | effect/tuner flags |
| 1173 | tuner on |
| 1174–1176 | clock source and lock |

`MboxPipeTest.exe` (2026-11-08) read every value and muted Hardware Input 1 audibly — the pipe works without Avid's panel.
