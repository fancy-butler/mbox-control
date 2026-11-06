#!/bin/bash
# A throwaway PipeWire for the real-audio tests: no sound card, no desktop session.
#   source tests/pw_stack.sh    (sets XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS, MBOX_TEST_PIPEWIRE)
#   pytest -q
# Builds: fake_mbox (an 8-channel "Mbox" output), fake_mbox_in (a 4-channel "Mbox" input), pipewire-pulse
# for pactl, and the app's own EQ chain (mbox_main → fake_mbox) written by mbox_control itself.
set -e
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="$(mktemp -d /tmp/mbox-pw.XXXXXX)"
export XDG_RUNTIME_DIR="$base/run"
mkdir -p "$XDG_RUNTIME_DIR" "$base/cfg/pipewire/filter-chain.conf.d"
chmod 700 "$XDG_RUNTIME_DIR"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork >/dev/null

pipewire >"$base/pipewire.log" 2>&1 &
for _ in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/pipewire-0" ] && break; sleep 0.1; done
wireplumber >"$base/wireplumber.log" 2>&1 &
sleep 1.5
pw-cli create-node adapter '{ factory.name=support.null-audio-sink node.name=fake_mbox media.class=Audio/Sink
  audio.position=[AUX0 AUX1 AUX2 AUX3 AUX4 AUX5 AUX6 AUX7] object.linger=true }' >/dev/null
pw-cli create-node adapter '{ factory.name=support.null-audio-sink node.name=fake_mbox_in
  media.class=Audio/Source/Virtual audio.position=[AUX0 AUX1 AUX2 AUX3] object.linger=true }' >/dev/null
pipewire-pulse >"$base/pulse.log" 2>&1 &

QT_QPA_PLATFORM=offscreen python3 -c "
import sys; sys.path.insert(0, '$here/..')
import mbox_control as mc
open('$base/cfg/pipewire/filter-chain.conf.d/eq.conf', 'w').write(
    mc.eq_chain_conf('Speakers (Mbox)', mc.MAIN_SINK, 'fake_mbox', ['AUX7', 'AUX6']))"
XDG_CONFIG_HOME="$base/cfg" pipewire -c filter-chain.conf >"$base/filter-chain.log" 2>&1 &

for _ in $(seq 100); do
    pw-cli ls Node 2>/dev/null | grep -q '"mbox_main"' && pactl info >/dev/null 2>&1 && break
    sleep 0.1
done
pw-cli ls Node | grep -q '"mbox_main"' || { echo "PipeWire test stack failed; logs in $base" >&2; return 1 2>/dev/null || exit 1; }
export MBOX_TEST_PIPEWIRE=1
echo "PipeWire $(pipewire --version | grep -o 'libpipewire [0-9.]*' | head -1 | cut -d' ' -f2) test stack up (logs: $base)"
set +e
