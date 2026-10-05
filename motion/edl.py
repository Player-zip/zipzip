# Edit decision list: which parts of the raw recording survive, and the
# mapping from source time to edited-timeline time used by every later step.
import json, sys

# (src_in, src_out, note[, video_in]) — video_in lets the picture start later
# than the sound (the gap is filled by freezing the first good frame).
SEGMENTS = [
    (1.60, 3.92, "Bom, então bora ver se você tem que"),
    (4.75, 10.98, "matar o macaco... tem um macacão aqui"),
    (13.20, 15.58, "Vamos ver, vamos ver. Vou entrar aqui. Aí você entra, ó"),
    (16.00, 18.47, "Até botaram assim, Gorila Warfare"),
    (19.10, 20.08, "Tá certo. Espere."),
    # 20.08-23.85: tela de carregamento (corte)
    (23.85, 28.60, "Beleza. A introdução. Ah, eu tenho uma cutscene de macaco"),
    (30.70, 31.60, "Ó eu aqui, ó"),
    (32.95, 36.10, "Nossa, a cutscene tá bem feita. Tem uns macaquinhos ali"),
    (37.60, 41.40, "Eles jogaram uma banana, velho"),
    (43.60, 46.30, "Que isso, (explosão)"),
    (48.70, 51.60, "pô? Até agora achei interessante"),
    (53.75, 55.00, "(helicóptero caído)"),
    (56.85, 57.60, "Ouvido"),
    (58.95, 59.75, "é armado"),
    (61.30, 65.35, "Tá OK. Isso foi meio esquisito. Tá. Sobreviva às ondas"),
    (66.05, 67.60, "Ah, mas eu tenho água. Não."),
    # 67.60-75.95: tempo morto + fragmento "Vê" (corte)
    (75.95, 80.55, "Nossa, se o Ibama ver isso aqui, o Ibama vai querer me banir"),
    (81.30, 82.62, "vai querer me churrascar"),
    (83.08, 87.10, "porque é literalmente um Zombie Rush, só que de macaco. Eu joguei granada hoje"),
    (88.10, 89.25, "cara. É literalmente"),
    (90.20, 91.30, "é aí"),
    (93.15, 96.00, "você pode tocar de..."),
    (96.50, 97.85, "..."),
    # 97.85-101.12: silêncio + tela do sistema aberta sem querer (100.13-101.43)
    (101.12, 105.40, "Nossa. Pera aí, pera aí, que fiz errado. Fiz errado.", 101.47),
    (105.95, 108.00, "Colocar barricada. Ai, os macacos dão"),
    (108.90, 113.85, "dano. Sai macaco, sai macaco. Não vou te dar banana."),
    # 113.85-fim: tela do YouTube/OBS (corte)
]


def segments():
    out, t = [], 0.0
    for s in SEGMENTS:
        a, b, note = s[0], s[1], s[2]
        vin = s[3] if len(s) > 3 else a
        out.append({"src_in": a, "src_out": b, "video_in": vin, "out_in": round(t, 3),
                    "out_out": round(t + b - a, 3), "note": note})
        t += b - a
    return out


def to_out(src):
    """Map a source timestamp to the edited timeline (None if it was cut)."""
    for s in segments():
        if s["src_in"] <= src <= s["src_out"]:
            return round(s["out_in"] + src - s["src_in"], 3)
    return None


if __name__ == "__main__":
    segs = segments()
    json.dump(segs, open("work/edl.json", "w"), ensure_ascii=False, indent=1)
    for s in segs:
        print(f'{s["out_in"]:7.2f}-{s["out_out"]:7.2f}  src {s["src_in"]:6.2f}-{s["src_out"]:6.2f}  {s["note"]}')
    print("total", segs[-1]["out_out"])
