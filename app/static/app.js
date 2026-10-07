let map;
let layers = [];
let lastRequest = null;
let progressPollToken = 0;
let replayScenario = null;
let tripData = null;
let selectedPlan = null;
// Stations table: which rows to show, and which row is expanded (hover) or pinned open (click).
let stationFilter = 'all';
let openStation = null;
let pinnedStation = null;
let hoverTimer = null;
// Map markers for chargers the selected plan doesn't charge at.
let chargerMarkers = new Map();
// Why each charger is or isn't used by the selected plan (station_id -> server explanation).
let explanations = {};
let explainToken = 0;
const whatIfResults = new Map();
// User stops between origin and destination, visited strictly in this order.
let stopsState = [];
const SAVED_TRIP_KEY = 'teslafare.lastTrip.v1';
const DWELL_OPTIONS = [[0, 'Pass through'], [15, '15 min'], [30, '30 min'], [45, '45 min'], [60, '1 hour'], [90, '1.5 hours'], [120, '2 hours'], [180, '3 hours'], [240, '4 hours'], [480, '8 hours'], [600, 'Overnight (10 h)']];
const stopLetter = index => String.fromCharCode(66 + index);  // A is the origin

// Prices this visitor typed in: kept only in this browser and sent with their own trips.
const MY_PRICES_KEY = 'teslafare.myPrices';
function myPrices() {
  try { return JSON.parse(localStorage.getItem(MY_PRICES_KEY) || '{}') || {}; } catch (_) { return {}; }
}
function setMyPrice(stationId, price) {
  const prices = myPrices();
  if (price == null) delete prices[stationId]; else prices[stationId] = {price, at: Date.now()};
  try { localStorage.setItem(MY_PRICES_KEY, JSON.stringify(prices)); } catch (_) { /* private mode: this trip only */ }
  return prices;
}
function myPricesPayload(prices = myPrices()) {
  // Newest first; the server accepts up to 200.
  return Object.fromEntries(Object.entries(prices).sort((a, b) => b[1].at - a[1].at).slice(0, 200).map(([id, v]) => [id, v.price]));
}
// Prices the TeslaFare browser helper captured from Tesla's pages: kept in this browser for 24 h and
// sent with this visitor's trips only (the server parses them per trip and never stores them).
const CAPTURED_KEY = 'teslafare.captured';
const CAPTURE_TTL_MS = 24 * 3600 * 1000;
let helperVersion = document.documentElement.getAttribute('data-teslafare-helper');
let replanTimer = null;
const waitingFor = new Set();
function capturedPrices() {
  try {
    const now = Date.now();
    const all = JSON.parse(localStorage.getItem(CAPTURED_KEY) || '{}') || {};
    return Object.fromEntries(Object.entries(all).filter(([, c]) => now - c.at < CAPTURE_TTL_MS));
  } catch (_) { return {}; }
}
function capturedPayload() {
  return Object.fromEntries(Object.entries(capturedPrices()).sort((a, b) => b[1].at - a[1].at).slice(0, 60).map(([id, c]) => [id, c.payload]));
}
const findusUrl = id => `https://www.tesla.com/findus?location=${encodeURIComponent(id)}`;
// A station worth getting a price for: no real price yet, and not ruled out for reasons a price can't fix.
function worthPricing(c) {
  return !c.user_excluded && !/detour|timezone/i.test(c.exclusion_reason || '')
    && (c.pricing_status === 'unknown' || c.pricing_status === 'estimated');
}
function missingStations() {
  if (!tripData) return [];
  const mine = myPrices();
  return tripData.nearby_chargers.filter(c => !mine[c.station_id] && worthPricing(c));
}
// The helper (userscript) announces itself and hands over captured prices with window.postMessage.
window.addEventListener('message', event => {
  if (event.source !== window || !event.data || event.data.type !== 'teslafare-captures') return;
  const firstContact = !helperVersion;
  helperVersion = event.data.helper || helperVersion || 'yes';
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(CAPTURED_KEY) || '{}') || {}; } catch (_) { /* private mode */ }
  const fresh = [];
  for (const [id, capture] of Object.entries(event.data.captures || {})) {
    if (!stored[id] || stored[id].at < capture.at) { stored[id] = capture; fresh.push(id); }
  }
  try { localStorage.setItem(CAPTURED_KEY, JSON.stringify(stored)); } catch (_) { /* this page only */ }
  fresh.forEach(id => waitingFor.delete(id));
  const missingIds = new Set(missingStations().map(c => c.station_id));
  if (lastRequest && fresh.some(id => missingIds.has(id))) {
    // Batch captures from several tabs into one re-plan.
    clearTimeout(replanTimer);
    renderMissing(`Got ${fresh.filter(id => missingIds.has(id)).length} new price(s) from Tesla, updating your plan…`);
    replanTimer = setTimeout(() => runTrip({...lastRequest, captured_prices: capturedPayload()}), 2500);
  } else if (firstContact && tripData) {
    renderMissing();
  }
});
window.postMessage({type: 'teslafare-helper-ping'}, location.origin);

// Watch the saved prices of every station in the current trip's list. The first check after a plan
// records what the server has; any station that later gets a new or updated price triggers a re-plan.
const PRICE_WATCH_MS = 5000;
let priceBaseline = null;
let priceBaselineTrip = null;
let replanning = false;
async function checkServerPrices() {
  if (!tripData || !lastRequest || replanning || document.hidden) return;
  const ids = tripData.nearby_chargers.filter(c => !c.user_excluded).map(c => c.station_id);
  if (!ids.length) return;
  let prices;
  try {
    prices = (await (await fetch(`/api/prices/status?ids=${encodeURIComponent(ids.join(','))}`, {cache: 'no-store'})).json()).prices || {};
  } catch (_) { return; }
  if (priceBaselineTrip !== tripData || !priceBaseline) {
    priceBaseline = prices;
    priceBaselineTrip = tripData;
    return;
  }
  const changed = Object.keys(prices).filter(id => prices[id] !== priceBaseline[id]);
  if (!changed.length) return;
  priceBaseline = prices;
  changed.forEach(id => waitingFor.delete(id));
  clearTimeout(replanTimer);
  const names = changed.map(id => shortName(chargerById(id)?.station_name || id)).slice(0, 3).join(', ');
  renderMissing(`New price for ${names}${changed.length > 3 ? ` +${changed.length - 3} more` : ''}, updating your plan…`);
  replanTimer = setTimeout(async () => {
    replanning = true;
    try { await runTrip({...lastRequest, captured_prices: capturedPayload()}); } finally { replanning = false; }
  }, 1500);
}
function watchServerPrices() { checkServerPrices(); }
setInterval(checkServerPrices, PRICE_WATCH_MS);
document.addEventListener('visibilitychange', checkServerPrices);
window.addEventListener('focus', checkServerPrices);

// Open Tesla tabs for missing stations; with the helper each one captures its price and closes itself.
function openMissing(ids) {
  const tabs = ids.map((id, i) => window.open('about:blank', `teslafare-plan-${i}`));
  const blocked = tabs.filter(tab => !tab).length;
  ids.forEach((id, i) => {
    if (!tabs[i]) return;
    waitingFor.add(id);
    setTimeout(() => { tabs[i].location.href = findusUrl(id); }, i * 1000);
    watchServerPrices();
  });
  renderMissing(blocked ? `Your browser blocked ${blocked} tab(s). Choose "Allow pop-ups" for this site, then click again.` : `Opened ${ids.length - blocked} Tesla tab(s); prices arrive in a few seconds…`);
}

