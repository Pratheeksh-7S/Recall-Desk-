"""Composes the demo's original background music (no samples, no copyright).

    python scripts/compose_music.py <seconds> music.wav
    ffmpeg -i demo.mp4 -i music.wav -map 0:v -map 1:a -c:v copy -c:a aac -shortest demo-music.mp4
"""
import numpy as np, wave, sys
SR = 44100
DUR = float(sys.argv[1])
BPM = 88.0
beat = 60 / BPM
bar = 4 * beat
n = int(SR * DUR)
t = np.arange(n) / SR
out = np.zeros((n, 2))

def hz(m): return 440 * 2 ** ((m - 69) / 12)
# Cmaj9 - Am9 - Fmaj7#11 - G6/9 (MIDI)
chords = [[48, 55, 59, 62, 64], [45, 52, 55, 59, 60], [41, 48, 52, 57, 59], [43, 50, 54, 57, 59]]
chords[3] = [43, 50, 55, 57, 59]
arp_pat = [0, 2, 3, 4, 3, 2, 1, 2]

def add(sig, start, ch, pan=0.5):
    s = int(start * SR)
    if s >= n: return
    e = min(n, s + len(sig))
    out[s:e, 0] += sig[:e - s] * (1 - pan) * ch
    out[s:e, 1] += sig[:e - s] * pan * ch

def env(length, a, r):
    m = int(length * SR); x = np.ones(m)
    ai, ri = int(a * SR), int(r * SR)
    x[:ai] = np.linspace(0, 1, ai); x[-ri:] *= np.linspace(1, 0, ri)
    return x

i = 0; start = 0.0
while start < DUR:
    ch = chords[i % 4]
    L = bar * 2
    tt = np.arange(int(L * SR)) / SR
    pad = sum(np.sin(2 * np.pi * hz(m + 12) * tt + d) * 0.5 + np.sin(2 * np.pi * hz(m + 12) * 1.003 * tt) * 0.5
              for d, m in enumerate(ch[1:])) / 4
    add(pad * env(L, 1.2, 1.5) * 0.22, start, 1.0, 0.5)
    bass = np.sin(2 * np.pi * hz(ch[0] - 12) * tt) + 0.2 * np.sin(2 * np.pi * hz(ch[0]) * tt)
    add(bass * env(L, 0.05, 0.8) * 0.28, start, 1.0, 0.5)
    for k in range(16):
        nt = start + k * beat / 2
        m = ch[arp_pat[k % 8]] + 24
        pl = np.arange(int(0.9 * SR)) / SR
        pluck = (np.sin(2 * np.pi * hz(m) * pl) + 0.3 * np.sin(4 * np.pi * hz(m) * pl)) * np.exp(-pl * 6)
        pluck[:200] *= np.linspace(0, 1, 200)
        add(pluck * 0.10, nt, 1.0, 0.3 + 0.4 * ((k % 4) / 3))
    for k in range(8):  # soft kick on beats 1 and 3, light hat offbeats
        kt = start + k * beat
        kl = np.arange(int(0.35 * SR)) / SR
        if k % 2 == 0:
            f = 110 * np.exp(-kl * 18) + 45
            kick = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-kl * 9)
            add(kick * 0.35, kt, 1.0, 0.5)
        hl = np.arange(int(0.05 * SR)) / SR
        hat = np.random.default_rng(k + i * 8).standard_normal(len(hl)) * np.exp(-hl * 90)
        add(np.diff(hat, prepend=0) * 0.03, kt + beat / 2, 1.0, 0.6)
    start += L; i += 1

# simple stereo echo for space
d = int(beat * 0.75 * SR)
for ch in (0, 1):
    y = out[:, ch].copy()
    for k, g in enumerate([0.35, 0.18, 0.09]):
        sh = d * (k + 1) + (ch * 300)
        y[sh:] += out[:-sh, ch] * g
    out[:, ch] = y
fade_in, fade_out = int(2 * SR), int(4 * SR)
out[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
out[-fade_out:] *= np.linspace(1, 0, fade_out)[:, None]
out /= np.max(np.abs(out)) * 1.12
with wave.open(sys.argv[2], 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((out * 32767).astype(np.int16).tobytes())
print("ok")
