// Mbox Probe: records how Avid's Mbox Pro control panel talks to its driver, so Mbox Control can do the same.
// Runs inside Panel.exe. Logs every DeviceInterface.dll call (AADI_*) with its arguments, and the bytes
// sent over the panel's named pipe to DriverInterface.exe. Reads only — changes nothing.
'use strict';

const NAMES = ['AADI_ConnectDevice', 'AADI_DisconnectDevice', 'AADI_FindDevices', 'AADI_FindRemoteDevices',
  'AADI_GetDeviceInfo', 'AADI_GetParamRanges', 'AADI_GetParamValue', 'AADI_GetParamValues',
  'AADI_IsDeviceConnected', 'AADI_ReleaseAll', 'AADI_RequestParamRanges', 'AADI_RequestParamValues',
  'AADI_SetParamValue', 'AADI_SetParamValues'];
const NOISY = new Set(['AADI_GetParamValue', 'AADI_GetParamValues', 'AADI_IsDeviceConnected',
  'AADI_RequestParamValues', 'AADI_GetParamRanges']);
const PS = Process.pointerSize;
const seen = new Map();          // noisy calls: only log when what they return changes
let pipeHandles = new Set();

function send_(kind, data) { send({ kind: kind, t: Date.now(), data: data }); }

function readable(p) {
  try { if (p.isNull()) return false; p.readU8(); return true; } catch (e) { return false; }
}

function dump(p, n) {
  if (!readable(p)) return null;
  try { return Array.from(new Uint8Array(p.readByteArray(n))).map(b => ('0' + b.toString(16)).slice(-2)).join(''); }
  catch (e) { return null; }
}

// A pointer, what it points to, and one level of pointers inside that (C++ objects: vtable, data ptrs).
function deep(p, n) {
  const out = { ptr: p.toString(), bytes: dump(p, n) };
  if (out.bytes === null) return out;
  out.inner = {};
  for (let off = 0; off + PS <= n; off += PS) {
    let q;
    try { q = p.add(off).readPointer(); } catch (e) { continue; }
    if (readable(q)) {
      out.inner[off] = dump(q, 64);
      try { const s = q.readUtf16String(64); if (s && /^[\x20-\x7e]{2,}$/.test(s)) out.inner[off + 's'] = s; } catch (e) {}
      try { const s = q.readCString(64); if (s && /^[\x20-\x7e]{2,}$/.test(s)) out.inner[off + 'a'] = s; } catch (e) {}
    }
  }
  return out;
}

function hookModule(m) {
  send_('module', { name: m.name, base: m.base.toString(), path: m.path });
  NAMES.forEach(function (name) {
    const f = m.findExportByName ? m.findExportByName(name) : Module.findExportByName(m.name, name);
    if (!f) { send_('missing', name); return; }
    Interceptor.attach(f, {
      onEnter: function (args) {
        this.name = name;
        this.args = [];
        for (let i = 0; i < 4; i++) this.args.push(args[i]);
        this.before = this.args.map(a => deep(a, 64));
      },
      onLeave: function (ret) {
        const after = this.args.map(a => deep(a, 64));
        const rec = { fn: this.name, ret: ret.toInt32(), before: this.before, after: after };
        if (NOISY.has(this.name)) {
          const key = this.name + JSON.stringify(this.before.map(b => b.bytes));
          const val = JSON.stringify(after.map(a => [a.bytes, a.inner])) + rec.ret;
          if (seen.get(key) === val) return;
          seen.set(key, val);
        }
        send_('call', rec);
      }
    });
  });
}

// The pipe to DriverInterface.exe: record what goes over it, both ways.
function hookPipes() {
  // KernelBase holds the real CreateFileW/WriteFile/ReadFile; kernel32's are forwarders to it
  const k32 = Process.findModuleByName('kernelbase.dll') || Process.getModuleByName('kernel32.dll');
  const createW = k32.findExportByName('CreateFileW');
  Interceptor.attach(createW, {
    onEnter: function (args) { try { this.path = args[0].readUtf16String(); } catch (e) { this.path = null; } },
    onLeave: function (ret) {
      if (this.path && this.path.toLowerCase().indexOf('\\\\.\\pipe\\') === 0) {
        pipeHandles.add(ret.toString());
        send_('pipe-open', { path: this.path, handle: ret.toString() });
      }
    }
  });
  ['WriteFile', 'ReadFile'].forEach(function (fn) {
    Interceptor.attach(k32.findExportByName(fn), {
      onEnter: function (args) { this.h = args[0].toString(); this.buf = args[1]; this.n = args[2].toInt32(); this.got = args[3]; },
      onLeave: function (ret) {
        if (!pipeHandles.has(this.h)) return;
        let n = this.n;
        try { if (!this.got.isNull()) n = this.got.readU32(); } catch (e) {}
        send_(fn === 'WriteFile' ? 'pipe-out' : 'pipe-in', { handle: this.h, n: n, bytes: dump(this.buf, Math.min(n, 4096)) });
      }
    });
  });
}

function waitForModule() {
  const m = Process.findModuleByName('DeviceInterface.dll');
  if (m) { hookModule(m); return; }
  setTimeout(waitForModule, 200);
}

send_('hello', { arch: Process.arch, pid: Process.id });
hookPipes();
waitForModule();