function renderMissing(status) {
  const section = document.getElementById('missing-prices');
  const missing = missingStations();
  section.hidden = !missing.length;
  if (!missing.length) { section.innerHTML = ''; return; }
  const estimate = Number(lastRequest?.fallback_price_per_kwh ?? 0.4).toFixed(2);
  const next = missing.filter(c => !waitingFor.has(c.station_id)).slice(0, 5);
  // The batch button is always offered: with the helper on Tesla's pages each tab captures its price and
  // closes, and the planner picks the prices up on its own (helper bridge or server price watch).
  const batchLabel = next.length === missing.length && next.length <= 5 ? `Open ${next.length === 1 ? 'it' : `all ${next.length}`} on Tesla` : `Open next ${next.length} on Tesla`;
  const actionsHtml = `<div class="missing-actions">
       <button id="fetch-missing" class="primary-btn" type="button" ${next.length ? '' : 'disabled'}>${batchLabel}</button>
       <span class="muted">${helperVersion
         ? 'Each tab reads the price and closes itself; your plan updates on its own.'
         : 'With the TeslaFare helper installed, each tab reads the price and closes itself and your plan updates on its own. Without it, the tabs just open so you can check prices.'}</span>
     </div>`;
  const helperHtml = actionsHtml + (helperVersion ? '' : `<details class="helper-setup"><summary><b>Get these prices automatically (1-minute setup)</b></summary>
         <ol>
           <li>Install <a href="https://www.tampermonkey.net/" target="_blank" rel="noopener">Tampermonkey</a> for your browser.</li>
           <li>Install the <a href="/collector.user.js" target="_blank">TeslaFare price helper</a> (Tampermonkey shows an Install button).</li>
           <li>Reload this page and plan again.</li>
         </ol>
         <p class="muted">The helper only reads the price Tesla's site shows you, in your own browser, and passes it to this page. Prices it captures are used for your trips and aren't shared.</p>
       </details>`);
  const rows = missing.map(c => `
    <li>
      <span><b>${esc(stationName(c.station_name))}</b><small>${esc(cityState(c.address) || '')}${waitingFor.has(c.station_id) ? ' · waiting for Tesla tab…' : ''}</small></span>
      <a href="${esc(findusUrl(c.station_id))}" target="teslafare-single-${esc(c.station_id)}" rel="opener">Open on Tesla ↗</a>
      <span class="my-price-inline"><input type="number" min="0.01" max="2" step="0.01" placeholder="$/kWh" aria-label="Price for ${esc(c.station_name)}"><button type="button" class="my-price-btn" data-my-price="${esc(c.station_id)}">Use</button></span>
    </li>`).join('');
  section.innerHTML = `
    <div class="missing-head">
      <div><h2 class="kicker">Prices missing for ${missing.length} station${missing.length === 1 ? '' : 's'}</h2>
      <p>This plan uses your $${estimate}/kWh estimate for them. Get the real price automatically, or open a station on Tesla and type its price.</p></div>
    </div>
    ${status ? `<div class="missing-status">${esc(status)}</div>` : ''}
    ${helperHtml}
    <details class="missing-list" ${missing.length <= 6 ? 'open' : ''}><summary>${missing.length} station${missing.length === 1 ? '' : 's'} without a price</summary><ul>${rows}</ul></details>`;
  document.getElementById('fetch-missing')?.addEventListener('click', () => openMissing(next.map(c => c.station_id)));
}
document.getElementById('missing-prices').addEventListener('click', event => {
  if (event.target.closest('a[href*="tesla.com/findus"]')) { watchServerPrices(); return; }
  const use = event.target.closest('.my-price-btn');
  if (!use) return;
  const input = use.parentElement.querySelector('input');
  const price = Number(input.value);
  if (!Number.isFinite(price) || price < 0.01 || price > 2) {
    input.setCustomValidity('Enter a price between $0.01 and $2.00 per kWh.');
    input.reportValidity();
    return;
  }
  use.disabled = true;
  runTrip({...lastRequest, price_overrides: myPricesPayload(setMyPrice(use.dataset.myPrice, price))});
});
const STATUS_LABELS = {verified:'Live', cached:'Cached', historical:'Historical', manual:'Entered by you', captured:'From your browser', estimated:'Estimated', unknown:'Unknown', fetching:'Fetching', excluded:'Excluded'};
const CATEGORY_LABELS = {'CHEAPEST':'Lowest cost', 'CHEAP + FAST':'Cheap + fast', 'BALANCED':'Balanced', 'FASTEST REASONABLE':'Fastest', 'MOST EXPENSIVE REASONABLE':'Highest cost', 'WHAT IF':'What-if'};
const icon = (id, cls = 'ico') => `<svg class="${cls}"><use href="#${id}"/></svg>`;
const boltIcon = `<svg><use href="#i-bolt"/></svg>`;

function esc(s) {
  return String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}
function fmtMinutes(mins) {
  const h = Math.floor(mins / 60), m = Math.round(mins % 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}
function fmtClock(iso) { return new Date(iso).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}); }
function fmtDay(iso) { return new Date(iso).toLocaleDateString([], {weekday:'short', month:'short', day:'numeric', year:'numeric'}); }
function fmtDayTime(iso) { return new Date(iso).toLocaleString([], {weekday:'short', hour:'numeric', minute:'2-digit'}); }
function money(v) { return `$${v.toFixed(2)}`; }
function shortName(name) { return String(name || '').split(/,| - /)[0].trim(); }
function cityState(address) {
  const parts = String(address || '').split(',').map(p => p.trim()).filter(Boolean);
  if (parts.length && /^\d{5}(-\d{4})?$/.test(parts.at(-1))) parts.pop();
  return parts.slice(-2).join(', ');
}
function placeLabel(text) {
  const parts = String(text || '').split(',').map(p => p.trim()).filter(Boolean);
  if (parts.length && /^\d{5}(-\d{4})?$/.test(parts.at(-1))) parts.pop();
  if (parts.length && /^(USA|United States)$/i.test(parts.at(-1))) parts.pop();
  return parts.length > 1 ? {city: parts.at(-2), region: parts.at(-1)} : {city: parts[0] || '', region: ''};
}
function placeText(text) { const p = placeLabel(text); return p.region ? `${p.city}, ${p.region}` : p.city; }

function saveLastTrip(request) {
  if (!request || request.replay_scenario) return;
  try {
    localStorage.setItem(SAVED_TRIP_KEY, JSON.stringify({
      version: 1,
      saved_at: new Date().toISOString(),
      request,
    }));
  } catch (_) {
    // Planning must continue when storage is disabled or full.
  }
}

function restoreLastTrip() {
  try {
    const saved = JSON.parse(localStorage.getItem(SAVED_TRIP_KEY) || 'null');
    const request = saved?.version === 1 ? saved.request : null;
    if (!request || typeof request.from_location !== 'string' || typeof request.to_location !== 'string') return false;

    document.getElementById('from').value = request.from_location;
    document.getElementById('to').value = request.to_location;
    stopsState = Array.isArray(request.stops) ? request.stops.slice(0, 8)
      .filter(stop => stop && typeof stop.location === 'string')
      .map(stop => ({location: stop.location, dwell: Number(stop.dwell_minutes) || 0})) : [];

    const values = {
      'fallback-price': request.fallback_price_per_kwh,
      'starting-soc': request.starting_soc,
      'min-charger-soc': request.min_charger_soc,
      'destination-soc': request.destination_soc,
      'custom-battery-kwh': request.custom_battery_usable_kwh,
      'custom-whmi': request.custom_highway_wh_per_mile,
      'custom-peak-kw': request.custom_peak_charge_kw,
    };
    for (const [id, value] of Object.entries(values)) {
      if (value !== null && value !== undefined && Number.isFinite(Number(value))) document.getElementById(id).value = value;
    }
    const vehicle = document.getElementById('vehicle-profile');
    if ([...vehicle.options].some(option => option.value === request.vehicle_profile_id)) vehicle.value = request.vehicle_profile_id;
    if (typeof request.use_charger_cache === 'boolean') document.getElementById('use-charger-cache').checked = request.use_charger_cache;

    // Do not restore an expired departure time; start with the fresh default instead.
    if (request.desired_departure_time && new Date(request.desired_departure_time) > new Date()) {
      document.getElementById('desired-departure').value = request.desired_departure_time.slice(0, 16);
    }
    return true;
  } catch (_) {
    return false;
  }
}
// "Lexington, KY - Meijer Way" -> "Lexington · Meijer Way"; "Lexington, KY" -> "Lexington Supercharger".
function stationName(name) {
  if (/supercharger/i.test(name)) return name;
  const site = String(name || '').split(' - ').slice(1).join(' - ').trim();
  return site ? `${shortName(name)} · ${site}` : `${shortName(name)} Supercharger`;
}
function planKey(p) { return `${p.departure_time}|${p.stops.map(s => s.station_id).join(',')}|${p.charging_cost}`; }
function chargerById(id) { return tripData?.nearby_chargers.find(c => c.station_id === id); }

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
function clockFromMinute(minute) {
  const h = Math.floor(minute / 60) % 24, m = minute % 60;
  return `${h % 12 || 12}${m ? `:${String(m).padStart(2, '0')}` : ''} ${h < 12 ? 'AM' : 'PM'}`;
}
function minuteOfDay(iso, timeZone) {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {hour: 'numeric', minute: 'numeric', hourCycle: 'h23', timeZone: timeZone || undefined}).formatToParts(new Date(iso));
    const get = type => Number(parts.find(p => p.type === type)?.value || 0);
    return get('hour') * 60 + get('minute');
  } catch (_) { return null; }
}
function bandCovers(band, minute) {
  if (band.start_minute === band.end_minute) return true;  // flat: all day
  return band.start_minute < band.end_minute
    ? minute >= band.start_minute && minute < band.end_minute
    : minute >= band.start_minute || minute < band.end_minute;
}
// Tesla often splits a day into adjacent bands at the same price; merge them (including across midnight).
function mergedBands(bands) {
  const sameRate = (a, b) => a.price_per_kwh === b.price_per_kwh && String(a.days || []) === String(b.days || []);
  const merged = [];
  for (const band of [...bands].sort((a, b) => a.start_minute - b.start_minute)) {
    const last = merged.at(-1);
    if (last && last.end_minute === band.start_minute && sameRate(last, band)) last.end_minute = band.end_minute;
    else merged.push({...band});
  }
  const first = merged[0], last = merged.at(-1);
  if (merged.length > 1 && last.end_minute % 1440 === first.start_minute && sameRate(first, last)) {
    first.start_minute = last.start_minute;
    merged.pop();
  }
  return merged.sort((a, b) => a.start_minute - b.start_minute);
}
// Price schedule as popup HTML. `atIso` highlights the band in effect at that moment.
function priceScheduleHtml(schedule, status, atIso) {
  if (!schedule?.bands?.length) return '<div class="popup-price muted">Price unavailable</div>';
  const note = status === 'estimated' ? ' <span class="muted">your fallback estimate</span>'
    : status === 'manual' ? ' <span class="muted">entered by you</span>'
    : status === 'historical' ? ' <span class="muted">historical</span>' : '';
  if (schedule.kind !== 'time_of_use') {
    return `<div class="popup-price"><b>$${schedule.bands[0].price_per_kwh.toFixed(2)}</b>/kWh${note}</div>`;
  }
  const at = atIso ? minuteOfDay(atIso, schedule.timezone) : null;
  const rows = mergedBands(schedule.bands).map(band => {
    const days = band.days?.length && band.days.length < 7 ? ` <span class="muted">${band.days.map(d => DAY_NAMES[d]).join(', ')}</span>` : '';
    const active = at != null && bandCovers(band, at) ? ' class="active"' : '';
    return `<tr${active}><td>${clockFromMinute(band.start_minute)}–${clockFromMinute(band.end_minute)}${days}</td><td>$${band.price_per_kwh.toFixed(2)}</td></tr>`;
  }).join('');
  return `<div class="popup-price">Time-of-use pricing${note}</div><table class="popup-bands">${rows}</table>`;
}
function priceAge(iso) {
  const hours = (Date.now() - new Date(iso).getTime()) / 3600000;
  if (!Number.isFinite(hours)) return '';
  return hours < 1 ? 'in the last hour' : hours < 48 ? `${Math.round(hours)} h ago` : `${Math.round(hours / 24)} days ago`;
}
function priceRange(schedule) {
  if (!schedule?.bands?.length) return null;
  const prices = schedule.bands.map(b => b.price_per_kwh);
  const lo = Math.min(...prices), hi = Math.max(...prices);
  return lo === hi ? `$${lo.toFixed(2)}` : `$${lo.toFixed(2)}–${hi.toFixed(2)}`;
}
function statusHtml(status) {
  return `<span class="status ${esc(status)}"><i></i>${esc(STATUS_LABELS[status] || status)}</span>`;
}
function setBattery(el, soc) {
  el.style.setProperty('--level', Math.max(0.04, Math.min(1, soc / 100)));
  el.classList.toggle('low', soc < 25);
}

