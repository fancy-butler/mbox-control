# Mbox Control

**A modern Linux control panel for the Avid Mbox Pro (3rd generation).**
Unofficial: not made, supported or endorsed by Avid. "Mbox" is a trademark of Avid Technology.

The Mbox Pro 3 is a great FireWire interface whose own software stopped at Windows 7 and macOS.
On Linux the audio already works through the kernel's `snd-dice` driver. This app gives you
control of everything else the box can do, and a few things it never could.

> IEEE 1394 · still kicking. 400 Mbit/s of pure spite.

![Live signal map](docs/screenshots/signal-map.png)

## What it does

- **Live signal map.** It draws what's plugged in, the box's internal mixes and where they go.
  Cables light up while sound is going through them.
- **Router and mixer.** Covers all 6 stereo mixes, the outputs, both headphone outputs and S/PDIF. Pan, mute, solo and link work on any channel.
- **Speaker sets.** Gives the front **Spkr** button real A/B/C speaker switching. Mute and dim work too.
- **Scenes.** One click sets the box up for listening, ripping, recording or playing guitar
  through the computer. Undo puts everything back. You can also save your own scenes.
- **Ripping tapes and records.** Hands-free mode starts recording when the music starts and stops
  at the end of the side. You can split by silence or by song count on a waveform, and look up
  titles on MusicBrainz or Discogs. Tracks save as tagged FLAC with cover art and ReplayGain. It
  can also correct a deck that runs fast or slow.
- **Guitar mode.** Opens Guitarix at low latency and wires it up for you. Each instrument input
  you choose gets its own Guitarix, so a guitar and a bass can play at once.
- **Quick takes.** Record a mic or instrument with one button, saved as FLAC.
- **Plug-in hints.** When sound turns up on an input that isn't going anywhere, it offers to let you
  hear it, rip it or record it.
- **Apps and speakers.** Send any app to the speakers, the headphones only, or AirPlay speakers
  around the house.
- **Phone remote.** Scan a QR code to control speakers, volume, scenes and recording from your phone.
- **Tray, shortcuts, MIDI.** There's a quick panel in the system tray, Ctrl+K to jump to anything,
  KDE global shortcuts, and MIDI controller mapping.
- **Recovery.** It reconnects after the box is power-cycled or the computer sleeps, revives frozen
  meters, and restores your setup when the box forgets it.

## Screenshots

| | |
|---|---|
| ![Home](docs/screenshots/home.png) | ![Mixer](docs/screenshots/mixer.png) |
| ![Split and tag](docs/screenshots/split-and-tag.png) | ![Guitar mode](docs/screenshots/guitar-mode.png) |
| ![First run](docs/screenshots/first-run.png) | <img src="docs/screenshots/phone-remote.png" width="260" alt="Phone remote"> |

## Supported systems

| | Tested | Notes |
|---|---|---|
| **Arch** and friends (CachyOS, Manjaro, EndeavourOS) | daily use | |
| **Ubuntu 24.04+** and friends (Mint 22, Pop!_OS, Kubuntu) | installer, end to end | PySide6 comes from pip |
| **Debian 12+** | installer written for it | Debian 13 packages more of the pieces |
| **Fedora 39+** | installer written for it | Uses `ffmpeg-free` unless you already have RPM Fusion's ffmpeg |

You need **PipeWire** for sound. It's the default on all of the above.

You also need a FireWire port. A PCIe card with a **Texas Instruments** chipset is the safe choice.

## Install

Download the latest release from the **Releases** page and unpack it, or `git clone` this repository.
Then run this inside the folder:

```
bash install.sh
```

Run it as yourself, not root. It asks for your password when it needs it. Running it again updates
the app. It:

1. Installs the packages for your system: Python and PySide6, NumPy, FFmpeg, alsa-utils, pactl
   and PipeWire tools. If your distro doesn't package PySide6, it installs it with pip, just for the app.
