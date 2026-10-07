// ==UserScript==
// @name         TeslaFare price collector
// @namespace    teslafare
// @version      1.0
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
  const seen = new Set();

  function collectorKey(ask) {
    let key = GM_getValue('collectorKey', '');
    if (!key || ask) {
      const entered = prompt('TeslaFare collector key (COLLECTOR_KEY on your server):', key);
      if (entered !== null) {
        key = entered.trim();
        GM_setValue('collectorKey', key);
      }
    }
    return key;
  }
  GM_registerMenuCommand('Set TeslaFare collector key', () => collectorKey(true));

  function toast(message, ok) {
    let el = document.getElementById('teslafare-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'teslafare-toast';
      el.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;padding:10px 14px;'
        + 'border-radius:8px;font:600 13px system-ui,sans-serif;color:#fff;box-shadow:0 4px 16px rgba(0,0,0,.4)';
      document.documentElement.appendChild(el);
    }
    el.style.background = ok ? '#15803d' : '#b45309';
    el.textContent = 'TeslaFare: ' + message;
    clearTimeout(el.hideTimer);
    el.hideTimer = setTimeout(() => el.remove(), 6000);
  }

  function send(requestUrl, payload) {
    const key = collectorKey(false);
    if (!key) { toast('no collector key set (Tampermonkey menu → Set TeslaFare collector key)', false); return; }
    GM_xmlhttpRequest({
      method: 'POST',
      url: SERVER + '/api/collector/price',
      headers: {'Content-Type': 'application/json', 'X-Collector-Key': key},
      data: JSON.stringify({page_url: location.href, request_url: requestUrl, payload}),
      onload: response => {
        let body = {};
        try { body = JSON.parse(response.responseText); } catch (_) { /* non-JSON error */ }
        if (response.status === 200) toast(`saved ${body.station_name}: ${body.summary}`, true);
        else toast(body.detail || ('server answered ' + response.status), false);
      },
      onerror: () => toast('could not reach ' + SERVER, false),
    });
  }

  // The map page loads each station's details (including pricing) from a get-charger-details request.
  // Read that same data once, then hand it to the server, which parses and saves it.
  async function check() {
    const urls = performance.getEntriesByType('resource').map(entry => entry.name)
      .filter(url => url.includes('get-charger-details') && !seen.has(url));
    for (const url of urls) {
      seen.add(url);
      try {
        const response = await fetch(url, {credentials: 'include'});
        send(url, await response.json());
      } catch (_) {
        toast("couldn't read this station's details", false);
      }
    }
  }
  performance.setResourceTimingBufferSize(1000);
  performance.addEventListener('resourcetimingbufferfull', () => performance.clearResourceTimings());
  setInterval(check, 1500);
})();
