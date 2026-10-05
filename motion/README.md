# Edição da gameplay "Sobreviva a um milhão de macacos"

Tudo é gerado por código: cortes com FFmpeg, motion graphics com HyperFrames (HTML + GSAP),
música e efeitos sintetizados em Python. Acento visual em verde (`--g: #22d46a`).

## Pipeline

```bash
cp <video bruto>.mp4 work/source.mp4
python3 edl.py        # lista de cortes (silêncios, respirações, erros de gravação)
python3 cut.py        # work/base.mp4 (sem a barra do sistema, 1920x1080) + work/voice.wav (voz +36 dB, limpa, -16 LUFS)
python3 timeline.py   # tempos de cada elemento/zoom/efeito -> hf/data.js e work/cues.json
cp work/base.mp4 hf/assets/base.mp4
(cd hf && HYPERFRAMES_BROWSER_PATH=<chromium headless_shell> npx --prefix .. hyperframes render -o ../work/visual.mp4 -q high)
python3 synth.py      # work/music.wav + work/sfx.wav
python3 mix.py        # mixagem com ducking; falha se música/efeitos passarem da voz
python3 finalize.py   # out/gameplay_editada.mp4 (-14 LUFS)
```

`NODE_PATH=./node_modules node tools/check_seek.js` confere que a composição renderiza igual
com vários workers (busca em saltos vs. sequencial).

Todos os tempos são escritos em tempo do vídeo bruto em `timeline.py` e convertidos pela EDL,
então mudar um corte em `edl.py` move junto os gráficos, zooms e efeitos sonoros.
