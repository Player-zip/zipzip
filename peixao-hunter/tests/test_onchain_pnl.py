import time

from peixao.onchain_pnl import (
    BlockscoutClient, ExplorerError, build_swaps, compute_metrics, enrich_onchain, merge_cached_metrics,
)
from peixao.evidence_ledger import canonical_metrics_for_wallet, sync_provider_caches

W = "0x" + "a" * 40
ROUTER = "0x" + "b" * 40
NOW = 1_800_000_000
ETH = 2000.0


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def token_tx(tx, ts, token, qty, incoming, symbol="MEME"):
    return {
        "transaction_hash": tx, "timestamp": iso(ts),
        "from": {"hash": ROUTER if incoming else W}, "to": {"hash": W if incoming else ROUTER},
        "token": {"address_hash": token, "symbol": symbol}, "total": {"value": str(int(qty * 1e18)), "decimals": "18"},
    }


def buy(tx, ts, token, qty, eth):
    return [token_tx(tx, ts, token, qty, True)], {"hash": tx, "timestamp": iso(ts), "from": {"hash": W},
                                                  "value": str(int(eth * 1e18)), "status": "ok"}, None


def sell(tx, ts, token, qty, eth):
    internal = {"transaction_hash": tx, "timestamp": iso(ts), "from": {"hash": ROUTER}, "to": {"hash": W},
                "value": str(int(eth * 1e18)), "success": True}
    return [token_tx(tx, ts, token, qty, False)], None, internal


def activity(*legs, truncated=False):
    out = {"transfers": [], "txs": [], "internals": [], "truncated": truncated}
    for transfers, tx, internal in legs:
        out["transfers"] += transfers
        if tx:
            out["txs"].append(tx)
        if internal:
            out["internals"].append(internal)
    return out


def day(n):
    return NOW - n * 86400


def test_win_rate_and_unknown_cost_sells_are_not_counted():
    act = activity(
        buy("0x1", day(10), "0xt1", 100, 0.1), sell("0x2", day(9), "0xt1", 100, 0.2),   # ganho
        buy("0x3", day(8), "0xt2", 100, 0.1), sell("0x4", day(7), "0xt2", 100, 0.05),   # perda
        buy("0x5", day(6), "0xt3", 100, 0.1), sell("0x6", day(5), "0xt3", 100, 0.3),    # ganho
        sell("0x7", day(4), "0xt9", 50, 1.0),                                           # custo desconhecido
    )
    swaps, stats = build_swaps(W, act, eth_usd=ETH)
    assert stats["swaps"] == 7
    m = compute_metrics(swaps, now_epoch=NOW)
    assert m["closed_positions"] == 3 and m["winning_tokens"] == 2
    assert abs(m["win_rate"] - 2 / 3) < 1e-9
    assert m["onchain_sell_coverage"] == 3 / 4          # a venda sem custo ficou de fora
    assert m["realized_profit_30d"] > 0 and m["realized_roi_unit"] == "ratio"


def test_low_coverage_leaves_win_rate_unknown_not_zero():
    act = activity(buy("0x1", day(5), "0xt1", 10, 0.1), sell("0x2", day(4), "0xt1", 10, 0.2),
                   sell("0x3", day(3), "0xa", 10, 0.5), sell("0x4", day(3), "0xb", 10, 0.5),
                   sell("0x5", day(3), "0xc", 10, 0.5))
    swaps, _ = build_swaps(W, act, eth_usd=ETH)
    m = compute_metrics(swaps, now_epoch=NOW)
    assert "win_rate" not in m and m["onchain_state"] == "LOW_COVERAGE"


def test_pure_transfers_and_multi_token_txs_are_not_swaps():
    airdrop = ([token_tx("0xaa", day(3), "0xt1", 5, True)], None, None)
    multi = ([token_tx("0xbb", day(3), "0xt1", 5, True), token_tx("0xbb", day(3), "0xt2", 5, True)],
             {"hash": "0xbb", "timestamp": iso(day(3)), "from": {"hash": W}, "value": str(10 ** 18), "status": "ok"}, None)
    swaps, stats = build_swaps(W, activity(airdrop, multi), eth_usd=ETH)
    assert swaps == [] and stats["ignored_multi_token"] == 1


