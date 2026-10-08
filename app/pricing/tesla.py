from __future__ import annotations

import asyncio
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

import httpx

from app.db.cache import CacheDB
from app.models import Charger, PriceBand, PricingSchedule
from app.pricing.base import PriceProvider


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def _minute(hh: str, mm: str, ampm: str) -> int:
    hour = int(hh) % 12
    if ampm.upper() == "PM":
        hour += 12
    return hour * 60 + int(mm)


def _hhmm_minute(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _minute_rates(row: dict) -> list[float]:
    """$/min by power tier (<=60, 60-100, 100-180, >180 kW) from a per-minute pricebook row."""
    four = [row.get(f"rateMinTier{i}") for i in range(1, 5)]
    if all(isinstance(v, (int, float)) for v in four):
        return [float(v) for v in four]
    two = [row.get("rateTier1"), row.get("rateTier2")]  # older two-tier sites: <=60 kW and above
    if all(isinstance(v, (int, float)) and v > 0 for v in two):
        return [float(two[0]), float(two[1]), float(two[1]), float(two[1])]
    base = row.get("rateBase")
    return [float(base)] * 4 if isinstance(base, (int, float)) and base > 0 else []


def _reference_kwh_price(rates: list[float]) -> float:
    """A typical $/kWh for per-minute rates (10-80% on a 250 kW, 75 kWh car), for display and comparisons.
    Plans use the real per-minute cost for the selected car instead."""
    from app.config.vehicle import CURVE_NCA_250
    from app.pricing.base import minute_rate_for_power
    from app.vehicle.charging import ChargingModel
    model = ChargingModel(75.0, CURVE_NCA_250)
    cost = sum(minutes * minute_rate_for_power(rates, kw) for minutes, kw in model.segments(10, 80))
    return round(cost / model.kwh_between(10, 80), 4)


def _parse_per_minute(node: dict, source_url: str | None) -> PricingSchedule | None:
    rows = [
        row for row in node.get("effectivePricebooks", [])
        if row.get("feeType") == "CHARGING" and str(row.get("uom", "")).lower() == "min" and row.get("vehicleMakeType") == "TSLA"
    ]
    rows = [row for row in rows if _minute_rates(row)]
    if not rows:
        return None
    congestion = next(
        (row.get("rateBase") for row in node.get("effectivePricebooks", [])
         if row.get("feeType") == "CONGESTION" and row.get("vehicleMakeType") == "TSLA" and isinstance(row.get("rateBase"), (int, float))),
        None,
    )
    tou_rows = [row for row in rows if row.get("isTou") and row.get("startTime") and row.get("endTime")]
    def band(row, start, end, days):
        rates = _minute_rates(row)
        return PriceBand(start_minute=start, end_minute=end, price_per_kwh=_reference_kwh_price(rates), days=days, minute_rates=rates)
    if tou_rows:
        bands = [
            band(row, _hhmm_minute(row["startTime"]), _hhmm_minute(row["endTime"]),
                 [int(d) for d in str(row.get("days", "")).split(",") if d.strip().isdigit()])
            for row in tou_rows
        ]
        kind = "time_of_use"
    else:
        bands = [band(rows[0], 0, 0, [])]
        kind = "flat"
    return PricingSchedule(kind=kind, unit="minute", bands=bands, source_url=source_url, timezone=node.get("timeZone"),
                           congestion_per_minute=float(congestion) if congestion else None)


def parse_tesla_pricing_payload(payload: dict, source_url: str | None = None) -> PricingSchedule:
    """Parse the public Find Us ``get-charger-details`` response.

    Only Tesla-vehicle charging rows are considered: per-kWh, or else per-minute (by power tier).
    Congestion/idle fees and non-Tesla pricebooks are deliberately excluded from the price.
    """
    node = payload
    while isinstance(node, dict) and isinstance(node.get("data"), dict):
        node = node["data"]
    if not isinstance(node, dict):
        return PricingSchedule(kind="unknown", source_url=source_url, note="Invalid charger-details payload")

    rows = [
        row for row in node.get("effectivePricebooks", [])
        if row.get("feeType") == "CHARGING"
        and str(row.get("uom", "")).lower() == "kwh"
        and row.get("vehicleMakeType") == "TSLA"
    ]
    tou_rows = [row for row in rows if row.get("isTou") and row.get("startTime") and row.get("endTime")]
    if tou_rows:
        bands = [
            PriceBand(
                start_minute=_hhmm_minute(row["startTime"]),
                end_minute=_hhmm_minute(row["endTime"]),
                price_per_kwh=float(row["rateBase"]),
                days=[int(day) for day in str(row.get("days", "")).split(",") if day.strip().isdigit()],
            )
            for row in tou_rows
        ]
        return PricingSchedule(
            kind="time_of_use", bands=bands, source_url=source_url, timezone=node.get("timeZone")
        )

    flat_rows = [row for row in rows if not row.get("isTou") and row.get("rateBase") is not None]
    if flat_rows:
        return PricingSchedule(
            kind="flat",
            bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=float(flat_rows[0]["rateBase"]))],
            source_url=source_url,
            timezone=node.get("timeZone"),
        )
    per_minute = _parse_per_minute(node, source_url)
    if per_minute is not None:
        return per_minute
    return PricingSchedule(kind="unknown", source_url=source_url, note="No Tesla charging pricebook found")


