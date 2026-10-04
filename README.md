# Tesla Cheap Trip — functional local MVP

A local-first personal Tesla road-trip optimizer that searches the **next 24 hours** for low-cost Supercharger plans. The primary objective is **minimum charging dollars**, while configurable detour limits prevent absurd routes.

This repo intentionally prioritizes **works > pretty**, **simple > enterprise**, **local > cloud**, and **real/unknown pricing > fabricated pricing**.

## What is implemented

- From/To-only UI; no departure-time or charger selection required.
- Photon geocoding with SQLite cache.
- Coordinate → IANA timezone lookup through the free TimeAPI HTTP endpoint, cached in SQLite; no native timezone package/compiler required.
- OSRM road routing, route geometry, alternative base routes, and road-distance/time matrix with SQLite cache.
- Supercharge.info open-site discovery and local cache.
- Route-corridor charger filtering before matrix construction.
- Tesla public Find Us price retrieval:
  - normal HTTP first;
  - visible Tesla-owner flat or time-of-use `$ / kWh` parsing;
  - optional Firefox/Playwright fallback for dynamically rendered pages;
  - successful schedules cached in SQLite;
  - missing/unreliable prices become **PRICE UNKNOWN**, never a made-up fallback price.
- Pricing debug page at `/debug/prices` with station, Tesla URL, last fetch, schedule, status/error.
- Configurable one-vehicle energy model.
- Configurable SOC-band charging-speed model rather than constant peak kW.
- Graph optimizer with state containing location, SOC, time, dollars, driving time, charging time and purchased kWh.
- Intelligent charge targets including "enough to reach downstream charger/destination + reserve" and 50/60/70/80/85% boundaries.
- Can charge more at a cheap station to skip an expensive station.
- 30-minute departure scan across the next 24 hours.
- Returns useful categories from evaluated feasible plans: Cheapest, Cheap + Fast, Balanced, Fastest Reasonable, Most Expensive Reasonable.
- Leaflet map with chosen route and Supercharger stops.
- Synthetic unit tests for energy, SOC feasibility, pricing, charging time, graph construction, cheap-detour behavior, skip-expensive-station behavior, and departure-time pricing.

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Internet access while using the app (Photon, public OSRM demo, Supercharge.info, TimeAPI, Tesla public Find Us, and OSM map tiles are remote public services).

The code uses no paid API or cloud service. **Visual Studio / Microsoft C++ Build Tools are not required.** The previous native `timezonefinder` dependency was removed.

## Setup

```bash
cd tesla-cheap-trip
uv sync
uv run playwright install firefox
```

Optional configuration:

```bash
cp .env.example .env
```

The app deliberately avoids a settings framework. Environment variables from `.env.example` are supported, but a `.env` file is **not auto-loaded**. Either export variables in your shell or launch through a shell/tool that loads `.env`.

Example on bash/zsh:

```bash
set -a
source .env
set +a
```

PowerShell example:

```powershell
$env:CORRIDOR_MILES="50"
$env:TESLA_PLAYWRIGHT_FALLBACK="true"
```

## Run

```bash
uv run uvicorn app.main:app --reload
```

Open:

- App: http://localhost:8000
- Price diagnostics: http://localhost:8000/debug/prices
- Health: http://localhost:8000/health

Typical workflow:

1. Enter `Chattanooga, TN`.
2. Enter `Niagara Falls, NY`.
3. Click **FIND CHEAPEST TRIP**.
4. The app resolves both places, gets a fastest base route, finds nearby open Superchargers, retrieves any public Tesla pricing it can verify, builds a road-routing graph, and evaluates departure times for the next 24 hours.

## Tests

```bash
uv run pytest -q
```

The tests are synthetic and do **not** depend on Tesla, Photon, OSRM, or Supercharge.info being online.

## Vehicle assumptions — edit these first

The target is a **2026 Tesla Model Y Standard RWD Juniper**, but Tesla does not provide a convenient authoritative usable-battery-kWh figure for every trim/model-year combination. Therefore the MVP does **not** present its battery/efficiency defaults as Tesla specifications.

