from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import statistics
import time

import requests


ZERION_BASE_URL = "https://api.zerion.io/v1"


def _float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _find_number(obj, names: tuple[str, ...]):
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                value = _float(obj.get(name))
                if value is not None:
                    return value
        for value in obj.values():
            if isinstance(value, dict):
                found = _find_number(value, names)
                if found is not None:
                    return found
    return None


def _auth_header(api_key: str) -> str:
    encoded = base64.b64encode((api_key + ":").encode("utf-8")).decode("ascii")
    return "Basic " + encoded


def _asset_nodes(attributes: dict) -> list[dict]:
    breakdown = attributes.get("breakdown")
    if not isinstance(breakdown, dict):
        return []
    by_id = breakdown.get("by_id")
    if isinstance(by_id, dict):
        return [value for value in by_id.values() if isinstance(value, dict)]
    if isinstance(by_id, list):
        return [value for value in by_id if isinstance(value, dict)]
    return []


def _normalize_roi_percentage(value):
    """Zerion fields ending in *_percentage are percentage points, not ratios."""
    roi = _float(value)
    if roi is None:
        return None
    return roi / 100.0


def _parse_pnl(payload: dict, *, chain: str) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        return {}

    realized = _find_number(attributes, ("realized_gain", "realized_pnl", "realized_profit"))
    raw_roi_percent = _find_number(
        attributes,
        (
            "relative_realized_gain_percentage",
            "realized_gain_percentage",
            "relative_total_gain_percentage",
        ),
    )
    roi = _normalize_roi_percentage(raw_roi_percent)

    pnls: list[float] = []
    for node in _asset_nodes(attributes):
        pnl = _find_number(node, ("realized_gain", "realized_pnl", "realized_profit"))
        cost = _find_number(node, ("realized_cost_basis", "cost_basis"))
        if (cost is not None and cost > 0) or (pnl is not None and pnl != 0):
            pnls.append(float(pnl or 0.0))

    closed = len(pnls)
    wins = [value for value in pnls if value > 0]
    win_rate = (len(wins) / closed) if closed else None
    positive_total = sum(wins)
    shares = sorted((value / positive_total for value in wins), reverse=True) if positive_total > 0 else []

    result = {
        "zerion_chain": chain,
        "zerion_evidence": True,
        "zerion_pnl_method": "FIFO",
        "zerion_roi_unit": "ratio",
        "realized_roi_unit": "ratio",
        "zerion_roi_raw_percent": raw_roi_percent,
        "realized_profit_30d": realized,
        "realized_roi_30d": roi,
        "zerion_total_fee_30d": _find_number(attributes, ("total_fee",)),
        "zerion_net_invested_30d": _find_number(attributes, ("net_invested",)),
        "zerion_unrealized_gain_30d": _find_number(attributes, ("unrealized_gain",)),
    }

    if closed:
        result.update({
            "win_rate": win_rate,
            "gmgn_winrate_30d": win_rate,
            "closed_positions": closed,
            "winning_tokens": len(wins),
            "tokens_traded": closed,
            "repeatability_score": win_rate,
            "median_pnl_per_token": float(statistics.median(pnls)),
            "profit_hhi": sum(share * share for share in shares) if shares else None,
            "largest_win_share": shares[0] if shares else None,
            "top3_profit_share": sum(shares[:3]) if shares else None,
            "new_positions_per_week": closed / (30.0 / 7.0),
            "new_positions_per_week_source": "zerion_realized_asset_breakdown_30d",
        })
    return result


def _retry_delay(response, fallback: float) -> float:
    try:
        header = response.headers.get("Retry-After")
        if header is not None:
            return max(float(header), fallback)
    except Exception:
        pass
    return fallback


def fetch_zerion_wallet_pnl(
    api_key: str,
    address: str,
    chain: str,
    *,
    timeout: float = 15.0,
    lookback_days: int = 30,
) -> tuple[dict | None, dict]:
    """Fetch chain-scoped Zerion PnL with bounded 429/503 backoff."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, int(lookback_days)))
    params = {
        "currency": "usd",
        "filter[chain_ids]": chain,
        "since": str(int(since.timestamp() * 1000)),
        "till": str(int(now.timestamp() * 1000)),
    }
    headers = {
        "Accept": "application/json",
        "Authorization": _auth_header(api_key),
    }

    calls = 0
    statuses: list[int] = []
    last_payload: dict = {}
    for attempt in range(3):
        try:
            response = requests.get(
                f"{ZERION_BASE_URL}/wallets/{address}/pnl",
                params=params,
                headers=headers,
                timeout=float(timeout),
            )
        except requests.RequestException as exc:
            return None, {"http_calls": calls, "statuses": statuses, "error": f"{type(exc).__name__}: {exc}"}

        calls += 1
        statuses.append(int(response.status_code))
        try:
            payload = response.json()
            last_payload = payload if isinstance(payload, dict) else {}
        except Exception:
            last_payload = {}

        if response.status_code == 200:
            metrics = _parse_pnl(last_payload, chain=chain)
            return (metrics if metrics else None), {"http_calls": calls, "statuses": statuses}

        if response.status_code in {429, 503} and attempt < 2:
            fallback = 1.2 if response.status_code == 429 else 1.0
            time.sleep(min(8.0, _retry_delay(response, fallback)))
            continue
        break

    return None, {
        "http_calls": calls,
        "statuses": statuses,
        "error": f"HTTP_{statuses[-1] if statuses else 'NETWORK'}",
    }
