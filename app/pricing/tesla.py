from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

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


def parse_tesla_pricing_text(text: str, source_url: str | None = None) -> PricingSchedule:
    """Parse Tesla Find Us visible text without inventing missing prices."""
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


class TeslaPriceProvider(PriceProvider):
    def __init__(
        self,
        cache: CacheDB,
        timeout: float,
        user_agent: str,
        cache_hours: int = 6,
        use_playwright_fallback: bool = True,
        max_playwright_fallbacks: int = 10,
    ):
        self.cache = cache
        self.timeout = timeout
        self.user_agent = user_agent
        self.cache_hours = cache_hours
        self.use_playwright_fallback = use_playwright_fallback
        self.max_playwright_fallbacks = max_playwright_fallbacks
        self._browser_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
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

    async def _browser_text(self, url: str) -> str:
        if not self.use_playwright_fallback:
            raise RuntimeError("Playwright fallback disabled")
        # The budget check must happen *inside* the lock. Otherwise many concurrent
        # requests can all pass the check and then queue up for slow browser work.
        async with self._browser_lock:
            if self._fallback_count >= self.max_playwright_fallbacks:
                raise RuntimeError("Playwright fallback budget exhausted")
            self._fallback_count += 1
            try:
                if self._browser is None:
                    from playwright.async_api import async_playwright

                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.firefox.launch(headless=True)
                page = await self._browser.new_page()
                try:
                    # Tesla pages keep background requests alive, so networkidle can wait
                    # needlessly. DOMContentLoaded + a short render pause is enough for this
                    # best-effort public-page fallback.
                    await page.goto(url, wait_until="domcontentloaded", timeout=min(12000, int(self.timeout * 1000 * 1.5)))
                    await page.wait_for_timeout(1500)
                    return await page.locator("body").inner_text(timeout=5000)
                finally:
                    await page.close()
            except (NotImplementedError, OSError, RuntimeError) as exc:
                # Some environments (including some Windows event-loop combinations) can
                # start the app process but still fail to launch a browser subprocess.
                # Disable the fallback instead of spamming unhandled Playwright errors.
                self._browser = None
                self._playwright = None
                self.use_playwright_fallback = False
                raise RuntimeError(
                    "Playwright fallback unavailable: browser subprocess launch is not supported in this environment"
                ) from exc

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def get_prices(self, station: Charger) -> PricingSchedule:
        url = station.tesla_url
        if not url:
            schedule = PricingSchedule(kind="unknown", note="No Tesla Find Us URL")
            return schedule

        cached = self.cache.get("pricing", station.location_id, max_age_seconds=self.cache_hours * 3600)
        if cached:
            return PricingSchedule.model_validate(cached)

        error_parts: list[str] = []
        schedule: PricingSchedule | None = None
        try:
            text = await self._http_text(url)
            schedule = parse_tesla_pricing_text(text, url)
        except Exception as exc:  # provider failures are surfaced in debug state
            error_parts.append(f"HTTP: {type(exc).__name__}: {exc}")

        if (schedule is None or schedule.kind == "unknown") and self.use_playwright_fallback:
            try:
                text = await self._browser_text(url)
                browser_schedule = parse_tesla_pricing_text(text, url)
                if browser_schedule.kind != "unknown":
                    schedule = browser_schedule
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