Edit `app/config/vehicle.py`:

```python
BATTERY_USABLE_KWH = 75.0       # explicit planning assumption
HIGHWAY_WH_PER_MILE = 260.0     # explicit planning assumption
STARTING_SOC = 100.0
MIN_CHARGER_SOC = 10.0
DESTINATION_SOC = 10.0
MAX_PREFERRED_CHARGE_SOC = 85.0
ABSOLUTE_MAX_CHARGE_SOC = 100.0
```

The default usable capacity and Wh/mi exist only so the MVP runs immediately. Replace them with values you trust for your own car/conditions. The same applies to the approximate SOC-band charging curve in that file.

## Data sources

### Photon

`https://photon.komoot.io`

Used for one-shot geocoding after the user clicks Search. Results are cached by normalized query.

### OSRM

`https://router.project-osrm.org`

Used for road route geometry, driving distance/time, and the candidate-node routing matrix. Straight-line distance is used only for preliminary corridor filtering, never for final feasibility.

The public OSRM demo is suitable for personal prototyping, not guaranteed production capacity. The `RouteProvider` abstraction makes self-hosting OSRM later straightforward.

### Supercharge.info

`https://supercharge.info/service/supercharge/allSites`

Used for open Supercharger station discovery. The station list is cached locally. Supercharge.info is **not** used as a price source.

### TimeAPI

`https://timeapi.io/api/timezone/coordinate`

Used only to convert charger/origin coordinates into an IANA timezone such as `America/New_York`, which is required to apply Tesla time-of-use windows in the station's local time. Results are cached locally for 180 days by default. If the lookup fails, the app does **not** guess: a time-of-use station without a known local timezone is excluded from cost optimization. Flat-price stations remain usable.

This replaces the earlier `timezonefinder` Python dependency, avoiding a Windows C/C++ compilation requirement. Python's standard `zoneinfo` is used for timezone conversion, with the pure-data `tzdata` package for Windows compatibility.

### Tesla public Find Us

`https://www.tesla.com/findus/location/supercharger/...`

Used as the preferred public price source. The parser looks only for visible Tesla-owner flat or time-of-use per-kWh pricing. If ordinary HTTP does not expose a reliable schedule, the app can use Playwright + Firefox to render the public page.

It does **not** log in, use private authenticated Tesla APIs, defeat CAPTCHAs, or fabricate missing rates.

## How the optimizer works

The graph nodes are:

- origin;
- priced candidate Superchargers;
- destination.

A search state tracks:

- current node;
- SOC;
- timestamp;
- charging dollars spent;
- driving minutes;
- charging minutes;
- road miles;
- purchased kWh;
- charging stops already used.

For each charger state, the search generates useful departure-SOC choices:

- 50%, 60%, 70%, 80%, 85%;
- exact-ish SOC required to reach each downstream node plus the configured reserve;
- >85% only when a downstream reachability requirement needs it.

A drive is feasible only when the calculated arrival SOC retains the required reserve. The energy model uses:

```text
energy_kWh = distance_miles * Wh_per_mile / 1000
SOC used   = energy_kWh / usable_battery_kWh * 100
```

Charging money uses the price schedule that is active when the charging session **starts**. Charging duration uses the configured SOC-band curve.

The main queue is ordered by charging dollars with only a tiny time tie-breaker. Search limits then stop money-saving detours from becoming ridiculous:

- `MAX_ROUTE_DETOUR_PERCENT`
- `MAX_CHARGER_DETOUR_MINUTES`
- `MAX_TOTAL_EXTRA_DRIVING_MINUTES`

## Pricing behavior and limitations

Tesla pricing is the hardest external dependency.

The app follows these rules:

1. Fetch the public station page over normal HTTP.
2. Parse a Tesla-owner flat or time-of-use per-kWh schedule if one is visible.
3. If necessary and enabled, render that same public page in Firefox with Playwright and parse the visible text.
4. Cache successful schedules.
5. Otherwise mark the station **PRICE UNKNOWN** and exclude it from cost optimization.

