# Changelog








## 4.25.2
- **Silent players get named.** Some players, VLC among them, never reconnect after the sound system went away (sleep, or the Repair button). They keep "playing" in silence, and it looks like the Mbox is broken. The app now remembers what was playing, and 20 seconds after waking or a repair tells you which program lost its connection and needs restarting.
- The Repair hint warns that some players need restarting afterwards.
- A refused driver reload ("Not authorized", no password prompt) is no longer logged as "cancelled at the password prompt".

## 4.25.1
- After waking from sleep, your output choice sticks. The app put the default output back once, but if that output reappeared a few seconds late, or WirePlumber switched it again afterwards, it gave up. It now watches for a minute after waking and puts it back whenever needed; every step goes to the recovery log.

## 4.25
- **Updates from GitHub.** Mbox Control now lives at github.com/fancy-butler/mbox-control. Tick Settings → Advanced → "Check for new versions once a day" and new releases are offered with one click (package installs are pointed to the package manager instead). Only GitHub is asked; nothing about you is sent.
- Releases publish themselves: when the version goes up and every test passes, GitHub tags it, posts this changelog section as the release notes, and attaches a ready AUR PKGBUILD.

## 4.24.1
- **Practice player stays in tune.** Slowed-down or sped-up playback could land up to 40 cents off pitch with Arch's current ffmpeg/Rubber Band (worst on low notes, like a bass's low E). Rubber Band now only stretches time, in its smooth mode, and transposing is done by exact resampling. Measured within 1 cent across the range.
- Without Rubber Band, transposing a 44.1 kHz song came out about 1½ semitones sharp. Fixed.
- GitHub now also tests on Arch with the current PipeWire, ffmpeg and Python, every push and every Monday, including starting and quitting the real app.

## 4.24
- **EQ graph.** The Tone card draws what you actually hear: the full curve (profile, Tone, overall level, subsonic filter), with the profile alone dashed underneath, and which profile is playing.
- **Neutral names.** The main output's PipeWire node is now `mbox_main` (was `sa600_sink`) and its file `mbox-main.conf` (was `mbox-sa600.conf`). Existing setups are converted once at start-up — file, saved settings and default output — with one short PipeWire restart.
- No more of the author's own gear in scene texts and examples.

## 4.23.1
- Turn EQ on/off now switches live. With the subsonic filter keeping the chain running, it used to change nothing until the next restart.
- The Speakers chip on Home is a button: pick Speakers A/B/C, switch the speakers off (headphones only — the headphone EQ follows), or tick "Use the headphone EQ". It shows 🎧 when the headphone EQ is playing.

## 4.23
- **Headphones get their own EQ.** The Mbox's headphone jack hears the same mix as the speakers, so the EQ can't follow the Spkr button for them. New Headphones row in Speakers → EQ; it plays when "Listening on headphones" is ticked or the speakers are switched off. A profile that was on Speakers B (where the AutoEQ dialog used to put headphones) is copied over.
- AutoEQ dialog defaults to Headphones.
- Bass/treble boosts lower the overall level only by as much as the combined curve really rises (computed from the actual filter shapes), not by the full boost on top of the profile's own headroom.
- EQ card shows which profile is playing now.

## 4.22.2
- **Metronome: "Beats" is now a time signature.** Choose from 4/4, 3/4, 2/4, 5/4, 6/8, 9/8, 12/8, 5/8,
  7/8, or no accent.
  - In 6/8, 9/8 and 12/8 the tempo counts the dotted-quarter beat, the way you'd feel a jig. The eighths
    tick softly in between, with an accent on the 1 and a lighter one on each beat.
  - 5/8 and 7/8 count eighth notes, grouped 3+2 and 2+2+3.
  - Your old "beats" setting carries over.

## 4.22.1
- **Fixed: the metronome and the practice player were silent on newer PipeWire (1.4+).** When `pw-play`
  reads from a pipe it now expects a sound file unless given `--raw`, so it rejected the app's raw audio
  and quit without a word. The app now passes `--raw` when pw-play supports it. If a player quits
  anyway, the reason is shown instead of silence. This is the same PipeWire change that broke Capture
  in 4.18.3.