/* Stops editor */
function renderStopsEditor(focusIndex) {
  const list = document.getElementById('stops-list');
  list.innerHTML = stopsState.map((stop, i) => `
    <li class="stop-row" data-index="${i}">
      <span class="route-letter">${stopLetter(i)}</span>
      <span class="grip" title="Drag to reorder" aria-hidden="true">${icon('i-grip')}</span>
      <input class="stop-location" value="${esc(stop.location)}" placeholder="Stop ${stopLetter(i)} — address or city" aria-label="Stop ${stopLetter(i)} location" required minlength="2">
      <select class="stop-dwell" aria-label="Time at stop ${stopLetter(i)}">${DWELL_OPTIONS.map(([value, text]) => `<option value="${value}" ${Number(stop.dwell) === value ? 'selected' : ''}>${text}</option>`).join('')}</select>
      <span class="stop-tools">
        <button type="button" class="icon-btn" data-action="up" ${i === 0 ? 'disabled' : ''} aria-label="Move stop ${stopLetter(i)} earlier">${icon('i-up')}</button>
        <button type="button" class="icon-btn" data-action="down" ${i === stopsState.length - 1 ? 'disabled' : ''} aria-label="Move stop ${stopLetter(i)} later">${icon('i-down')}</button>
        <button type="button" class="icon-btn remove" data-action="remove" aria-label="Remove stop ${stopLetter(i)}">${icon('i-x')}</button>
      </span>
    </li>`).join('');
  document.getElementById('to-letter').textContent = stopLetter(stopsState.length);
  document.getElementById('add-stop').disabled = stopsState.length >= 8;
  if (focusIndex != null) list.querySelector(`[data-index="${focusIndex}"] .stop-location`)?.focus();
  updateSummary();
}
function moveStop(from, to) {
  if (to < 0 || to >= stopsState.length || from === to) return;
  const [stop] = stopsState.splice(from, 1);
  stopsState.splice(to, 0, stop);
  renderStopsEditor(to);
}
function stopsPayload() {
  return stopsState
    .map(stop => ({location: stop.location.trim(), dwell_minutes: Number(stop.dwell) || 0}))
    .filter(stop => stop.location.length >= 2);
}
function setupStopsEditor() {
  const list = document.getElementById('stops-list');
  document.getElementById('add-stop').addEventListener('click', () => {
    stopsState.push({location: '', dwell: 0});
    renderStopsEditor(stopsState.length - 1);
  });
  list.addEventListener('input', event => {
    const row = event.target.closest('.stop-row');
    if (!row) return;
    const stop = stopsState[Number(row.dataset.index)];
    if (event.target.classList.contains('stop-location')) stop.location = event.target.value;
    if (event.target.classList.contains('stop-dwell')) stop.dwell = Number(event.target.value);
    updateSummary();
  });
  list.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const index = Number(button.closest('.stop-row').dataset.index);
    if (button.dataset.action === 'up') moveStop(index, index - 1);
    if (button.dataset.action === 'down') moveStop(index, index + 1);
    if (button.dataset.action === 'remove') { stopsState.splice(index, 1); renderStopsEditor(); }
  });
  // Drag to reorder: only the grip starts a drag so text in the inputs stays selectable.
  let dragIndex = null;
  list.addEventListener('pointerdown', event => {
    const row = event.target.closest('.stop-row');
    if (row) row.draggable = Boolean(event.target.closest('.grip'));
  });
  list.addEventListener('dragstart', event => {
    const row = event.target.closest('.stop-row');
    dragIndex = Number(row.dataset.index);
    row.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(dragIndex));
  });
  list.addEventListener('dragover', event => {
    const row = event.target.closest('.stop-row');
    if (dragIndex == null || !row) return;
    event.preventDefault();
    list.querySelectorAll('.drop-target').forEach(el => el.classList.remove('drop-target'));
    if (Number(row.dataset.index) !== dragIndex) row.classList.add('drop-target');
  });
  list.addEventListener('drop', event => {
    const row = event.target.closest('.stop-row');
    event.preventDefault();
    if (dragIndex != null && row) moveStop(dragIndex, Number(row.dataset.index));
  });
  list.addEventListener('dragend', () => {
    dragIndex = null;
    list.querySelectorAll('.stop-row').forEach(el => { el.classList.remove('dragging', 'drop-target'); el.draggable = false; });
  });
}

/* Trip summary bar */
function updateVehicleAssumption() {
  const select = document.getElementById('vehicle-profile');
  const custom = select.value === 'custom';
  document.querySelectorAll('.custom-vehicle-field').forEach(el => { el.hidden = !custom; });
  const option = select.selectedOptions[0];
  const kwh = custom ? Number(document.getElementById('custom-battery-kwh').value) : Number(option.dataset.kwh);
  const whmi = custom ? Number(document.getElementById('custom-whmi').value) : Number(option.dataset.whmi);
  const peak = custom ? Number(document.getElementById('custom-peak-kw').value) : Number(option.dataset.peak);
  const range = kwh > 0 && whmi > 0 ? Math.round(kwh * 1000 / whmi) : null;
  document.getElementById('vehicle-assumption').innerHTML = range
    ? `<b>Estimated highway range: ${range} mi at 100%</b><br>${kwh} kWh usable · ${whmi} Wh/mi · charges up to ~${peak} kW. Fixed-condition estimate; elevation, weather and speed are not yet modeled.`
    : 'Enter usable battery capacity and highway efficiency to estimate range.';
}

function updateSummary(departureIso) {
  document.getElementById('sum-from').textContent = placeText(document.getElementById('from').value) || '—';
  document.getElementById('sum-to').textContent = placeText(document.getElementById('to').value) || '—';
  const via = stopsPayload();
  const viaEl = document.getElementById('sum-via');
  viaEl.textContent = via.length ? `via ${via.map((s, i) => `${stopLetter(i)} ${placeLabel(s.location).city}`).join(' · ')}` : '';
  viaEl.title = viaEl.textContent;
  const depart = departureIso || document.getElementById('desired-departure').value;
  document.getElementById('sum-depart').textContent = depart ? `${fmtDay(depart)} · ${fmtClock(depart)}` : '—';
  const soc = Number(document.getElementById('starting-soc').value) || 0;
  document.getElementById('sum-soc').textContent = `${soc}%`;
  setBattery(document.getElementById('sum-battery-icon'), soc);
}
function toggleForm(show) {
  const form = document.getElementById('trip-form');
  form.hidden = show === undefined ? !form.hidden : !show;
  if (!form.hidden) document.getElementById('from').focus();
}

function routeTravelLegs(plan) {
  const events = [
    ...(plan.stops || []).map(stop => ({
      arrivalTime: stop.arrival_time,
      departureTime: new Date(new Date(stop.arrival_time).getTime() + stop.charging_minutes * 60000).toISOString(),
      label: shortName(stop.station_name),
    })),
    ...(plan.waypoints || []).map(stop => ({
      arrivalTime: stop.arrival_time,
      departureTime: stop.departure_time,
      label: `Stop ${stopLetter(stop.index)}`,
    })),
  ].sort((a, b) => new Date(a.arrivalTime) - new Date(b.arrivalTime));
  let previousDeparture = new Date(plan.departure_time);
  let previousLabel = 'Start';
  for (const event of events) {
    event.fromLabel = previousLabel;
    event.drivingMinutes = Math.max(0, (new Date(event.arrivalTime) - previousDeparture) / 60000);
    previousDeparture = new Date(event.departureTime);
    previousLabel = event.label;
  }
  return {
    events,
    destination: {
      fromLabel: previousLabel,
      drivingMinutes: Math.max(0, (new Date(plan.arrival_time) - previousDeparture) / 60000),
    },
  };
}

