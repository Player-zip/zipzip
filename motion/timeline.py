# Single source of truth for the edit's timing. Every visual beat and sound cue is
# written here in SOURCE time (the raw recording, where the words were measured)
# and mapped to the edited timeline through the EDL, so a change to the cuts moves
# graphics, zooms and sound effects together.
#   -> hf/data.js        (camera, shakes, element times for the HyperFrames comp)
#   -> work/cues.json    (sound-effect cues + music sections for synth.py)
import json
from edl import segments, to_out

SEGS = segments()
TOTAL = SEGS[-1]["out_out"]
CUTS = [s["out_in"] for s in SEGS]


def o(src):
    t = to_out(src)
    assert t is not None, f"source time {src} falls in a cut"
    return t


def at_cut(i):
    return SEGS[i]["out_in"]


# Element times (edited timeline, seconds). Words come from the transcript line
# starts, which line up with the speech onsets measured in the audio.
T = {
    "hook_in": 0.25, "hook_swap": o(4.85), "hook_out": o(8.80),
    "mal": o(9.35), "macacao": o(10.30), "ring_out": at_cut(2) - 0.12,
    "gw_zoom": o(16.85), "gw_mark": o(17.05), "gw_card": o(17.15), "gw_sub": o(17.75),
    "gw_out": at_cut(4) - 0.15,
    "bars_in": at_cut(5), "chap_in": o(24.92), "chap_swap": o(25.90), "chap_out": o(28.35),
    "me_in": o(30.85), "me_out": at_cut(7) - 0.08,
    "mk_in": o(34.80), "mk_out": at_cut(8) - 0.1,
    "ban_in": o(38.45), "ban_tag": o(39.05), "ban_hit": o(40.37), "ban_out": at_cut(9) - 0.1,
    "qi_in": o(43.83), "boom": o(44.80), "qi_out": o(45.75),
    "rate_in": o(49.30), "rate_fill": o(49.80), "rate_out": o(50.70), "crash": o(50.83),
    "bars_out": at_cut(14),
    "waves_in": o(64.16), "waves_out": o(66.60),
    "toast_in": o(76.90), "toast_out": o(78.55),
    "cut_in": o(78.70), "cut_click": o(79.95), "cut_stamp": o(80.10), "cut_fire": o(82.10),
    "cut_out": at_cut(18),
    "zr_in": o(84.40), "zr_strike": o(85.40), "zr_out": o(86.95),
    "gren_in": o(90.33), "gren_out": at_cut(21) - 0.1,
    "glitch": at_cut(23), "ops_in": o(102.25), "ops_out": o(103.60),
    "barr_in": o(104.29), "barr_out": at_cut(24) - 0.1,
    "hp_in": o(105.25) if to_out(105.25) else at_cut(24), "hp_hit": o(106.12), "hp_out": o(106.33),
    "sai1": o(106.50), "sai2": o(107.00), "sai_out": o(107.85),
    "nb_in": o(109.00), "nb_word": o(109.75), "nb_out": o(110.95),
    "outro_in": o(111.20), "outro_click": o(112.25), "fade": TOTAL - 0.5,
}

