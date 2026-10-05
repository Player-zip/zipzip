import sqlite3
import time
from datetime import datetime, timezone

from peixao.evidence_ledger import canonical_metrics_for_wallet, prune_evidence, record_metrics


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rows(db, address):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT provider, metric, value, observed_epoch FROM wallet_evidence WHERE address=? ORDER BY observed_epoch",
            (address,),
        ).fetchall()


def test_fresh_evidence_beats_stale_higher_priority_provider(tmp_path):
    db = tmp_path / "ev.sqlite3"
    now = int(time.time())
    record_metrics(db, chain="solana", address="W1", provider="NANSEN", metrics={"win_rate": 0.90}, observed_at=_iso(now - 60 * 86400))
    record_metrics(db, chain="solana", address="W1", provider="BIRDEYE", metrics={"win_rate": 0.30}, observed_at=_iso(now - 3600))
    canonical = canonical_metrics_for_wallet(db, "solana", "W1", max_age_days=30)
    assert canonical["win_rate"] == 0.30
    assert canonical["evidence_stale_metrics"] == 0

    record_metrics(db, chain="solana", address="W1", provider="NANSEN", metrics={"win_rate": 0.80}, observed_at=_iso(now - 7200))
    assert canonical_metrics_for_wallet(db, "solana", "W1", max_age_days=30)["win_rate"] == 0.80


def test_stale_evidence_is_used_only_when_nothing_fresh_exists(tmp_path):
    db = tmp_path / "ev.sqlite3"
    now = int(time.time())
    record_metrics(db, chain="base", address="0xAB", provider="NANSEN", metrics={"win_rate": 0.7}, observed_at=_iso(now - 90 * 86400))
    canonical = canonical_metrics_for_wallet(db, "base", "0xab", max_age_days=30)
    assert canonical["win_rate"] == 0.7
    assert canonical["evidence_stale_metrics"] == 2  # win_rate + gmgn_winrate_30d


def test_repeated_values_refresh_timestamp_instead_of_growing(tmp_path):
    db = tmp_path / "ev.sqlite3"
    base = int(time.time()) - 10 * 3600
    for hour in range(5):
        record_metrics(
            db, chain="base", address="0xdef", provider="INLINE",
            metrics={"win_rate": 0.7, "realized_profit_30d": 10.0}, observed_at=_iso(base + hour * 3600),
        )
    rows = _rows(db, "0xdef")
    assert len(rows) == 3  # win_rate, gmgn_winrate_30d, realized_profit_30d
    assert {r[3] for r in rows} == {base + 4 * 3600}

    record_metrics(db, chain="base", address="0xdef", provider="INLINE", metrics={"win_rate": 0.75}, observed_at=_iso(base + 5 * 3600))
    assert len(_rows(db, "0xdef")) == 5  # novo valor => nova observação (win_rate + espelho gmgn)


def test_prune_keeps_latest_observation_per_metric(tmp_path):
    db = tmp_path / "ev.sqlite3"
    now = int(time.time())
    record_metrics(db, chain="solana", address="W2", provider="BIRDEYE", metrics={"realized_profit_30d": 1.0}, observed_at=_iso(now - 400 * 86400))
    record_metrics(db, chain="solana", address="W2", provider="BIRDEYE", metrics={"realized_profit_30d": 2.0}, observed_at=_iso(now - 300 * 86400))
    record_metrics(db, chain="solana", address="W3", provider="BIRDEYE", metrics={"realized_profit_30d": 5.0}, observed_at=_iso(now - 400 * 86400))
    result = prune_evidence(db, retention_days=120)
    assert result["deleted"] == 1
    remaining = {(r[1], r[2]) for r in _rows(db, "W2")} | {(r[1], r[2]) for r in _rows(db, "W3")}
    assert remaining == {("realized_profit_30d", 2.0), ("realized_profit_30d", 5.0)}