/* Recommended card */
function renderRecommended(plan) {
  const card = document.getElementById('recommended');
  const best = tripData.plans[0];
  const isBest = best && planKey(best) === planKey(plan);
  const cheapest = Math.min(...[...tripData.plans, ...tripData.departure_options].map(p => p.charging_cost));
  const delta = plan.charging_cost - cheapest;
  const badge = CATEGORY_LABELS[plan.category] || (delta < 0.005 ? 'Lowest cost' : `+${money(delta)} vs lowest`);
  const startSoc = plan.starting_soc ?? Number(lastRequest.starting_soc);
  const endSoc = plan.arrival_soc;
  const chargerReserve = Number(lastRequest.min_charger_soc ?? tripData.vehicle_assumptions?.min_charger_soc ?? 10);
  const destinationReserve = Number(lastRequest.destination_soc ?? tripData.vehicle_assumptions?.destination_soc ?? 10);
  const vehicle = tripData.vehicle_assumptions || {};
  const hasWaypoints = Boolean(plan.waypoints?.length);
  const travel = routeTravelLegs(plan);
  const stops = plan.stops.map((s, index) => {
    const charger = chargerById(s.station_id);
    const next = plan.stops[index + 1];
    const where = cityState(charger?.address) || shortName(s.station_name);
    const tag = s.tesla_url ? 'a' : 'div';
    const href = s.tesla_url ? ` href="${esc(s.tesla_url)}" target="_blank" rel="noopener"` : '';
    const bridgeCharge = next && next.price_per_kwh + 0.001 < s.price_per_kwh
      && s.departure_soc - s.arrival_soc <= 10.5
      && Math.abs(next.arrival_soc - chargerReserve) <= 1.1;
    const explanation = bridgeCharge
      ? `<div class="stop-explanation">${icon('i-info')}<span><b>Bridge charge</b> — only enough energy is bought here to reach the cheaper $${next.price_per_kwh.toFixed(2)}/kWh station while keeping your ${chargerReserve}% charger reserve.</span></div>`
      : '';
    return `<div class="stop-item"><span class="stop-badge">${boltIcon}</span>
      <${tag} class="stop-card"${href}>
        <span><b>${esc(stationName(s.station_name))}</b><small>${esc(where)} · ${fmtMinutes(s.charging_minutes)} · arrive ${fmtClock(s.arrival_time)}</small></span>
        <span class="soc">${Math.round(s.arrival_soc)}% ${icon('i-arrow')} ${Math.round(s.departure_soc)}%</span>
        <span class="cost"><b>${money(s.cost)}</b><small>$${s.price_per_kwh.toFixed(2)}/kWh${s.price_is_estimate ? ' est.' : ''}</small></span>
        ${s.tesla_url ? icon('i-chev') : '<span></span>'}
      </${tag}>${explanation}</div>`;
  });
  const waypointItems = (plan.waypoints || []).map(w => `<div class="stop-item waypoint"><span class="stop-badge wp">${stopLetter(w.index)}</span>
      <div class="stop-card">
        <span><b>Stop ${stopLetter(w.index)} · ${esc(placeText(w.name))}</b><small>arrive ${fmtDayTime(w.arrival_time)}${w.dwell_minutes ? ` · leave ${fmtClock(w.departure_time)}` : ''}</small></span>
        <span class="soc">${Math.round(w.arrival_soc)}%</span>
        <span class="cost"><b>${w.dwell_minutes ? fmtMinutes(w.dwell_minutes) : '—'}</b><small>${w.dwell_minutes ? 'at stop' : 'pass through'}</small></span>
        <span></span>
      </div></div>`);
  const eventHtml = new Map([
    ...plan.stops.map((s, i) => [s.arrival_time, stops[i]]),
    ...(plan.waypoints || []).map((w, i) => [w.arrival_time, waypointItems[i]]),
  ]);
  const travelLeg = leg => `<div class="travel-leg"><span>${esc(leg.fromLabel)}</span>${icon('i-arrow')}<b>${fmtMinutes(leg.drivingMinutes)} drive</b></div>`;
  const timeline = travel.events.map(event => `${travelLeg(event)}${eventHtml.get(event.arrivalTime) || ''}`).join('');
  const destinationLeg = `<div class="travel-leg destination-leg"><span>${esc(travel.destination.fromLabel)}</span>${icon('i-arrow')}<b>${fmtMinutes(travel.destination.drivingMinutes)} drive to destination</b></div>`;
  card.innerHTML = `
    <div class="rec-head"><h2 class="kicker">${plan.category === 'WHAT IF' ? 'What-if plan' : isBest ? 'Recommended departure' : 'Selected departure'}</h2><span class="badge">${esc(badge)}</span></div>
    <div class="rec-stats">
      <div><b class="big">${fmtClock(plan.departure_time)}</b><span>${fmtDay(plan.departure_time)}</span></div>
      <div><b>${money(plan.charging_cost)}</b><span>Est. charging cost <span title="${plan.kwh_purchased.toFixed(1)} kWh purchased. Planning estimate, not a quote.">${icon('i-info')}</span></span></div>
      <div><b>${fmtMinutes(plan.total_minutes)}</b><span>Est. arrival<br>${fmtDayTime(plan.arrival_time)}</span></div>
      <div><b>${plan.stops.length} ${hasWaypoints ? 'charge' : 'stop'}${plan.stops.length === 1 ? '' : 's'}</b><span>Total charging<br>${fmtMinutes(plan.charging_minutes)}${plan.dwell_minutes ? `<br>At stops ${fmtMinutes(plan.dwell_minutes)}` : ''}</span></div>
    </div>
    <div class="soc-row">
      <i class="battery lg" id="rec-start-batt"><i></i></i>
      <div><b>${Math.round(startSoc)}%</b><span>Start</span></div>
      <span class="dash">${icon('i-chev')}</span>
      ${endSoc == null ? '' : `<div class="end"><b>${Math.round(endSoc)}%</b><span>Arrival</span></div><i class="battery lg" id="rec-end-batt"><i></i></i>`}
    </div>
    <div class="reserve-rule">${icon('i-info')}<span>Modeled as <b>${esc(vehicle.profile_label || 'selected vehicle')}</b> at ${Math.round(vehicle.highway_wh_per_mile || 0)} Wh/mi (${Math.round(vehicle.estimated_highway_range_miles || 0)} mi estimated highway range). This plan keeps at least <b>${chargerReserve}%</b> at chargers and <b>${destinationReserve}%</b> at ${hasWaypoints ? 'each stop and ' : ''}the destination. Elevation and weather are not yet modeled. <button type="button" class="reserve-edit">Edit trip</button>.</span></div>
    <div class="stop-list">${timeline}${destinationLeg}${plan.stops.length || plan.waypoints?.length ? '' : '<div class="no-stops">No charging stop required for this departure.</div>'}</div>`;
  setBattery(document.getElementById('rec-start-batt'), startSoc);
  if (endSoc != null) setBattery(document.getElementById('rec-end-batt'), endSoc);
  card.querySelector('.reserve-edit').addEventListener('click', () => {
    toggleForm(true);
    document.getElementById('min-charger-soc').focus();
  });
}

/* Map */
function drawMap(plan) {
  const el = document.getElementById('map');
  if (typeof L === 'undefined') {
    el.innerHTML = '<div class="map-empty">The route was calculated, but the map library could not load.</div>';
    return;
  }
  if (!map) {
    el.innerHTML = '';
    map = L.map('map', {zoomControl: false});
    L.control.zoom({position: 'bottomleft'}).addTo(map);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '&copy; OpenStreetMap'}).addTo(map);
    // Price tags only once zoomed in enough for them not to overlap.
    const zoomClass = () => el.classList.toggle('zoom-near', map.getZoom() >= 7);
    map.on('zoomend', zoomClass);
    map.whenReady(zoomClass);
  } else {
    map.invalidateSize();
  }
  layers.forEach(layer => map.removeLayer(layer));
  layers = [];
  const add = layer => { layer.addTo(map); layers.push(layer); return layer; };
  const label = (marker, title, sub, dir = 'right') => marker.bindTooltip(
    `${esc(title)}${sub ? `<small>${esc(sub)}</small>` : ''}`,
    {permanent: true, direction: dir, offset: dir === 'right' ? [14, 0] : dir === 'left' ? [-14, 0] : [0, 0], className: `map-label ${dir}`});

  const matched = plan && tripData.plans.find(p => planKey(p) === planKey(plan));
  const travel = plan ? routeTravelLegs(plan) : null;
  const travelByArrival = new Map((travel?.events || []).map(event => [event.arrivalTime, event.drivingMinutes]));
  const geometry = plan?.route_geometry?.length ? plan.route_geometry : matched?.route_geometry?.length ? matched.route_geometry : tripData.base_route.geometry;
  const latlngs = geometry.map(([lon, lat]) => [lat, lon]);
  const chosen = new Set((plan?.stops || []).map(s => s.station_id));

  if (latlngs.length) {
    add(L.polyline(latlngs, {color: '#5b0d10', weight: 9, opacity: .6}));
    const line = add(L.polyline(latlngs, {color: '#e82127', weight: 4, opacity: 1}));
    map.fitBounds(line.getBounds(), {padding: [50, 50]});
  }
  const o = tripData.origin.coordinate, d = tripData.destination.coordinate;
  label(add(L.marker([o.lat, o.lon], {icon: L.divIcon({className: '', html: '<div class="origin-dot"></div>', iconSize: [22, 22], iconAnchor: [11, 11]})})),
    placeLabel(document.getElementById('from').value).city, placeLabel(document.getElementById('from').value).region);
  label(add(L.marker([d.lat, d.lon], {icon: L.divIcon({className: 'pin-icon', html: '<svg><use href="#i-pin"/></svg>', iconSize: [34, 34], iconAnchor: [17, 32]})})),
    placeLabel(document.getElementById('to').value).city, travel ? `${fmtMinutes(travel.destination.drivingMinutes)} drive from last stop` : placeLabel(document.getElementById('to').value).region);
  const visits = plan?.waypoints?.length ? plan.waypoints : (tripData.waypoints || []).map((loc, index) => ({index, name: lastRequest.stops?.[index]?.location || loc.label, coordinate: loc.coordinate}));
  visits.forEach(w => {
    const marker = add(L.marker([w.coordinate.lat, w.coordinate.lon], {icon: L.divIcon({className: '', html: `<div class="waypoint-pin">${stopLetter(w.index)}</div>`, iconSize: [26, 26], iconAnchor: [13, 13]}), zIndexOffset: 500}));
    const where = placeLabel(w.name);
    // Stops label to the left, chargers to the right, so a stop and a charger in the same town stay readable.
    const drive = w.arrival_time ? travelByArrival.get(w.arrival_time) : null;
    const action = w.dwell_minutes ? `${fmtMinutes(w.dwell_minutes)} at stop` : 'pass through';
    label(marker, `${stopLetter(w.index)} · ${where.city}`, drive == null ? action : `${fmtMinutes(drive)} drive · ${action}`, 'left');
  });
  (plan?.stops || []).forEach(s => {
    const marker = add(L.marker([s.coordinate.lat, s.coordinate.lon], {icon: L.divIcon({className: '', html: `<div class="charge-pin">${boltIcon}</div>`, iconSize: [30, 30], iconAnchor: [15, 36]})}));
    const drive = travelByArrival.get(s.arrival_time);
    label(marker, placeLabel(chargerById(s.station_id)?.address).city || shortName(s.station_name), `${fmtMinutes(drive || 0)} drive · ${fmtMinutes(s.charging_minutes)} charge`);
    const c = chargerById(s.station_id);
    marker.bindPopup(`<b>${esc(s.station_name)}</b><br>Arrive ${fmtDayTime(s.arrival_time)} · ${Math.round(s.arrival_soc)}% → ${Math.round(s.departure_soc)}%<br>Charging <b>$${s.price_per_kwh.toFixed(2)}/kWh</b> · ${s.kwh_purchased.toFixed(1)} kWh · <b>${money(s.cost)}</b>${priceScheduleHtml(c?.pricing, c?.pricing_status, s.arrival_time)}`);
  });
  drawChargerMarkers();
}
// Every charger the plan skips: a dot colored like its "why" chip, with the price you'd pay when passing.
function drawChargerMarkers() {
  if (!map || !tripData) return;
  const reopen = [...chargerMarkers].find(([, m]) => m.isPopupOpen())?.[0];
  chargerMarkers.forEach(m => map.removeLayer(m));
  chargerMarkers = new Map();
  const chosen = new Set((selectedPlan?.stops || []).map(s => s.station_id));
  tripData.nearby_chargers.filter(c => !chosen.has(c.station_id)).forEach(c => {
    const info = whyInfo(c);
    const usable = c.eligible && !c.user_excluded;
    const e = explanations[c.station_id];
    const price = e?.price_at_pass ?? (c.pricing?.bands?.length ? Math.min(...c.pricing.bands.map(b => b.price_per_kwh)) : null);
    const html = `<div class="cmark ${usable ? info.tone : 'off'}"><i></i>${usable && price != null ? `<b>$${price.toFixed(2)}</b>` : ''}</div>`;
    const marker = L.marker([c.coordinate.lat, c.coordinate.lon], {
      icon: L.divIcon({className: 'cmark-wrap', html, iconSize: [12, 12], iconAnchor: [6, 6]}),
      zIndexOffset: usable ? 0 : -200,
    })
      .bindTooltip(`${esc(stationName(c.station_name))} · ${esc(info.label)}`, {direction: 'top', offset: [0, -8]})
      .bindPopup(() => chargerPopupHtml(c), {maxWidth: 320, minWidth: 260})
      .addTo(map);
    marker.on('click', () => setOpenStation(c.station_id, {scroll: false}));
    chargerMarkers.set(c.station_id, marker);
  });
  if (reopen) chargerMarkers.get(reopen)?.openPopup();
  highlightMarker(openStation);
}
function highlightMarker(stationId) {
  chargerMarkers.forEach((m, id) => m.getElement()?.querySelector('.cmark')?.classList.toggle('hl', id === stationId));
}

