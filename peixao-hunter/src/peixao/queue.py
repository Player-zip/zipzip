from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json


VALID_STATUSES = {"PENDING", "DISCOVERY", "VALIDATING", "ENRICHING", "DONE", "RETRY", "FAILED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class QueueItem:
    key: str
    kind: str = "wallet"
    status: str = "PENDING"
    priority: str = "P3"
    attempts: int = 0
    last_error: str | None = None
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid queue status: {self.status}")
        if not self.updated_at:
            self.updated_at = utc_now()


class JsonQueue:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[QueueItem]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [QueueItem(**item) for item in raw]

    def save(self, items: list[QueueItem]) -> None:
        payload = [asdict(item) for item in items]
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert(self, item: QueueItem) -> list[QueueItem]:
        items = self.load()
        by_key = {row.key.lower(): row for row in items}
        by_key[item.key.lower()] = item
        merged = list(by_key.values())
        self.save(merged)
        return merged
