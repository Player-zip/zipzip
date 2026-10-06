# Sound-effect synthesis shared by the edits (synth.py, ../bio/lofi.py): numpy only,
# no samples. sfx(kind, rng) returns a stereo 48 kHz cue; each seed gives a variation.
import json
import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt, sosfiltfilt

SR = 48000


def t_(d):
    return np.arange(int(d * SR)) / SR


def env(n, a, d, curve=4.0):
    """Attack (s) then exponential-ish decay over the rest of n samples."""
    na = max(1, int(a * SR))
    e = np.ones(n)
    e[:na] = np.linspace(0, 1, na)
    rest = n - na
    if rest > 0:
        e[na:] = np.exp(-np.linspace(0, curve, rest)) * (1 - np.linspace(0, 1, rest) ** 8)
    return e


def lp(x, f, order=2):
    return sosfilt(butter(order, min(f, SR / 2 - 100), "low", fs=SR, output="sos"), x)


def hp(x, f, order=2):
    return sosfilt(butter(order, f, "high", fs=SR, output="sos"), x)


def bp(x, f1, f2, order=2):
    return sosfilt(butter(order, [f1, min(f2, SR / 2 - 100)], "band", fs=SR, output="sos"), x)


def glide(f0, f1, n, shape="exp"):
    k = np.linspace(0, 1, n)
    f = f0 * (f1 / f0) ** k if shape == "exp" else f0 + (f1 - f0) * k
    return 2 * np.pi * np.cumsum(f) / SR


def sweep_filter(x, f0, f1, q=1.2, shape=None):
    """Band-pass whose centre moves from f0 to f1 (state-variable filter, per sample)."""
    n = len(x)
    fc = f0 * (f1 / f0) ** (np.linspace(0, 1, n) if shape is None else shape)
    g = np.tan(np.pi * np.clip(fc, 20, SR * 0.45) / SR)
    k = 1.0 / q
    a1 = 1 / (1 + g * (g + k)); a2 = g * a1; a3 = g * a2
    y = np.zeros(n); ic1 = ic2 = 0.0
    xl = x.tolist(); a1l, a2l, a3l = a1.tolist(), a2.tolist(), a3.tolist()
    out = [0.0] * n
    for i in range(n):
        v3 = xl[i] - ic2
        v1 = a1l[i] * ic1 + a2l[i] * v3
        v2 = ic2 + a2l[i] * ic1 + a3l[i] * v3
        ic1 = 2 * v1 - ic1; ic2 = 2 * v2 - ic2
        out[i] = v1
    return np.array(out)


def pan(m, p):
    """Mono -> stereo with constant-power pan p in [-1, 1] (array or scalar)."""
    p = np.clip(p, -1, 1)
    a = (p + 1) * np.pi / 4
    return np.stack([m * np.cos(a), m * np.sin(a)], 1)


def norm(x, peak=1.0):
    m = np.max(np.abs(x)) + 1e-12
    return x / m * peak


def place(bus, sig, t):
    i = int(round(t * SR))
    if i < 0:
        sig, i = sig[-i:], 0
    j = min(len(bus), i + len(sig))
    if j > i:
        bus[i:j] += sig[: j - i]