/* Why a charger is (not) used */
function filterShort(reason) {
  if (!reason) return 'Not usable';
  if (/detour/i.test(reason)) return 'Detour too long';
  if (/timezone/i.test(reason)) return 'Time zone unknown';
  if (/price/i.test(reason)) return 'No price';
  return reason;
}
// {tone, label, short, text}: short fits in a table row; text is the full sentence.
function whyInfo(c) {
  if (c.user_excluded) return {tone: 'muted', label: 'Turned off', short: 'Unchecked by you', text: 'You unchecked this station.'};
  if (!c.eligible) return {tone: 'muted', label: 'Filtered out', short: filterShort(c.exclusion_reason), text: esc(c.exclusion_reason || 'Not usable for planning.')};
  const e = explanations[c.station_id];
  if (!e) return {tone: 'muted', label: '…', short: 'Checking…', text: 'Checking…'};
  const tried = whatIfResults.get(`${planKey(selectedPlan)}|${c.station_id}`);
  if (tried?.feasible && tried.cost_delta < -0.005) {
    return {tone: 'good', label: 'Cheaper option', short: `Saves ${money(-tried.cost_delta)}`, text: `Charging here saves ${money(-tried.cost_delta)}. The planner's search missed this; use <b>Show plan</b> to switch.`};
  }
  const at = e.pass_time ? fmtClock(e.pass_time) : '';
  const price = e.price_at_pass != null ? `$${e.price_at_pass.toFixed(2)}` : 'Unknown price';
  const refName = e.ref_station_name ? esc(stationName(e.ref_station_name)) : '';
  const refPrice = e.ref_price != null ? `$${e.ref_price.toFixed(2)}` : '';
  const ref = refName ? `${refName} (${refPrice})` : '';
  switch (e.verdict) {
    case 'used': {
      const stop = selectedPlan?.stops.find(st => st.station_id === c.station_id);
      return {tone: 'good', label: 'In plan', short: stop ? `${money(stop.cost)} · ${Math.round(stop.arrival_soc)}% → ${Math.round(stop.departure_soc)}%` : 'Charges here', text: whySelected(stop)};
    }
    case 'pricier': return {tone: 'warn', label: 'Pricier then', short: `${price} vs ${refPrice} at ${refName}`, text: `${price}/kWh when you'd arrive at ${at}; the plan charges at ${ref}.${e.cheaper_at ? ` Drops to $${e.cheaper_price.toFixed(2)} at ${fmtClock(e.cheaper_at)}.` : ''}`};
    case 'out_of_range': return {tone: 'bad', label: 'Out of range', short: `Would arrive at ${Math.max(0, Math.round(e.pass_soc))}%`, text: `You'd arrive with about ${Math.max(0, Math.round(e.pass_soc))}%, below your ${lastRequest.min_charger_soc ?? 10}% charger reserve.`};
    case 'too_early': return {tone: 'muted', label: 'Not needed yet', short: `${Math.round(e.pass_soc)}% when passing`, text: `You'd pass with ${Math.round(e.pass_soc)}% battery, too early to stop. The plan buys its energy later, mainly at ${ref}.`};
    case 'not_needed': return {tone: 'muted', label: 'Not needed', short: `${Math.round(e.pass_soc)}% when passing`, text: `You'd pass with ${Math.round(e.pass_soc)}%, enough to finish without charging.`};
    case 'detour': return {tone: 'muted', label: 'Detour', short: `+${Math.round(e.detour_minutes)} min detour`, text: `Adds about ${Math.round(e.detour_minutes)} min of driving for no better price than ${ref}.`};
    case 'off_path': return {tone: 'muted', label: 'Off this path', short: 'Not on this route', text: "This plan's route doesn't pass it."};
    default: return {tone: 'muted', label: 'No saving', short: `No cheaper than ${refName}`, text: `${price}/kWh at ${at}, no cheaper than ${ref}, so an extra stop here wouldn't lower the total.`};
  }
}
// Why the plan chose a charging stop, from the plan itself.
function whySelected(stop) {
  if (!stop) return 'The plan charges here.';
  const stops = selectedPlan.stops;
  const cheapest = Math.min(...stops.map(st => st.price_per_kwh));
  const next = stops[stops.indexOf(stop) + 1];
  const reserve = lastRequest.min_charger_soc ?? 10;
  const bought = `${Math.round(stop.arrival_soc)}% → ${Math.round(stop.departure_soc)}% (${stop.kwh_purchased.toFixed(1)} kWh) for ${money(stop.cost)}`;
  if (stop.price_per_kwh <= cheapest + 0.0005 && stops.length > 1) {
    return `Cheapest rate on this trip ($${stop.price_per_kwh.toFixed(2)}/kWh), so the plan buys the most here: ${bought}.`;
  }
  if (next && next.price_per_kwh < stop.price_per_kwh - 0.0005) {
    return `Short top-up: just enough to reach ${esc(stationName(next.station_name))} at $${next.price_per_kwh.toFixed(2)}/kWh with your ${reserve}% reserve. ${bought}.`;
  }
  return `Best price reachable at ${fmtClock(stop.arrival_time)} ($${stop.price_per_kwh.toFixed(2)}/kWh): ${bought}.`;
}
function whyChip(info) { return `<span class="why-chip ${info.tone}"><i></i>${esc(info.label)}</span>`; }
function whatIfHtml(c) {
  const result = whatIfResults.get(`${planKey(selectedPlan)}|${c.station_id}`);
  if (!result) return `<button type="button" class="whatif-btn" data-station-id="${esc(c.station_id)}">What if I charge here?</button>`;
  if (result.loading) return `<div class="whatif-result muted">Comparing…</div>`;
  if (result.error) return `<div class="whatif-result muted">${esc(result.error)}</div>`;
  if (!result.feasible) return `<div class="whatif-result muted">No workable plan charges here at this departure.</div>`;
  const signed = (v, fmt) => `${v > 0 ? '+' : v < 0 ? '−' : '±'}${fmt(Math.abs(v))}`;
  const tone = result.cost_delta > 0.005 ? 'worse' : 'better';
  return `<div class="whatif-result"><span class="${tone}"><b>${signed(result.cost_delta, money)}</b> · ${signed(result.minutes_delta, fmtMinutes)}</span> vs this plan
    <button type="button" class="link-btn" data-show-whatif="${esc(c.station_id)}">Show plan</button></div>`;
}
function chargerPopupHtml(c) {
  const info = whyInfo(c);
  const canAsk = c.eligible && !c.user_excluded && explanations[c.station_id] && explanations[c.station_id].verdict !== 'used';
  return `<div class="popup-head"><b>${esc(c.station_name)}</b>${whyChip(info)}</div>
    <div class="muted">${esc(c.address)}</div>
    <div class="muted">${c.corridor_distance_miles.toFixed(1)} mi from route${c.detour_minutes == null ? '' : ` · ~${Math.round(c.detour_minutes)} min detour`} · ${esc(STATUS_LABELS[c.pricing_status])} pricing</div>
    ${priceScheduleHtml(c.pricing, c.pricing_status, explanations[c.station_id]?.pass_time)}
    <div class="why-text">${info.text}</div>
    ${canAsk ? `<div class="whatif" data-whatif-for="${esc(c.station_id)}">${whatIfHtml(c)}</div>` : ''}`;
}
async function loadExplanations(plan) {
  const token = ++explainToken;
  explanations = {};
  if (!tripData?.trip_id || !plan) return;
  try {
    const {route_geometry, ...lean} = plan;
    const response = await fetch(`/api/trips/${encodeURIComponent(tripData.trip_id)}/explain`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan: lean}),
    });
    if (!response.ok || token !== explainToken) return;
    explanations = (await response.json()).chargers || {};
    renderStations();
    drawChargerMarkers();
  } catch (_) { /* Explanations are optional. */ }
}
async function runWhatIf(stationId) {
  const key = `${planKey(selectedPlan)}|${stationId}`;
  const reopen = () => { renderStations(); drawChargerMarkers(); };
  whatIfResults.set(key, {loading: true});
  reopen();
  try {
    const {route_geometry, ...lean} = selectedPlan;
    const response = await fetch(`/api/trips/${encodeURIComponent(tripData.trip_id)}/what-if`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan: lean, station_id: stationId}),
    });
    const data = await response.json();
    whatIfResults.set(key, response.ok ? data : {error: data.detail || 'Comparison failed.'});
  } catch (error) {
    whatIfResults.set(key, {error: 'Comparison failed.'});
  }
  reopen();
}
document.addEventListener('click', event => {
  const ask = event.target.closest('.whatif-btn');
  if (ask) { runWhatIf(ask.dataset.stationId); return; }
  const show = event.target.closest('[data-show-whatif]');
  if (show) {
    const result = whatIfResults.get(`${planKey(selectedPlan)}|${show.dataset.showWhatif}`);
    if (result?.plan) { map.closePopup(); selectPlan(result.plan); }
  }
});

