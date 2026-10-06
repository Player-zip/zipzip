from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import time
from typing import Any

import pandas as pd
import requests


TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _is_missing(value: Any) -> bool:
    try:
        return value is None or pd.isna(value) or str(value).strip().lower() in {"", "nan", "none"}
    except Exception:
        return value is None


@dataclass
class QuickNodeCreditBudget:
    state_path: Path
    daily_limit: int = 330_000
    run_limit: int = 75_000

    def __post_init__(self) -> None:
        raw = _load_json(self.state_path)
        today = _today_utc()
        if str(raw.get("date", "")) != today:
            raw = {"date": today, "spent": 0}
        self._state = raw
        self._run_spent = 0

    @property
    def spent_today(self) -> int:
        return max(0, int(self._state.get("spent", 0) or 0))

    @property
    def spent_run(self) -> int:
        return max(0, int(self._run_spent))

    @property
    def remaining_today(self) -> int:
        return max(0, int(self.daily_limit) - self.spent_today)

    @property
    def remaining_run(self) -> int:
        return max(0, int(self.run_limit) - self.spent_run)

    def can_reserve(self, credits: int) -> bool:
        amount = max(0, int(credits))
        return amount <= self.remaining_today and amount <= self.remaining_run

    def reserve(self, credits: int) -> bool:
        amount = max(0, int(credits))
        if not self.can_reserve(amount):
            return False
        self._run_spent += amount
        self._state["date"] = _today_utc()
        self._state["spent"] = self.spent_today + amount
        _atomic_json(self.state_path, self._state)
        return True

    def summary(self) -> dict:
        return {
            "daily_limit": int(self.daily_limit),
            "run_limit": int(self.run_limit),
            "spent_today": self.spent_today,
            "spent_run": self.spent_run,
            "remaining_today": self.remaining_today,
            "remaining_run": self.remaining_run,
        }


_UNSUPPORTED_HTTP = ("HTTP_400", "HTTP_404", "HTTP_405", "HTTP_413", "HTTP_501")
_UNSUPPORTED_RPC_HINTS = ("-32601", "-32602", "method not found", "not supported", "unsupported", "jsonparsed")


def _is_unsupported_method_error(error: str | None) -> bool:
    """Erro definitivo de capacidade do provedor (não transitório)."""
    text = str(error or "")
    if any(code in text for code in _UNSUPPORTED_HTTP):
        return True
    return "RPC_ERROR" in text and any(hint in text.lower() for hint in _UNSUPPORTED_RPC_HINTS)


