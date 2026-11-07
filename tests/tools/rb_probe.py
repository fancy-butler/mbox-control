"""How far off pitch does ffmpeg's Rubber Band leave a pure tone? Prints cents per tempo × frequency."""
import subprocess
import numpy as np

def cents(freq, filt):
    a = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3",
                        "-af", filt, "-ac", "1", "-ar", "48000", "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(a, np.int16).astype(np.float32)
    seg = x[len(x) // 4: 3 * len(x) // 4]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 8 * len(seg)))
    return 1200 * np.log2(np.argmax(spec) * 48000 / (8 * len(seg)) / freq)

freqs = [82.4, 110, 220, 370, 440, 494, 880, 1760]
print("ffmpeg:", subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.split("\n")[0])
for name, mk in [("rubberband tempo", lambda t: f"rubberband=tempo={t}"),
                 ("rubberband tempo crisp", lambda t: f"rubberband=tempo={t}:transients=crisp"),
                 ("rubberband tempo smooth", lambda t: f"rubberband=tempo={t}:transients=smooth:phase=independent"),
                 ("atempo", lambda t: f"atempo={t}")]:
    for t in (0.5, 0.75, 0.8919, 1.25):
        print(f"{name:26s} {t:6}", " ".join(f"{cents(f, mk(t)):+5.0f}" for f in freqs))
print("freqs:", freqs)
