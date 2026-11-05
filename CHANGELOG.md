# Changelog

## 4.19.3
- **After recording** (Quick take and Capture): leave the take as is, **normalize** it (the loudest point
  goes to −1 dB, volume only), or **even it out** with a gentle compressor and then normalize.
  - "Even out" levels the take to full scale first, so it behaves the same however loud you played.
    In testing, a 22 dB gap between soft and hard notes came out at 10 dB.
  - Multitrack is left untouched, since those tracks are for mixing.
  - This runs before conversion, so it works with any "Save takes as" format.

## 4.19.2
- **Quick take: hear it while recording** (on by default). Plays the input through Outputs 1/2 straight
  from the Mbox's mixer, with no delay, and puts it back when the take stops. Quick take and Capture
  can both listen to the same input without undoing each other.

## 4.19.1
- **Fixed: "Capture what I just played" could listen to the wrong input after a quick take.** Its
  "Stereo: …" entry meant "whatever the Rip page records from", which Quick take borrows while it
  records. It now offers plain choices instead: each input, plus Inputs 1/2, 3/4 and Aux In 5/6 as
  stereo pairs. These record straight from their own channels and never touch the Rip page's setting.
  Old saved choices move over by themselves.
- "Hear it while listening" now works for stereo pairs too (left to left, right to right).
- Quick take's borrowing of the rip input is now crash-safe: if the app closes mid-take, the Rip
  page's source is put back at the next start.

## 4.19
- **Save takes as:** pick FLAC (default), WAV 24/16-bit, AIFF, ALAC, MP3 (V0/320/192), AAC (.m4a), Opus or
  Ogg Vorbis for quick takes, "Capture what I just played" and multitrack. Rips stay FLAC.
- **Convert audio** (Record → Tools): add files, whole folders (subfolders included), or drop them on
  the list, then pick a format and convert.
  - Tags and cover art come along, wherever the format can hold them.
  - 96 kHz audio is resampled to 48 kHz for MP3, AAC, Opus and Vorbis.
  - WAV 16-bit is dithered.
  - Copies go into a "Converted (FORMAT)" folder beside each file, or a folder you pick. The originals
    are never touched, and half-finished files never get left behind.

## 4.18.4
- **Live level and spectrum** in Quick take and "Capture what I just played":
  - The last minute of playing scrolls by, coloured green, amber or red, with a dashed line at the
    −6 dB target.
  - A live meter with peak hold and a plain-words verdict ("quiet — gain up", "good",
    "CLIPPING — gain down").
  - A spectrum, so you can see lows and highs as you play.
  - In Capture, a shaded area shows what "Save last 30 s" would grab.
- The spectrum shares one audio stream per input and only runs while it's on screen.
- Quick take's input list now shows your names for inputs (e.g. "Mono: Input 1 (Jazz Bass)").

## 4.18.3
- **Fixed: "Capture what I just played" saved loud, distorted noise on newer PipeWire.** When it writes
  to a pipe, newer `pw-record` wraps the audio in an AU container (byte-swapped samples behind a
  header) unless it's given `--raw`. The app now passes `--raw` when pw-record supports it.
- **Hear it while listening** (on by default): plays the input through Outputs 1/2 from the Mbox's own
  mixer, with no delay, while "Capture what I just played" is on. When you stop, it's put back the way
  it was. Inputs you can already hear are left alone.

## 4.18.2
- **Fixed: "Capture what I just played" did nothing on newer PipeWire (1.4+).** Newer `pw-record`
  reads a single channel name like `AUX0` as a layout and refuses it. One-channel captures are now
  recorded as a pair, and the app keeps the channel you picked. Multitrack with a single input is
  fixed the same way.
- If listening fails, the reason now shows in the card instead of only flashing in the status bar.
- Hardware view: the rear mic jacks are numbered 04 and 03, like the real panel. The jack picker for
  inputs 3/4 now says "Line (¼")" (both jacks are on the back).

## 4.18.1
- **Front/rear and mic/line jacks keep their own names.** Inputs 1–2 (F/R button) and 3–4 (Mic/Ln
  button) each have two jacks. Name each jack on Hardware → The box. To tell the app which jack is
  in use, click the F/R or Mic/Ln button in the app's picture of the Mbox, or use the picker in
  Your setup. The Mbox doesn't report those buttons, so the app can't see them itself. Names you
  already set move to the jack in use.
- Automatic-scene messages now pop up on screen even when the app is hidden behind the app that
  triggered them.
- The Add button for automatic scenes now gives feedback: Added, Already set, or Changed (pick a
  different scene for the same app to change it).

## 4.18
- **Automatic scenes** (Settings → Controls): pick an app and a scene, and the scene switches on when the
  app opens. When the app closes, your previous settings come back (you can turn this off per app).
- **Mixer undo/redo:** Undo and Redo buttons on the Mixer, plus Ctrl+Z and Ctrl+Shift+Z / Ctrl+Y. Covers
  faders, pans, routing, mutes and solo, and remembers the last 100 changes.
- **A/B compare:** flick between two versions of a mix. B starts as a copy of A.
- **Compact mixer:** a small separate window with one slider per input, level bars and mute buttons,
  and an "In use" filter. It updates when anything else changes the mix.
- **Colour tags:** pick a colour per input in Your setup, or from a mixer channel's right-click menu. The
  colour shows on the mixer, the compact mixer and the signal map. A stereo pair shares one colour.
- Write-loop guard loosened slightly (8 identical writes in 10 s) so fast undo/redo never trips it.

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
