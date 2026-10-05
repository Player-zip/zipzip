# Applies the EDL: cuts the raw recording into work/base.mp4 (picture only,
# GNOME top bar cropped off, 1920x1080) and work/voice.wav (cleaned, boosted voice).
import subprocess, json
from edl import segments

SRC = "work/source.mp4"
segs = segments()
FADE = 0.012  # tiny audio fades at every cut so joins never click

v, a = [], []
for i, s in enumerate(segs):
    dur = s["src_out"] - s["src_in"]
    freeze = s["video_in"] - s["src_in"]
    vf = f'[0:v]trim=start={s["video_in"]}:end={s["src_out"]},setpts=PTS-STARTPTS'
    if freeze > 0:
        vf += f",tpad=start_duration={freeze:.3f}:start_mode=clone"
    v.append(vf + f"[v{i}]")
    a.append(f'[1:a]atrim=start={s["src_in"]}:end={s["src_out"]},asetpts=PTS-STARTPTS,'
             f'afade=t=in:d={FADE},afade=t=out:st={dur - FADE:.3f}:d={FADE}[a{i}]')

n = len(segs)
# Voice chain: the mic was recorded ~40 dB too quiet. Boost, clean rumble and
# hiss, then even out the level with a gentle compressor.
voice = ("pan=mono|c0=0.5*c0+0.5*c1,highpass=f=85,lowpass=f=11000,volume=36dB,"
         "afftdn=nr=10:nf=-44:tn=1,"
         "acompressor=threshold=-24dB:ratio=3:attack=6:release=140:makeup=3dB")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-af", voice, "-ar", "48000",
                "-c:a", "pcm_s24le", "work/voice_full.wav"], check=True)
graph = ";".join(v + a +
                 ["".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,"
                  "crop=1202:676:39:44,scale=1920:1080:flags=lanczos,setsar=1,fps=30[vout]",
                  "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[aout]"])

subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-i", "work/voice_full.wav", "-filter_complex", graph,
                "-map", "[vout]", "-c:v", "libx264", "-preset", "medium", "-crf", "14",
                "-pix_fmt", "yuv420p", "-an", "work/base.mp4",
                "-map", "[aout]", "-ar", "48000", "-c:a", "pcm_s24le", "work/voice_raw.wav"],
               check=True)

# Two-pass loudness normalisation of the voice to -16 LUFS (the anchor of the mix).
m = subprocess.run(["ffmpeg", "-hide_banner", "-i", "work/voice_raw.wav", "-af",
                    "loudnorm=I=-16:TP=-1.5:LRA=9:print_format=json", "-f", "null", "-"],
                   capture_output=True, text=True).stderr
j = json.loads(m[m.rindex("{"):m.rindex("}") + 1])
ln = (f"loudnorm=I=-16:TP=-1.5:LRA=9:measured_I={j['input_i']}:measured_TP={j['input_tp']}:"
      f"measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "work/voice_raw.wav", "-af", ln,
                "-ar", "48000", "-c:a", "pcm_s24le", "work/voice.wav"], check=True)
print("voice measured:", j["input_i"], "LUFS ->", -16)
