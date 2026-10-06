from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import Charger, PricingSchedule


class ChargerKnowledgeStore:
    """Durable, human-readable Supercharger knowledge keyed by Tesla location ID."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "chargers": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("chargers"), dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": 1, "chargers": {}}

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        payload["version"] = 1
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def remember_station(self, charger: Charger) -> None:
        self.remember_stations([charger])

    def remember_stations(self, chargers: list[Charger]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            payload = self._read_unlocked()
            for charger in chargers:
                record = payload["chargers"].setdefault(charger.location_id, {})
                record.update({
                    "location_id": charger.location_id,
                    "name": charger.name,
                    "coordinate": charger.coordinate.model_dump(),
                    "address": charger.address,
                    "status": charger.status,
                    "stalls": charger.stalls,
                    "power_kw": charger.power_kw,
                    "tesla_url": charger.tesla_url,
                    "station_updated_at": now,
                })
            self._write_unlocked(payload)

    def remember_pricing(self, charger: Charger, schedule: PricingSchedule) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            payload = self._read_unlocked()
            record = payload["chargers"].setdefault(charger.location_id, {})
            record.update({
                "location_id": charger.location_id,
                "name": charger.name,
                "coordinate": charger.coordinate.model_dump(),
                "address": charger.address,
                "status": charger.status,
                "stalls": charger.stalls,
                "power_kw": charger.power_kw,
                "tesla_url": charger.tesla_url,
                "station_updated_at": now,
            })
            record["pricing"] = schedule.model_dump(mode="json")
            record["pricing_updated_at"] = now
            self._write_unlocked(payload)

    def pricing(self, location_id: str) -> PricingSchedule | None:
        with self._lock:
            record = self._read_unlocked()["chargers"].get(location_id)
        if not isinstance(record, dict) or not isinstance(record.get("pricing"), dict):
            return None
        try:
            return PricingSchedule.model_validate(record["pricing"])
        except (TypeError, ValueError):
            return None

    def remember_manual_pricing(self, location_id: str, schedule: PricingSchedule) -> bool:
        with self._lock:
            payload = self._read_unlocked()
            record = payload["chargers"].get(location_id)
            if not isinstance(record, dict):
                return False
            record["pricing"] = schedule.model_dump(mode="json")
            record["pricing_updated_at"] = datetime.now(timezone.utc).isoformat()
            record["pricing_source"] = "manual"
            self._write_unlocked(payload)
        return True

    def count(self) -> int:
        with self._lock:
            return len(self._read_unlocked()["chargers"])
