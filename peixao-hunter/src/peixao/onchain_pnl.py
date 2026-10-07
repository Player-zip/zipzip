"""Evidência de WR/PnL 100% on-chain e gratuita (Blockscout v2).

Por que existe: toda evidência de win rate EVM dependia de Nansen/Zerion pagos,
em lotes de poucas wallets e com cooldown de dias. Resultado: centenas de
candidatas ficavam eternamente em "sem WR". Este módulo reconstrói posições
fechadas direto do histórico on-chain (transferências ERC-20 + valor nativo
das transações e transações internas), sem custo de crédito e em paralelo.

Regras de honestidade (mesmas do bootstrap):

- só vira *swap* a transação em que a wallet troca UM token negociável por ativo
  de cotação (ETH nativo/WETH/stablecoin). Transferência != compra/venda;
- venda sem custo conhecido (compra fora do histórico lido) NÃO entra no WR:
  custo desconhecido jamais vira custo zero;
- cobertura baixa => WR fica ``UNKNOWN`` (ausente), nunca 0%;
- PnL é realizado, método custo médio, em USD estimado (ETH pelo preço spot de
  uma única cotação por ciclo; compra e venda usam o mesmo preço, então WR/ROI
  não dependem dele).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import statistics
import threading
import time
from typing import Any

import pandas as pd
import requests

from .units import RATIO, ROI_UNIT_KEY

DEFAULT_EXPLORERS = {
    "base": "https://base.blockscout.com",
    "robinhood": "https://explorer.mainnet.chain.robinhood.com",
}
EXPLORER_ENV = {
    "base": "PEIXAO_BASE_BLOCKSCOUT_URL",
    "robinhood": "PEIXAO_ROBINHOOD_BLOCKSCOUT_URL",
}

WETH_SYMBOLS = {"WETH", "ETH"}
STABLE_SYMBOLS = {
    "USDC", "USDT", "DAI", "USDC.E", "USDBC", "USDS", "USDE", "LUSD", "FRAX", "PYUSD", "USDT0", "USD₮0",
}
ETH_USD_FALLBACK = 3000.0


# ---------------------------------------------------------------- utilidades


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _addr(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("hash") or value.get("address_hash") or value.get("address")
    return str(value or "").strip().lower()


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> int:
    """Epoch (s) de um timestamp ISO 8601 do Blockscout; 0 se inválido."""
    if not value:
        return 0
    try:
        text = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return 0


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_evm_address(value: str) -> bool:
    text = str(value or "").strip().lower()
    if len(text) != 42 or not text.startswith("0x"):
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return True


class RateLimiter:
    """Intervalo mínimo global entre requests (thread-safe)."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


class ExplorerError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- coleta


class BlockscoutClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 15.0,
        rps: float = 4.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = float(timeout)
        self.limiter = RateLimiter(rps)
        self.session = session or requests.Session()
        self.calls = 0
        self._lock = threading.Lock()

    def get(self, path: str, params: dict | None = None) -> dict:
        query = dict(params or {})
        if self.api_key:
            query["apikey"] = self.api_key
        last: Exception | None = None
        for attempt in range(3):
            self.limiter.wait()
            try:
                response = self.session.get(
                    self.base_url + path, params=query,
                    headers={"Accept": "application/json"}, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last = ExplorerError(f"{type(exc).__name__}: {exc}")
                time.sleep(0.5 * (attempt + 1))
                continue
            with self._lock:
                self.calls += 1
            if response.status_code == 429 or response.status_code >= 500:
                last = ExplorerError(f"HTTP_{response.status_code}", response.status_code)
                try:
                    wait = float(response.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    wait = 0.0
                time.sleep(min(8.0, max(wait, 1.0 * (attempt + 1))))
                continue
            if response.status_code != 200:
                raise ExplorerError(f"HTTP_{response.status_code}", response.status_code)
            try:
                body = response.json()
            except ValueError as exc:
                raise ExplorerError("INVALID_JSON") from exc
            return body if isinstance(body, dict) else {}
        raise last or ExplorerError("UNKNOWN")

    def paged(self, path: str, params: dict, *, max_pages: int, since_epoch: int = 0) -> tuple[list[dict], bool]:
        """Itens mais novos primeiro. ``truncated`` = ainda havia páginas mais antigas."""
        items: list[dict] = []
        query = dict(params)
        for _ in range(max(1, int(max_pages))):
            body = self.get(path, query)
            page = [x for x in (body.get("items") or []) if isinstance(x, dict)]
            items.extend(page)
            nxt = body.get("next_page_params")
            if not isinstance(nxt, dict) or not nxt:
                return items, False
            if since_epoch and page and _ts(page[-1].get("timestamp")) and _ts(page[-1].get("timestamp")) < since_epoch:
                return items, False
            query = {**params, **nxt}
        return items, True


def fetch_wallet_activity(client: BlockscoutClient, address: str, *, max_pages: int, since_epoch: int) -> dict:
    transfers, t_trunc = client.paged(
        f"/api/v2/addresses/{address}/token-transfers", {"type": "ERC-20"},
        max_pages=max_pages, since_epoch=since_epoch,
    )
    txs, x_trunc = client.paged(
        f"/api/v2/addresses/{address}/transactions", {}, max_pages=max_pages, since_epoch=since_epoch,
    )
    try:
        internals, i_trunc = client.paged(
            f"/api/v2/addresses/{address}/internal-transactions", {},
            max_pages=max_pages, since_epoch=since_epoch,
        )
    except ExplorerError:
        # Sem internas só se perde o ETH recebido em vendas; compras seguem válidas.
        internals, i_trunc = [], False
    return {
        "transfers": transfers, "txs": txs, "internals": internals,
        "truncated": bool(t_trunc or x_trunc or i_trunc),
    }


# ---------------------------------------------------------------- swaps


def _token_info(item: dict) -> tuple[str, str, float | None]:
    token = item.get("token") if isinstance(item.get("token"), dict) else {}
    total = item.get("total") if isinstance(item.get("total"), dict) else {}
    address = _addr(token.get("address_hash") or token.get("address"))
    symbol = str(token.get("symbol") or "").strip().upper()
    decimals = _num(total.get("decimals") if total.get("decimals") is not None else token.get("decimals"))
    value = _num(total.get("value"))
    qty = value / (10 ** int(decimals)) if value is not None and decimals is not None else None
    return address, symbol, qty


def build_swaps(address: str, activity: dict, *, eth_usd: float, min_usd: float = 1.0) -> tuple[list[dict], dict]:
    """Agrupa por tx e devolve compras/vendas de um único token vs ativo de cotação."""
    wallet = address.lower()
    per_tx: dict[str, dict] = {}

    def slot(tx_hash: str, ts: int) -> dict:
        entry = per_tx.setdefault(tx_hash, {
            "ts": ts, "native_out": 0.0, "native_in": 0.0, "quote_out": 0.0, "quote_in": 0.0, "tokens": {},
        })
        if ts and not entry["ts"]:
            entry["ts"] = ts
        return entry

    for tx in activity.get("txs", []):
        tx_hash = str(tx.get("hash") or "").lower()
        if not tx_hash or str(tx.get("status") or tx.get("result") or "ok").lower() in {"error", "failed"}:
            continue
        if _addr(tx.get("from")) == wallet:
            value = _num(tx.get("value")) or 0.0
            if value > 0:
                slot(tx_hash, _ts(tx.get("timestamp")))["native_out"] += value / 1e18

    for tx in activity.get("internals", []):
        tx_hash = str(tx.get("transaction_hash") or tx.get("tx_hash") or "").lower()
        if not tx_hash or tx.get("success") is False:
            continue
        value = _num(tx.get("value")) or 0.0
        if value <= 0:
            continue
        if _addr(tx.get("to")) == wallet:
            slot(tx_hash, _ts(tx.get("timestamp")))["native_in"] += value / 1e18
        elif _addr(tx.get("from")) == wallet:
            slot(tx_hash, _ts(tx.get("timestamp")))["native_out"] += value / 1e18

    for item in activity.get("transfers", []):
        tx_hash = str(item.get("transaction_hash") or item.get("tx_hash") or "").lower()
        token, symbol, qty = _token_info(item)
        if not tx_hash or not token or not qty or qty <= 0:
            continue
        incoming = _addr(item.get("to")) == wallet
        outgoing = _addr(item.get("from")) == wallet
        if incoming == outgoing:  # nem entrada nem saída (ou auto-transferência)
            continue
        entry = slot(tx_hash, _ts(item.get("timestamp")))
        if symbol in WETH_SYMBOLS:
            entry["quote_in" if incoming else "quote_out"] += qty * eth_usd
        elif symbol in STABLE_SYMBOLS:
            entry["quote_in" if incoming else "quote_out"] += qty
        else:
            net = entry["tokens"].setdefault(token, [0.0, symbol])
            net[0] += qty if incoming else -qty

    swaps: list[dict] = []
    stats = {"txs": len(per_tx), "swaps": 0, "ignored_transfers": 0, "ignored_multi_token": 0, "ignored_dust": 0}
    for tx_hash, entry in per_tx.items():
        moved = {t: v for t, v in entry["tokens"].items() if abs(v[0]) > 1e-12}
        if not moved:
            continue
        if len(moved) != 1:
            stats["ignored_multi_token"] += 1
            continue
        token, (net, symbol) = next(iter(moved.items()))
        usd_out = entry["quote_out"] + entry["native_out"] * eth_usd
        usd_in = entry["quote_in"] + entry["native_in"] * eth_usd
        if net > 0 and usd_out > usd_in:
            side, usd = "buy", usd_out - usd_in
        elif net < 0 and usd_in > usd_out:
            side, usd = "sell", usd_in - usd_out
        else:
            stats["ignored_transfers"] += 1
            continue
        if usd < min_usd:
            stats["ignored_dust"] += 1
            continue
        stats["swaps"] += 1
        swaps.append({"tx": tx_hash, "ts": entry["ts"], "token": token, "symbol": symbol,
                      "side": side, "qty": abs(net), "usd": usd})
    swaps.sort(key=lambda s: (s["ts"], s["side"] != "buy"))
    return swaps, stats


# ---------------------------------------------------------------- métricas


def compute_metrics(
    swaps: list[dict],
    *,
    now_epoch: int,
    lookback_days: int = 30,
    min_sell_coverage: float = 0.5,
    truncated: bool = False,
) -> dict:
    """Custo médio por token; só vendas dentro da janela e com custo conhecido contam."""
    window_start = now_epoch - int(lookback_days) * 86400
    book: dict[str, dict] = {}
    realized: dict[str, float] = {}
    realized_cost: dict[str, float] = {}
    window_sells = covered_sells = window_swaps = 0
    first_buy_window: set[str] = set()

    for swap in swaps:
        in_window = swap["ts"] >= window_start
        pos = book.setdefault(swap["token"], {"qty": 0.0, "cost": 0.0, "seen": False})
        if in_window:
            window_swaps += 1
        if swap["side"] == "buy":
            if in_window and not pos["seen"]:
                first_buy_window.add(swap["token"])
            pos["seen"] = True
            pos["qty"] += swap["qty"]
            pos["cost"] += swap["usd"]
            continue
        if in_window:
            window_sells += 1
        covered = min(swap["qty"], pos["qty"])
        if covered <= 0 or pos["qty"] <= 0:
            continue
        proceeds = swap["usd"] * (covered / swap["qty"])
        avg = pos["cost"] / pos["qty"]
        basis = avg * covered
        pos["qty"] -= covered
        pos["cost"] -= basis
        if in_window:
            covered_sells += 1
            realized[swap["token"]] = realized.get(swap["token"], 0.0) + (proceeds - basis)
            realized_cost[swap["token"]] = realized_cost.get(swap["token"], 0.0) + basis

    closed = {t: p for t, p in realized.items() if realized_cost.get(t, 0.0) > 0}
    coverage = (covered_sells / window_sells) if window_sells else None
    metrics: dict = {
        "onchain_evidence": True,
        "onchain_method": "AVG_COST_REALIZED_BLOCKSCOUT",
        "onchain_window_days": int(lookback_days),
        "onchain_truncated": bool(truncated),
        "onchain_sell_coverage": coverage,
        "total_trades": int(window_swaps),
        "onchain_window_sells": int(window_sells),
        "new_positions_per_week": len(first_buy_window) / (lookback_days / 7.0),
        "new_positions_per_week_source": "onchain_first_buys_in_window",
        ROI_UNIT_KEY: RATIO,
        "realized_roi_unit": RATIO,
    }
    if not closed or coverage is None or coverage < float(min_sell_coverage):
        metrics["onchain_state"] = "LOW_COVERAGE" if closed else "NO_CLOSED_POSITIONS"
        return metrics

    pnls = list(closed.values())
    wins = [p for p in pnls if p > 0]
    total_cost = sum(realized_cost[t] for t in closed)
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls)
    positive = sum(wins)
    shares = sorted((p / positive for p in wins), reverse=True) if positive > 0 else []
    metrics.update({
        "onchain_state": "OK",
        "win_rate": win_rate,
        "gmgn_winrate_30d": win_rate,
        "closed_positions": len(pnls),
        "winning_tokens": len(wins),
        "tokens_traded": len(pnls),
        "repeatability_score": win_rate,
        "realized_profit_30d": total_pnl,
        "realized_roi_30d": (total_pnl / total_cost) if total_cost > 0 else None,
        "median_pnl_per_token": float(statistics.median(pnls)),
        "profit_hhi": sum(s * s for s in shares) if shares else None,
        "largest_win_share": shares[0] if shares else None,
        "top3_profit_share": sum(shares[:3]) if shares else None,
    })
    return metrics


# ---------------------------------------------------------------- preço ETH


def get_eth_usd(state_dir: Path, *, timeout: float = 8.0, ttl: int = 3600) -> float:
    path = state_dir / "eth_usd.json"
    cached = _load_json(path)
    now = time.time()
    if cached.get("price") and now - float(cached.get("epoch", 0)) < ttl:
        return float(cached["price"])
    try:
        response = requests.get("https://api.coinbase.com/v2/prices/ETH-USD/spot", timeout=timeout)
        price = float(response.json()["data"]["amount"])
        if price > 0:
            _atomic_json(path, {"price": price, "epoch": now})
            return price
    except Exception:
        pass
    if cached.get("price"):
        return float(cached["price"])
    return float(os.getenv("PEIXAO_ETH_USD") or ETH_USD_FALLBACK)


# ---------------------------------------------------------------- runner


def analyze_wallet(
    client: BlockscoutClient, address: str, *, eth_usd: float, lookback_days: int, max_pages: int,
    min_sell_coverage: float, now_epoch: int,
) -> dict:
    # Lê além da janela: compras anteriores dão o custo das vendas dentro dela.
    since = now_epoch - int(lookback_days) * 3 * 86400
    activity = fetch_wallet_activity(client, address, max_pages=max_pages, since_epoch=since)
    swaps, stats = build_swaps(address, activity, eth_usd=eth_usd)
    metrics = compute_metrics(
        swaps, now_epoch=now_epoch, lookback_days=lookback_days,
        min_sell_coverage=min_sell_coverage, truncated=activity["truncated"],
    )
    metrics["onchain_swaps_found"] = stats["swaps"]
    return metrics


def _retry_after(entry: dict, *, ttl: int, no_data_ttl: int, error_ttl: int) -> int:
    status = entry.get("status")
    if status == "OK":
        return ttl
    if status == "ERROR":
        return error_ttl
    return no_data_ttl


def select_wallets(candidates: list[dict], entries: dict, *, now: int, ttl: int, no_data_ttl: int,
                   error_ttl: int, limit: int) -> tuple[list[str], dict]:
    """Nunca consultadas primeiro (maior prioridade barata antes), depois as mais velhas."""
    never, stale, fresh = [], [], 0
    for row in candidates:
        address = row["address"]
        entry = entries.get(address) if isinstance(entries.get(address), dict) else None
        if entry is None:
            never.append(row)
            continue
        age = now - int(entry.get("checked_epoch", 0) or 0)
        if age < _retry_after(entry, ttl=ttl, no_data_ttl=no_data_ttl, error_ttl=error_ttl):
            fresh += 1
        else:
            stale.append((age, row))
    never.sort(key=lambda r: -float(r.get("priority") or 0.0))
    stale.sort(key=lambda item: -item[0])
    ordered = [r["address"] for r in never] + [row["address"] for _, row in stale]
    return ordered[: max(0, int(limit))], {"never_checked": len(never), "stale": len(stale), "fresh": fresh}


def enrich_onchain(
    candidates: list[dict],
    cache_path: Path,
    client: BlockscoutClient,
    *,
    eth_usd: float,
    max_wallets: int = 60,
    workers: int = 6,
    max_seconds: float = 420.0,
    lookback_days: int = 30,
    max_pages: int = 4,
    ttl_seconds: int = 12 * 3600,
    no_data_ttl_seconds: int = 24 * 3600,
    error_ttl_seconds: int = 1800,
    min_sell_coverage: float = 0.5,
    now_epoch: int | None = None,
) -> dict:
    """Consulta em paralelo, respeitando prazo; persiste o cache após o lote (e a cada 20)."""
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    payload = _load_json(cache_path)
    entries = payload.get("entries") if isinstance(payload.get("entries"), dict) else {}
    selected, queue_stats = select_wallets(
        candidates, entries, now=now, ttl=ttl_seconds, no_data_ttl=no_data_ttl_seconds,
        error_ttl=error_ttl_seconds, limit=max_wallets,
    )
    deadline = time.monotonic() + max(1.0, float(max_seconds))
    lock = threading.Lock()
    counts = {"OK": 0, "NO_DATA": 0, "ERROR": 0, "SKIPPED_DEADLINE": 0}
    errors: dict[str, int] = {}
    done_since_save = [0]

    def work(address: str) -> None:
        if time.monotonic() > deadline:
            with lock:
                counts["SKIPPED_DEADLINE"] += 1
            return
        try:
            metrics = analyze_wallet(
                client, address, eth_usd=eth_usd, lookback_days=lookback_days, max_pages=max_pages,
                min_sell_coverage=min_sell_coverage, now_epoch=now,
            )
            status = "OK" if metrics.get("onchain_state") == "OK" else "NO_DATA"
            entry = {"status": status, "metrics": metrics if status == "OK" else {}, "reason": metrics.get("onchain_state"),
                     "onchain_swaps_found": metrics.get("onchain_swaps_found", 0)}
        except ExplorerError as exc:
            entry = {"status": "ERROR", "metrics": {}, "reason": str(exc)}
        except Exception as exc:  # um erro de wallet nunca derruba o lote nem apaga o resto
            entry = {"status": "ERROR", "metrics": {}, "reason": f"{type(exc).__name__}: {exc}"}
        entry.update({"checked_epoch": int(time.time()), "checked_at": _now_iso()})
        with lock:
            previous = entries.get(address) if isinstance(entries.get(address), dict) else None
            if entry["status"] == "ERROR" and previous and previous.get("metrics"):
                # Falha nova não apaga evidência válida antiga: só adia a próxima tentativa.
                previous["last_error"] = entry["reason"]
                previous["last_error_epoch"] = entry["checked_epoch"]
            else:
                entries[address] = entry
            counts[entry["status"]] += 1
            if entry["status"] == "ERROR":
                errors[entry["reason"]] = errors.get(entry["reason"], 0) + 1
            done_since_save[0] += 1
            if done_since_save[0] >= 20:
                done_since_save[0] = 0
                _atomic_json(cache_path, {"entries": entries})

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        list(pool.map(work, selected))
    _atomic_json(cache_path, {"entries": entries})

    with_wr = sum(1 for e in entries.values() if isinstance(e, dict) and (e.get("metrics") or {}).get("win_rate") is not None)
    attempted = counts["OK"] + counts["NO_DATA"] + counts["ERROR"]
    return {
        "status": "ERROR" if attempted and counts["ERROR"] == attempted else ("PARTIAL" if counts["ERROR"] else "DONE"),
        "selected": len(selected),
        "attempted": attempted,
        "new_wr": counts["OK"],
        "no_data": counts["NO_DATA"],
        "errors": counts["ERROR"],
        "error_reasons": errors,
        "skipped_deadline": counts["SKIPPED_DEADLINE"],
        "http_calls": client.calls,
        "wallets_with_wr_total": with_wr,
        "seconds": round(time.monotonic() - started, 1),
        **queue_stats,
    }


def load_candidates(paths: list[Path]) -> list[dict]:
    """Endereços EVM únicos (eligíveis) das tabelas da fila, com prioridade barata."""
    seen: dict[str, dict] = {}
    for path in paths:
        if path is None or not Path(path).is_file():
            continue
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if frame.empty or "address" not in frame.columns:
            continue
        for row in frame.to_dict("records"):
            address = str(row.get("address", "") or "").strip().lower()
            if not is_evm_address(address):
                continue
            eligible = row.get("eligible_enrichment")
            if eligible is not None and str(eligible).strip().lower() in {"false", "0", "no"}:
                continue
            priority = 0.0
            for key in ("execution_priority_score", "discovery_score"):
                value = _num(row.get(key))
                if value is not None and value == value:
                    priority = value
                    break
            if address not in seen or priority > seen[address]["priority"]:
                seen[address] = {"address": address, "priority": priority}
    return list(seen.values())


def explorer_url(chain: str) -> str:
    return (os.getenv(EXPLORER_ENV.get(chain, "")) or DEFAULT_EXPLORERS.get(chain, "")).strip()


def run_onchain_evidence(cfg, chain: str, input_paths: list[Path]) -> dict:
    """Etapa do ciclo horário: evidência gratuita para a fila da rede ``chain``."""
    if os.getenv("PEIXAO_ONCHAIN_EVIDENCE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {"status": "DISABLED", "chain": chain, "http_calls": 0}
    if not cfg.chain_enabled(chain):
        return {"status": "CHAIN_DISABLED", "chain": chain, "http_calls": 0}
    base_url = explorer_url(chain)
    if not base_url:
        return {"status": "NO_EXPLORER", "chain": chain, "http_calls": 0}
    candidates = load_candidates(input_paths)
    if not candidates:
        return {"status": "NO_INPUT", "chain": chain, "wallets": 0, "http_calls": 0}

    def env_num(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, default))
        except ValueError:
            return float(default)

    client = BlockscoutClient(
        base_url, api_key=cfg.blockscout_api_key, timeout=env_num("PEIXAO_ONCHAIN_TIMEOUT", 15),
        rps=env_num("PEIXAO_ONCHAIN_RPS", 4),
    )
    result = enrich_onchain(
        candidates,
        cfg.state_dir / f"onchain_pnl_{chain}_cache.json",
        client,
        eth_usd=get_eth_usd(cfg.state_dir),
        max_wallets=int(env_num("PEIXAO_ONCHAIN_WALLETS_PER_CYCLE", 60)),
        workers=int(env_num("PEIXAO_ONCHAIN_WORKERS", 6)),
        max_seconds=env_num("PEIXAO_ONCHAIN_MAX_SECONDS", 420),
        lookback_days=int(env_num("PEIXAO_ONCHAIN_LOOKBACK_DAYS", 30)),
        max_pages=int(env_num("PEIXAO_ONCHAIN_MAX_PAGES", 4)),
        min_sell_coverage=env_num("PEIXAO_ONCHAIN_MIN_SELL_COVERAGE", 0.5),
    )
    return {"chain": chain, "wallets": len(candidates), "explorer": base_url, **result}


def merge_cached_metrics(input_path: Path, cache_path: Path, output_path: Path) -> Path:
    """Copia a tabela de prioridade com as métricas on-chain preenchendo SÓ campos ausentes.

    Usada para que o Nansen/Zerion pulem quem já tem WR on-chain (poupa crédito).
    """
    entries = _load_json(cache_path).get("entries", {})
    if not input_path.is_file() or not entries:
        return input_path
    try:
        frame = pd.read_csv(input_path)
    except Exception:
        return input_path
    if frame.empty or "address" not in frame.columns:
        return input_path
    rows = frame.to_dict("records")
    for row in rows:
        entry = entries.get(str(row.get("address", "") or "").strip().lower())
        metrics = entry.get("metrics") if isinstance(entry, dict) else None
        if not metrics or metrics.get("win_rate") is None:
            continue
        for key, value in metrics.items():
            current = row.get(key)
            if value is not None and (current is None or (isinstance(current, float) and current != current)):
                row[key] = value
        row["wallet_evidence_state"] = "PERFORMANCE_ENRICHED"
    pd.DataFrame(rows).to_csv(output_path, index=False)
    return output_path
