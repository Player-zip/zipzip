# Lo-fi bed (78 BPM, Dm9-G13-Cmaj9-Am9) + the sound effects, all synthesised.
# Reuses the SFX synth from ../motion/sfx_lib.py. -> work/music.wav, work/sfx.wav
import json, sys
import numpy as np
from scipy.io import wavfile
sys.path.insert(0, "../motion")
from sfx_lib import SR, sfx, place, pan, lp, hp, bp, t_

cues = json.load(open("work/cues.json"))
TOTAL = cues["total"]
N = int((TOTAL + 1) * SR)
rng = np.random.default_rng(78)

# ---------------- sfx (same synth as the gameplay edit, new seeds) ----------------
bus = np.zeros((N, 2))
for c in cues["sfx"]:
    r = np.random.default_rng(c["seed"])
    place(bus, sfx(c["kind"], r, c["arg"]) * c["gain"] * 10 ** (r.uniform(-1.5, 1) / 20), c["t"])
wavfile.write("work/sfx.wav", SR, bus[: int(TOTAL * SR)].astype(np.float32))

# ---------------- lo-fi ----------------
BEAT = 60 / 78
midi = lambda m: 440 * 2 ** ((m - 69) / 12)
CH = [[50, 53, 57, 60, 64], [43, 53, 57, 59, 64], [48, 52, 55, 59, 62], [45, 48, 52, 55, 59]]  # Dm9 G13 Cmaj9 Am9
keys, drums, bass = (np.zeros((N, 2)) for _ in range(3))


def rhodes(m, d, g):
    tt = t_(d); f = midi(m) * (1 + 0.0025 * np.sin(2 * np.pi * 0.7 * tt))  # tape wow
    ph = 2 * np.pi * np.cumsum(f) / SR
    y = np.sin(ph + 0.9 * np.exp(-tt * 5) * np.sin(ph)) + 0.18 * np.sin(2 * ph) * np.exp(-tt * 3)
    y *= np.minimum(1, tt / 0.01) * np.exp(-tt * 1.1) * np.minimum(1, (d - tt) / 0.15)
    return pan(y * g * (1 + 0.2 * np.sin(2 * np.pi * 4.5 * tt)), rng.uniform(-0.3, 0.3))


def kick(g):
    tt = t_(0.35); return pan(np.tanh(np.sin(2 * np.pi * np.cumsum(55 + 70 * np.exp(-tt * 30)) / SR) * np.exp(-tt * 9) * 1.5) * g, 0)


def snare(g):
    n = int(0.3 * SR); tt = t_(0.3)
    y = lp(rng.standard_normal(n), 3500) * np.exp(-tt * 16) + 0.4 * np.sin(2 * np.pi * 190 * tt) * np.exp(-tt * 25)
    return pan(y * g, 0.05)


def hat(g):
    n = int(0.05 * SR); return pan(hp(rng.standard_normal(n), 6000) * np.exp(-np.linspace(0, 7, n)) * g, rng.uniform(0.1, 0.35))


nb = int(TOTAL / BEAT) + 1
for b in range(nb):
    t = b * BEAT; bar, beat = divmod(b, 4); ch = CH[bar % 4]
    sw = BEAT / 2 + BEAT * 0.09                                   # lazy swing
    if beat == 0:                                                 # chord, slightly strummed
        for k, m in enumerate(ch[1:]):
            place(keys, rhodes(m, BEAT * 4 + 0.3, 0.16), t + 0.012 * k)
        place(bass, pan(np.sin(2 * np.pi * midi(ch[0] - 12) * t_(BEAT * 1.8)) * np.exp(-t_(BEAT * 1.8) * 1.2) * 0.5, 0), t)
    if beat == 2 and rng.random() < 0.6:
        place(bass, pan(np.sin(2 * np.pi * midi(ch[0] - 5) * t_(BEAT * 0.9)) * np.exp(-t_(BEAT * 0.9) * 2) * 0.35, 0), t + sw - BEAT / 2)
    if t < 1.5 or t > TOTAL - 2.2:                                # drums out on the intro and the last chord
        continue
    if beat == 0 or (beat == 2 and rng.random() < 0.5):
        place(drums, kick(0.75), t)
    if beat == 2 and bar % 2 == 1:
        place(drums, kick(0.5), t + sw)
    if beat in (1, 3):
        place(drums, snare(0.45), t + 0.02)
    place(drums, hat(0.2 * rng.uniform(0.7, 1)), t); place(drums, hat(0.12 * rng.uniform(0.6, 1)), t + sw)
    if rng.random() < 0.18:                                       # little melodic fill on top
        place(keys, rhodes(ch[int(rng.integers(2, 5))] + 12, BEAT, 0.12), t + sw)

mix = keys * 0.9 + drums * 0.8 + bass * 0.9
# vinyl: hiss + crackle
crack = np.zeros(N)
idx = rng.integers(0, N, int(TOTAL * 9)); crack[idx] = rng.uniform(-1, 1, len(idx)) * rng.uniform(0.1, 0.6, len(idx))
vinyl = bp(rng.standard_normal(N), 1000, 9000) * 0.012 + bp(crack, 1500, 7000) * 0.5
mix += np.stack([vinyl, np.roll(vinyl, 97)], 1)
mix = np.stack([lp(mix[:, c], 5200, 2) for c in range(2)], 1)    # dusty top end
mix = mix[: int(TOTAL * SR)]
fade = int(1.5 * SR); mix[-fade:] *= np.linspace(1, 0, fade)[:, None] ** 2
mix[: int(1.0 * SR)] *= np.linspace(0, 1, int(1.0 * SR))[:, None]
mix = np.tanh(mix / np.max(np.abs(mix)) * 1.1) * 0.85
wavfile.write("work/music.wav", SR, mix.astype(np.float32))
print("ok", round(TOTAL, 2))
