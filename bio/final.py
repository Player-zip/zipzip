# Composite: cut footage + transparent panels at their edited times + checked mix
# (from ../motion/mix.py) normalised to -14 LUFS. Kept under 30 MB so it can be sent in chat.
import json, subprocess

T = json.load(open("work/times.json"))
starts = [T["hist"] - 0.15, T["c_in"] - 0.15, T["cta_in"] - 0.15]
clips = ["out/01-formacao.mov", "out/02-credenciais.mov", "out/03-siga.mov"]

m = subprocess.run(["ffmpeg", "-hide_banner", "-i", "work/stem_mix.wav", "-af", "ebur128", "-f", "null", "-"],
                   capture_output=True, text=True).stderr
gain = -14.0 - float(m[m.rindex("I:"):].split()[1])

inputs, chain, last = ["-i", "work/base.mp4"], [], "[0:v]"
for k, (c, s) in enumerate(zip(clips, starts), 1):
    inputs += ["-itsoffset", f"{s:.3f}", "-i", c]
    chain.append(f"{last}[{k}:v]overlay=eof_action=pass:format=auto[v{k}]")
    last = f"[v{k}]"
inputs += ["-i", "work/stem_mix.wav"]
chain.append(f"[4:a]volume={gain:.2f}dB,alimiter=limit=0.89:level=disabled[a]")
subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain),
                "-map", last, "-map", "[a]", "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-maxrate", "3M",
                "-bufsize", "6M", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "out/autobiografia_editada.mp4"], check=True)
print(f"gain {gain:+.1f} dB")
