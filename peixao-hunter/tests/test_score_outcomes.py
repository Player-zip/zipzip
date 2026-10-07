import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd

from peixao.evidence_ledger import record_metrics
from peixao.score_outcomes import SUMMARY_FILE, tier_track_record, track_record_text, update_score_outcomes
from peixao.status_v23 import _backtest_lines

DAY = 86400


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stage(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)


def test_signals_are_immutable_and_evaluated_without_survivorship(tmp_path):
    db = tmp_path / "m.sqlite3"
    stage = tmp_path / "stage.csv"
    out = tmp_path / SUMMARY_FILE
    t0 = int(time.time()) - 40 * DAY
    _stage(stage, [
        {"address": "0x" + "a" * 40, "chain": "base", "selective_alpha_tier": "A", "selective_alpha_score": 74, "win_rate": 0.7},
        {"address": "0x" + "b" * 40, "chain": "base", "selective_alpha_tier": "D", "selective_alpha_score": 30, "win_rate": 0.4},
    ])
    first = update_score_outcomes(stage, db, out, now_epoch=t0)
    assert first["signals_added"] == 2
    # Mesma wallet no mesmo tier não gera sinal novo (sem fotos repetidas).
    assert update_score_outcomes(stage, db, out, now_epoch=t0 + 3600)["signals_added"] == 0

    # 31 dias depois: observação real do Nansen (cache) para a wallet A;
    # nenhuma para a D. A tabela atual nem contém mais as wallets.
    record_metrics(db, chain="base", address="0x" + "a" * 40, provider="NANSEN",
                   metrics={"win_rate": 0.66, "realized_profit_30d": 900.0}, observed_at=_iso(t0 + 31 * DAY),
                   methodology="Nansen 30d profiler PnL")
    # Linha copiada do pipeline não conta como observação.
    record_metrics(db, chain="base", address="0x" + "b" * 40, provider="INLINE",
                   metrics={"win_rate": 0.9}, observed_at=_iso(t0 + 31 * DAY), methodology="pipeline source row")
    _stage(stage, [])
    result = update_score_outcomes(stage, db, out, now_epoch=t0 + 40 * DAY)
    assert result["observed"] >= 1

    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute(
            "SELECT s.tier, o.status FROM signal_outcomes o JOIN score_signals s USING(signal_id) WHERE o.horizon_days=30"
        ).fetchall())
    assert rows == {"A": "OBSERVED", "D": "NO_OBSERVATION"}

    record = tier_track_record(tmp_path, "A")
    assert record["observed"] == 1 and record["coverage"] == 1.0 and record["kept_wr60_rate"] == 1.0
    assert "coletando dados (n=1)" in track_record_text(record, "A")


def test_track_record_text_and_status_lines():
    record = {"observed": 12, "kept_wr60_rate": 0.62, "coverage": 0.4, "lift_vs_control": 1.8}
    text = track_record_text(record, "A")
    assert "62% mantiveram WR≥60%" in text and "n=12" in text and "1.8× o controle" in text
    lines = _backtest_lines([{"horizon_days": 30, "tier": "A", **record, "control_kept_wr60_rate": 0.34}])
    assert any("62% mantiveram" in line for line in lines)
    assert any("Controle (C/D): 34%" in line for line in lines)
    assert "Coletando sinais" in _backtest_lines([])[-1]


def test_status_lines_hide_missing_control_and_render_costs():
    from peixao.status_v23 import _cost_lines

    lines = _backtest_lines([{"horizon_days": 30, "tier": "A", "observed": 0, "control_kept_wr60_rate": float("nan")}])
    assert not any("nan" in line for line in lines)
    cost = _cost_lines({
        "mode": "economy",
        "spend_24h": {"NANSEN": {"units": 250, "unit": "chamadas", "daily_cap": 250, "skipped_by_cap": 2}},
        "rpc_today": {"alchemy": {"used": 120, "limit": 3000, "denied": 0}},
        "new_alpha_7d": 4,
        "cost_per_new_alpha_7d": {"NANSEN": 62.5},
    })
    text = "\n".join(cost)
    assert "Nansen 24h: 250/250 chamadas ⛔ teto atingido" in text
    assert "RPC alchemy hoje: 120/3000" in text
    assert "Custo por alpha nova (7d, n=4): Nansen 62" in text
