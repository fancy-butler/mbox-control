# Changelog

## 4.17
- **Practice player** (Record → Practice) for learning tunes by ear:
  - Slow down to 40% or speed up to 125% without changing the pitch (Rubber Band when ffmpeg has it).
  - Transpose ±12 semitones, or pick your instrument's key and it detects the song's key and moves it
    there (a D whistle gets G-major tunes in D, and E-minor tunes in B minor).
  - Drag across the waveform to loop a section, with an option to speed up 5% each time round.
  - Play on any output, e.g. the headphones only.
  - Save the slowed or transposed version (whole song or just the loop) as FLAC or MP3.

## 4.16
- **Capture what I just played.** Keeps the last 5 minutes of an input in memory (never on disk until
  you save). Save the last 30 s, minute or 5 minutes, from the app, the tray panel, your phone or a
  keyboard shortcut. Silence at the start is trimmed off.
- **Multitrack:** record several inputs at once, each to its own sample-aligned FLAC, named after what's
  plugged in.
- **Metronome:** tap tempo, beats per bar with an accented first beat, and volume. Clicks go to
  "Headphones (Mbox)" so they stay out of speaker recordings. Tempo changes apply while it plays.
- **Level check:** play your loudest part for 10 seconds and it tells you which way to turn the gain
  knob, and roughly how far.
- New shortcut commands: save-retro, quick-take, metronome.

## 4.15
- **EQ that follows the Spkr button.** Speakers & apps → EQ: give each speaker set its own EQ, so your
  headphones get their correction and your speakers theirs. Switching is instant and silent, and
  "Off (flat)" is bit-for-bit the same as no EQ.
- **Find my headphones (AutoEQ):** search over 5,000 measured headphones (e.g. Sennheiser HD 280 Pro by
  oratory1990) and apply the correction in one click. You can also import ParametricEQ.txt files
  from REW or Equalizer APO for room or speaker correction.
- **Tone:** bass and treble on top of any profile, with automatic headroom so boosts never clip.
- **Clean mic (voice chain):** a new input, "Mic — clean (Mbox)", with low-cut, noise reduction (RNNoise)
  and a gentle compressor, using whichever of those plugins you have.
- Fix: lists that rebuild (EQ, apps, AirPlay) no longer leave stale rows behind.

## 4.14.1
- **The window no longer freezes when the Mbox locks up.** Any helper that stops answering (PipeWire
  or the Mbox's control service) is skipped for 30 seconds instead of being waited on over and over.
  The app says "The Mbox has stopped answering — switch it off and on", then reconnects by itself.
- **Write-loop brake.** The app refuses to send the same command to the Mbox more than 4 times in
  10 seconds, or 200 commands in 3 seconds, and logs where the loop came from. Nothing a person or a
  scene does comes close to that.
- Fix: loopback could keep retrying a change the Mbox refused.

## 4.14
- **Loopback.** Record exactly what any output plays (mic and computer mixed by the Mbox itself) as
  "Loopback (Mbox)". **Make it a stream mix** sets up mic + computer on a spare output in one click.
- **Volume pop-up** when you turn the big knob or press Spkr. On KDE it uses Plasma's own OSD.
- **App rules:** pin 📌 an app to an output and it goes there every time it starts.
- **Export / import settings** (File menu or Settings → App). Your phone-remote key is never
  exported, and other keys only if you say so.
- **Crash and freeze log:** errors, crashes and any time the window stops responding for 5 seconds
  are written to `~/.config/mbox-control/crash.log`, and **Copy diagnostics** includes them.
- Settings → App → Meters: switch off fast meter reading if it ever misbehaves.
- Fix: speaker-set names now show on the tray panel and the phone remote.

## 4.13
- **Phone remote is paired.** Only phones that scanned the QR code can control the Mbox. **Unpair
  all phones** makes a new code.
- **Much lighter on the CPU.** Meters are read straight from the sound card instead of starting
  `amixer` about 40 times a second. The app checks this against `amixer` first and falls back if
  anything differs.
- **Installs don't freeze the window.** Audacity, Chromaprint, AirPlay and firewall changes run in the
  background with a progress box.
- **Updates:** an opt-in daily check for new releases on GitHub. Each download is verified before it
  replaces anything. Picking up new versions from ~/Downloads is now a "Tester mode" setting.
- **Cleaner look:** icons on cards and tabs, and long explanations shortened to one line with the
  rest in a hover (ⓘ).
- Fix: icons were missing when PySide6 came from pip (Ubuntu/Debian).

## 4.12.1
- `mbox-control --version` and `mbox-control --diagnostics`, plus **Settings → App → Copy diagnostics**
  for bug reports.

## 4.12
- **Guitar mode with several instruments.** Choose which of Inputs 1–4 have instruments. Each one gets its
  own Guitarix (its own amp and pedals) and is wired up for you.
- **Installer for Arch, Debian/Ubuntu and Fedora.** It builds the Mbox control service and any
  libraries your distro is missing.
- In-app installs (Audacity, Guitarix, Chromaprint, Avahi) use your distro's package manager.

## 4.11
- **Plug-in suggestions.** When sound turns up on an input that isn't going anywhere, the app offers to
  let you hear it, rip it or record it.
- **First-run setup** asks what's plugged in where.
- Neutral defaults: the main output is called "Speakers (Mbox)" until you name it.
- GPL-3.0-or-later license and notice.

## 4.10
- **Quick take:** one-button mic or instrument recordings, from the app, the tray panel or your phone.
- **Phone remote redesign:** installable to your home screen, speaker names, mute, live meters, and
  record or rip controls with progress.
- Fix: the firewall check crashed when ufw was installed but turned off.

## 4.9
- **AirPlay speakers:** one-click setup, and "Also play" copies sound to a speaker in another room.
- **Tape speed fix** for decks that run fast or slow, with a "Measure" button that estimates the error.

## 4.8.1
- Brings dead meters back by itself (for example after a sample-rate change).

## 4.8
- **Live signal map:** what's plugged in, the internal mixes and the outputs, with cables that light up
  while sound flows.
- Fewer pages: Apps moved into Speakers, and Your setup moved into Settings.

## 4.0 – 4.7
- Router and mixer for all six stereo mixes, outputs, headphones and S/PDIF, with pan, mute, solo and link.
- Real A/B/C speaker sets on the front **Spkr** button, plus mute and dim.
- Scenes with undo, and your own saved scenes.
- Tape and vinyl ripping: hands-free start and stop, waveform splitting, MusicBrainz and Discogs lookup,
  song identification, tagged FLAC with cover art and ReplayGain, and a rip queue.
- Guitar mode with Guitarix, a tuner and live analysis.
- Phone remote with QR code and one-click firewall setup.
- Tray quick panel, Ctrl+K command palette, KDE global shortcuts and MIDI controller mapping.
- Light and dark themes that follow KDE.
- Recovery after power cycles, sleep and frozen meters.
