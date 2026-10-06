# Autobiography edit, step 1: cut breaths/pauses, punch-in on alternate cuts,
# clean the voice, and map the transcript's key words onto the edited timeline.
#   -> work/base.mp4, work/voice.wav, work/times.json, work/cues.json
import json, subprocess
import numpy as np
from scipy.io import wavfile
from scipy.ndimage import uniform_filter1d

SRC = "work/source.mp4"
PRE, POST, MIN_GAP = 0.10, 0.12, 0.40   # keep a little air around words; cut pauses longer than this

subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-ac", "1", "-ar", "16000", "work/a16.wav"], check=True)
sr, x = wavfile.read("work/a16.wav")
x = x / 32768.0
hop = sr // 100
n = len(x) // hop
db = uniform_filter1d(20 * np.log10(np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-9), 5)
on = db > np.percentile(db, 10) + 12
speech, i = [], 0
while i < n:
    if on[i]:
        j = i
        while j < n and on[j]:
            j += 1
        if (j - i) > 8:
            speech.append([i / 100, j / 100])
        i = j
    else:
        i += 1

keep = [[speech[0][0] - PRE, speech[0][1] + POST]]
for a, b in speech[1:]:
    if a - keep[-1][1] + POST < MIN_GAP:
        keep[-1][1] = b + POST
    else:
        keep.append([a - PRE, b + POST])
keep[-1][1] += 0.35

segs, t = [], 0.0
for a, b in keep:
    segs.append({"src_in": round(a, 3), "src_out": round(b, 3), "out_in": round(t, 3)})
    t += b - a
TOTAL = round(t, 3)


def o(src):
    """Source time -> edited time (snaps into the nearest kept segment)."""
    for s in segs:
        if s["src_in"] <= src <= s["src_out"]:
            return round(s["out_in"] + src - s["src_in"], 3)
    nxt = min((s for s in segs if s["src_in"] > src), key=lambda s: s["src_in"], default=segs[-1])
    return nxt["out_in"]


# Picture: alternate 1.0x / 1.08x framing per segment so jump cuts read as deliberate.
FX, FY = 360, 560  # face
v, a = [], []
for k, s in enumerate(segs):
    z = 1.0 if k % 2 == 0 else 1.08
    w, h = int(720 / z) // 2 * 2, int(1280 / z) // 2 * 2
    cx, cy = min(max(FX - w // 2, 0), 720 - w), min(max(FY - h // 2, 0), 1280 - h)
    v.append(f'[0:v]trim=start={s["src_in"]}:end={s["src_out"]},setpts=PTS-STARTPTS,'
             f'crop={w}:{h}:{cx}:{cy},scale=720:1280:flags=lanczos,setsar=1[v{k}]')
    d = s["src_out"] - s["src_in"]
    a.append(f'[1:a]atrim=start={s["src_in"]}:end={s["src_out"]},asetpts=PTS-STARTPTS,'
             f'afade=t=in:d=0.012,afade=t=out:st={d - 0.012:.3f}:d=0.012[a{k}]')
m = len(segs)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-af",
                "pan=mono|c0=0.5*c0+0.5*c1,highpass=f=80,afftdn=nr=6:nf=-50,"
                "acompressor=threshold=-22dB:ratio=2.5:attack=8:release=150:makeup=2dB",
                "-ar", "48000", "-c:a", "pcm_s24le", "work/voice_full.wav"], check=True)
graph = ";".join(v + a + ["".join(f"[v{k}]" for k in range(m)) + f"concat=n={m}:v=1:a=0,fps=30[vo]",
                          "".join(f"[a{k}]" for k in range(m)) + f"concat=n={m}:v=0:a=1[ao]"])
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-i", "work/voice_full.wav", "-filter_complex", graph,
                "-map", "[vo]", "-c:v", "libx264", "-crf", "15", "-preset", "medium", "-pix_fmt", "yuv420p", "work/base.mp4",
                "-map", "[ao]", "-ar", "48000", "-c:a", "pcm_s24le", "work/voice_raw.wav"], check=True)
out = subprocess.run(["ffmpeg", "-hide_banner", "-i", "work/voice_raw.wav", "-af",
                      "loudnorm=I=-16:TP=-1.5:LRA=9:print_format=json", "-f", "null", "-"],
                     capture_output=True, text=True).stderr
j = json.loads(out[out.rindex("{"): out.rindex("}") + 1])
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "work/voice_raw.wav", "-af",
                f"loudnorm=I=-16:TP=-1.5:LRA=9:measured_I={j['input_i']}:measured_TP={j['input_tp']}:"
                f"measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true",
                "-ar", "48000", "-c:a", "pcm_s24le", "work/voice.wav"], check=True)

# Key words (source seconds, from the transcript lines + measured speech onsets).
W = {
    "hist": 3.6, "hist_out": 6.0,
    "f_in": 6.9, "f2008": 11.9, "f2009": 13.9, "f2012": 15.4, "fuel": 16.6, "f10": 19.0,
    "n_in": 21.6, "n2018": 25.0, "n2021": 27.6, "nopar": 29.3, "f_out": 30.0,
    "c_in": 35.8, "usp": 36.6, "uni": 40.9, "cert": 46.97, "acsm": 51.4, "bar": 53.4, "abne": 55.9, "c_out": 59.7,
    "a_in": 60.3, "emag": 63.6, "lon": 64.7, "mar": 65.5, "onl": 66.4, "a_out": 67.2,
    "cta_in": 69.2, "siga": 72.9, "cta_out": 74.6,
}
T = {k: o(s) for k, s in W.items()}
T["total"] = TOTAL
json.dump(T, open("work/times.json", "w"), indent=1)

cues = [(T["hist"], "whoosh", 0.35), (T["f_in"], "whoosh", 0.4), (T["f2008"], "ping", 0.25), (T["f2009"], "pop", 0.25),
        (T["f2012"], "ping", 0.28), (T["f10"], "chime", 0.3), (T["n_in"], "swish", 0.35), (T["n2018"], "pop", 0.25),
        (T["n2021"], "ping", 0.28), (T["f_out"], "whoosh_out", 0.25), (T["c_in"], "whoosh", 0.4),
        (T["usp"], "pop", 0.25), (T["uni"], "pop", 0.25), (T["cert"], "swish", 0.35), (T["acsm"], "ping", 0.25),
        (T["bar"], "ping", 0.25), (T["abne"], "chime", 0.3), (T["c_out"], "whoosh_out", 0.25),
        (T["a_in"], "whoosh", 0.4), (T["lon"], "bloop", 0.25), (T["mar"], "bloop", 0.25), (T["onl"], "bloop", 0.25),
        (T["a_out"], "whoosh_out", 0.25), (T["cta_in"], "whoosh", 0.4), (T["siga"], "click", 0.45),
        (T["siga"] + 0.08, "success", 0.35)]
json.dump({"total": TOTAL, "sfx": [{"t": c[0], "kind": c[1], "gain": c[2], "arg": None, "seed": 3001 + 31 * i}
                                   for i, c in enumerate(cues)]}, open("work/cues.json", "w"), indent=1)
print(f"{len(segs)} segments, {75.07:.1f}s -> {TOTAL:.1f}s")
print(json.dumps(T))
