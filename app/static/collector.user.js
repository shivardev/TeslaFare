// ==UserScript==
// @name         TeslaFare price helper
// @namespace    teslafare
// @version      3.0
// @description  Reads the Supercharger price Tesla's Find Us page shows you and hands it to your TeslaFare planner.
// @match        https://www.tesla.com/findus*
// @include      __SERVER__/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_addValueChangeListener
// @grant        GM_registerMenuCommand
// @connect      __HOST__
// ==/UserScript==
(function () {
  'use strict';
  const VERSION = '3.0';
  const SERVER = '__SERVER__';
  const BUILT_IN_KEY = '__KEY__';
  // Captured prices are kept this long for the visitor's own trips.
  const CAPTURE_TTL_MS = 24 * 3600 * 1000;

  // Host mode: with a collector key, prices are also saved on the server for everyone.
  // Visitor mode (no key): prices only go to this browser's TeslaFare tab, for this person's trips.
  function collectorKey() {
    if (BUILT_IN_KEY && !BUILT_IN_KEY.startsWith('__')) return BUILT_IN_KEY;
    return GM_getValue('collectorKey', '');
  }
  GM_registerMenuCommand('Set TeslaFare collector key (site owners only)', () => {
    const entered = prompt('TeslaFare collector key (leave empty if you are not the site owner):', GM_getValue('collectorKey', ''));
    if (entered !== null) GM_setValue('collectorKey', entered.trim());
  });

  function captures() {
    const now = Date.now();
    const all = GM_getValue('captures', {}) || {};
    return Object.fromEntries(Object.entries(all).filter(([, c]) => now - c.at < CAPTURE_TTL_MS));
  }

  // ---------- On the TeslaFare planner page: pass captured prices to the page ----------
  if (location.href.startsWith(SERVER)) {
    document.documentElement.setAttribute('data-teslafare-helper', VERSION);
    const deliver = () => window.postMessage({type: 'teslafare-captures', captures: captures(), helper: VERSION}, location.origin);
    deliver();
    GM_addValueChangeListener('captures', (name, oldValue, newValue, remote) => { if (remote) deliver(); });
    window.addEventListener('message', event => {
      if (event.source === window && event.data && event.data.type === 'teslafare-helper-ping') deliver();
    });
    return;
  }

  // ---------- On Tesla's Find Us page: capture the price ----------
  // Tabs opened by TeslaFare (planner or /collect) close themselves once their price is captured.
  const OPENED_BY_TESLAFARE = window.name.startsWith('teslafare-');
  const seen = new Set();

  function api(method, path, body) {
    return new Promise(resolve => GM_xmlhttpRequest({
      method,
      url: SERVER + path,
      headers: {'Content-Type': 'application/json', 'X-Collector-Key': collectorKey()},
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
  // Browsers only let a script close a tab it opened (and one that hasn't navigated around).
  function closeTab(message) {
    (typeof unsafeWindow !== 'undefined' ? unsafeWindow : window).close();
    setTimeout(() => panel(message || 'done. You can close this tab.', 'ok', Boolean(collectorKey())), 500);
  }

  function panel(message, tone, withActions = false) {
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

  // Next/Skip walk the owner's collection queue, so they only exist in host mode.
  async function goNext() {
    if (!collectorKey()) return;
    panel('finding the next station…', 'info');
    const {status, data} = await api('GET', '/api/collector/next?count=1');
    if (status !== 200) { panel(data.detail || ('server answered ' + status), 'warn', true); return; }
    if (data.paused_until) { panel('collection is paused because Tesla blocked a page. Try again later.', 'warn'); return; }
    if (!data.items.length) { panel('every station has a fresh price 🎉', 'ok'); return; }
    location.href = data.items[0].tesla_url;
  }
  async function skip() {
    if (!collectorKey()) return;
    const id = stationId();
    if (id) await api('POST', '/api/collector/skip', {station_id: id});
    if (OPENED_BY_TESLAFARE) closeTab('skipped.'); else goNext();
  }
  document.addEventListener('keydown', event => {
    if (!collectorKey() || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.closest && event.target.closest('input, textarea, select, [contenteditable]')) return;
    if (event.key === 'n' || event.key === 'N') goNext();
    if (event.key === 's' || event.key === 'S') skip();
  });

  // Tesla's bot protection page: report it once (host mode) and stop, rather than keep loading pages.
  if (/access denied/i.test(document.title) || /access denied/i.test(document.body?.innerText?.slice(0, 300) || '')) {
    if (collectorKey()) api('POST', '/api/collector/blocked');
    panel("Tesla shows Access Denied right now. Please wait a while before opening more stations.", 'warn');
    return;
  }

  // The map loads each station's details (including pricing) from a get-charger-details request.
  // Read that same data once: keep it for this browser's planner tab, and in host mode save it on the server.
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
      const id = stationId();
      if (id) {
        const all = captures();
        all[id] = {payload, at: Date.now()};
        GM_setValue('captures', all);
      }
      let message = 'price captured for your TeslaFare trip';
      if (collectorKey()) {
        const {status, data} = await api('POST', '/api/collector/price', {page_url: location.href, request_url: url, payload});
        message = status === 200 ? `saved ${data.station_name}: ${data.summary}` : (data.detail || ('server answered ' + status));
        if (status !== 200) { panel(message, 'warn', true); continue; }
      }
      if (OPENED_BY_TESLAFARE) {
        panel(message + '. Closing…', 'ok');
        setTimeout(() => closeTab(message), 600);
      } else {
        panel(message, 'ok', Boolean(collectorKey()));
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
