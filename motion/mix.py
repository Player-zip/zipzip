# Mix: the voice is the anchor. Music sits ~14 LU under it and ducks a further
# ~8 dB whenever he speaks; sound effects are kept below the voice too.
# Writes work/mix.wav and checks, window by window, that music+SFX never get
# louder than the voice.
import json, subprocess
import numpy as np
from scipy.io import wavfile
from scipy.ndimage import maximum_filter1d, uniform_filter1d

SR = 48000
VOICE_LUFS, MUSIC_LUFS = -16.0, -27.0
SFX_PEAK_DB = -11.0      # loudest single effect, in dBFS (voice peaks reach -1.5)
DUCK_DB = -7.0


def read(p):
    sr, x = wavfile.read(p)
    assert sr == SR, p
    scale = {np.dtype("int16"): 2 ** 15, np.dtype("int32"): 2 ** 31}.get(x.dtype, 1)  # 24-bit loads as int32
    x = x.astype(np.float64) / scale
    return x if x.ndim == 2 else np.stack([x, x], 1)


def lufs(path):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(out[out.rindex("I:"):].split()[1])


voice = read("work/voice.wav")
music = read("work/music.wav")
sfx = read("work/sfx.wav")
n = min(len(voice), len(music), len(sfx))
voice, music, sfx = voice[:n], music[:n], sfx[:n]

music *= 10 ** ((MUSIC_LUFS - lufs("work/music.wav")) / 20)
sfx *= 10 ** (SFX_PEAK_DB / 20) / (np.max(np.abs(sfx)) + 1e-12)

# Sidechain duck: voice envelope (20 ms RMS), held 250 ms, smoothed into a gain curve.
hop = int(SR * 0.02)
v = voice.mean(1)
rms = np.sqrt(uniform_filter1d(v ** 2, hop))
vdb = 20 * np.log10(rms + 1e-9)
speaking = maximum_filter1d((vdb > -42).astype(float), int(SR * 0.25))
gain_db = DUCK_DB * speaking
# asymmetric smoothing: fast attack (60 ms), slow release (350 ms)
cur = 0.0
ds = 32  # work on a decimated curve, then interpolate back
gd = gain_db[::ds]; gs = np.empty_like(gd)
att, rel = 1 - np.exp(-ds / (SR * 0.06)), 1 - np.exp(-ds / (SR * 0.35))
for i, x in enumerate(gd):
    cur += (x - cur) * (att if x < cur else rel)
    gs[i] = cur
g = np.interp(np.arange(n), np.arange(len(gs)) * ds, gs)
music_d = music * (10 ** (g / 20))[:, None]

# SFX ceiling, tied to the voice: while he talks an effect stays >= 4 dB under what he
# is saying at that moment; in pauses it stays >= 8 dB under his normal speaking level.
def momentary_arr(x):
    wavfile.write("work/_tmp.wav", SR, x.astype(np.float32))
    return momentary("work/_tmp.wav")

def momentary(path):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128=metadata=1,ametadata=print:key=lavfi.r128.M",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return np.array([float(l.split("=")[1]) for l in out.splitlines() if "lavfi.r128.M=" in l])

mv0 = momentary_arr(voice)
v_med = np.median(mv0[mv0 > -30])
for _ in range(4):
    mb0 = momentary_arr(music_d + sfx)
    k = min(len(mv0), len(mb0))
    ceil = np.where(mv0[:k] > -30, np.minimum(mv0[:k] - 4, v_med - 8), v_med - 8)
    over = np.maximum(0, mb0[:k] - ceil)
    if over.max() < 0.2:
        break
    # M value i covers the 400 ms ending at (i+1)*0.1 s: spread the cut over that window.
    red = maximum_filter1d(over, 5, origin=-2) + 0.5 * (over > 0)
    red_t = np.interp(np.arange(n) / SR, (np.arange(k) + 1) * 0.1 - 0.2, red, left=0, right=0)
    red_t = uniform_filter1d(red_t, int(SR * 0.03))
    sfx *= (10 ** (-red_t / 20))[:, None]

mix = voice + music_d + sfx
peak = np.max(np.abs(mix))
print(f"mix peak {20*np.log10(peak):.1f} dBFS")
for name, sig in (("voice", voice), ("bed", music_d + sfx), ("music_ducked", music_d), ("sfx", sfx), ("mix", mix)):
    wavfile.write(f"work/stem_{name}.wav", SR, sig.astype(np.float32))

# ---- verification: momentary loudness (400 ms, EBU R128) per 100 ms step ----
mv, mb = momentary("work/stem_voice.wav"), momentary("work/stem_bed.wav")
k = min(len(mv), len(mb)); mv, mb = mv[:k], mb[:k]
talk = mv > -30
diff = mb[talk] - mv[talk]
print(f"bed max {mb.max():.1f} LUFS-M; ceiling in pauses {np.median(mv[talk]) - 8:.1f}")
print(f"while talking: bed+sfx vs voice  median {np.median(diff):.1f} dB, worst {np.max(diff):.1f} dB "
      f"(over {talk.sum()} windows)")
print(f"loudest bed+sfx moment overall: {mb.max():.1f} LUFS-M vs voice median {np.median(mv[talk]):.1f} LUFS-M")
json.dump({"median_diff": float(np.median(diff)), "worst_diff": float(np.max(diff)),
           "bed_max_M": float(mb.max()), "voice_median_M": float(np.median(mv[talk]))},
          open("work/mix_check.json", "w"), indent=1)
assert np.max(diff) < 0 and mb.max() < np.median(mv[talk]) - 6, "music/SFX louder than the voice somewhere"
print("OK: music and SFX stay under the voice everywhere")