2. Builds **snd-dice-ctl-service** from
   [snd-firewire-ctl-services](https://github.com/alsa-project/snd-firewire-ctl-services) if you
   don't have it already. This takes 5–10 minutes, once. Along the way it builds any of
   libhinawa, libhitaki and alsa-gobject your distro doesn't package. If your Rust is older than
   1.78, it installs the current one with rustup in your home folder.
3. Lets you use the Mbox without root. Most current distros already allow this; on older ones the
   installer adds a udev rule.
4. Copies the app to `~/mbox-control` and adds it to your app launcher.

Optional extras get installed from inside the app when you first use them, with one password
prompt: Audacity, Guitarix, Chromaprint for song identification, and Avahi for AirPlay.

### Arch (AUR)

Once it's published: `paru -S mbox-control`, or the same with `yay`. The AUR package gets the control service from
`snd-firewire-ctl-services-git`.

## Reporting problems

Open an issue and paste the output of **Settings → App → Copy diagnostics**. Nothing personal is
included.

## First run

A short setup asks what's plugged in where, for example what Outputs 1/2 feed and what's on each
input. Everything is optional, and you can change it any time in **Settings → Your setup**.

## Subsonic filter

**Speakers → EQ** has a **Subsonic filter** switch for each speaker set. It cuts everything below
30 Hz (24 dB/octave): the rumble and pluck thumps that make bookshelf speakers pop, well below the
lowest note of a bass guitar (41 Hz). Turn it off for a subwoofer or any speaker you want to hear
everything on. It filters computer sound only, not the Mbox's internal mixer.

## Sleep and wake

On some systems the Mbox's sound card doesn't come back after the PC wakes up. The FireWire unit
reappears (`/sys/bus/firewire/devices/fw1.0`), but the ALSA card doesn't, and the old card can't be
cleared because programs still hold it.

Mbox Control handles this in three layers:

1. **Before sleep**, the app lets go of the card: meters, events, direct reads and the control
   service. It listens for logind's `PrepareForSleep` signal and holds a short "delay" inhibitor so it
   has time to do this.
2. **After wake**, it reconnects by itself, using udev events (`python-pyudev`) to notice the card
   coming back. It finds the card by name, never by number, restarts the control service and puts
   your routing and mixer back if the box forgot them.
3. **If the card is stuck** (FireWire unit present, ALSA card missing), the app offers **Recover Mbox**
   (also in the tray, and in **Settings → App → Sleep & wake**). This:
   - stops the control service and the audio server (PipeWire with its sockets, or PulseAudio),
   - reloads the sound driver through a small root helper (one password prompt, via polkit),
   - starts everything again.

   The driver and its control service are detected from sysfs, not hardcoded (`snd_dice` ↔
   `snd-dice-ctl-service`, `snd_bebob` ↔ `snd-bebob-ctl-service`, …).

**Optional: automatic recovery without a password.** **Settings → App → Sleep & wake → Install
sleep/wake fix** adds a systemd sleep hook. After every wake it gives the card 8 seconds to come back on its own, then checks
whether a FireWire audio card is missing. Only if one is, it stops the logged-in users' audio servers,
reloads the sound driver and starts everything again. It has no hardcoded user names or paths, and it
logs every step to `/var/log/mbox-control-wake.log` and the journal (`journalctl -t mbox-control`).
Remove it from the same button.

The AUR package installs the helper (`/usr/lib/mbox-control/mbox-reload-driver`), the recovery script
and the polkit action. The hook is only shipped as a template
(`/usr/share/mbox-control/mbox-control.sleep-hook`) and is **not enabled by default**.

What it never does: touch the FireWire controller driver (`firewire_ohci`). Unbinding or reloading
that wedges the whole FireWire stack until a reboot.

If recovery doesn't work on your system, send **Copy diagnostics**. It includes the recovery log.
Recovery has so far been proven on one system: TI XIO2213 FireWire card, CachyOS, PipeWire.

## Known limits

- Only tested on the Mbox Pro 3. Other DICE-based interfaces won't work without changes.
- Above 96 kHz the box only has two stereo mixes and no reverb. This is a hardware limit.
- AirPlay plays about 1.5 seconds behind, so use it for other rooms.
- The phone remote works over plain HTTP on your home network, paired by the code in its QR link.
  It can go on your home screen but opens in a browser tab.

## Development

The tests run against a simulated Mbox, built from a real Mbox Pro 3's control list. You don't
need the hardware, PipeWire or a screen:

```
pip install PySide6 numpy pytest
pytest -q
```

GitHub runs the same tests, plus a lint check, on every push.

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). You can use, share and change it, and anything
built from it has to stay open source under the same terms.

## Credits

The [snd-dice](https://github.com/torvalds/linux/tree/master/sound/firewire/dice) kernel driver
and Takashi Sakamoto's snd-firewire-ctl-services do the hard part. This app is the friendly face
on top of them.
