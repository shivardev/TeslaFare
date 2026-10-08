"""Guardrails for prices sent in by browsers (the owner's helper and visitors' helpers).

Every submission: Tesla's exact payload shape, sane price bounds, and the right station.
Visitor (community) submissions additionally: a big change from the saved price waits for a second,
independent report; a per-IP hourly cap; and an audit log that the owner can undo by contributor.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import PricingSchedule

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_PRICEBOOK_ROWS = 60
MIN_PRICE_PER_KWH = 0.05
MAX_PRICE_PER_KWH = 1.50
HHMM = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


class PriceRejected(ValueError):
    """The submission isn't acceptable; the message says why (shown to the submitter)."""


def validate_payload_shape(payload: Any) -> None:
    """Must look like Tesla's get-charger-details response."""
    try:
        size = len(json.dumps(payload))
    except (TypeError, ValueError) as exc:
        raise PriceRejected("Payload isn't valid JSON") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise PriceRejected("Payload is too large to be a Tesla station response")
    node = payload
    while isinstance(node, dict) and isinstance(node.get("data"), dict):
        node = node["data"]
    rows = node.get("effectivePricebooks") if isinstance(node, dict) else None
    if not isinstance(rows, list) or not rows:
        raise PriceRejected("No Tesla pricebook in that data")
    if len(rows) > MAX_PRICEBOOK_ROWS:
        raise PriceRejected("Too many pricebook rows")
    for row in rows:
        if not isinstance(row, dict):
            raise PriceRejected("Malformed pricebook row")
        if not isinstance(row.get("feeType", ""), str) or not isinstance(row.get("uom", ""), str):
            raise PriceRejected("Malformed pricebook row")
        rate = row.get("rateBase")
        if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float))):
            raise PriceRejected("Price isn't a number")
        for key in ("startTime", "endTime"):
            value = row.get(key)
            if value not in (None, "") and (not isinstance(value, str) or not HHMM.match(value)):
                raise PriceRejected("Malformed time-of-use time")
        days = row.get("days")
        if days not in (None, "") and (not isinstance(days, str) or not re.fullmatch(r"[0-6](,[0-6])*", days.replace(" ", ""))):
            raise PriceRejected("Malformed time-of-use days")


def check_price_bounds(schedule: PricingSchedule) -> None:
    if not schedule.bands or len(schedule.bands) > 48:
        raise PriceRejected("Unexpected number of price bands")
    for band in schedule.bands:
        if not MIN_PRICE_PER_KWH <= band.price_per_kwh <= MAX_PRICE_PER_KWH:
            raise PriceRejected(
                f"${band.price_per_kwh:.2f}/kWh is outside the accepted range "
                f"(${MIN_PRICE_PER_KWH:.2f}–${MAX_PRICE_PER_KWH:.2f})"
            )
        if not (0 <= band.start_minute < 1440 and 0 <= band.end_minute <= 1440):
            raise PriceRejected("Price band times are out of range")


def names_other_station(payload: Any, station_id: str, known_ids: set[str]) -> bool:
    """True if the payload itself identifies a different known station."""
    stack, seen = [payload], 0
    while stack and seen < 2000:
        item = stack.pop()
        seen += 1
        if isinstance(item, str):
            if item in known_ids and item != station_id:
                return True
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def signature(schedule: PricingSchedule) -> tuple:
    return tuple(sorted((b.start_minute, b.end_minute, round(b.price_per_kwh, 2)) for b in schedule.bands))


def big_change(old: PricingSchedule | None, new: PricingSchedule, threshold: float) -> bool:
    """More than `threshold` (0.5 = 50%) away from the saved price at its cheapest or dearest."""
    if old is None or not old.bands:
        return False
    old_prices = [b.price_per_kwh for b in old.bands]
    new_prices = [b.price_per_kwh for b in new.bands]
    for before, after in ((min(old_prices), min(new_prices)), (max(old_prices), max(new_prices))):
        if before > 0 and abs(after - before) / before > threshold:
            return True
    return False


def contributor_id(ip: str, salt: str) -> str:
    """A short, salted hash of the submitter's IP: enough to group and undo, without storing IPs."""
    return hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()[:12]


class CommunityGate:
    """Decides whether a visitor's price goes live, waits for confirmation, or is refused; logs every decision."""

    def __init__(self, audit_path: Path, per_hour: int, change_threshold: float):
        self.audit_path = audit_path
        self.per_hour = per_hour
        self.change_threshold = change_threshold
        self._lock = threading.Lock()
        self._accepted_at: dict[str, deque] = defaultdict(deque)
        # station id -> signature -> {contributor ids}, for big changes waiting on a second report
        self._pending: dict[str, dict[tuple, set[str]]] = defaultdict(dict)

    def decide(self, station_id: str, schedule: PricingSchedule, contributor: str, saved: PricingSchedule | None) -> str:
        """'accepted', 'pending' (big change, needs a second independent report) or 'rate_limited'."""
        now = time.time()
        with self._lock:
            recent = self._accepted_at[contributor]
            while recent and now - recent[0] > 3600:
                recent.popleft()
            if self.per_hour > 0 and len(recent) >= self.per_hour:
                return "rate_limited"
            if saved is not None and saved.bands and signature(saved) == signature(schedule):
                recent.append(now)
                return "accepted"  # confirms what we already have
            if self.change_threshold > 0 and big_change(saved, schedule, self.change_threshold):
                reporters = self._pending[station_id].setdefault(signature(schedule), set())
                reporters.add(contributor)
                if len(reporters) < 2:
                    return "pending"
                self._pending.pop(station_id, None)
            recent.append(now)
            return "accepted"

    def log(self, entry: dict) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(), **entry}
        with self._lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")

    def recent(self, limit: int = 300) -> list[dict]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-limit:]
        entries = []
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
        return entries

    def accepted_by(self, contributor: str) -> list[dict]:
        if not self.audit_path.exists():
            return []
        out = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("contributor") == contributor and entry.get("outcome") == "accepted":
                out.append(entry)
        return out
