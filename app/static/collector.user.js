// ==UserScript==
// @name         TeslaFare price collector
// @namespace    teslafare
// @version      2.1
// @description  Sends the Supercharger price Tesla's Find Us page shows you to your TeslaFare server.
// @match        https://www.tesla.com/findus*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      __HOST__
// ==/UserScript==
(function () {
  'use strict';
  const SERVER = '__SERVER__';
  const BUILT_IN_KEY = '__KEY__';
  // Tabs opened from /collect (Open next, Open next 5, or a row's link) close themselves once their price
  // is saved. Tesla tabs you open yourself are left alone.
  const COLLECTOR_TAB = window.name.startsWith('teslafare-');
  const seen = new Set();

  function collectorKey(ask) {
    if (BUILT_IN_KEY && !BUILT_IN_KEY.startsWith('__') && !ask) return BUILT_IN_KEY;
    let key = GM_getValue('collectorKey', '');
    if (!key || ask) {
      const entered = prompt('TeslaFare collector key:', key);
      if (entered !== null) { key = entered.trim(); GM_setValue('collectorKey', key); }
    }
    return key;
  }
  GM_registerMenuCommand('Set TeslaFare collector key', () => collectorKey(true));

  function api(method, path, body) {
    return new Promise(resolve => GM_xmlhttpRequest({
      method,
      url: SERVER + path,
      headers: {'Content-Type': 'application/json', 'X-Collector-Key': collectorKey(false)},
      data: body ? JSON.stringify(body) : undefined,
      onload: response => {
        let data = {};
        try { data = JSON.parse(response.responseText); } catch (_) { /* non-JSON error */ }
        resolve({status: response.status, data});
      },
      onerror: () => resolve({status: 0, data: {detail: 'could not reach ' + SERVER}}),
    }));
  }

  const stationId = () => new URLSearchParams(location.search).get('location') || '';
  // Browsers only let a script close a tab it opened (and one that hasn't navigated around);
  // if closing is refused, fall back to the Next/Skip panel.
  function closeTab(message) {
    (typeof unsafeWindow !== 'undefined' ? unsafeWindow : window).close();
    setTimeout(() => panel(message || 'done. Close this tab or press N for the next station.', 'ok'), 500);
  }

  // A small panel bottom-right: status, plus Next/Skip (also keys N and S).
  function panel(message, tone, withActions = true) {
    let el = document.getElementById('teslafare-panel');
    if (!el) {
      el = document.createElement('div');
      el.id = 'teslafare-panel';
      el.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:380px;padding:12px 14px;'
        + 'border-radius:10px;font:600 13px/1.4 system-ui,sans-serif;color:#fff;box-shadow:0 6px 20px rgba(0,0,0,.45)';
      document.documentElement.appendChild(el);
    }
    el.style.background = {ok: '#15803d', warn: '#b45309', info: '#1f2937'}[tone] || '#1f2937';
    el.innerHTML = '';
    const text = document.createElement('div');
    text.textContent = 'TeslaFare: ' + message;
    el.appendChild(text);
    if (withActions) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:8px;margin-top:8px';
      for (const [label, action] of [['Next → (N)', goNext], ['Skip (S)', skip]]) {
        const button = document.createElement('button');
        button.textContent = label;
        button.style.cssText = 'padding:5px 10px;border:1px solid rgba(255,255,255,.5);border-radius:6px;background:rgba(0,0,0,.2);color:#fff;font:600 12px system-ui;cursor:pointer';
        button.addEventListener('click', action);
        row.appendChild(button);
      }
      el.appendChild(row);
    }
  }

  async function goNext() {
    panel('finding the next station…', 'info', false);
    const {status, data} = await api('GET', '/api/collector/next?count=1');
    if (status !== 200) { panel(data.detail || ('server answered ' + status), 'warn'); return; }
    if (data.paused_until) { panel('collection is paused because Tesla blocked a page. Try again later.', 'warn', false); return; }
    if (!data.items.length) { panel('every station has a fresh price 🎉', 'ok', false); return; }
    location.href = data.items[0].tesla_url;
  }

  async function skip() {
    const id = stationId();
    if (id) await api('POST', '/api/collector/skip', {station_id: id});
    if (COLLECTOR_TAB) closeTab('skipped. Close this tab or press N for the next station.'); else goNext();
  }

  document.addEventListener('keydown', event => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.closest && event.target.closest('input, textarea, select, [contenteditable]')) return;
    if (event.key === 'n' || event.key === 'N') goNext();
    if (event.key === 's' || event.key === 'S') skip();
  });

  // Tesla's bot protection page: report it once and stop, rather than keep loading pages.
  if (/access denied/i.test(document.title) || /access denied/i.test(document.body?.innerText?.slice(0, 300) || '')) {
    api('POST', '/api/collector/blocked');
    panel("Tesla shows Access Denied, so collection is paused for a while. Please don't keep reloading.", 'warn', false);
    return;
  }

  async function remainingText() {
    const {status, data} = await api('GET', '/api/collector/queue?limit=1');
    return status === 200 ? ` · ${(data.counts.stale + data.counts.missing).toLocaleString()} left` : '';
  }

  // The map loads each station's details (including pricing) from a get-charger-details request.
  // Read that same data once and hand it to the server, which parses and saves it.
  async function check() {
    const urls = performance.getEntriesByType('resource').map(entry => entry.name)
      .filter(url => url.includes('get-charger-details') && !seen.has(url));
    for (const url of urls) {
      seen.add(url);
      let payload;
      try {
        payload = await (await fetch(url, {credentials: 'include'})).json();
      } catch (_) {
        panel("couldn't read this station's details", 'warn');
        continue;
      }
      const {status, data} = await api('POST', '/api/collector/price', {page_url: location.href, request_url: url, payload});
      if (status === 200) {
        if (COLLECTOR_TAB) {
          panel(`saved ${data.station_name}: ${data.summary}. Closing…`, 'ok', false);
          setTimeout(() => closeTab(`saved ${data.station_name}: ${data.summary}`), 600);
        } else {
          panel(`saved ${data.station_name}: ${data.summary}${await remainingText()}`, 'ok');
        }
      } else {
        panel(data.detail || ('server answered ' + status), 'warn');
      }
    }
  }
  performance.setResourceTimingBufferSize(1000);
  performance.addEventListener('resourcetimingbufferfull', () => performance.clearResourceTimings());
  // React as soon as the page loads a station's details (timers are slowed down in background tabs).
  new PerformanceObserver(() => check()).observe({type: 'resource', buffered: true});
  setInterval(check, 3000);  // safety net
  check();
})();