def _method_credits_from_env() -> dict[str, int]:
    """PEIXAO_QUICKNODE_METHOD_CREDITS='{"getTransaction": 30, "getSignaturesForAddress": 30}'."""
    raw = os.getenv("PEIXAO_QUICKNODE_METHOD_CREDITS", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    out: dict[str, int] = {}
    if isinstance(value, dict):
        for key, credits in value.items():
            try:
                out[str(key)] = max(1, int(credits))
            except (TypeError, ValueError):
                continue
    return out


class SolanaRpcRouter:
    """QuickNode-first JSON-RPC router with sticky Helius/Shyft fallbacks."""

    def __init__(
        self,
        *,
        quicknode_rpc_url: str | None,
        fallback_rpc_urls: list[tuple[str, str | None]] | None,
        budget: QuickNodeCreditBudget,
        call_credit_estimate: int = 30,
        timeout: float = 20.0,
        delay: float = 0.075,
        method_credit_estimates: dict[str, int] | None = None,
    ) -> None:
        providers: list[tuple[str, str]] = []
        qn = str(quicknode_rpc_url or "").strip()
        if qn:
            providers.append(("quicknode", qn))
        for name, url in fallback_rpc_urls or []:
            clean = str(url or "").strip()
            if clean and all(existing != clean for _, existing in providers):
                providers.append((str(name), clean))
        self.providers = providers
        self.budget = budget
        self.call_credit_estimate = max(1, int(call_credit_estimate))
        self.timeout = max(1.0, float(timeout))
        self.delay = max(0.0, float(delay))
        self.session = requests.Session()
        self._id = 0
        self._sticky_index = 0
        self.provider_calls: dict[str, int] = {}
        self.provider_errors: dict[str, int] = {}
        # Custo por método (créditos QuickNode), com o estimado geral como padrão.
        self.method_credit_estimates = {
            str(k): max(1, int(v)) for k, v in (method_credit_estimates or _method_credits_from_env()).items()
        }
        # None = ainda não testado; False = provedor rejeita getMultipleAccounts(jsonParsed).
        self._multiple_accounts_supported: bool | None = None
        self.fanout_calls = 0

    @property
    def configured(self) -> bool:
        return bool(self.providers)

    def _provider_order(self) -> list[int]:
        if not self.providers:
            return []
        start = min(max(0, self._sticky_index), len(self.providers) - 1)
        return list(range(start, len(self.providers))) + list(range(0, start))

    def credits_for(self, method: str) -> int:
        return self.method_credit_estimates.get(str(method), self.call_credit_estimate)

    def call(self, method: str, params: list | dict | None = None) -> tuple[Any | None, str | None, str | None]:
        """Chamada JSON-RPC; getMultipleAccounts cai para getAccountInfo só quando o
        provedor rejeita o método (não em timeout, 429 ou 5xx)."""
        if method != "getMultipleAccounts":
            return self._call_raw(method, params)
        if self._multiple_accounts_supported is False:
            return self._fanout_accounts(params)
        result, provider, error = self._call_raw(method, params)
        if error is None:
            self._multiple_accounts_supported = True
            return result, provider, None
        if _is_unsupported_method_error(error):
            self._multiple_accounts_supported = False
            return self._fanout_accounts(params, provider=provider, original_error=error)
        return None, provider, error

    def _fanout_accounts(self, params, provider=None, original_error=None):
        if not isinstance(params, list) or not params or not isinstance(params[0], list):
            return None, provider, original_error or "INVALID_MULTIPLE_ACCOUNTS_PARAMS"
        addresses = [str(x or "").strip() for x in params[0] if str(x or "").strip()]
        if not addresses:
            return {"value": []}, provider, None
        config = params[1] if len(params) > 1 and isinstance(params[1], dict) else {
            "encoding": "jsonParsed",
            "commitment": "confirmed",
        }
        values: list[Any | None] = []
        last_provider = provider
        successes = 0
        for address in addresses:
            account_result, account_provider, account_error = self._call_raw("getAccountInfo", [address, config])
            self.fanout_calls += 1
            if account_provider:
                last_provider = account_provider
            value = account_result.get("value") if isinstance(account_result, dict) else None
            values.append(value)
            if account_error is None and isinstance(value, dict):
                successes += 1
            if account_error and "BUDGET_EXHAUSTED" in account_error:
                break
        if successes:
            values.extend([None] * (len(addresses) - len(values)))
            return {"value": values}, last_provider, None
        return None, last_provider, original_error or "ACCOUNT_INFO_FANOUT_FAILED"

    def _call_raw(self, method: str, params: list | dict | None = None) -> tuple[Any | None, str | None, str | None]:
        if not self.providers:
            return None, None, "NO_RPC_PROVIDER"

        last_error = "RPC_FAILED"
        for idx in self._provider_order():
            provider, url = self.providers[idx]
            if provider == "quicknode" and not self.budget.reserve(self.credits_for(method)):
                last_error = "QUICKNODE_BUDGET_EXHAUSTED"
                continue

            self._id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": method,
                "params": params or [],
            }
            self.provider_calls[provider] = self.provider_calls.get(provider, 0) + 1
            try:
                response = self.session.post(
                    url,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
                if self.delay > 0:
                    time.sleep(self.delay)
                if response.status_code != 200:
                    last_error = f"{provider}:HTTP_{response.status_code}"
                    self.provider_errors[provider] = self.provider_errors.get(provider, 0) + 1
                    if response.status_code in {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}:
                        continue
                    return None, provider, last_error
                body = response.json()
                if isinstance(body, dict) and body.get("error"):
                    err = body.get("error")
                    last_error = f"{provider}:RPC_ERROR:{err}"
                    self.provider_errors[provider] = self.provider_errors.get(provider, 0) + 1
                    continue
                if not isinstance(body, dict) or "result" not in body:
                    last_error = f"{provider}:INVALID_RPC_RESPONSE"
                    self.provider_errors[provider] = self.provider_errors.get(provider, 0) + 1
                    continue
                self._sticky_index = idx
                return body.get("result"), provider, None
            except (requests.RequestException, ValueError) as exc:
                if self.delay > 0:
                    time.sleep(self.delay)
                last_error = f"{provider}:{type(exc).__name__}"
                self.provider_errors[provider] = self.provider_errors.get(provider, 0) + 1
                continue

        return None, None, last_error


def _token_account_owner(account: dict) -> str:
    data = account.get("data") if isinstance(account, dict) else None
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    owner = info.get("owner") if isinstance(info, dict) else None
    return str(owner or "").strip()


def _token_balance_map(transaction: dict, owner: str) -> dict[str, float]:
    meta = transaction.get("meta") if isinstance(transaction, dict) else None
    if not isinstance(meta, dict):
        return {}

    totals: dict[str, float] = {}
    for side_name, sign in (("preTokenBalances", -1.0), ("postTokenBalances", 1.0)):
        rows = meta.get(side_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or str(row.get("owner", "") or "").strip() != owner:
                continue
            mint = str(row.get("mint", "") or "").strip()
            amount = row.get("uiTokenAmount") if isinstance(row.get("uiTokenAmount"), dict) else {}
            raw = _num(amount.get("amount"))
            decimals = int(_num(amount.get("decimals"), 0.0))
            value = raw / (10 ** max(0, decimals))
            if mint:
                totals[mint] = totals.get(mint, 0.0) + sign * value
    return totals


def _wallet_tx_metrics(wallet: str, signatures: list[dict], transactions: list[dict]) -> dict:
    token_mints: set[str] = set()
    buy_events = 0
    sell_events = 0
    buy_mints: set[str] = set()
    sell_mints: set[str] = set()
    days: set[str] = set()
    successful = 0

    for sig in signatures:
        if isinstance(sig, dict) and sig.get("err") is None:
            successful += 1

    for tx in transactions:
        if not isinstance(tx, dict):
            continue
        block_time = tx.get("blockTime")
        try:
            if block_time:
                day = datetime.fromtimestamp(int(block_time), tz=timezone.utc).date().isoformat()
                days.add(day)
        except Exception:
            pass
        deltas = _token_balance_map(tx, wallet)
        for mint, delta in deltas.items():
            if abs(delta) <= 1e-12:
                continue
            token_mints.add(mint)
            if delta > 0:
                buy_events += 1
                buy_mints.add(mint)
            else:
                sell_events += 1
                sell_mints.add(mint)

    total = len(signatures)
    return {
        "quicknode_recent_signatures": int(total),
        "quicknode_recent_successful_txs": int(successful),
        "quicknode_recent_success_rate": round(successful / total, 4) if total else None,
        "quicknode_active_days": int(len(days)),
        "quicknode_unique_token_mints": int(len(token_mints)),
        "quicknode_buy_events": int(buy_events),
        "quicknode_sell_events": int(sell_events),
        "quicknode_unique_buy_mints": int(len(buy_mints)),
        "quicknode_unique_sell_mints": int(len(sell_mints)),
        "quicknode_activity_score": round(
            min(
                100.0,
                20.0 * min(1.0, len(days) / 7.0)
                + 30.0 * min(1.0, len(token_mints) / 12.0)
                + 25.0 * min(1.0, buy_events / 10.0)
                + 25.0 * min(1.0, sell_events / 10.0),
            ),
            2,
        ),
    }


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, OSError):
        return pd.DataFrame()


def run_quicknode_solana_discovery(
    shortlist_path: Path,
    output_dir: Path,
    state_dir: Path,
    *,
    quicknode_rpc_url: str | None,
    helius_rpc_url: str | None = None,
    shyft_rpc_url: str | None = None,
    enabled: bool = True,
    timeout: float = 20.0,
    delay: float = 0.075,
    max_tokens: int = 5,
    max_wallets: int = 25,
    tx_per_wallet: int = 80,
    daily_credit_budget: int = 330_000,
    run_credit_budget: int = 75_000,
    call_credit_estimate: int = 30,
    cache_ttl_seconds: int = 1800,
) -> dict:
    """Discover Solana wallets from top token holders and deep-dive them through QuickNode.

    Discovery evidence never creates Alpha by itself. The output is merged later with
    Birdeye/Nansen-style performance evidence; QuickNode-only rows stay un-enriched.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    holder_out = output_dir / "V22_quicknode_token_holders.csv"
    candidate_out = output_dir / "V22_quicknode_wallet_candidates.csv"

    if not enabled:
        for path in (holder_out, candidate_out):
            pd.DataFrame().to_csv(path, index=False)
        return {"status": "DISABLED", "rpc_calls": 0, "quicknode_credits": 0}

    shortlist = _read_csv(shortlist_path).head(max(0, int(max_tokens)))
    if shortlist.empty or "token_address" not in shortlist.columns:
        for path in (holder_out, candidate_out):
            pd.DataFrame().to_csv(path, index=False)
        return {"status": "SKIPPED_NO_RADAR_SHORTLIST", "rpc_calls": 0, "quicknode_credits": 0}

    budget = QuickNodeCreditBudget(
        state_dir / "quicknode_credit_budget.json",
        daily_limit=max(0, int(daily_credit_budget)),
        run_limit=max(0, int(run_credit_budget)),
    )
    router = SolanaRpcRouter(
        quicknode_rpc_url=quicknode_rpc_url,
        fallback_rpc_urls=[("helius", helius_rpc_url), ("shyft", shyft_rpc_url)],
        budget=budget,
        call_credit_estimate=max(1, int(call_credit_estimate)),
        timeout=timeout,
        delay=delay,
    )
    if not router.configured:
        for path in (holder_out, candidate_out):
            pd.DataFrame().to_csv(path, index=False)
        return {
            "status": "NOT_CONFIGURED_RPC",
            "rpc_calls": 0,
            "quicknode_credits": 0,
            "budget": budget.summary(),
        }

    cache_path = state_dir / "quicknode_solana_cache.json"
    cache = _load_json(cache_path)
    now = int(time.time())
    if (
        isinstance(cache.get("holder_rows"), list)
        and isinstance(cache.get("candidate_rows"), list)
        and now - int(cache.get("checked_epoch", 0) or 0) < max(0, int(cache_ttl_seconds))
    ):
        holder_frame = pd.DataFrame(cache.get("holder_rows", []))
        candidate_frame = pd.DataFrame(cache.get("candidate_rows", []))
        holder_frame.to_csv(holder_out, index=False)
        candidate_frame.to_csv(candidate_out, index=False)
        return {
            "status": "DONE_CACHED",
            "cached": True,
            "tokens": int(holder_frame["token_address"].nunique()) if not holder_frame.empty and "token_address" in holder_frame.columns else 0,
            "holders": int(len(holder_frame)),
            "candidate_wallets": int(len(candidate_frame)),
            "rpc_calls": 0,
            "quicknode_credits": 0,
            "budget": budget.summary(),
            "output": str(candidate_out),
        }

    token_scores = {
        str(row.token_address): float(getattr(row, "radar_score", 0.0) or 0.0)
        for row in shortlist.itertuples()
        if str(getattr(row, "token_address", "") or "").strip()
    }
    holder_rows: list[dict] = []

    for token, radar_score in token_scores.items():
        largest, provider, error = router.call(
            "getTokenLargestAccounts",
            [token, {"commitment": "confirmed"}],
        )
        values = largest.get("value") if isinstance(largest, dict) else None
        if not isinstance(values, list) or not values:
            continue

        token_accounts = [str(x.get("address", "") or "").strip() for x in values if isinstance(x, dict)]
        token_accounts = [x for x in token_accounts if x]
        if not token_accounts:
            continue

        accounts, accounts_provider, accounts_error = router.call(
            "getMultipleAccounts",
            [token_accounts, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        account_values = accounts.get("value") if isinstance(accounts, dict) else None
        if not isinstance(account_values, list):
            continue

        for idx, (largest_row, account) in enumerate(zip(values, account_values, strict=False), start=1):
            if not isinstance(largest_row, dict) or not isinstance(account, dict):
                continue
            owner = _token_account_owner(account)
            if not owner:
                continue
            balance = _num(largest_row.get("uiAmountString", largest_row.get("uiAmount")))
            holder_rows.append(
                {
                    "address": owner,
                    "token_address": token,
                    "holder_rank": idx,
                    "token_balance_ui": balance,
                    "radar_score": radar_score,
                    "quicknode_provider": accounts_provider or provider,
                    "quicknode_source": "getTokenLargestAccounts+getMultipleAccounts",
                }
            )

    holder_frame = pd.DataFrame(holder_rows)
    holder_frame.to_csv(holder_out, index=False)
    if holder_frame.empty:
        pd.DataFrame().to_csv(candidate_out, index=False)
        return {
            "status": "DONE_NO_HOLDERS",
            "cached": False,
            "tokens": int(len(token_scores)),
            "holders": 0,
            "candidate_wallets": 0,
            "rpc_calls": int(sum(router.provider_calls.values())),
            "quicknode_credits": int(budget.spent_run),
            "provider_calls": router.provider_calls,
            "provider_errors": router.provider_errors,
            "budget": budget.summary(),
        }

    candidates: list[dict] = []
    for address, group in holder_frame.groupby("address", sort=False):
        hits = int(group["token_address"].nunique())
        signal = 0.0
        for row in group.itertuples():
            signal += (float(getattr(row, "radar_score", 0.0) or 0.0) / 100.0) / math.sqrt(
                max(1.0, float(getattr(row, "holder_rank", 1) or 1))
            )
        weighted = min(1.0, signal / 1.20)
        candidates.append(
            {
                "address": str(address),
                "cross_token_hits": hits,
                "independent_cross_token_hits": hits,
                "weighted_cross_token_score": round(weighted, 4),
                "best_token_rank": int(group["holder_rank"].min()),
                "mean_token_rank": round(float(group["holder_rank"].mean()), 2),
                "radar_tokens": "|".join(sorted(set(group["token_address"].astype(str)))),
                "risk_flags": "",
                "risk_tagged": False,
                "discovery_score": round(100.0 * weighted, 2),
                "quicknode_holder_entries": int(len(group)),
                "quicknode_holder_balance_sum": round(float(group["token_balance_ui"].fillna(0).sum()), 8),
                "quicknode_candidate_source": "top_holders",
                "candidate_sources": "quicknode",
            }
        )

    candidate_frame = pd.DataFrame(candidates)
    candidate_frame = candidate_frame.sort_values(
        ["cross_token_hits", "weighted_cross_token_score", "best_token_rank"],
        ascending=[False, False, True],
        kind="mergesort",
    ).head(max(0, int(max_wallets))).reset_index(drop=True)

    deep_rows: list[dict] = []
    wallet_cache = cache.get("wallets") if isinstance(cache.get("wallets"), dict) else {}
    for row in candidate_frame.to_dict("records"):
        wallet = str(row.get("address", "") or "").strip()
        if not wallet:
            continue
        cached_wallet = wallet_cache.get(wallet) if isinstance(wallet_cache.get(wallet), dict) else {}
        if (
            isinstance(cached_wallet.get("metrics"), dict)
            and now - int(cached_wallet.get("checked_epoch", 0) or 0) < max(0, int(cache_ttl_seconds))
        ):
            deep_rows.append({**row, **cached_wallet["metrics"]})
            continue

        balance_result, balance_provider, _ = router.call("getBalance", [wallet, {"commitment": "confirmed"}])
        lamports = _num(balance_result.get("value")) if isinstance(balance_result, dict) else 0.0

        signatures_result, signature_provider, signature_error = router.call(
            "getSignaturesForAddress",
            [wallet, {"limit": max(1, min(1000, int(tx_per_wallet))), "commitment": "confirmed"}],
        )
        signatures = signatures_result if isinstance(signatures_result, list) else []
        transactions: list[dict] = []
        for sig_row in signatures:
            if not isinstance(sig_row, dict):
                continue
            signature = str(sig_row.get("signature", "") or "").strip()
            if not signature:
                continue
            tx_result, _, _ = router.call(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if isinstance(tx_result, dict):
                transactions.append(tx_result)
            if not router.providers:
                break

        metrics = {
            "quicknode_sol_balance": round(lamports / 1_000_000_000.0, 9),
            "quicknode_deep_provider": signature_provider or balance_provider,
            **_wallet_tx_metrics(wallet, signatures, transactions),
        }
        wallet_cache[wallet] = {
            "checked_epoch": now,
            "checked_at": _now_iso(),
            "metrics": metrics,
            "signature_error": signature_error,
        }
        deep_rows.append({**row, **metrics})

    candidate_frame = pd.DataFrame(deep_rows)
    candidate_frame.to_csv(candidate_out, index=False)
    cache_payload = {
        "checked_epoch": now,
        "checked_at": _now_iso(),
        "holder_rows": holder_frame.to_dict("records"),
        "candidate_rows": candidate_frame.to_dict("records"),
        "wallets": wallet_cache,
    }
    _atomic_json(cache_path, cache_payload)

    return {
        "status": "DONE",
        "cached": False,
        "tokens": int(len(token_scores)),
        "holders": int(len(holder_frame)),
        "candidate_wallets": int(len(candidate_frame)),
        "rpc_calls": int(sum(router.provider_calls.values())),
        "quicknode_credits": int(budget.spent_run),
        "provider_calls": router.provider_calls,
        "provider_errors": router.provider_errors,
        "budget": budget.summary(),
        "holders_output": str(holder_out),
        "output": str(candidate_out),
    }


def merge_quicknode_with_birdeye(
    birdeye_enriched_path: Path,
    quicknode_candidate_path: Path,
    output_path: Path,
) -> dict:
    """Merge discovery-only QuickNode evidence without manufacturing performance metrics."""
    birdeye = _read_csv(birdeye_enriched_path)
    quicknode = _read_csv(quicknode_candidate_path)

    if birdeye.empty and quicknode.empty:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "birdeye_wallets": 0, "quicknode_wallets": 0}

    frames = []
    if not birdeye.empty and "address" in birdeye.columns:
        b = birdeye.copy()
        b["candidate_sources"] = b.get("candidate_sources", "birdeye")
        b["candidate_sources"] = b["candidate_sources"].fillna("birdeye").replace("", "birdeye")
        frames.append(b)
    if not quicknode.empty and "address" in quicknode.columns:
        q = quicknode.copy()
        q["candidate_sources"] = q.get("candidate_sources", "quicknode")
        q["candidate_sources"] = q["candidate_sources"].fillna("quicknode").replace("", "quicknode")
        frames.append(q)

    if not frames:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "birdeye_wallets": 0, "quicknode_wallets": 0}

    joined = pd.concat(frames, ignore_index=True, sort=False)
    joined["address"] = joined["address"].astype(str).str.strip()
    joined = joined[joined["address"].ne("") & joined["address"].ne("nan")].copy()

    merged_rows: list[dict] = []
    for _address, group in joined.groupby("address", sort=False):
        records = group.to_dict("records")
        records.sort(
            key=lambda rec: sum(0 if _is_missing(v) else 1 for v in rec.values()),
            reverse=True,
        )
        base = dict(records[0])
        sources: set[str] = set()
        for rec in records:
            for source in str(rec.get("candidate_sources", "") or "").split("|"):
                source = source.strip()
                if source:
                    sources.add(source)
            for key, value in rec.items():
                if key not in base or _is_missing(base.get(key)):
                    if not _is_missing(value):
                        base[key] = value

        for key in (
            "cross_token_hits",
            "independent_cross_token_hits",
            "weighted_cross_token_score",
            "discovery_score",
        ):
            values = [_num(rec.get(key), float("-inf")) for rec in records if not _is_missing(rec.get(key))]
            if values:
                base[key] = max(values)

        token_set: set[str] = set()
        for rec in records:
            for token in str(rec.get("radar_tokens", "") or "").split("|"):
                token = token.strip()
                if token and token.lower() != "nan":
                    token_set.add(token)
        if token_set:
            base["radar_tokens"] = "|".join(sorted(token_set))

        base["candidate_sources"] = "|".join(sorted(sources)) if sources else "unknown"
        base["quicknode_cross_provider_confirmed"] = bool({"quicknode", "birdeye"}.issubset(sources))
        merged_rows.append(base)

    merged = pd.DataFrame(merged_rows)
    if not merged.empty:
        sort_cols = [c for c in ["quicknode_cross_provider_confirmed", "discovery_score", "cross_token_hits"] if c in merged.columns]
        if sort_cols:
            merged = merged.sort_values(sort_cols, ascending=[False] * len(sort_cols), kind="mergesort")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_path, index=False)
    return {
        "status": "DONE",
        "wallets": int(len(merged)),
        "birdeye_wallets": int(len(birdeye)),
        "quicknode_wallets": int(len(quicknode)),
        "cross_provider_wallets": int(merged.get("quicknode_cross_provider_confirmed", pd.Series(dtype=bool)).fillna(False).sum()),
        "output": str(output_path),
    }


def quicknode_solana_enabled() -> bool:
    from .config import env_bool

    return env_bool("PEIXAO_QUICKNODE_SOLANA", True)


def run_quicknode_solana_from_settings(cfg) -> dict:
    """Mesma configuração para o ciclo rápido (30 min) e o ciclo de 6h.

    Antes cada ciclo tinha seus próprios padrões (25 vs 30 wallets, TTL 1800 vs
    3600) para as mesmas variáveis de ambiente.
    """
    from .config import cost_int, env_float, env_int

    if not quicknode_solana_enabled():
        return {"status": "DISABLED", "rpc_calls": 0, "quicknode_credits": 0}
    return run_quicknode_solana_discovery(
        cfg.output_dir / "V22_token_radar_shortlist.csv",
        cfg.output_dir,
        cfg.state_dir,
        quicknode_rpc_url=cfg.quicknode_rpc_url,
        helius_rpc_url=cfg.helius_rpc_url,
        shyft_rpc_url=cfg.shyft_rpc_url,
        enabled=True,
        timeout=env_float("PEIXAO_QUICKNODE_TIMEOUT", 20.0),
        delay=env_float("PEIXAO_QUICKNODE_DELAY", 0.075),
        max_tokens=env_int("PEIXAO_QUICKNODE_MAX_TOKENS", 5),
        max_wallets=env_int("PEIXAO_QUICKNODE_MAX_WALLETS", 30),
        tx_per_wallet=env_int("PEIXAO_QUICKNODE_TX_PER_WALLET", 80),
        daily_credit_budget=cost_int("PEIXAO_QUICKNODE_DAILY_CREDITS"),
        run_credit_budget=cost_int("PEIXAO_QUICKNODE_RUN_CREDITS"),
        call_credit_estimate=env_int("PEIXAO_QUICKNODE_CALL_CREDITS", 30),
        cache_ttl_seconds=cost_int("PEIXAO_QUICKNODE_CACHE_TTL"),
    )


def run_quicknode_solana_budgeted(cfg) -> dict:
    """QuickNode Solana respeitando rede ativa e teto diário de créditos."""
    from .config import env_int
    from .cost_control import quicknode_credits, run_paid_step, solana_fallback_calls

    if not cfg.chain_enabled("solana"):
        return {"status": "CHAIN_DISABLED", "chain": "solana", "quicknode_credits": 0}
    return run_paid_step(
        cfg.master_db, "QUICKNODE", "quicknode_solana", lambda remaining: run_quicknode_solana_from_settings(cfg),
        units_from=quicknode_credits, extra_spend=solana_fallback_calls, chain="solana",
        min_units=float(env_int("PEIXAO_QUICKNODE_CALL_CREDITS", 30)),
    )
