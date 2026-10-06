# Music and sound effects synthesised from scratch (numpy only, no samples).
import json
from sfx_lib import *

cues = json.load(open("work/cues.json"))
TOTAL = cues["total"]
N = int(TOTAL * SR) + SR

sfx_bus = np.zeros((N, 2))
for c in cues["sfx"]:
    r = np.random.default_rng(c["seed"])
    s = sfx(c["kind"], r, c["arg"]) * c["gain"]
    # Never sit a cue on a fixed level: a small seeded gain drift on top of the cue gain.
    s *= 10 ** (r.uniform(-1.5, 1.0) / 20)
    place(sfx_bus, s, c["t"])
wavfile.write("work/sfx.wav", SR, sfx_bus[: int(TOTAL * SR)].astype(np.float32))
print("sfx cues:", len(cues["sfx"]))

# ======================================================================
# Music: 112 BPM, A minor, Am-F-C-G. Marimba lead (tropical, it's a monkey island)
# ======================================================================
M = cues["music"]
BPM = M["bpm"]; BEAT = 60 / BPM; BAR = 4 * BEAT
sections = M["sections"] + [(TOTAL + 1, "end")]


def section_at(t):
    for (a, name), (b, _) in zip(sections, sections[1:]):
        if a <= t < b:
            return name
    return "end"


A4 = 440.0
midi = lambda m: A4 * 2 ** ((m - 69) / 12)
CHORDS = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]  # Am F C G
ROOTS = [45, 41, 48, 43]
PENTA = [57, 60, 62, 64, 67, 69, 72, 74, 76, 79]

layers = {k: np.zeros((N, 2)) for k in ("drums", "bass", "marimba", "pad", "fx")}
rng = np.random.default_rng(2024)


def kick(g=1.0):
    d = 0.42; n = int(d * SR); tt = t_(d)
    y = np.sin(glide(150, 46, n)) * np.exp(-tt * 9) + hp(rng.standard_normal(n), 2000) * np.exp(-tt * 120) * 0.15
    return pan(np.tanh(y * 1.4) * g, 0)


def clap(g=1.0):
    out = np.zeros(int(0.35 * SR))
    for i in range(3):
        n = int(0.012 * SR)
        place(out, bp(rng.standard_normal(n), 900, 5000) * np.exp(-np.linspace(0, 6, n)), i * 0.011)
    n = int(0.28 * SR)
    place(out, bp(rng.standard_normal(n), 1000, 4500) * np.exp(-np.linspace(0, 7, n)) * 0.6, 0.033)
    return pan(out * g, rng.uniform(-0.1, 0.1))


def hat(g=1.0, open_=False):
    d = 0.18 if open_ else rng.uniform(0.03, 0.05); n = int(d * SR)
    y = hp(rng.standard_normal(n), 7000) * np.exp(-np.linspace(0, 5 if open_ else 8, n))
    return pan(y * g, rng.uniform(0.15, 0.4))


def shaker(g=1.0):
    n = int(0.09 * SR)
    y = bp(rng.standard_normal(n), 5000, 11000) * np.sin(np.linspace(0, np.pi, n)) ** 2
    return pan(y * g, rng.uniform(-0.45, -0.2))


def tom(g=1.0, f=70):
    d = 0.9; n = int(d * SR); tt = t_(d)
    y = np.sin(glide(f * 1.6, f, n)) * np.exp(-tt * 5) + lp(rng.standard_normal(n), 600) * np.exp(-tt * 18) * 0.5
    return pan(np.tanh(y * 1.2) * g, rng.uniform(-0.2, 0.2))


def marimba(m, g=1.0, d=0.6):
    f = midi(m) * rng.uniform(0.998, 1.002); n = int(d * SR); tt = t_(d)
    y = (np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(2 * np.pi * f * 3.93 * tt) * np.exp(-tt * 18)
         + 0.08 * np.sin(2 * np.pi * f * 9.2 * tt) * np.exp(-tt * 40)) * np.exp(-tt * rng.uniform(5.5, 8))
    y *= np.minimum(1, tt / 0.002)
    return pan(y * g, rng.uniform(-0.35, 0.35))