def parse_tesla_pricing_text(text: str, source_url: str | None = None) -> PricingSchedule:
    """Parse Tesla Find Us visible text without inventing missing prices."""
    for line in text.splitlines():
        if line.startswith("TESLA_PRICE_JSON="):
            try:
                schedule = parse_tesla_pricing_payload(json.loads(line.split("=", 1)[1]), source_url)
                if schedule.kind != "unknown":
                    return schedule
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
    text = html.unescape(text).replace("\u00a0", " ")
    lower = text.lower()
    if "live supercharger utilization" in lower or "dynamic pricing" in lower and "charging fees" not in lower:
        return PricingSchedule(kind="dynamic_unknown", source_url=source_url, note="Live/dynamic price not reliably predictable")

    starts = [
        text.find("Charging Fees for Tesla Owner"),
        text.find("Pricing for Tesla & Members"),
        text.find("Pricing for Tesla Owner"),
    ]
    starts = [s for s in starts if s >= 0]
    section = text[min(starts):] if starts else text
    stop_tokens = ["Charging Fees for All EVs", "Charging Fees for Other EVs", "Pricing for Non-Tesla", "Roadside Assistance"]
    stops = [section.find(tok) for tok in stop_tokens if section.find(tok) >= 0]
    if stops:
        section = section[: min(stops)]

    flat = re.search(r"Base Rate\s*\$\s*(\d+(?:\.\d+)?)\s*/\s*kWh", section, flags=re.I | re.S)
    if flat:
        price = float(flat.group(1))
        return PricingSchedule(
            kind="flat",
            bands=[PriceBand(start_minute=0, end_minute=0, price_per_kwh=price)],
            source_url=source_url,
        )

    pattern = re.compile(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*\$\s*(\d+(?:\.\d+)?)\s*/\s*kWh",
        flags=re.I | re.S,
    )
    bands: list[PriceBand] = []
    for m in pattern.finditer(section):
        bands.append(
            PriceBand(
                start_minute=_minute(m.group(1), m.group(2), m.group(3)),
                end_minute=_minute(m.group(4), m.group(5), m.group(6)),
                price_per_kwh=float(m.group(7)),
            )
        )
    if bands:
        return PricingSchedule(kind="time_of_use", bands=bands, source_url=source_url)
    return PricingSchedule(kind="unknown", source_url=source_url, note="No reliable Tesla-owner $/kWh schedule found")


class TeslaBlockedError(RuntimeError):
    """Tesla's bot protection answered with its "Access Denied" page."""


class LiveLookupUnavailable(RuntimeError):
    """No request was sent to Tesla (blocked cooldown or hourly limit); the caller uses saved data or an estimate."""


# After Tesla blocks the browser, stop trying for a while instead of hitting the block for every station.
BLOCK_COOLDOWN_SECONDS = 15 * 60
# Tesla answers plain HTTP requests with 403; after one, skip that wasted request for a while.
HTTP_FORBIDDEN_SKIP_SECONDS = 60 * 60


