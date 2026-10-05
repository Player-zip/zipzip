# Reprodução dos achados de ANALISE_PROBLEMAS.md.
# Rodar a partir da raiz de peixao-hunter-main, com as dependências instaladas.
import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "src")
import pandas as pd
from peixao.alpha_v22 import alpha_score_v22
from peixao.selective_alpha import selective_alpha_score
from peixao.evidence_ledger import record_metrics, canonical_metrics_for_wallet

base = dict(address="0xabc", chain="base", nansen_evidence=True, closed_positions=25,
            realized_profit_30d=4000.0, repeatability_score=0.7, new_positions_per_week=3.0,
            median_pnl_per_token=150.0)

print("== 1) Nansen ROI em pontos percentuais (12.5 = 12,5%) vs razão (0.125)")
for roi in (12.5, 0.125):
    r = pd.Series({**base, "win_rate": 0.66, "gmgn_winrate_30d": 0.66, "realized_roi_30d": roi})
    a = alpha_score_v22(r); s = selective_alpha_score(r)
    print(f"  roi={roi:>6}: alpha22_pnl_roi={a['alpha22_pnl_roi']:6.2f} alpha22={a['alpha22_score']:6.2f} selective_alpha={s['selective_alpha_score']:6.2f} tier={s['selective_alpha_tier']}")

try:
    from peixao.nansen_evm import _normalized_metrics  # caminho real: adaptador -> score
    m = _normalized_metrics({"win_rate": 0.66, "realized_pnl_percent": 12.5}, [], chain="base")
    print(f"  via adaptador Nansen (realized_pnl_percent=12.5): realized_roi_30d={m['realized_roi_30d']}")
except Exception as exc:  # versão original
    print("  via adaptador Nansen:", type(exc).__name__)

print("== 2) Win rate em % (55 = 55%) no alpha_v22 vs selective")
r = pd.Series({**base, "win_rate": 55.0, "gmgn_winrate_30d": 55.0, "realized_roi_30d": 0.125})
a = alpha_score_v22(r); s = selective_alpha_score(r)
print(f"  alpha22_gate={a['alpha22_gate_status']} wilson={a['alpha22_winrate_wilson_lb']} | selective_win_rate={s['selective_win_rate']} | final={s['selective_alpha_score']} tier={s['selective_alpha_tier']} deep={s['selective_deep_dive_candidate']}")

print("== 3) Ledger: dado NANSEN antigo vence dado BIRDEYE recente")
db = Path(tempfile.mkdtemp()) / "ev.sqlite3"
old = "2025-01-01T00:00:00Z"
record_metrics(db, chain="solana", address="W1", provider="NANSEN", metrics={"win_rate": 0.90}, observed_at=old)
record_metrics(db, chain="solana", address="W1", provider="BIRDEYE", metrics={"win_rate": 0.30})
print("  canonical win_rate =", canonical_metrics_for_wallet(db, "solana", "W1")["win_rate"], "(Nansen de 2025-01-01 = 0.9; Birdeye de hoje = 0.3)")

print("== 4) Ledger cresce a cada ciclo quando observed_at muda (updated_at = now)")
for i in range(3):
    record_metrics(db, chain="base", address="0xdef", provider="INLINE", metrics={"win_rate": 0.7, "realized_profit_30d": 10}, observed_at=f"2026-10-05T0{i}:00:00Z")
import sqlite3
print("  linhas para 1 wallet após 3 ciclos:", sqlite3.connect(db).execute("select count(*) from wallet_evidence where address='0xdef'").fetchone()[0])