# Camera: (time, scale, focus_x, focus_y, duration, ease). duration 0 = hard set
# (used on every edit cut, so jump cuts read as deliberate punch-ins).
CAM = [
    (0.00, 1.08, 960, 540, 0, ""), (0.00, 1.12, 960, 520, CUTS[1] - 0.02, "sine.inOut"),  # crops the browser banner
    (CUTS[1], 1.12, 960, 420, 0, ""), (CUTS[1] + 0.01, 1.18, 960, 420, 3.6, "sine.inOut"),
    (T["mal"] - 0.35, 1.45, 962, 380, 0.5, "power3.inOut"),
    (T["macacao"] - 0.30, 1.70, 1415, 405, 0.45, "power3.inOut"),
    (CUTS[2], 1.00, 960, 540, 0, ""), (CUTS[2] + 0.01, 1.07, 900, 500, 2.3, "sine.inOut"),
    (CUTS[3], 1.15, 842, 480, 0, ""), (T["gw_zoom"], 2.05, 842, 505, 0.55, "power3.inOut"),
    (CUTS[4], 1.00, 960, 540, 0, ""),
    (CUTS[5], 1.00, 960, 540, 0, ""), (o(25.05), 1.08, 960, 540, 3.4, "sine.inOut"),
    (CUTS[6], 1.30, 742, 420, 0, ""),
    (CUTS[7], 1.00, 960, 540, 0, ""), (T["mk_in"] - 0.30, 1.18, 1100, 470, 0.55, "power3.inOut"),
    (CUTS[8], 1.00, 960, 500, 0, ""), (T["ban_in"] - 0.1, 1.15, 960, 500, 0.5, "power3.out"),
    (T["ban_in"] + 0.45, 1.20, 960, 500, 1.4, "sine.inOut"),
    (T["ban_hit"], 1.10, 1180, 480, 0, ""),
    (CUTS[9], 1.00, 960, 540, 0, ""), (T["qi_in"], 1.08, 960, 540, 0.3, "power2.out"),
    (T["boom"], 1.32, 960, 540, 0, ""), (T["boom"] + 0.01, 1.04, 960, 540, 1.0, "expo.out"),
    (CUTS[10], 1.00, 960, 540, 0, ""), (CUTS[10] + 0.01, 1.05, 960, 540, 2.0, "sine.inOut"),
    (T["crash"], 1.22, 960, 540, 0, ""), (T["crash"] + 0.01, 1.00, 960, 540, 0.75, "expo.out"),
    (CUTS[11], 1.10, 960, 540, 0, ""), (CUTS[12], 1.00, 960, 540, 0, ""),
    (CUTS[13], 1.15, 960, 560, 0, ""),
    (CUTS[14], 1.00, 960, 540, 0, ""), (T["waves_in"] - 0.2, 1.06, 960, 560, 1.2, "sine.inOut"),
    (CUTS[15], 1.10, 960, 600, 0, ""),
    (CUTS[16], 1.00, 960, 540, 0, ""), (CUTS[16] + 0.01, 1.08, 960, 560, 2.6, "sine.inOut"),
    (CUTS[18], 1.00, 960, 540, 0, ""), (T["zr_in"], 1.05, 960, 560, 2.0, "sine.inOut"),
    (CUTS[19], 1.12, 960, 600, 0, ""), (CUTS[20], 1.00, 960, 540, 0, ""),
    (CUTS[21], 1.08, 960, 600, 0, ""), (CUTS[22], 1.00, 960, 540, 0, ""),
    (CUTS[23], 1.20, 960, 600, 0, ""), (CUTS[23] + 0.4, 1.12, 960, 600, 2.4, "sine.inOut"),
    (T["barr_in"] - 0.35, 1.00, 960, 540, 0.3, "power3.inOut"),
    (CUTS[24], 1.10, 960, 600, 0, ""),
    (T["sai1"], 1.22, 960, 580, 0, ""), (T["sai1"] + 0.01, 1.14, 960, 580, 0.45, "expo.out"),
    (T["sai2"], 1.30, 960, 580, 0, ""), (T["sai2"] + 0.01, 1.10, 960, 580, 0.6, "expo.out"),
    (CUTS[25], 1.00, 960, 540, 0, ""), (CUTS[25] + 0.01, 1.10, 960, 560, 1.6, "sine.inOut"),
    (T["outro_in"] - 0.3, 1.00, 960, 540, 1.2, "power2.inOut"),
]

SHAKES = [  # (time, amplitude px, duration, seed)
    (T["boom"], 24, 0.75, 11), (T["crash"], 16, 0.5, 23), (T["glitch"], 12, 0.35, 37),
    (T["sai1"], 11, 0.3, 41), (T["sai2"], 14, 0.35, 53), (T["hp_hit"], 6, 0.2, 61),
]