class TeslaPriceProvider(PriceProvider):
    def __init__(
        self,
        cache: CacheDB,
        timeout: float,
        user_agent: str,
        cache_hours: int = 6,
        use_playwright_fallback: bool = True,
        max_playwright_fallbacks: int = 10,
        playwright_headless: bool = False,
        playwright_browser: str = "firefox",
        browser_backend: str = "selenium",
    ):
        self.cache = cache
        self.timeout = timeout
        self.user_agent = user_agent
        self.cache_hours = cache_hours
        self.use_playwright_fallback = use_playwright_fallback
        self.max_playwright_fallbacks = max_playwright_fallbacks
        self.playwright_headless = playwright_headless
        self.playwright_browser = playwright_browser.lower()
        self.browser_backend = browser_backend.lower()
        self._browser_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._selenium_driver = None
        self._fallback_count = 0
        self._blocked_until = 0.0
        self._http_forbidden_until = 0.0
        self._recent_requests: list[float] = []
        self.requests_per_hour = 30
        # Uvicorn uses an event loop on Windows that cannot always create browser
        # subprocesses. Keep all synchronous Playwright work on one dedicated thread.
        self._browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tesla-pricing")

    def reset_browser_budget(self) -> None:
        """Start a fresh per-trip station-inspection budget."""
        self._fallback_count = 0

    async def _http_text(self, url: str) -> str:
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.tesla.com/",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        parser = _TextExtractor()
        parser.feed(response.text)
        return parser.text()

    def _take_request_budget(self) -> bool:
        """Count one request to tesla.com against the hourly limit; False when the limit is used up."""
        now = time.monotonic()
        self._recent_requests = [t for t in self._recent_requests if now - t < 3600]
        if len(self._recent_requests) >= self.requests_per_hour:
            return False
        self._recent_requests.append(now)
        return True

    @property
    def requests_in_last_hour(self) -> int:
        now = time.monotonic()
        return len([t for t in self._recent_requests if now - t < 3600])

    @property
    def rate_limited(self) -> bool:
        now = time.monotonic()
        return len([t for t in self._recent_requests if now - t < 3600]) >= self.requests_per_hour

    @property
    def blocked(self) -> bool:
        """True while Tesla is blocking automated lookups and the cooldown hasn't passed."""
        return time.monotonic() < self._blocked_until

    async def _browser_text(self, url: str) -> str:
        if not self.use_playwright_fallback:
            raise RuntimeError("Playwright fallback disabled")
        if self.blocked:
            raise LiveLookupUnavailable("Tesla is blocking automated price lookups; waiting before retrying")
        # The budget check must happen *inside* the lock. Otherwise many concurrent
        # requests can all pass the check and then queue up for slow browser work.
        async with self._browser_lock:
            if self._fallback_count >= self.max_playwright_fallbacks:
                raise RuntimeError("Playwright fallback budget exhausted")
            if self.blocked:
                raise LiveLookupUnavailable("Tesla is blocking automated price lookups; waiting before retrying")
            if not self._take_request_budget():
                raise LiveLookupUnavailable("Hourly live price lookup limit reached")
            self._fallback_count += 1
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(self._browser_executor, self._browser_text_sync, url)
            except TeslaBlockedError:
                self._blocked_until = time.monotonic() + BLOCK_COOLDOWN_SECONDS
                raise
            except Exception as exc:
                if self.browser_backend == "selenium":
                    # A single station can be missing, renamed, or temporarily fail.
                    # Keep the shared Firefox session available for later candidates.
                    raise RuntimeError(f"Selenium pricing fetch failed: {exc}") from exc
                # Some environments (including some Windows event-loop combinations) can
                # start the app process but still fail to launch a browser subprocess.
                # Disable the fallback instead of spamming unhandled Playwright errors.
                self._browser = None
                self._playwright = None
                self.use_playwright_fallback = False
                raise RuntimeError(f"Playwright fallback unavailable: {exc}") from exc

    def _browser_text_sync(self, url: str) -> str:
        if self.browser_backend == "selenium":
            return self._selenium_text_sync(url)

        from playwright.sync_api import sync_playwright

        if self._browser is None:
            self._playwright = sync_playwright().start()
            if self.playwright_browser == "msedge":
                self._browser = self._playwright.chromium.launch(channel="msedge", headless=self.playwright_headless)
            elif self.playwright_browser == "chrome":
                self._browser = self._playwright.chromium.launch(channel="chrome", headless=self.playwright_headless)
            else:
                self._browser = self._playwright.firefox.launch(headless=self.playwright_headless)

        page = self._browser.new_page()
        try:
            location_id = urlparse(url).path.rstrip("/").split("/")[-1]
            map_url = f"https://www.tesla.com/findus?location={quote(location_id)}&functionType=supercharger"
            detail_responses = []
            page.on(
                "response",
                lambda response: detail_responses.append(response)
                if "get-charger-details" in response.url.lower() else None,
            )
            page.goto(map_url, wait_until="domcontentloaded", timeout=max(30000, int(self.timeout * 1000 * 3)))
            if "access denied" in (page.title() or "").lower():
                raise TeslaBlockedError("Tesla returned Access Denied")
            if not detail_responses:
                try:
                    page.wait_for_event(
                        "response",
                        predicate=lambda response: "get-charger-details" in response.url.lower(),
                        timeout=15000,
                    )
                except Exception:
                    page.wait_for_timeout(3000)
            parts = [page.locator("body").inner_text(timeout=10000)]
            for response in detail_responses:
                try:
                    parts.append("TESLA_PRICE_JSON=" + json.dumps(response.json(), ensure_ascii=False))
                except Exception:
                    try:
                        parts.append(response.text())
                    except Exception:
                        pass
            return "\n".join(parts)
        finally:
            page.close()

    def _selenium_text_sync(self, url: str) -> str:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.support.ui import WebDriverWait

        if self._selenium_driver is None:
            options = Options()
            if self.playwright_headless:
                options.add_argument("-headless")
            options.set_preference("permissions.default.geo", 2)
            options.set_preference("geo.enabled", False)
            self._selenium_driver = webdriver.Firefox(options=options)
            self._selenium_driver.set_page_load_timeout(max(15, int(self.timeout * 2)))
            self._selenium_driver.set_script_timeout(30)

        try:
            return self._selenium_fetch(self._selenium_driver, url)
        except Exception:
            # A crashed or wedged Firefox fails every later lookup; start a fresh one next time.
            try:
                self._selenium_driver.title
            except Exception:
                try:
                    self._selenium_driver.quit()
                except Exception:
                    pass
                self._selenium_driver = None
            raise

    def _selenium_fetch(self, driver, url: str) -> str:
        from selenium.webdriver.support.ui import WebDriverWait

        location_id = urlparse(url).path.rstrip("/").split("/")[-1]
        map_url = (
            "https://www.tesla.com/findus?filters=tesla_exclusive_superchargers"
            f"&location={quote(location_id)}"
        )
        driver.get(map_url)
        if "access denied" in (driver.title or "").lower():
            raise TeslaBlockedError("Tesla returned Access Denied")

        def detail_urls(current_driver):
            return current_driver.execute_script(
                "return performance.getEntriesByType('resource')"
                ".map(e => e.name).filter(u => u.includes('get-charger-details'))"
            )

        resource_urls = WebDriverWait(driver, 12).until(detail_urls)
        payload = driver.execute_async_script(
            "const url=arguments[0], done=arguments[arguments.length-1];"
            "fetch(url,{credentials:'include'}).then(r => {"
            " if(!r.ok) throw new Error('HTTP '+r.status); return r.json();"
            "}).then(done).catch(e => done({__fetch_error:String(e)}));",
            resource_urls[-1],
        )
        if isinstance(payload, dict) and payload.get("__fetch_error"):
            raise RuntimeError(payload["__fetch_error"])
        body_text = driver.find_element("tag name", "body").text
        return body_text + "\nTESLA_PRICE_JSON=" + json.dumps(payload, ensure_ascii=False)

    async def close(self) -> None:
        def close_sync() -> None:
            if self._selenium_driver is not None:
                self._selenium_driver.quit()
                self._selenium_driver = None
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._browser_executor, close_sync)
        self._browser_executor.shutdown(wait=True)

    async def get_prices(self, station: Charger, force_refresh: bool = False) -> PricingSchedule:
        url = station.tesla_url
        if not url:
            schedule = PricingSchedule(kind="unknown", note="No Tesla Find Us URL")
            return schedule

        if not force_refresh:
            cached = self.cache.get("pricing", station.location_id, max_age_seconds=self.cache_hours * 3600)
            if cached:
                return PricingSchedule.model_validate(cached)

        if self.blocked:
            raise LiveLookupUnavailable("Tesla is blocking automated price lookups; waiting before retrying")
        error_parts: list[str] = []
        schedule: PricingSchedule | None = None
        if time.monotonic() >= self._http_forbidden_until:
            if not self._take_request_budget():
                raise LiveLookupUnavailable("Hourly live price lookup limit reached")
            try:
                text = await self._http_text(url)
                schedule = parse_tesla_pricing_text(text, url)
            except Exception as exc:  # provider failures are surfaced in debug state
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
                    self._http_forbidden_until = time.monotonic() + HTTP_FORBIDDEN_SKIP_SECONDS
                error_parts.append(f"HTTP: {type(exc).__name__}: {exc}")

        if (schedule is None or schedule.kind == "unknown") and self.use_playwright_fallback:
            try:
                text = await self._browser_text(url)
                browser_schedule = parse_tesla_pricing_text(text, url)
                if browser_schedule.kind != "unknown":
                    schedule = browser_schedule
            except TeslaBlockedError as exc:
                error_parts.append(f"Blocked by Tesla: {exc}")
            except Exception as exc:
                error_parts.append(f"Playwright: {type(exc).__name__}: {exc}")

        if schedule is None:
            schedule = PricingSchedule(kind="unknown", source_url=url, note="Pricing fetch failed")
        schedule.fetched_at = datetime.now(timezone.utc)
        if schedule.kind in {"flat", "time_of_use", "dynamic_unknown"}:
            self.cache.set("pricing", station.location_id, schedule.model_dump(mode="json"))

        status = "ok" if schedule.kind in {"flat", "time_of_use"} else schedule.kind
        self.cache.record_pricing_debug(
            station_id=station.location_id,
            station_name=station.name,
            tesla_url=url,
            status=status,
            schedule=schedule.model_dump(mode="json"),
            error=" | ".join(error_parts) or schedule.note,
        )
        return schedule