This means a real trip can legitimately return "no feasible priced route" if enough required stations hide or omit public prices. That is intentional: the tool would rather fail clearly than lie about the cost.

Tesla can also use live/dynamic utilization-based pricing in some situations. A future price that cannot be known reliably ahead of time should not be treated as a guaranteed rate; such data belongs in an explicit dynamic/unknown state rather than an invented prediction.

## Caching

SQLite cache location defaults to:

```text
.data/cache.sqlite3
```

Current TTLs in code:

- geocoding: 30 days;
- Supercharge.info list: 24 hours;
- OSRM route/table: 7 days;
- successful Tesla pricing: configurable, default 6 hours;
- coordinate timezone lookups: configurable, default 180 days.

Delete `.data/cache.sqlite3` if you want a completely fresh local cache.

## Project layout

```text
tesla-cheap-trip/
├── pyproject.toml
├── README.md
├── .env.example
├── app/
│   ├── main.py
│   ├── models.py
│   ├── timezones.py
│   ├── config/
│   │   ├── settings.py
│   │   └── vehicle.py
│   ├── db/cache.py
│   ├── geocoding/photon.py
│   ├── routing/osrm.py
│   ├── chargers/supercharge_info.py
│   ├── pricing/
│   │   ├── base.py
│   │   └── tesla.py
│   ├── vehicle/
│   │   ├── energy.py
│   │   └── charging.py
│   ├── optimizer/
│   │   ├── graph.py
│   │   └── search.py
│   ├── templates/
│   └── static/
└── tests/
```

## Current limitations

- This is a personal MVP, not a Tesla-navigation clone.
- Energy does not yet model elevation, temperature, wind, traffic speed, HVAC, rain/snow, tire setup, payload, or historical TeslaMate efficiency.
- The charging curve is an approximation and does not model preconditioning, charger sharing, thermal limits, battery temperature, congestion, or actual stall availability.
- Public Tesla Find Us pages are an external surface and can change markup at any time.
- TimeAPI is another free public dependency; cached timezone results reduce repeated calls. If it is unavailable for an uncached TOU station, that station is excluded rather than applying its local price window in the wrong timezone.
- If a station page exposes no reliable future price, it remains `PRICE UNKNOWN`.
- The optimizer scans fixed 30-minute departures; it does not add deliberate waiting at a station yet.
- Road traffic is not modeled because the public OSRM route is not a live-traffic service.
- Public/demo services have usage limits and no SLA.
- Leaflet uses online OpenStreetMap tiles; the backend itself has no paid map dependency.

## Good next improvements

Without changing the basic architecture:

1. Add TeslaMate historical Wh/mi as an optional `EnergyModel` implementation.
2. Add weather/elevation penalties.
3. Add a `WAIT` action for cases where waiting briefly crosses into a much cheaper TOU window.
4. Persist trip runs so actual versus planned charging can be compared.
5. Self-host Photon/OSRM if public demo reliability becomes annoying.
6. Add a manual, explicitly user-entered price override for a station whose public Tesla page is unknown; keep it visually labeled as manual rather than pretending it came from Tesla.

## Debugging

Server logs intentionally expose the important MVP milestones:

```text
Origin resolved: ...
Destination resolved: ...
Base route: ...
Candidate chargers: N
Pricing available: N; unknown: N
Optimization: departures tested=N states evaluated=N routes found=N
Best route: $... / ... min / ... stops
```

For individual price failures, visit `/debug/prices`.

## First-run speed

Version 0.1.2 fixes two first-run stalls from the earlier prototype:

- dense OSRM route geometry is now sampled and indexed once before Supercharger corridor filtering, instead of recomputing the whole route for every U.S. charger;
- the Playwright fallback budget is enforced inside the browser lock, preventing many slow Tesla page loads from queueing accidentally.

The app now limits the initial corridor to 24 candidates, uses a shorter pricing timeout, resolves station timezones only for time-of-use prices, and prints pricing progress to the terminal. Successful external results are cached in SQLite, so repeated searches should be faster.