def bass(m, d, g=1.0):
    f = midi(m); n = int(d * SR); tt = t_(d)
    saw = 2 * ((f * tt) % 1) - 1
    y = 0.8 * np.sin(2 * np.pi * f * tt) + 0.35 * lp(saw, 700)
    y *= np.minimum(1, tt / 0.004) * np.exp(-tt * 2.2) * np.minimum(1, (d - tt) / 0.02)
    return pan(y * g, 0)


def pad(ch, d, g=1.0):
    n = int(d * SR); tt = t_(d); y = np.zeros(n)
    for m in ch:
        for det in (-0.12, 0.0, 0.11):
            f = midi(m + det) / 2 * 2
            y += 2 * ((f * tt + rng.random()) % 1) - 1
    y = lp(y, 1400, 4) / (3 * len(ch))
    a = min(0.6, d / 3)
    e = np.minimum(1, tt / a) * np.minimum(1, (d - tt) / (a * 1.6))
    return np.stack([y * e * g, np.roll(y, 240) * e * g], 1)


nbeats = int(TOTAL / BEAT) + 2
for b in range(nbeats):
    t = b * BEAT
    if t >= TOTAL:
        break
    sec = section_at(t)
    bar, beat = divmod(b, 4)
    ci = bar % 4
    swing = BEAT / 2 + BEAT * 0.06  # light swing on the off-8ths
    full = sec in ("groove", "groove2", "muffled")
    if sec == "intro":
        if bar >= 1 and beat in (0, 2):
            place(layers["drums"], kick(0.55), t)
        place(layers["drums"], shaker(0.35), t); place(layers["drums"], shaker(0.25), t + swing)
        if beat == 0:
            place(layers["bass"], bass(ROOTS[ci], BAR * 0.9, 0.45), t)
    if sec == "cinematic":
        if beat in (0, 2):
            place(layers["drums"], tom(0.5 + 0.25 * (t - 14.4) / 13, 62 if beat == 0 else 74), t)
        if beat == 0 and bar % 2 == 0:
            place(layers["pad"], pad(CHORDS[(bar // 2) % 4], BAR * 2 + 0.7, 0.55), t)
        place(layers["bass"], bass(ROOTS[(bar // 2) % 4], BEAT / 2 * 0.8, 0.35), t)
        place(layers["bass"], bass(ROOTS[(bar // 2) % 4], BEAT / 2 * 0.8, 0.25), t + BEAT / 2)
    if sec == "halftime":
        if beat == 0:
            place(layers["drums"], kick(0.8), t)
        if beat == 2:
            place(layers["drums"], clap(0.6), t)
        place(layers["drums"], hat(0.18), t)
        if beat == 0:
            place(layers["pad"], pad(CHORDS[ci], BAR + 0.6, 0.35), t)
            place(layers["bass"], bass(ROOTS[ci], BAR * 0.7, 0.55), t)
    if full:
        if beat in (0, 2) or (beat == 1 and bar % 2):
            place(layers["drums"], kick(0.85), t)
        if beat in (1, 3):
            place(layers["drums"], clap(0.55), t)
        place(layers["drums"], hat(0.22), t); place(layers["drums"], hat(0.14), t + swing)
        if sec == "groove2" and beat == 3:
            place(layers["drums"], hat(0.12, True), t + swing)
        place(layers["drums"], shaker(0.18), t + swing)
        # syncopated bass: root on 1, octave on the "and" of 2, fifth on 4
        if beat == 0:
            place(layers["bass"], bass(ROOTS[ci], BEAT * 1.3, 0.7), t)
        if beat == 1:
            place(layers["bass"], bass(ROOTS[ci] + 12, BEAT * 0.4, 0.45), t + swing)
        if beat == 3:
            place(layers["bass"], bass(ROOTS[ci] + 7, BEAT * 0.8, 0.5), t)
        if beat == 0 and bar % 2 == 0:
            place(layers["pad"], pad(CHORDS[ci], BAR * 2 + 0.7, 0.22), t)
    if sec in ("outro",) and beat == 0 and t < TOTAL - 1.0:
        place(layers["pad"], pad([57, 60, 64, 69], TOTAL - t, 0.4), t)
        place(layers["bass"], bass(45, 2.5, 0.6), t)
        place(layers["drums"], kick(0.8), t)
        break

# Marimba melody: 8th-note line over the chord tones, new phrase every 2 bars,
# with rests so it never loops identically.
mel_rng = np.random.default_rng(7)
for e in range(int(TOTAL / (BEAT / 2))):
    t = e * BEAT / 2 + (BEAT * 0.06 if e % 2 else 0)
    sec = section_at(t)
    if sec not in ("intro", "groove", "groove2", "muffled", "halftime"):
        continue
    bar = int(e // 8); ci = bar % 4
    density = {"intro": 0.75, "groove": 0.55, "groove2": 0.62, "muffled": 0.5, "halftime": 0.3}[sec]
    if mel_rng.random() > density:
        continue
    tones = CHORDS[ci] + [c + 12 for c in CHORDS[ci]]
    if mel_rng.random() < 0.25:
        tones = PENTA
    m = tones[int(mel_rng.integers(0, len(tones)))] + 12
    g = 0.42 if e % 2 == 0 else 0.3
    place(layers["marimba"], marimba(m, g, 0.55), t)
    if sec == "groove2" and mel_rng.random() < 0.18:  # occasional double-stop
        place(layers["marimba"], marimba(m + 7, g * 0.5, 0.5), t)
# outro: final rising arpeggio
ot = [s for s in sections if s[1] == "outro"][0][0]
for i, m in enumerate([69, 72, 76, 81, 84]):
    place(layers["marimba"], marimba(m, 0.45, 1.2), ot + i * BEAT / 2)

# Riser into the explosion.
rg = [s for s in sections if s[1] == "riser_gap"][0][0]
d = 0.55; n = int(d * SR)
ris = sweep_filter(rng.standard_normal(n), 400, 7000, q=1.4) * np.linspace(0, 1, n) ** 2
place(layers["fx"], pan(norm(ris, 0.5), 0), rg)

mix = (layers["drums"] * 0.9 + layers["bass"] * 0.8 + layers["marimba"] * 0.75
       + layers["pad"] * 0.55 + layers["fx"] * 0.6)

# Section treatments: muffled (low-pass) during the b-roll, stutter on the glitch,
# silence right before the explosion, tail fade at the end.
tt = np.arange(N) / SR
for (a, name), (b, _) in zip(sections, sections[1:]):
    i, j = int(a * SR), min(N, int(b * SR))
    if name == "muffled":
        seg = mix[max(0, i - 2400): j + 2400].copy()
        filt = np.stack([sosfiltfilt(butter(4, 650, "low", fs=SR, output="sos"), seg[:, c]) for c in range(2)], 1) * 0.8
        ramp = np.ones(len(seg)); ramp[:2400] = np.linspace(0, 1, 2400); ramp[-2400:] = np.linspace(1, 0, 2400)
        mix[max(0, i - 2400): j + 2400] = seg * (1 - ramp[:, None]) + filt * ramp[:, None]
    if name == "stutter":
        gate = ((tt[i:j] - a) / (BEAT / 4)).astype(int) % 2 == 0
        mix[i:j] *= gate[:, None] * 0.9 + 0.1
    if name == "riser_gap":
        g = np.ones(j - i); g[:1200] = np.linspace(1, 0, 1200); g[1200:] = 0
        keep = layers["fx"][i:j] * 0.6
        mix[i:j] = mix[i:j] * g[:, None] + keep * (1 - g[:, None])

mix = mix[: int(TOTAL * SR)]
fade = int(0.6 * SR)
mix[-fade:] *= np.linspace(1, 0, fade)[:, None] ** 1.5
mix = np.tanh(mix / (np.max(np.abs(mix)) + 1e-9) * 1.2) * 0.85
wavfile.write("work/music.wav", SR, mix.astype(np.float32))
print("music:", round(TOTAL, 2), "s")
