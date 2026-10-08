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
    census_geocoder_url: str = os.getenv("CENSUS_GEOCODER_URL", "https://geocoding.geo.census.gov")
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
    optimizer_max_states: int = int(os.getenv("OPTIMIZER_MAX_STATES", "5000"))

    pricing_cache_hours: int = int(os.getenv("PRICING_CACHE_HOURS", "6"))
    timezone_cache_days: int = int(os.getenv("TIMEZONE_CACHE_DAYS", "180"))
    tesla_playwright_fallback: bool = _bool("TESLA_PLAYWRIGHT_FALLBACK", True)
    tesla_playwright_max_fallbacks: int = int(os.getenv("TESLA_PLAYWRIGHT_MAX_FALLBACKS", "40"))
    tesla_playwright_headless: bool = _bool("TESLA_PLAYWRIGHT_HEADLESS", True)
    tesla_playwright_browser: str = os.getenv("TESLA_PLAYWRIGHT_BROWSER", "firefox")
    tesla_browser_backend: str = os.getenv("TESLA_BROWSER_BACKEND", "selenium")

    cache_db_path: Path = Path(os.getenv("CACHE_DB_PATH", ".data/cache.sqlite3"))
    trip_share_days: int = int(os.getenv("TRIP_SHARE_DAYS", "7"))
    charger_knowledge_path: Path = Path(os.getenv("CHARGER_KNOWLEDGE_PATH", ".data/superchargers.json"))
    request_timeout_seconds: float = 20.0
    pricing_request_timeout_seconds: float = float(os.getenv("PRICING_REQUEST_TIMEOUT_SECONDS", "8"))
    # A trip waits at most this long for live prices; stations still pending use the fallback estimate.
    pricing_deadline_seconds: float = float(os.getenv("PRICING_DEADLINE_SECONDS", "45"))
    # A station whose lookup failed is not retried for this long (stops one bad station stalling every trip).
    # Cap on requests to tesla.com per hour across all visitors, so public traffic can't trigger Tesla's bot block.
    live_price_lookups_per_hour: int = int(os.getenv("LIVE_PRICE_LOOKUPS_PER_HOUR", "30"))
    # Prices committed to git; merged into the local store at startup so fresh clones start with known prices.
    price_seed_path: Path = Path(os.getenv("PRICE_SEED_PATH", "app/data/price_seed.json"))
    # Prices come from the host's browser userscript (/collect). The server only contacts Tesla itself when
    # SERVER_PRICE_LOOKUPS=true (headless Firefox, background refresher).
    server_price_lookups: bool = _bool("SERVER_PRICE_LOOKUPS", False)
    collector_key: str = os.getenv("COLLECTOR_KEY", "")
    # Off by default: anyone's helper can send prices. Set REQUIRE_COLLECTOR_KEY=true to require a key again.
    require_collector_key: bool = _bool("REQUIRE_COLLECTOR_KEY", False)
    # Visitors' captured prices also go into the shared prices, behind guardrails (shape, bounds, a second
    # report for big changes, a per-IP hourly cap, and an audit log the owner can undo).
    community_prices: bool = _bool("COMMUNITY_PRICES", True)
    # Behind Cloudflare or a reverse proxy every request comes from the proxy; name the header that carries the
    # visitor's real IP (e.g. CF-Connecting-IP or X-Forwarded-For). Leave empty when visitors connect directly.
    client_ip_header: str = os.getenv("CLIENT_IP_HEADER", "")
    # Optional extra rules, off by default (0): a per-IP hourly cap, and holding a change bigger than this
    # fraction (e.g. 0.5 = 50%) until a second independent report confirms it.
    community_per_hour: int = int(os.getenv("COMMUNITY_PER_HOUR", "0"))
    community_change_threshold: float = float(os.getenv("COMMUNITY_CHANGE_THRESHOLD", "0"))
    # Extra people allowed to send prices: "alice:key1,bob:key2". Prices record who sent them.
    contributor_keys: str = os.getenv("CONTRIBUTOR_KEYS", "")
    # After a collector reports Tesla's "Access Denied", collection pauses this long.
    collector_pause_minutes: float = float(os.getenv("COLLECTOR_PAUSE_MINUTES", "30"))
    # Public address of this server (e.g. https://teslaflare.blazingbane.com); used in the userscript.
    public_url: str = os.getenv("PUBLIC_URL", "")
    # Shared price database. Every instance pulls it daily; with CENTRAL_CONTRIBUTOR_KEY set, prices
    # collected here are also forwarded to it. Set CENTRAL_PRICE_URL= (empty) to turn both off.
    central_price_url: str = os.getenv("CENTRAL_PRICE_URL", "https://teslaflare.blazingbane.com")
    central_contributor_key: str = os.getenv("CENTRAL_CONTRIBUTOR_KEY", "")
    central_pull_hours: float = float(os.getenv("CENTRAL_PULL_HOURS", "24"))
    price_fresh_hours: float = float(os.getenv("PRICE_FRESH_HOURS", "24"))
    # Background refresh: stations per hour (0 turns it off), how old a price must be to refresh it,
    # which countries to cover, and how many of the hourly requests are always kept for visitors.
    price_refresh_per_hour: int = int(os.getenv("PRICE_REFRESH_PER_HOUR", "20"))
    price_refresh_stale_days: float = float(os.getenv("PRICE_REFRESH_STALE_DAYS", "7"))
    price_refresh_countries: str = os.getenv("PRICE_REFRESH_COUNTRIES", "USA")
    price_refresh_visitor_reserve: int = int(os.getenv("PRICE_REFRESH_VISITOR_RESERVE", "10"))
    # Prices typed in by visitors only apply to their own trips. Set true to let entered prices be saved for everyone.
    allow_shared_manual_prices: bool = _bool("ALLOW_SHARED_MANUAL_PRICES", False)
    pricing_retry_failed_minutes: float = float(os.getenv("PRICING_RETRY_FAILED_MINUTES", "30"))
    user_agent: str = "teslafare/0.1 open-source-route-planner"


settings = Settings()
