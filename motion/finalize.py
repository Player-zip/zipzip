# Final master: rendered visuals + mixed audio, loudness set for YouTube (-14 LUFS,
# true peak <= -1 dBTP). The gain is the same for every stem, so the voice/music/SFX
# balance checked in mix.py is preserved.
import subprocess, json

m = subprocess.run(["ffmpeg", "-hide_banner", "-i", "work/stem_mix.wav", "-af",
                    "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                   capture_output=True, text=True).stderr
j = json.loads(m[m.rindex("{"):m.rindex("}") + 1])
gain = -14.0 - float(j["input_i"])
print(f"mix {j['input_i']} LUFS -> -14 (gain {gain:+.1f} dB)")

subprocess.run([
    "ffmpeg", "-v", "error", "-y", "-i", "work/visual.mp4", "-i", "work/stem_mix.wav",
    "-filter_complex", f"[1:a]volume={gain:.2f}dB,alimiter=limit=0.89:attack=2:release=60:level=disabled,"
                       "aresample=48000[a]",
    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
    "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
    "-c:a", "aac", "-b:a", "256k", "-shortest", "out/gameplay_editada.mp4"], check=True)

r = subprocess.run(["ffmpeg", "-hide_banner", "-i", "out/gameplay_editada.mp4", "-af", "ebur128=peak=true",
                    "-f", "null", "-"], capture_output=True, text=True).stderr
s = r[r.rindex("Summary:"):]
print(" ".join(l.strip() for l in s.splitlines() if l.strip().startswith(("I:", "Peak:"))))