# ======================================================================
# Sound effects
# ======================================================================
def sfx(kind, r, arg=None):
    u = lambda a, b: a + (b - a) * r.random()
    if kind in ("whoosh", "whoosh_out", "whoosh_rev", "transition", "zoom"):
        d = {"whoosh": u(0.38, 0.62), "whoosh_out": u(0.24, 0.36), "whoosh_rev": u(0.5, 0.7),
             "transition": u(0.65, 0.85), "zoom": u(0.3, 0.45)}[kind]
        n = int(d * SR)
        noise = r.standard_normal(n)
        lo, hi = u(250, 700), u(2200, 5200)
        f0, f1 = (lo, hi) if kind != "whoosh_out" else (hi, lo)
        peak = u(0.45, 0.75) if kind != "whoosh_out" else u(0.15, 0.3)
        k = np.linspace(0, 1, n)
        shp = np.where(k < peak, np.clip(k / peak, 0, 1) ** 0.5 * 0.5, 0.5 + 0.5 * np.clip((k - peak) / (1 - peak), 0, 1) ** 1.5)
        y = sweep_filter(noise, f0, f1, q=u(0.9, 2.2), shape=shp)
        e = np.where(k < peak, (k / peak) ** u(1.5, 2.5), np.clip(1 - (k - peak) / (1 - peak), 0, 1) ** u(1.6, 3.0))
        y = y * e
        if kind == "whoosh_rev":
            y = y * np.linspace(0.3, 1, n) ** 2
        if kind in ("transition", "zoom"):
            tone = np.sin(glide(u(70, 110), u(160, 260), n) if kind == "transition" else glide(u(180, 260), u(700, 1100), n))
            y = y + tone * e * (0.5 if kind == "transition" else 0.12)
        side = r.choice([-1, 1])
        return pan(norm(y, 0.8), side * np.linspace(-u(0.3, 0.7), u(0.3, 0.7), n))
    if kind == "swish":
        n = int(u(0.12, 0.2) * SR)
        y = hp(r.standard_normal(n), u(2500, 4500)) * env(n, u(0.03, 0.06), 0, 5)
        return pan(norm(y, 0.6), u(-0.4, 0.4))
    if kind == "pop":
        n = int(u(0.07, 0.11) * SR)
        f0 = u(420, 900)
        y = np.sin(glide(f0, f0 * u(0.45, 0.65), n)) * env(n, 0.002, 0, u(5, 8))
        y += hp(r.standard_normal(n), 3000) * env(n, 0.001, 0, 30) * 0.3
        return pan(norm(y, 0.8), u(-0.25, 0.25))
    if kind in ("ping", "ding", "chime", "notify", "success", "meter"):
        notes = {"ping": [1], "ding": [1], "chime": [1, 1.5], "notify": [1, 4 / 3],
                 "success": [1, 1.25, 1.5, 2], "meter": [1, 9 / 8, 5 / 4, 3 / 2]}[kind]
        base = r.choice([659.3, 739.9, 880.0, 987.8, 1108.7, 1318.5]) * (0.5 if kind == "notify" else 1)
        step = {"ping": 0, "ding": 0, "chime": 0.09, "notify": 0.13, "success": 0.075, "meter": 0.06}[kind]
        out = np.zeros(int(1.4 * SR))
        ratio = r.choice([2.0, 3.5, 1.41, 2.76])
        for i, m in enumerate(notes):
            f = base * m
            d = u(0.5, 0.9) if kind != "meter" else u(0.15, 0.25)
            n = int(d * SR); tt = t_(d)
            mod = np.sin(2 * np.pi * f * ratio * tt) * u(1.0, 2.5) * np.exp(-tt * u(6, 12))
            y = np.sin(2 * np.pi * f * tt + mod) * env(n, 0.003, 0, u(4, 7))
            place(out, y * (0.85 ** i), i * step)
        return pan(norm(out, 0.7), u(-0.3, 0.3))
    if kind == "type":
        d = arg or 1.0
        out = np.zeros(int((d + 0.1) * SR))
        tt = 0.0
        while tt < d:
            n = int(0.012 * SR)
            c = bp(r.standard_normal(n), u(1800, 3200), u(4000, 6500)) * env(n, 0.0005, 0, 9) * u(0.4, 1)
            place(out, c, tt)
            tt += u(0.045, 0.11)
        return pan(norm(out, 0.5), u(-0.2, 0.2))
    if kind == "marker":
        n = int(u(0.28, 0.4) * SR)
        y = bp(r.standard_normal(n), u(2000, 2800), u(4500, 6000))
        y *= (0.6 + 0.4 * np.sin(2 * np.pi * u(28, 45) * np.arange(n) / SR)) * env(n, 0.04, 0, 3)
        return pan(norm(y, 0.5), np.linspace(-0.4, 0.4, n))
    if kind in ("hit", "boom", "crash", "punch", "thump", "stamp"):
        d = {"hit": 1.6, "boom": 2.4, "crash": 1.8, "punch": 0.45, "thump": 0.3, "stamp": 0.6}[kind]
        n = int(d * SR); tt = t_(d)
        lo_f = {"hit": u(48, 60), "boom": u(38, 48), "crash": u(55, 70), "punch": u(70, 95), "thump": u(110, 160), "stamp": u(90, 120)}[kind]
        body = np.sin(glide(lo_f * 2.4, lo_f, n)) * np.exp(-tt * {"hit": 2.2, "boom": 1.6, "crash": 3, "punch": 9, "thump": 16, "stamp": 10}[kind])
        nz = r.standard_normal(n)
        crunch = lp(nz, {"hit": 900, "boom": 1400, "crash": 6000, "punch": 3000, "thump": 1500, "stamp": 2500}[kind]) \
            * np.exp(-tt * {"hit": 6, "boom": 2.2, "crash": 2.6, "punch": 22, "thump": 30, "stamp": 14}[kind])
        y = body * 1.0 + crunch * {"hit": 0.6, "boom": 1.2, "crash": 0.9, "punch": 0.5, "thump": 0.4, "stamp": 0.8}[kind]
        if kind == "boom":  # debris crackle
            for _ in range(int(u(25, 40))):
                k = int(r.random() ** 1.6 * n * 0.8); m = int(0.004 * SR)
                y[k:k + m] += r.standard_normal(min(m, n - k)) * u(0.1, 0.35) * np.exp(-k / n * 3)
        if kind == "crash":  # metallic partials
            for p in r.uniform(400, 3200, 6):
                y += np.sin(2 * np.pi * p * tt) * np.exp(-tt * u(3, 7)) * 0.08
        y = np.tanh(y * {"punch": 2.2, "boom": 1.3}.get(kind, 1.0))
        return pan(norm(y, 0.9), u(-0.15, 0.15)) if kind not in ("boom", "hit") else \
            np.stack([norm(y, 0.9), norm(np.roll(y, int(0.011 * SR)), 0.9)], 1)
    if kind == "riser":
        d = 0.55; n = int(d * SR)
        y = sweep_filter(r.standard_normal(n), u(300, 500), u(5000, 8000), q=1.5) * np.linspace(0, 1, n) ** 2
        y += np.sin(glide(u(150, 220), u(900, 1300), n)) * np.linspace(0, 1, n) ** 3 * 0.3
        return pan(norm(y, 0.7), 0)
    if kind in ("bloop", "lock", "blip"):
        if kind == "bloop":
            n = int(u(0.09, 0.14) * SR)
            f0 = u(260, 420); y = np.sin(glide(f0, f0 * u(1.8, 2.6), n)) * env(n, 0.004, 0, 4)
        else:
            reps = 2 if kind == "lock" else 1
            out = np.zeros(int(0.3 * SR)); f = u(1100, 1700)
            for i in range(reps):
                n = int(0.05 * SR)
                s = np.sign(np.sin(2 * np.pi * f * (1 + 0.25 * i) * t_(0.05))) * env(n, 0.002, 0, 5)
                place(out, lp(s, 4000), i * 0.075)
            y = out
        return pan(norm(y, 0.6), u(-0.3, 0.3))
    if kind in ("boing", "boing_down"):
        d = u(0.45, 0.65); n = int(d * SR); tt = t_(d)
        f0 = u(160, 240)
        f = f0 * (1 + 0.35 * np.sin(2 * np.pi * u(9, 13) * tt) * np.exp(-tt * 4))
        f *= np.linspace(1, 1.6, n) if kind == "boing" else np.linspace(1.4, 0.55, n)
        y = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.005, 0, 3.5)
        y = np.tanh(y * 1.5)
        return pan(norm(y, 0.7), u(-0.2, 0.2))
    if kind == "click":
        out = np.zeros(int(0.06 * SR))
        for i, g in enumerate((1.0, 0.6)):
            n = int(0.006 * SR)
            place(out, bp(r.standard_normal(n), u(2000, 3000), 8000) * env(n, 0.0003, 0, 10) * g, i * u(0.03, 0.045))
        return pan(norm(out, 0.6), u(-0.2, 0.2))
    if kind == "sizzle":
        d = u(0.7, 0.9); n = int(d * SR)
        y = hp(r.standard_normal(n), u(3500, 5000)) * 0.3
        for _ in range(int(u(40, 70))):
            k = r.integers(0, n - 300)
            y[k:k + 200] += r.standard_normal(200) * u(0.3, 0.9)
        y *= env(n, 0.08, 0, 2.5)
        return pan(norm(y, 0.5), u(-0.3, 0.3))
    if kind == "scratch":
        n = int(u(0.18, 0.26) * SR)
        y = sweep_filter(r.standard_normal(n), u(4000, 5500), u(900, 1400), q=3) * env(n, 0.01, 0, 3)
        return pan(norm(y, 0.6), u(-0.3, 0.3))
    if kind == "glitch":
        d = 0.42; out = np.zeros(int(d * SR)); tt = 0.0
        while tt < d - 0.02:
            seg = u(0.015, 0.045); n = int(seg * SR)
            f = r.choice([110, 220, 440, 880, 1760]) * u(0.97, 1.03)
            s = np.sign(np.sin(2 * np.pi * f * t_(seg))) * u(0.3, 1)
            if r.random() < 0.4:
                s = r.standard_normal(n) * 0.8
            s = np.round(s * 4) / 4  # bit-crush
            place(out, s * np.hanning(n) ** 0.2, tt)
            tt += seg + (u(0.0, 0.02) if r.random() < 0.3 else 0)
        return pan(norm(lp(out, 7000), 0.6), u(-0.5, 0.5))
    raise ValueError(kind)