# Sound-effect cues. Every cue gets its own seed so no two hits sound alike.
SFX = [
    (T["hook_in"], "whoosh", 0.55), (T["hook_in"] + 0.15, "type", 0.30, 1.6),
    (T["hook_swap"], "pop", 0.55), (T["hook_swap"] + 0.12, "swish", 0.35),
    (T["hook_out"], "whoosh_out", 0.35),
    (T["mal"] - 0.35, "zoom", 0.45), (T["mal"], "ping", 0.45),
    (T["macacao"] - 0.30, "zoom", 0.45), (T["macacao"], "ping", 0.5),
    (T["gw_zoom"], "zoom", 0.5), (T["gw_mark"], "marker", 0.4), (T["gw_card"], "pop", 0.45),
    (T["gw_sub"], "type", 0.22, 0.6), (T["gw_out"], "whoosh_out", 0.3),
    (T["bars_in"], "hit", 0.55), (T["chap_in"], "whoosh", 0.5), (T["chap_in"] + 0.05, "ping", 0.3),
    (T["chap_swap"], "click", 0.4), (T["chap_out"], "whoosh_out", 0.32),
    (T["me_in"], "swish", 0.45), (T["me_in"] + 0.18, "pop", 0.4),
    (T["mk_in"], "bloop", 0.42), (T["mk_in"] + 0.22, "bloop", 0.42), (T["mk_in"] + 0.45, "pop", 0.35),
    (T["ban_in"], "lock", 0.45), (T["ban_tag"], "blip", 0.4), (T["ban_hit"], "boing", 0.5),
    (T["qi_in"], "punch", 0.6), (T["boom"] - 0.5, "riser", 0.4), (T["boom"], "boom", 0.85),
    (T["qi_out"], "whoosh_out", 0.3),
    (T["rate_in"], "whoosh", 0.4), (T["rate_fill"], "meter", 0.35), (T["crash"], "crash", 0.7),
    (T["rate_out"], "whoosh_out", 0.25),
    (T["bars_out"], "whoosh_rev", 0.5), (T["waves_in"], "whoosh", 0.45), (T["waves_in"] + 0.1, "chime", 0.35),
    (T["waves_out"], "whoosh_out", 0.3),
    (T["toast_in"], "notify", 0.5), (T["toast_out"], "whoosh_out", 0.25),
    (T["cut_in"], "transition", 0.6), (T["cut_click"], "click", 0.55), (T["cut_stamp"], "stamp", 0.75),
    (T["cut_fire"], "sizzle", 0.45), (T["cut_out"], "transition", 0.5),
    (T["zr_in"], "whoosh", 0.4), (T["zr_strike"], "scratch", 0.45), (T["zr_out"], "whoosh_out", 0.28),
    (T["gren_in"], "ping", 0.45),
    (T["glitch"], "glitch", 0.6), (T["ops_in"], "boing_down", 0.5), (T["ops_out"], "whoosh_out", 0.25),
    (T["barr_in"], "ping", 0.45), (T["hp_in"], "whoosh", 0.35),
    (T["hp_hit"], "thump", 0.45), (T["hp_hit"] + 0.10, "thump", 0.4), (T["hp_hit"] + 0.20, "thump", 0.35),
    (T["sai1"], "punch", 0.55), (T["sai2"], "punch", 0.6), (T["sai_out"], "whoosh_out", 0.28),
    (T["nb_in"], "pop", 0.45), (T["nb_word"], "ding", 0.4), (T["nb_out"], "whoosh_out", 0.28),
    (T["outro_in"], "whoosh", 0.5), (T["outro_click"], "click", 0.5), (T["outro_click"] + 0.08, "success", 0.45),
]

# Music arrangement (edited timeline): section starts.
MUSIC = {
    "bpm": 112, "total": TOTAL,
    "sections": [
        (0.0, "intro"), (T["bars_in"], "cinematic"), (T["boom"] - 0.55, "riser_gap"),
        (T["boom"], "halftime"), (T["bars_out"], "groove"), (T["cut_in"], "muffled"),
        (T["cut_out"], "groove2"), (T["glitch"], "stutter"), (T["glitch"] + 0.4, "groove2"),
        (T["outro_in"], "outro"),
    ],
}

if __name__ == "__main__":
    data = {"total": TOTAL, "T": T, "cam": CAM, "shakes": SHAKES}
    open("hf/data.js", "w").write("window.D = " + json.dumps(data, indent=1) + ";\n")
    cues = [{"t": round(c[0], 3), "kind": c[1], "gain": c[2], "arg": c[3] if len(c) > 3 else None,
             "seed": i * 7919 + 17} for i, c in enumerate(SFX)]
    json.dump({"total": TOTAL, "sfx": cues, "music": MUSIC}, open("work/cues.json", "w"), indent=1)
    for k, v in T.items():
        print(f"{k:12s} {v:7.2f}")
