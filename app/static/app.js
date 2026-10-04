let map;
let layers = [];

function fmtMinutes(mins) {
  const h = Math.floor(mins / 60), m = Math.round(mins % 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function fmtTime(iso) {
  return new Date(iso).toLocaleString([], {weekday:'short', hour:'numeric', minute:'2-digit'});
}
function esc(s) {
  return String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}
function planHtml(p, hero=false) {
  const stops = p.stops.map(s => `
    <div class="stop"><b>${esc(s.station_name)}</b><br>
    ${fmtTime(s.arrival_time)} · arrive ${s.arrival_soc.toFixed(1)}% → leave ${s.departure_soc.toFixed(1)}% ·
    $${s.price_per_kwh.toFixed(3)}/kWh · ${s.kwh_purchased.toFixed(1)} kWh · ${fmtMinutes(s.charging_minutes)} · <b>$${s.cost.toFixed(2)}</b></div>`).join('');
  return `<article class="${hero ? 'hero' : 'plan'}">
    <h3><span>${esc(p.category || 'PLAN')}</span><span>$${p.charging_cost.toFixed(2)}</span></h3>
    <div class="metrics">
      <div class="metric"><b>${fmtTime(p.departure_time)}</b><span>leave</span></div>
      <div class="metric"><b>${p.total_miles.toFixed(1)} mi</b><span>distance (+${p.extra_miles_vs_fastest.toFixed(1)})</span></div>
      <div class="metric"><b>${p.stops.length}</b><span>charging stops</span></div>
      <div class="metric"><b>${fmtMinutes(p.charging_minutes)}</b><span>charging</span></div>
      <div class="metric"><b>${fmtMinutes(p.total_minutes)}</b><span>total journey</span></div>
      <div class="metric"><b>${p.kwh_purchased.toFixed(1)} kWh</b><span>purchased</span></div>
    </div>${stops}</article>`;
}
function drawPlan(data, plan) {
  const el = document.getElementById('map');
  el.style.display = 'block';
  if (!map) {
    map = L.map('map');
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '&copy; OpenStreetMap'}).addTo(map);
  }
  layers.forEach(l => map.removeLayer(l)); layers = [];
  const latlngs = plan.route_geometry.map(([lon,lat]) => [lat,lon]);
  if (latlngs.length) { const line = L.polyline(latlngs).addTo(map); layers.push(line); map.fitBounds(line.getBounds(), {padding:[20,20]}); }
  const points = [
    [data.origin.coordinate.lat, data.origin.coordinate.lon, 'Origin'],
    ...plan.stops.map(s => [s.coordinate.lat, s.coordinate.lon, s.station_name]),
    [data.destination.coordinate.lat, data.destination.coordinate.lon, 'Destination']
  ];
  points.forEach(([lat,lon,label]) => { const m=L.marker([lat,lon]).bindPopup(label).addTo(map); layers.push(m); });
}

document.getElementById('trip-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const button = e.target.querySelector('button');
  const status = document.getElementById('status');
  button.disabled = true;
  const startedAt = Date.now();
  status.textContent = 'Calculating route and finding nearby Superchargers…';
  const progressTimer = setInterval(() => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    status.textContent = `Working… ${seconds}s elapsed. First run may take longer while public Tesla prices are cached.`;
  }, 1000);
  document.getElementById('warnings').innerHTML = ''; document.getElementById('best').innerHTML=''; document.getElementById('plans').innerHTML='';
  try {
    const r = await fetch('/api/trip', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({from_location:document.getElementById('from').value,to_location:document.getElementById('to').value})});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Trip request failed');
    status.textContent = `Base route ${data.base_route.distance_miles.toFixed(1)} mi / ${fmtMinutes(data.base_route.duration_minutes)} · ${data.candidate_chargers} candidate chargers · ${data.pricing_available} priced · ${data.departures_tested} departure times tested`;
    document.getElementById('warnings').innerHTML = data.warnings.map(w => `<div class="warning">${esc(w)}</div>`).join('');
    if (!data.plans.length) return;
    const cheapest = data.plans[0];
    const expensive = data.plans.find(p => p.category === 'MOST EXPENSIVE REASONABLE');
    let save = expensive ? Math.max(0, expensive.charging_cost - cheapest.charging_cost) : 0;
    document.getElementById('best').innerHTML = planHtml(cheapest, true) + (save > 0 ? `<p><b>SAVE $${save.toFixed(2)}</b> versus the most expensive reasonable evaluated plan.</p>` : '');
    document.getElementById('plans').innerHTML = '<h2>Other useful plans</h2>' + data.plans.slice(1).map(p => planHtml(p)).join('');
    drawPlan(data, cheapest);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  } finally { clearInterval(progressTimer); button.disabled = false; }
});