/* Departure timeline */
function costColor(t) {
  const stops = [[34, 197, 94], [245, 177, 76], [232, 33, 39]];
  const [a, b, f] = t < .5 ? [stops[0], stops[1], t * 2] : [stops[1], stops[2], (t - .5) * 2];
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * f)).join(',')})`;
}
function renderTimeline() {
  const section = document.getElementById('departures');
  const options = tripData.departure_options || [];
  section.hidden = !options.length;
  if (!options.length) return;
  const costs = options.map(o => o.charging_cost);
  const lo = Math.min(...costs), hi = Math.max(...costs), span = Math.max(0.01, hi - lo);
  const selectedIndex = Math.max(0, options.findIndex(o => o.departure_time === selectedPlan?.departure_time));
  const bars = options.map((o, i) => {
    const t = (o.charging_cost - lo) / span;
    return `<button type="button" class="bar ${i === selectedIndex ? 'selected' : ''}" data-index="${i}" style="--h:${Math.round(55 + t * 45)}%;--c:${costColor(t)}" title="${fmtClock(o.departure_time)} · ${money(o.charging_cost)} · ${fmtMinutes(o.total_minutes)}" aria-label="Depart ${fmtClock(o.departure_time)}, ${money(o.charging_cost)}"></button>`;
  }).join('');
  const hourly = options.map((o, i) => ({o, i})).filter(({o}) => new Date(o.departure_time).getMinutes() === 0);
  const step = Math.ceil(hourly.length / 13) || 1;
  const pct = i => ((i + .5) / options.length * 100).toFixed(2);
  const axis = hourly.filter((_, k) => k % step === 0).map(({o, i}, k) =>
    `<span class="${i === selectedIndex ? 'on' : ''} ${k % 3 ? 'm-hide' : ''}" style="left:${pct(i)}%">${fmtClock(o.departure_time)}</span>`).join('');
  const sel = options[selectedIndex];
  section.innerHTML = `
    <div class="section-head">
      <div><h2 class="kicker">Departure time &amp; estimated cost</h2><p>Choose when to leave to see total trip cost and duration. ${tripData.departures_tested} departure times compared using each station's time-of-use prices.</p></div>
      <div class="legend"><span><i style="background:var(--green)"></i>Lower cost</span><span><i style="background:var(--amber)"></i>Moderate</span><span><i style="background:var(--red)"></i>Higher cost</span></div>
    </div>
    <div class="timeline">
      <div class="bar-tip" style="--x:${pct(selectedIndex)}%"><small>${fmtClock(sel.departure_time)}</small><b>${money(sel.charging_cost)}</b><small>${fmtMinutes(sel.total_minutes)}</small></div>
      <div class="bars">${bars}</div>
      <div class="axis">${axis}</div>
    </div>`;
  section.querySelector('.bars').addEventListener('click', event => {
    const bar = event.target.closest('.bar');
    if (bar) selectPlan(options[Number(bar.dataset.index)]);
  });
}

/* Stations table */
function stationRows() {
  const used = new Set((selectedPlan?.stops || []).map(st => st.station_id));
  const all = [...tripData.nearby_chargers].sort((a, b) => a.route_progress - b.route_progress);
  if (stationFilter === 'plan') return all.filter(c => used.has(c.station_id));
  if (stationFilter === 'skipped') return all.filter(c => !used.has(c.station_id));
  return all;
}
function stationDetailHtml(c, stop, info) {
  const e = explanations[c.station_id];
  const facts = [];
  if (stop) {
    facts.push(['Arrive', `${fmtDayTime(stop.arrival_time)} with ${Math.round(stop.arrival_soc)}%`]);
    facts.push(['Charge', `${Math.round(stop.arrival_soc)}% → ${Math.round(stop.departure_soc)}% · ${fmtMinutes(stop.charging_minutes)}`]);
    facts.push(['Energy', `${stop.kwh_purchased.toFixed(1)} kWh at $${stop.price_per_kwh.toFixed(2)}${stop.price_is_estimate ? ' (estimate)' : ''}`]);
    facts.push(['Cost', `<b>${money(stop.cost)}</b>`]);
  } else if (e?.pass_time) {
    facts.push(["You'd pass", fmtDayTime(e.pass_time)]);
    facts.push(['Battery then', `${Math.max(0, Math.round(e.pass_soc))}%`]);
    facts.push(['Price then', e.price_at_pass != null ? `<b>$${e.price_at_pass.toFixed(2)}/kWh</b>` : 'Unknown']);
    if (e.cheaper_at) facts.push(['Cheaper from', `${fmtClock(e.cheaper_at)} at $${e.cheaper_price.toFixed(2)}`]);
  }
  if (e?.miles_from_last_charge != null) {
    facts.push(['Since last charge', `${Math.round(e.miles_from_last_charge)} mi from ${esc(e.last_charge_name === 'Start' ? 'the start' : stationName(e.last_charge_name))}`]);
    facts.push(['Trip so far', `${Math.round(e.trip_miles)} mi`]);
  }
  facts.push(['Off route', `${c.corridor_distance_miles.toFixed(1)} mi${c.detour_minutes == null ? '' : ` · ~${Math.round(c.detour_minutes)} min detour`}`]);
  const site = [c.stalls ? `${c.stalls} stalls` : '', c.power_kw ? `${c.power_kw} kW` : ''].filter(Boolean).join(' · ');
  if (site) facts.push(['Site', site]);
  const canAsk = !stop && c.eligible && !c.user_excluded && e && e.verdict !== 'used';
  return `<div class="detail">
      <div class="detail-col">
        <h4>${stop ? 'Why it was chosen' : 'Why it was skipped'}</h4>
        <p>${info.text}</p>
        ${canAsk ? `<div class="whatif" data-whatif-for="${esc(c.station_id)}">${whatIfHtml(c)}</div>` : ''}
      </div>
      <div class="detail-col">
        <h4>${stop ? 'Charging stop' : 'If you stopped here'}</h4>
        <dl>${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      </div>
      <div class="detail-col">
        <h4>Price schedule</h4>
        ${priceScheduleHtml(c.pricing, c.pricing_status, stop?.arrival_time || e?.pass_time)}
        ${c.pricing?.fetched_at ? `<p class="muted small">Price checked ${priceAge(c.pricing.fetched_at)}</p>` : ''}
        <p class="muted small">${esc(c.address)}${c.tesla_url ? ` · <a href="${esc(c.tesla_url)}" target="_blank" rel="noopener">Tesla page</a>` : ''}</p>
      </div>
    </div>`;
}
function renderStations() {
  const section = document.getElementById('stations');
  const all = tripData.nearby_chargers;
  section.hidden = !all.length;
  if (!all.length) return;
  const stopById = new Map((selectedPlan?.stops || []).map(st => [st.station_id, st]));
  const rows = stationRows();
  const priced = all.filter(c => c.pricing_status !== 'unknown').length;
  const inPlan = all.filter(c => stopById.has(c.station_id)).length;
  const body = rows.map((c, i) => {
    const stop = stopById.get(c.station_id);
    const info = whyInfo(c);
    const e = explanations[c.station_id];
    const status = c.user_excluded ? 'excluded' : c.pricing_status;
    const now = stop ? stop.price_per_kwh : e?.price_at_pass;
    const mine = myPrices()[c.station_id];
    const needsPrice = !mine && worthPricing(c);
    const manual = needsPrice
      ? `<div class="my-price"><a class="tesla-check" href="${esc(findusUrl(c.station_id))}" target="teslafare-single-${esc(c.station_id)}" rel="opener" data-check="${esc(c.station_id)}" title="${helperVersion ? 'Opens Tesla; the helper grabs the price and updates your plan' : 'Opens the station on Tesla'}">Check on Tesla ↗</a><span><input type="number" min="0.01" max="2" step="0.01" placeholder="$/kWh" aria-label="Price for ${esc(c.station_name)}"><button type="button" class="my-price-btn" data-my-price="${esc(c.station_id)}">Use</button></span></div>`
      : mine ? `<b class="price-now">$${Number(mine.price).toFixed(2)}</b><small>Yours · <button type="button" class="link-btn" data-clear-price="${esc(c.station_id)}">clear</button></small>` : '';
    const priceCell = manual || `<b class="price-now">${now != null ? `$${now.toFixed(2)}` : (priceRange(c.pricing) || '—')}</b><small>${esc(STATUS_LABELS[status] || status)}${now != null && c.pricing?.kind === 'time_of_use' ? ` · ${priceRange(c.pricing)}` : ''}</small>`;
    const open = c.station_id === openStation ? ' open' : '';
    const pinned = c.station_id === pinnedStation ? ' pinned' : '';
    return `<tr class="station-row${open}${pinned}${stop ? ' on-route' : ''}${c.user_excluded ? ' excluded' : ''}" data-id="${esc(c.station_id)}" tabindex="0" aria-expanded="${Boolean(open)}">
        <td class="num">${i + 1}</td>
        <td><div class="station-cell"><span class="mini-charge ${c.eligible && !c.user_excluded ? '' : 'off'}">${boltIcon}</span><span><b class="st-name">${esc(stationName(c.station_name))}</b><small>${esc(cityState(c.address) || '')}</small></span></div></td>
        <td class="leg-miles">${e?.miles_from_last_charge != null ? `<b>${Math.round(e.miles_from_last_charge)} mi</b><small>from ${esc(e.last_charge_name === 'Start' ? 'start' : stationName(e.last_charge_name))}</small>` : '<span class="muted">—</span>'}</td>
        <td class="off-route">${c.corridor_distance_miles.toFixed(1)} mi<small>${c.detour_minutes == null ? '' : `${Math.round(c.detour_minutes)} min detour`}</small></td>
        <td class="price-cell">${priceCell}</td>
        <td><div class="why-cell">${whyChip(info)}<span class="why-line">${info.short}</span></div></td>
        <td class="use"><input class="charger-toggle" type="checkbox" value="${esc(c.station_id)}" ${c.user_excluded ? '' : 'checked'} aria-label="Use ${esc(c.station_name)}"></td>
        <td class="expander">${icon('i-down')}</td>
      </tr>
      <tr class="detail-row${open}" data-for="${esc(c.station_id)}"><td colspan="8"><div class="detail-wrap"><div>${stationDetailHtml(c, stop, info)}</div></div></td></tr>`;
  }).join('');
  const chip = (key, text) => `<button type="button" class="seg ${stationFilter === key ? 'on' : ''}" data-filter="${key}">${text}</button>`;
  section.innerHTML = `
    <div class="section-head">
      <div><h2 class="kicker">Charging stations along your route</h2><p>Hover a row for details, or click to keep it open. Uncheck stations you don't want, then recalculate. Missing a price? Check it on Tesla and enter it. It's only used for your trips and stays in this browser.</p></div>
      <div class="station-meta">
        <div class="found-count"><div><i class="dot"></i>${all.length} nearby chargers found <span class="sepbar">|</span> ${priced} of ${all.length} prices retrieved</div><div class="bar-track"><span style="width:${Math.round(priced / all.length * 100)}%"></span></div></div>
        <div class="segmented">${chip('all', `All ${all.length}`)}${chip('plan', `In plan ${inPlan}`)}${chip('skipped', `Skipped ${all.length - inPlan}`)}</div>
      </div>
    </div>
    <div class="table-wrap"><table class="stations-table">
      <thead><tr><th>#</th><th>Station</th><th title="Road miles driven since your last charge (or the start) when you reach this charger">From last charge</th><th>Off route</th><th>Price when you pass</th><th>In this plan</th><th>Use</th><th></th></tr></thead>
      <tbody>${body}</tbody>
    </table></div>
    <div class="table-foot"><span>${tripData.pricing_estimated} estimated · ${tripData.charger_cache_entries} saved stations · base route ${tripData.base_route.distance_miles.toFixed(0)} mi</span><button id="recalculate" class="ghost-btn" type="button">Recalculate</button></div>`;
  section.querySelectorAll('[data-filter]').forEach(btn => btn.addEventListener('click', () => { stationFilter = btn.dataset.filter; renderStations(); }));
  document.getElementById('recalculate').addEventListener('click', () => {
    const shown = [...section.querySelectorAll('.charger-toggle')];
    const excluded = new Set(lastRequest.excluded_station_ids);
    shown.forEach(el => el.checked ? excluded.delete(el.value) : excluded.add(el.value));
    runTrip({...lastRequest, excluded_station_ids: [...excluded]});
  });
  const tbody = section.querySelector('tbody');
  const rowId = target => target.closest('tr.station-row')?.dataset.id || target.closest('tr.detail-row')?.dataset.for;
  tbody.addEventListener('mouseover', event => {
    if (pinnedStation) return;
    const id = rowId(event.target);
    clearTimeout(hoverTimer);
    if (id && id !== openStation) hoverTimer = setTimeout(() => setOpenStation(id), 180);
  });
  tbody.addEventListener('mouseleave', () => {
    clearTimeout(hoverTimer);
    if (!pinnedStation) hoverTimer = setTimeout(() => setOpenStation(null), 250);
  });
  const togglePin = id => {
    pinnedStation = pinnedStation === id ? null : id;
    setOpenStation(pinnedStation || id);
  };
  tbody.addEventListener('click', event => {
    const row = event.target.closest('tr.station-row');
    if (!row || event.target.closest('input, button, a')) return;
    togglePin(row.dataset.id);
  });
  tbody.addEventListener('keydown', event => {
    const row = event.target.closest('tr.station-row');
    if (row && (event.key === 'Enter' || event.key === ' ') && event.target === row) { event.preventDefault(); togglePin(row.dataset.id); }
  });
}
function setOpenStation(id, {scroll = false} = {}) {
  openStation = id;
  document.querySelectorAll('#stations tr.station-row').forEach(tr => {
    const on = tr.dataset.id === id;
    tr.classList.toggle('open', on);
    tr.classList.toggle('pinned', on && pinnedStation === id);
    tr.setAttribute('aria-expanded', String(on));
    if (on && scroll) tr.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  });
  document.querySelectorAll('#stations tr.detail-row').forEach(tr => tr.classList.toggle('open', tr.dataset.for === id));
  highlightMarker(id);
}
function renderLiveStations(rows) {
  const section = document.getElementById('stations');
  if (!rows?.length) return;
  section.hidden = false;
  const done = rows.filter(r => r.status !== 'fetching').length;
  const statusOf = r => r.status === 'found' ? 'verified' : r.status;
  section.innerHTML = `
    <div class="section-head"><div><h2 class="kicker">Checking charging stations (${rows.length})</h2><p>Retrieving Supercharger prices along the route…</p></div>
      <div class="found-count"><div><i class="dot"></i>${rows.length} nearby chargers found <span class="sepbar">|</span> ${done} of ${rows.length} checked</div><div class="bar-track"><span style="width:${Math.round(done / rows.length * 100)}%"></span></div></div></div>
    <div class="table-wrap"><table><thead><tr><th>#</th><th>Station</th><th>Price ($/kWh)</th><th>Price status</th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr><td>${i + 1}</td><td><div class="station-cell"><span class="mini-charge">${boltIcon}</span>${esc(stationName(r.station_name))}</div></td><td>${priceRange(r.pricing) || '—'}</td><td>${statusHtml(statusOf(r))}</td></tr>`).join('')}
    </tbody></table></div>`;
}

/* Other plans */
function planSequence(p) {
  const items = [
    ...p.stops.map(s => [s.arrival_time, esc(shortName(s.station_name))]),
    ...(p.waypoints || []).map(w => [w.arrival_time, `<span class="wp-letter">${stopLetter(w.index)}</span> ${esc(placeLabel(w.name).city)}`]),
  ].sort((a, b) => new Date(a[0]) - new Date(b[0]));
  return items.length ? items.map(([, html]) => html).join(' → ') : 'No charging stops';
}
function renderPlans() {
  const section = document.getElementById('plans');
  if (tripData.plans.length < 2) { section.innerHTML = ''; return; }
  section.innerHTML = `<h2>Other useful plans</h2><div class="plan-grid">${tripData.plans.map((p, i) => `
    <button type="button" class="plan-card ${planKey(p) === planKey(selectedPlan) ? 'selected' : ''}" data-index="${i}">
      <span class="cat">${esc(CATEGORY_LABELS[p.category] || p.category)}</span>
      <span class="price">${money(p.charging_cost)}</span>
      <span class="line">Leave ${fmtDayTime(p.departure_time)} · ${fmtMinutes(p.total_minutes)} · ${p.stops.length} ${p.waypoints?.length ? 'charge' : 'stop'}${p.stops.length === 1 ? '' : 's'}<br>${planSequence(p)}</span>
    </button>`).join('')}</div>`;
  section.querySelectorAll('.plan-card').forEach(el => el.addEventListener('click', () => selectPlan(tripData.plans[Number(el.dataset.index)])));
}

function selectPlan(plan) {
  selectedPlan = plan;
  loadExplanations(plan);
  updateSummary(plan.departure_time);
  renderRecommended(plan);
  drawMap(plan);
  renderTimeline();
  renderStations();
  renderPlans();
}

function renderReplayValidation(replay) {
  const section = document.getElementById('replay-validation');
  if (!replay) { section.innerHTML = ''; return; }
  const costDelta = replay.cost_delta >= 0 ? `${money(replay.cost_delta)} more` : `${money(Math.abs(replay.cost_delta))} less`;
  section.innerHTML = `<article class="panel replay-card"><h2>Recent-trip reproduction result</h2><p>A validation run using the four rates observed on October 2 — not a recommendation based on old prices.</p>
    <div class="replay-metrics"><div><b>${money(replay.actual_cost)}</b><span>actual trip charging</span></div><div><b>${money(replay.modeled_cost)}</b><span>modeled at the same departure</span></div><div><b>${costDelta}</b><span>model difference</span></div><div><b>${replay.actual_kwh.toFixed(1)} kWh</b><span>actual purchased</span></div><div><b>${replay.modeled_kwh.toFixed(1)} kWh</b><span>modeled purchased</span></div></div>
    <p class="${replay.exact_station_sequence ? 'replay-match' : 'replay-miss'}">${replay.exact_station_sequence ? 'Exact station sequence reproduced.' : `${replay.matched_station_count} of ${replay.actual_stations.length} actual stations appeared in the modeled route.`}</p>
    <p><b>Actual:</b> ${replay.actual_stations.map(esc).join(' → ')}</p><p><b>Modeled:</b> ${replay.modeled_stations.length ? replay.modeled_stations.map(esc).join(' → ') : 'No charging stops'}</p></article>`;
}

/* Progress */
function updateProgressPanel(state) {
  const panel = document.getElementById('progress');
  panel.hidden = Boolean(state.complete && state.stage === 'complete');
  document.getElementById('progress-message').textContent = state.message || 'Working…';
  const done = state.pricing_completed || 0, total = state.pricing_total || 0;
  const departureDone = state.departure_completed || 0, departureTotal = state.departure_total || 0;
  const optimizing = state.stage === 'optimizing' && departureTotal;
  document.getElementById('progress-count').textContent = optimizing ? `${departureDone}/${departureTotal} departure times` : total ? `${done}/${total} checked · ${state.pricing_found || 0} prices found` : '';
  document.getElementById('progress-bar').style.width = optimizing ? `${Math.round(departureDone / departureTotal * 100)}%` : total ? `${Math.round(done / total * 100)}%` : '8%';
  if (state.chargers?.length && !state.complete) renderLiveStations(state.chargers);
  const skip = document.getElementById('skip-pricing');
  skip.hidden = !(state.stage === 'pricing' && !state.complete && total && done < total);
  if (skip.hidden) skip.disabled = false;
}
async function pollProgress(progressId, token) {
  while (token === progressPollToken) {
    try {
      const response = await fetch(`/api/trip/progress/${encodeURIComponent(progressId)}`, {cache:'no-store'});
      if (response.ok) {
        const state = await response.json();
        if (token !== progressPollToken) return;
        updateProgressPanel(state);
        if (state.complete) return;
      }
    } catch (_) { /* The main request reports network errors. */ }
    await new Promise(resolve => setTimeout(resolve, 400));
  }
}

async function runTrip(payload) {
  const button = document.querySelector('#trip-form button[type=submit]');
  const status = document.getElementById('status');
  button.disabled = true;
  const progressId = `trip_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  lastRequest = {...payload, excluded_station_ids: payload.excluded_station_ids || [], progress_id: progressId};
  const token = ++progressPollToken;
  updateSummary();
  status.textContent = '';
  updateProgressPanel({message:'Calculating the base route and finding nearby Superchargers…'});
  pollProgress(progressId, token);
  document.getElementById('warnings').innerHTML = '';
  try {
    const response = await fetch('/api/trip', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(lastRequest)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Trip request failed');
    ++progressPollToken;
    updateProgressPanel({stage:'complete', complete:true});
    tripData = data;
    openStation = pinnedStation = null;
    status.textContent = `Base route ${data.base_route.distance_miles.toFixed(1)} mi · ${data.candidate_chargers} nearby chargers · ${data.pricing_available} priced · ${data.departures_tested} departure times tested`;
    document.getElementById('warnings').innerHTML = data.warnings.map(w => `<div class="warning">${esc(w)}</div>`).join('');
    renderReplayValidation(data.replay_validation);
    renderMissing();
    priceBaseline = null;
    checkServerPrices();  // record what the server has right now, so later changes trigger a re-plan
    const best = data.plans[0];
    if (best) {
      toggleForm(false);
      selectPlan(best);
    } else {
      selectedPlan = null;
      document.getElementById('recommended').innerHTML = '<h2 class="kicker">Recommended departure</h2><div class="empty-state"><p>No feasible charging plan was found. Try a higher starting battery or include more stations.</p></div>';
      drawMap(null);
      renderTimeline();
      renderStations();
      renderPlans();
    }
  } catch (error) {
    ++progressPollToken;
    updateProgressPanel({stage:'error', message:`Trip calculation failed: ${error.message}`, complete:true});
  } finally {
    button.disabled = false;
  }
}

document.getElementById('stations').addEventListener('click', event => {
  const check = event.target.closest('a[data-check]');
  if (check) { waitingFor.add(check.dataset.check); watchServerPrices(); return; }
  const use = event.target.closest('.my-price-btn');
  const clear = event.target.closest('[data-clear-price]');
  if (!use && !clear) return;
  let prices;
  if (use) {
    const input = use.parentElement.querySelector('input');
    const price = Number(input.value);
    if (!Number.isFinite(price) || price < 0.01 || price > 2) {
      input.setCustomValidity('Enter a price between $0.01 and $2.00 per kWh.');
      input.reportValidity();
      return;
    }
    input.setCustomValidity('');
    use.disabled = true;
    prices = setMyPrice(use.dataset.myPrice, price);
  } else {
    prices = setMyPrice(clear.dataset.clearPrice, null);
  }
  runTrip({...lastRequest, price_overrides: myPricesPayload(prices)});
});

document.getElementById('trip-form').addEventListener('submit', event => {
  event.preventDefault();
  const request = {
    from_location:document.getElementById('from').value,
    to_location:document.getElementById('to').value,
    stops:replayScenario ? [] : stopsPayload(),
    fallback_price_per_kwh:Number(document.getElementById('fallback-price').value),
    starting_soc:Number(document.getElementById('starting-soc').value),
    min_charger_soc:Number(document.getElementById('min-charger-soc').value),
    destination_soc:Number(document.getElementById('destination-soc').value),
    vehicle_profile_id:document.getElementById('vehicle-profile').value,
    custom_battery_usable_kwh:document.getElementById('vehicle-profile').value === 'custom' ? Number(document.getElementById('custom-battery-kwh').value) : null,
    custom_highway_wh_per_mile:document.getElementById('vehicle-profile').value === 'custom' ? Number(document.getElementById('custom-whmi').value) : null,
    custom_peak_charge_kw:document.getElementById('vehicle-profile').value === 'custom' ? Number(document.getElementById('custom-peak-kw').value) : null,
    use_charger_cache:document.getElementById('use-charger-cache').checked,
    desired_departure_time:document.getElementById('desired-departure').value || null,
    departure_window_hours:12,
    replay_scenario:replayScenario,
    excluded_station_ids: [],
    price_overrides: myPricesPayload(),
    captured_prices: capturedPayload()
  };
  saveLastTrip(request);
  runTrip(request);
});

document.getElementById('load-replay').addEventListener('click', event => {
  replayScenario = replayScenario ? null : 'oct_2026_nashville_streetsboro';
  event.currentTarget.classList.toggle('active', Boolean(replayScenario));
  event.currentTarget.textContent = replayScenario ? 'Recent trip replay loaded' : 'Load recent trip replay';
  document.getElementById('from').value = replayScenario ? '2606 8th Ave S, Nashville, TN' : document.getElementById('from').defaultValue;
  document.getElementById('to').value = replayScenario ? 'Wingate by Wyndham Streetsboro, Streetsboro, OH' : document.getElementById('to').defaultValue;
  if (replayScenario) document.getElementById('desired-departure').value = '2026-10-02T00:30';
  if (replayScenario) stopsState = [];
  renderStopsEditor();
});

document.getElementById('skip-pricing').addEventListener('click', async event => {
  if (!lastRequest?.progress_id) return;
  event.currentTarget.disabled = true;
  try {
    await fetch(`/api/trip/progress/${encodeURIComponent(lastRequest.progress_id)}/skip-pricing`, {method: 'POST'});
  } catch (_) { /* The trip still finishes at the server's deadline. */ }
});
document.getElementById('edit-trip').addEventListener('click', () => toggleForm());
document.getElementById('open-settings').addEventListener('click', () => { toggleForm(true); document.getElementById('fallback-price').focus(); });
document.getElementById('trip-form').addEventListener('input', () => { if (!tripData) updateSummary(); });
document.getElementById('vehicle-profile').addEventListener('change', updateVehicleAssumption);
document.getElementById('custom-battery-kwh').addEventListener('input', updateVehicleAssumption);
document.getElementById('custom-whmi').addEventListener('input', updateVehicleAssumption);
document.getElementById('custom-peak-kw').addEventListener('input', updateVehicleAssumption);

const defaultDeparture = new Date(Date.now() + 60 * 60 * 1000);
defaultDeparture.setMinutes(Math.ceil(defaultDeparture.getMinutes() / 30) * 30, 0, 0);
document.getElementById('desired-departure').value = new Date(defaultDeparture.getTime() - defaultDeparture.getTimezoneOffset() * 60000).toISOString().slice(0,16);
restoreLastTrip();
updateVehicleAssumption();
setupStopsEditor();
renderStopsEditor();