## 4.22 — J-card maker
- **Make a J-card** (Record → Tools → Make a tape). It designs the tape's paper insert and shows a live
  preview with the folds marked. It fills in from **Plan sides…**:
  - song names from the tags, with times,
  - cover art from the songs' embedded art or a cover/folder image.
- The front holds the cover art with a title band (or big type if there's no art). The spine text is
  sized to fit. The track list wraps long names and shrinks the type to fit the 27 mm flap.
- Fold-out layouts (4 or 5 panels) put the track list on the fold-out.
- Optional tape type, noise reduction and year.
- **Save PDF to print:** actual size (104.8 × 101.6 mm, checked by measuring the rendered PDF), with cut
  marks, fold marks and a 50 mm check line. The paper is Letter or A4 depending on your country (Canada
  gets Letter).
- **Save PNG (600 dpi):** the flat card, for a print shop.
- Remembers your look (colours, layout, paper) between cards.

## 4.21.1
- **Fixed: the metronome could start and play nothing.** It plays through "Headphones (Mbox)" (computer
  1/2), which only reaches a headphone jack whose mix includes those channels. After a scene change or a
  reset, or with headphones in the B jack, the clicks went nowhere.
  - The metronome now adds itself to whatever mix each headphone jack is playing (never the speakers'),
    and puts it back when it stops.
  - It says which jack it's on.
  - If no headphone jack has its own mix, it explains and offers to set one up.

## 4.21
- **Subsonic filter** (Speakers → EQ, per speaker set). A 24 dB/octave high-pass at 30 Hz cuts the
  rumble and thumps from hard bass plucks that make small speakers pop. Measured through PipeWire:
  15 Hz down 24 dB, while a bass guitar's low E (41 Hz) loses 0.3 dB.
  - It applies to computer sound only. The Mbox's own mixer (live inputs) can't be filtered.
  - Existing users get a one-time suggestion to turn it on, with a ✕ to dismiss it.
  - Switching it on or off is instant (when off, the filter sits at an inaudible 5 Hz). Only the first
    time it's needed does the output chain get rebuilt, with one short PipeWire restart.
- Health suggestions can now have a "don't suggest this again" ✕.

## 4.20.2
- **Sound comes back after wake, not just the card.** While the Mbox is gone, PipeWire moves the default
  output and the app's "Speakers (Mbox)" / "Headphones (Mbox)" streams to another device, and newer
  versions don't move them back. After a wake or a recovery, the app now:
  - switches the Mbox back to pro-audio if needed,
  - moves its streams back onto the Mbox,
  - restores the default output from before sleep,
  - logs what it found and changed. It checks at 2, 6 and 15 seconds.
- Fixed: a crash when the meters paused for sleep (`events_ok` on a stopped poller).
- Fixed: time spent asleep was logged as "window not responding" (the watchdog now uses a clock that
  stops during suspend).
- **Copy diagnostics** is now also in Settings → App → Sleep & wake, where you'd look for it.

## 4.20.1
- The wake hook waits 8 seconds (was 15) for the card to come back on its own before recovering it.
  A hand-made hook that reloads unconditionally after 3 seconds worked on the test machine, so 15
  seconds was just extra silence.

## 4.20 — surviving sleep and wake
- **Lets go of the Mbox before sleep.** The app listens for logind's PrepareForSleep, holds a short
  "delay" inhibitor, and closes every handle it has on the card first: meters, events, direct reads
  and the control service. Your routing and mixer are saved.
- **Reconnects by itself after wake.** udev events (python-pyudev) trigger the reconnect immediately,
  with the old watchdog as a fallback. It finds the card by name, never by number, restarts the control
  service, and restores the saved routing and mixer if the box forgot them.
- **Recover Mbox** (offered automatically when the card is stuck, plus the tray and Settings → App →
  Sleep & wake). It stops the control service and the audio server (PipeWire including its sockets,
  or PulseAudio), reloads the sound driver through a polkit-authorised root helper, then starts
  everything again. It reports any program still holding the card.
- **Optional sleep/wake fix:** a systemd sleep hook that recovers a stuck card after wake without a
  password. It only acts when a FireWire audio unit is present but its card is missing. No hardcoded
  user names, paths or card numbers. Off by default, and can be removed from the same setting.
- The kernel driver and its control daemon are detected from sysfs (with a modalias fallback), using
  one mapping table for all snd-firewire-ctl-services drivers.
- Never touches firewire_ohci.
- Every step is logged to recovery.log, and the wake hook logs to /var/log/mbox-control-wake.log.
  Both are included in Copy diagnostics.
- **Replaces the old wake-up fix (mbox-rebind)**, which unbound the driver while PipeWire still held
  the card. That can leave exactly this kind of stuck card. The app now warns if the old fix is
  installed and offers to replace it.
- Packaging: the helper, recovery script and polkit action ship in the AUR package. The hook ships as
  a template only. python-pyudev is a new dependency, also added to install.sh for Ubuntu/Debian,
  Fedora and Arch.

## 4.19.8
- **Show folder on Plasma:** the file manager's own "show this folder" request wasn't enough under
  Plasma's focus-stealing prevention. After it, a tiny one-off KWin script now brings the file-manager
  window showing that folder to the front (and un-minimizes it). Works with Plasma 5 and 6.
- **Multitrack: "Normalize each track after recording"** (off by default). Each track's loudest point
  goes to −1 dB (−2 dB for lossy formats), so a quiet mic track comes up next to the bass. It changes
  volume only.

## 4.19.7
- **Multitrack: "Hear them while recording"** (on by default). The ticked inputs play from the Mbox's
  own mixer, with no delay, exactly as they're being recorded. They're put back when you stop.
- **"Hear it" now reaches the headphones.** Quick take, Capture and Multitrack add the input to every
  mix your speakers and headphones play, not only Outputs 1/2.
- If a headphone output plays the computer directly (so no input can be added), the app explains that
  once and offers to open the Mixer.
- **Low-cut (75 Hz) switches for Inputs 1–4**, right in Quick take and Multitrack. Before, only the
  "Record voice" scene turned it on. Use it for mics; leave it off for bass. It's the Mbox's own
  filter, so it also applies to Capture and to what you hear.
- **Show folder brings the folder to the front** even if it's already open behind other windows, and
  selects your last take. It uses the desktop's file-manager service (Dolphin and others), and falls
  back to simply opening the folder.
- Checked: the Mbox doesn't report its Multi button to Linux (only meter events arrive), so it can't
  be used.

## 4.19.6
- **Scrolling the page never changes a setting.** Drop-downs, number boxes and sliders (mixer faders
  included) only respond to the wheel or trackpad after you click them. Otherwise the scroll moves the
  page. Qt's default was to change whatever happened to be under the pointer.
- The "very quiet" warning now triggers when peaks stay under −30 dB (it was −40). A real AT2020 take
  peaking at −37 dB slipped past the old threshold.

## 4.19.5
- **Every input is watched on its own** in Quick take, Capture and Multitrack, so a live bass can no
  longer hide a dead mic. Multitrack is now watched too.
- **New "very quiet" warning:** peaks under −40 dB after 6 seconds suggest turning that input's gain up.
  If +48V is off on a mic input, the warning offers to switch it on.
- **Multitrack shows a level strip per ticked input**, each with its name and a one-word verdict.
- Multitrack checkboxes now update when you rename inputs or flip the F/R button.

## 4.19.4
- **Silent input warning.** If a quick take or capture runs for 4 seconds on an input with no signal,
  a bar explains why:
  - On a mic input with +48V off, it offers **Turn on +48V** (with a "turn your speakers down first"
    note).
  - Otherwise, it points to the cable, the gain knob, and the F/R or Mic/Ln button.
- Takes saved as MP3, AAC, Opus or Ogg are normalized to −2 dB instead of −1 dB, because lossy
  encoders overshoot a little.

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
