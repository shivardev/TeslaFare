from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    photon_base_url: str = os.getenv("PHOTON_BASE_URL", "https://photon.komoot.io")
    osrm_base_url: str = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org")
    supercharge_info_url: str = os.getenv(
        "SUPERCHARGE_INFO_URL", "https://supercharge.info/service/supercharge/allSites"
    )
    tesla_findus_base_url: str = os.getenv(
        "TESLA_FINDUS_BASE_URL", "https://www.tesla.com/findus/location/supercharger"
    )
    timezone_api_url: str = os.getenv(
        "TIMEZONE_API_URL", "https://timeapi.io/api/timezone/coordinate"
    )

    departure_search_hours: int = int(os.getenv("DEPARTURE_SEARCH_HOURS", "24"))
    departure_interval_minutes: int = int(os.getenv("DEPARTURE_INTERVAL_MINUTES", "30"))
    corridor_miles: float = float(os.getenv("CORRIDOR_MILES", "50"))
    max_candidate_chargers: int = int(os.getenv("MAX_CANDIDATE_CHARGERS", "24"))
    max_route_detour_percent: float = float(os.getenv("MAX_ROUTE_DETOUR_PERCENT", "15"))
    max_charger_detour_minutes: int = int(os.getenv("MAX_CHARGER_DETOUR_MINUTES", "15"))
    max_total_extra_driving_minutes: int = int(os.getenv("MAX_TOTAL_EXTRA_DRIVING_MINUTES", "60"))
    max_results_per_departure: int = int(os.getenv("MAX_RESULTS_PER_DEPARTURE", "3"))

    pricing_cache_hours: int = int(os.getenv("PRICING_CACHE_HOURS", "6"))
    timezone_cache_days: int = int(os.getenv("TIMEZONE_CACHE_DAYS", "180"))
    tesla_playwright_fallback: bool = _bool("TESLA_PLAYWRIGHT_FALLBACK", True)
    tesla_playwright_max_fallbacks: int = int(os.getenv("TESLA_PLAYWRIGHT_MAX_FALLBACKS", "3"))

    cache_db_path: Path = Path(os.getenv("CACHE_DB_PATH", ".data/cache.sqlite3"))
    request_timeout_seconds: float = 20.0
    pricing_request_timeout_seconds: float = float(os.getenv("PRICING_REQUEST_TIMEOUT_SECONDS", "8"))
    user_agent: str = "tesla-cheap-trip/0.1 personal-project"


settings = Settings()
