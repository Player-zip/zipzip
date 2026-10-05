from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Callable


DEFAULT_STAGE_LIMITS = {
    "radar": 0,
    "tx_origin": 40,
    "classification": 15,
    "wallet_validation": 5,
    "deep_dive": 20,
}


class RpcBudgetManager:
    """Persistent, fail-closed RPC cost guard.

    Every physical HTTP attempt consumes one unit before it is sent, including
    retries. Unknown stages have a zero budget by default. This makes accidental
    new RPC paths cheap-by-default instead of silently expanding usage.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        enabled: bool = True,
        run_limit: int = 60,
        hourly_limit: int = 60,
        daily_limit: int = 240,
        stage_limits: dict[str, int] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.enabled = bool(enabled)
        self.run_limit = max(0, int(run_limit))
        self.hourly_limit = max(0, int(hourly_limit))
        self.daily_limit = max(0, int(daily_limit))
        self.stage_limits = dict(DEFAULT_STAGE_LIMITS)
        if stage_limits:
            self.stage_limits.update({str(k): max(0, int(v)) for k, v in stage_limits.items()})
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._run_used = 0
        self._stage_used: Counter[str] = Counter()
        self._cache_hits: Counter[str] = Counter()
        self._skipped: Counter[str] = Counter()
        self._denied_reasons: Counter[str] = Counter()
        self._provider_used: Counter[str] = Counter()
        self._init_db()

    def _now(self) -> datetime:
        value = self._now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rpc_budget_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    hour_key TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    method TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rpc_budget_day ON rpc_budget_usage(day_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rpc_budget_hour ON rpc_budget_usage(hour_key)")

    def _persistent_usage(self, *, day_key: str | None = None, hour_key: str | None = None) -> int:
        where = []
        params: list[str] = []
        if day_key is not None:
            where.append("day_key = ?")
            params.append(day_key)
        if hour_key is not None:
            where.append("hour_key = ?")
            params.append(hour_key)
        sql = "SELECT COALESCE(SUM(cost), 0) FROM rpc_budget_usage"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            return int(conn.execute(sql, params).fetchone()[0] or 0)

    def note_cache_hit(self, stage: str, count: int = 1) -> None:
        count = max(0, int(count))
        if count:
            self._cache_hits[str(stage)] += count

    def _deny(self, stage: str, reason: str) -> bool:
        self._skipped[str(stage)] += 1
        self._denied_reasons[reason] += 1
        return False

    def authorize(self, stage: str, provider: str, method: str, *, cost: int = 1) -> bool:
        """Reserve budget for one physical RPC attempt before sending it."""
        stage = str(stage)
        provider = str(provider or "unknown")
        method = str(method or "unknown")
        cost = max(1, int(cost))
        if not self.enabled:
            return True

        stage_limit = max(0, int(self.stage_limits.get(stage, 0)))
        if self._stage_used[stage] + cost > stage_limit:
            return self._deny(stage, "stage_limit")
        if self._run_used + cost > self.run_limit:
            return self._deny(stage, "run_limit")

        now = self._now()
        day_key = now.strftime("%Y-%m-%d")
        hour_key = now.strftime("%Y-%m-%dT%H")
        hourly_used = self._persistent_usage(hour_key=hour_key)
        if hourly_used + cost > self.hourly_limit:
            return self._deny(stage, "hourly_limit")
        daily_used = self._persistent_usage(day_key=day_key)
        if daily_used + cost > self.daily_limit:
            return self._deny(stage, "daily_limit")

        occurred_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                """
                INSERT INTO rpc_budget_usage (
                    occurred_at, day_key, hour_key, stage, provider, method, cost, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ATTEMPT')
                """,
                (occurred_at, day_key, hour_key, stage, provider, method, cost),
            )
        self._run_used += cost
        self._stage_used[stage] += cost
        self._provider_used[provider] += cost
        return True

    def summary(self) -> dict:
        now = self._now()
        day_key = now.strftime("%Y-%m-%d")
        hour_key = now.strftime("%Y-%m-%dT%H")
        stage_names = sorted(set(self.stage_limits) | set(self._stage_used) | set(self._cache_hits) | set(self._skipped))
        stages = {
            stage: {
                "budget": int(self.stage_limits.get(stage, 0)),
                "used": int(self._stage_used.get(stage, 0)),
                "cache_hits": int(self._cache_hits.get(stage, 0)),
                "skipped_budget": int(self._skipped.get(stage, 0)),
            }
            for stage in stage_names
        }
        return {
            "status": "ENABLED" if self.enabled else "DISABLED",
            "run_budget": int(self.run_limit),
            "run_used": int(self._run_used),
            "run_remaining": max(0, int(self.run_limit - self._run_used)),
            "hourly_budget": int(self.hourly_limit),
            "hourly_used": int(self._persistent_usage(hour_key=hour_key)) if self.enabled else None,
            "daily_budget": int(self.daily_limit),
            "daily_used": int(self._persistent_usage(day_key=day_key)) if self.enabled else None,
            "cache_hits": int(sum(self._cache_hits.values())),
            "skipped_budget": int(sum(self._skipped.values())),
            "denied_reasons": dict(sorted(self._denied_reasons.items())),
            "provider_used": dict(sorted(self._provider_used.items())),
            "stages": stages,
            "fail_closed_unknown_stage": True,
        }