def test_partial_sell_uses_average_cost():
    act = activity(buy("0x1", day(5), "0xt1", 100, 0.1), sell("0x2", day(4), "0xt1", 50, 0.1))
    swaps, _ = build_swaps(W, act, eth_usd=ETH)
    m = compute_metrics(swaps, now_epoch=NOW)
    # custo médio 0.05 ETH p/ metade; recebeu 0.1 ETH => lucro 0.05 ETH
    assert m["realized_profit_30d"] == 0.05 * ETH and m["win_rate"] == 1.0


class FakeClient(BlockscoutClient):
    def __init__(self, acts):
        super().__init__("http://x", rps=0)
        self.acts = acts

    def paged(self, path, params, *, max_pages, since_epoch=0):
        addr = path.split("/")[4]
        act = self.acts[addr]
        if isinstance(act, Exception):
            raise act
        key = "transfers" if "token-transfers" in path else "internals" if "internal" in path else "txs"
        return act[key], False


def test_enrich_parallel_caches_and_ledger_sync(tmp_path):
    w_ok, w_err, w_empty = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40
    good = activity(buy("0x1", day(5), "0xt1", 10, 0.1), sell("0x2", day(4), "0xt1", 10, 0.2))
    client = FakeClient({w_ok: good, w_err: ExplorerError("HTTP_500", 500),
                         w_empty: activity()})
    # os endereços da atividade usam W; reescreve para a wallet certa
    for key in ("transfers", "txs", "internals"):
        for item in good[key]:
            for side in ("from", "to"):
                if isinstance(item.get(side), dict) and item[side]["hash"] == W:
                    item[side]["hash"] = w_ok
    cache = tmp_path / "onchain_pnl_robinhood_cache.json"
    cands = [{"address": a, "priority": 1.0} for a in (w_ok, w_err, w_empty)]
    res = enrich_onchain(cands, cache, client, eth_usd=ETH, now_epoch=int(time.time()), workers=3)
    assert (res["new_wr"], res["errors"], res["no_data"]) == (1, 1, 1)

    # segunda rodada: nada vencido, exceto o erro (ttl curto) — sem reconsultar quem já tem evidência
    res2 = enrich_onchain(cands, cache, client, eth_usd=ETH, now_epoch=int(time.time()), workers=3)
    assert res2["selected"] == 0 and res2["fresh"] == 3

    db = tmp_path / "ev.sqlite3"
    sync_provider_caches(tmp_path, db)
    assert canonical_metrics_for_wallet(db, "robinhood", w_ok)["win_rate"] == 1.0
    assert "win_rate" not in canonical_metrics_for_wallet(db, "robinhood", w_empty)

    src = tmp_path / "prio.csv"
    src.write_text(f"address,discovery_score\n{w_ok},10\n{w_empty},10\n")
    merged = merge_cached_metrics(src, cache, tmp_path / "out.csv")
    import pandas as pd
    df = pd.read_csv(merged).set_index("address")
    assert df.loc[w_ok, "win_rate"] == 1.0 and pd.isna(df.loc[w_empty].get("win_rate"))


def test_new_error_does_not_erase_old_evidence(tmp_path):
    import json
    w = "0x" + "1" * 40
    cache = tmp_path / "onchain_pnl_base_cache.json"
    cache.write_text(json.dumps({"entries": {w: {"status": "OK", "checked_epoch": 1, "metrics": {"win_rate": 0.7}}}}))
    client = FakeClient({w: ExplorerError("HTTP_500", 500)})
    enrich_onchain([{"address": w, "priority": 1}], cache, client, eth_usd=ETH, now_epoch=int(time.time()))
    assert json.loads(cache.read_text())["entries"][w]["metrics"]["win_rate"] == 0.7
