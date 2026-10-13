// Day data viewer: time-series charts + record inspector.
// Chart look follows the sensor's Trends page (html_templates.h drawChart):
// #0f3460 plot area, #2d3561 grid, #8899aa axis text, same per-metric colours.
'use strict';

const DataView = (() => {
  const C = {
    plot: '#0f3460', grid: '#2d3561', axis: '#8899aa', text: '#e0e0e0', muted: '#b2bec3',
    sel: '#4a90d9', cross: '#dfe6e9', wet: '#e17055', dry: '#00b894', surface: '#16213e',
  };
  // One metric per chart (no dual axes).  Colours match the sensor's UI.
  const CHARTS = [
    { id: 'c-temp', unit: '°C', digits: 1, legend: 'leg-temp', series: [
      { key: 'ambient_temp', label: 'Ambient', color: '#ffeaa7' },
      { key: 'sky_temp', label: 'Sky', color: '#a29bfe' }] },
    { id: 'c-cloud', unit: '%', digits: 0, min: 0, max: 100,
      series: [{ key: 'cloud_cover', label: 'Cloud cover', color: '#dfe6e9' }] },
    { id: 'c-lux', unit: 'lx', log: true,
      series: [{ key: 'brightness', label: 'Brightness', color: '#fdcb6e' }] },
    { id: 'c-sqm', unit: 'mag/arcsec²', digits: 2,
      series: [{ key: 'sqm', label: 'SQM', color: '#00cec9' }] },
    { id: 'c-pres', unit: 'hPa', digits: 1,
      series: [{ key: 'pressure', label: 'Pressure', color: '#81ecec' }] },
    { id: 'c-hum', unit: '%', digits: 1,
      series: [{ key: 'humidity', label: 'Humidity', color: '#74b9ff' }] },
  ];
  const ROWS = [
    ['cloud_cover', 'Cloud Cover', v => v.toFixed(0) + ' %'],
    ['ambient_temp', 'Ambient Temp', v => v.toFixed(1) + ' °C'],
    ['sky_temp', 'Sky Temp', v => v.toFixed(1) + ' °C'],
    ['brightness', 'Brightness', fmtLux],
    ['sqm', 'SQM', v => v.toFixed(2)],
    ['pressure', 'Pressure', v => v.toFixed(1) + ' hPa'],
    ['humidity', 'Humidity', v => v.toFixed(1) + ' %'],
    ['raining', 'Rain', v => v ? 'Raining' : 'Dry'],
  ];
  const P = { t: 10, r: 14, b: 22, l: 56 };
  // Observatory states (roof, pier, NINA) in the status strip; unlisted
  // states (offline, no data) are left empty.
  const STATE = {
    open: '#55efc4', closed: '#636e72', opening: '#fdcb6e', closing: '#fdcb6e',
    error: '#e17055', unknown: '#e17055',
    unparked: '#fdcb6e', parked: '#636e72',
    capturing: '#00b894', tracking: '#74b9ff', slewing: '#fd79a8', focusing: '#ffeaa7',
    calibrating: '#ffeaa7', waiting: '#a29bfe', idle: '#636e72', disconnected: '#636e72',
  };
  const LANE_H = 14;
  let OBS = [];        // [{key, label, kind, detail?}] from the page
  let OBS_ROWS = [];   // the same as [key, label, format] rows, kept apart from ROWS

  let D = null, cfg = null, hover = -1, sel = -1, gapMs = Infinity, fmtTime = null;
  const tips = {};

  function fmtLux(v) { return v >= 100 ? v.toFixed(0) + ' lx' : Number(v.toPrecision(3)) + ' lx'; }
  function fmtVal(c, v) {
    if (v === null || v === undefined) return '--';
    if (c.log) return fmtLux(v);
    return v.toFixed(c.digits ?? 1) + (c.unit === '%' ? ' %' : c.unit === '°C' ? ' °C' : '');
  }

  // ── geometry ────────────────────────────────────────────────────────────
  function setup(el) {
    let cv = el.querySelector('canvas');
    if (!cv) {
      cv = document.createElement('canvas');
      el.appendChild(cv);
      const tip = document.createElement('div');
      tip.className = 'tip'; tip.hidden = true;
      el.appendChild(tip);
      tips[el.id] = tip;
      cv.addEventListener('mousemove', e => setHover(idxAt(el, e.offsetX)));
      cv.addEventListener('mouseleave', () => setHover(-1));
      cv.addEventListener('click', e => select(idxAt(el, e.offsetX)));
      cv.addEventListener('touchstart', e => {
        const r = cv.getBoundingClientRect();
        select(idxAt(el, e.touches[0].clientX - r.left));
      }, { passive: true });
    }
    const dpr = window.devicePixelRatio || 1;
    const W = el.clientWidth, H = +el.dataset.h;
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, W, H, cw: W - P.l - P.r, ch: H - P.t - P.b };
  }
  const t0 = () => D.t[0], tSpan = () => (D.t[D.t.length - 1] - D.t[0]) || 1;
  const xOf = (g, i) => P.l + (D.t[i] - t0()) / tSpan() * g.cw;

  function idxAt(el, px) {
    const cw = el.clientWidth - P.l - P.r;
    const tt = t0() + (px - P.l) / cw * tSpan();
    let lo = 0, hi = D.t.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (D.t[m] < tt) lo = m; else hi = m; }
    return (tt - D.t[lo] < D.t[hi] - tt) ? lo : hi;
  }

  function timeAxis(g) {
    const { ctx, cw, ch } = g;
    const hours = tSpan() / 3.6e6;
    const step = Math.max(1, Math.ceil(hours / Math.max(2, Math.floor(cw / 70)))) * 3.6e6;
    const first = Math.ceil(t0() / 3.6e6) * 3.6e6;
    ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    for (let tt = first; tt <= D.t[D.t.length - 1]; tt += step) {
      const x = P.l + (tt - t0()) / tSpan() * cw;
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, P.t); ctx.lineTo(x, P.t + ch); ctx.stroke();
      ctx.fillStyle = C.axis; ctx.fillText(fmtTime.format(tt), x, P.t + ch + 14);
    }
  }

  function markers(g) {
    const { ctx, ch } = g;
    if (sel >= 0) {
      ctx.strokeStyle = C.sel; ctx.lineWidth = 2; ctx.setLineDash([4, 3]);
      const x = xOf(g, sel);
      ctx.beginPath(); ctx.moveTo(x, P.t); ctx.lineTo(x, P.t + ch); ctx.stroke();
      ctx.setLineDash([]);
    }
    if (hover >= 0) {
      ctx.strokeStyle = C.cross; ctx.lineWidth = 1;
      const x = xOf(g, hover);
      ctx.beginPath(); ctx.moveTo(x, P.t); ctx.lineTo(x, P.t + ch); ctx.stroke();
    }
  }

  // ── line chart ──────────────────────────────────────────────────────────
  function drawLine(c) {
    const el = document.getElementById(c.id);
    const g = setup(el);
    const { ctx, W, H, cw, ch } = g;
    ctx.fillStyle = C.plot; ctx.fillRect(0, 0, W, H);

    const tf = c.log ? (v => (v > 0 ? Math.log10(v) : null)) : (v => v);
    let mn = Infinity, mx = -Infinity;
    for (const s of c.series) for (const v of D[s.key]) {
      const y = v === null ? null : tf(v);
      if (y !== null && isFinite(y)) { mn = Math.min(mn, y); mx = Math.max(mx, y); }
    }
    if (!isFinite(mn)) {
      ctx.fillStyle = C.axis; ctx.textAlign = 'center'; ctx.font = '13px sans-serif';
      ctx.fillText('No data for this metric', W / 2, H / 2);
      return;
    }
    if (c.min !== undefined) { mn = c.min; mx = c.max; }
    else if (c.log) { mn = Math.floor(mn); mx = Math.max(Math.ceil(mx), mn + 1); }
    else { const sp = (mx - mn) || 1; mn -= sp * 0.08; mx += sp * 0.08; }
    const yOf = y => P.t + (1 - (y - mn) / (mx - mn)) * ch;

    // Y grid + labels
    ctx.font = '10px monospace'; ctx.textAlign = 'right';
    const ticks = c.log ? Array.from({ length: mx - mn + 1 }, (_, k) => mn + k)
                        : Array.from({ length: 5 }, (_, k) => mn + (mx - mn) * k / 4);
    for (const y of ticks) {
      const yp = yOf(y);
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(P.l, yp); ctx.lineTo(P.l + cw, yp); ctx.stroke();
      ctx.fillStyle = C.axis;
      const lbl = c.log ? (y >= 0 ? String(10 ** y) : '1e' + y)
                        : y.toFixed((mx - mn) > 20 ? 0 : (c.digits ?? 1) > 1 ? 2 : 1);
      ctx.fillText(lbl, P.l - 4, yp + 3);
    }
    timeAxis(g);

    // Lines (2px), broken across capture gaps
    for (const s of c.series) {
      ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.lineJoin = 'round';
      ctx.beginPath();
      let on = false;
      D[s.key].forEach((v, i) => {
        const y = v === null ? null : tf(v);
        if (y === null || !isFinite(y) || (i > 0 && D.t[i] - D.t[i - 1] > gapMs)) { on = false; if (y === null) return; }
        const x = xOf(g, i), yp = yOf(Math.min(mx, Math.max(mn, y)));
        if (!on) { ctx.moveTo(x, yp); on = true; } else ctx.lineTo(x, yp);
      });
      ctx.stroke();
    }
    markers(g);

    // Hover dots with a surface ring
    if (hover >= 0) for (const s of c.series) {
      const v = D[s.key][hover]; const y = v === null ? null : tf(v);
      if (y === null || !isFinite(y)) continue;
      ctx.beginPath(); ctx.arc(xOf(g, hover), yOf(y), 4, 0, 2 * Math.PI);
      ctx.fillStyle = s.color; ctx.fill();
      ctx.lineWidth = 2; ctx.strokeStyle = C.plot; ctx.stroke();
    }
    tooltip(el, g, hover, c.series.map(s => [s.color, s.label, fmtVal(c, D[s.key][hover])]));
  }

  // ── rain strip ──────────────────────────────────────────────────────────
  function drawRain() {
    const el = document.getElementById('c-rain');
    const g = setup(el);
    const { ctx, W, H, cw, ch } = g;
    ctx.fillStyle = C.plot; ctx.fillRect(0, 0, W, H);
    const n = D.t.length;
    for (let i = 0; i < n; i++) {
      const v = D.raining[i];
      if (v === null) continue;
      const x1 = xOf(g, i), x2 = i + 1 < n && D.t[i + 1] - D.t[i] <= gapMs ? xOf(g, i + 1) : x1 + 1;
      ctx.fillStyle = v ? C.wet : C.dry;
      ctx.fillRect(x1, P.t, Math.max(1, x2 - x1), ch);
    }
    ctx.fillStyle = C.axis; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText('Rain', P.l - 4, P.t + ch / 2 + 3);
    timeAxis(g);
    markers(g);
    const v = hover >= 0 ? D.raining[hover] : null;
    tooltip(el, g, hover, [[v ? C.wet : C.dry, 'Rain', v === null ? '--' : v ? 'Raining' : 'Dry']]);
  }

  // ── observatory status strip: one lane per roof / pier / rig ─────────────
  function drawObs() {
    const el = document.getElementById('c-obs');
    if (!el || !OBS.length) return;
    el.dataset.h = P.t + OBS.length * LANE_H + P.b;
    const g = setup(el);
    const { ctx, W, H } = g;
    ctx.fillStyle = C.plot; ctx.fillRect(0, 0, W, H);
    const n = D.t.length;
    OBS.forEach((row, lane) => {
      const y = P.t + lane * LANE_H;
      const vals = D[row.key] || [];
      for (let i = 0; i < n; i++) {
        const col = STATE[vals[i]];
        if (!col) continue;
        const x1 = xOf(g, i), x2 = i + 1 < n && D.t[i + 1] - D.t[i] <= gapMs ? xOf(g, i + 1) : x1 + 1;
        ctx.fillStyle = col;
        ctx.fillRect(x1, y + 2, Math.max(1, x2 - x1), LANE_H - 4);
      }
      ctx.fillStyle = C.axis; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
      ctx.fillText(row.label, P.l - 4, y + LANE_H / 2 + 3);
    });
    timeAxis(g);
    markers(g);
    tooltip(el, g, hover, OBS.map(row => {
      const v = hover >= 0 ? (D[row.key] || [])[hover] : null;
      const d = row.detail && hover >= 0 ? (D[row.detail] || [])[hover] : null;
      return [STATE[v] || C.grid, row.label, (v || '--') + (d ? ` (${d})` : '')];
    }));
  }

  function obsLegend() {
    if (!OBS.length) return;
    const seen = new Set();
    for (const row of OBS) for (const v of D[row.key] || []) if (STATE[v]) seen.add(v);
    legend('leg-obs', Object.keys(STATE).filter(k => seen.has(k)).map(k => [STATE[k], k, true]));
  }

  function tooltip(el, g, i, rows) {
    const tip = tips[el.id];
    if (i < 0) { tip.hidden = true; return; }
    tip.replaceChildren();
    const h = document.createElement('div'); h.className = 'tip-time'; h.textContent = fmtTime.format(D.t[i]);
    tip.appendChild(h);
    for (const [color, label, val] of rows) {
      const r = document.createElement('div');
      const sw = document.createElement('span'); sw.className = 'sw'; sw.style.background = color;
      const l = document.createElement('span'); l.textContent = label + ' ';
      const b = document.createElement('b'); b.textContent = val;
      r.append(sw, l, b); tip.appendChild(r);
    }
    tip.hidden = false;
    const x = xOf(g, i);
    const left = x + 12 + tip.offsetWidth > g.W ? x - 12 - tip.offsetWidth : x + 12;
    tip.style.left = left + 'px'; tip.style.top = P.t + 'px';
  }

  function legend(id, items) {
    const el = document.getElementById(id);
    el.replaceChildren(...items.map(([color, label, box]) => {
      const s = document.createElement('span');
      const sw = document.createElement('i'); sw.className = box ? 'box' : 'line'; sw.style.background = color;
      s.append(sw, document.createTextNode(label));
      return s;
    }));
  }

  function drawAll() {
    if (!D || !D.t.length) return;
    CHARTS.forEach(drawLine);
    drawRain();
    drawObs();
  }

  function setHover(i) { if (i !== hover) { hover = i; drawAll(); } }

  // ── record inspector ────────────────────────────────────────────────────
  async function select(i) {
    if (!D || i < 0 || i >= D.t.length) return;
    sel = i; drawAll();
    document.getElementById('rec-time').textContent = fmtTime.format(D.t[i]);
    document.getElementById('rec-pos').textContent = `${i + 1} / ${D.t.length}`;
    document.getElementById('rec-json').href = `${cfg.recordBase}/${i}.json`;
    document.getElementById('rec-json').download = `cloudroll-${cfg.day}-${i}.json`;
    const csvLink = document.getElementById('rec-csv');
    csvLink.hidden = !D.grid[i];
    csvLink.href = `${cfg.recordBase}/${i}.csv`;
    const img = document.getElementById('rec-img'), noimg = document.getElementById('rec-noimg');
    img.hidden = !D.grid[i]; noimg.hidden = D.grid[i];
    if (D.grid[i]) img.src = `${cfg.recordBase}/${i}.png`;
    const fill = (id, rows) => {
      const tbl = document.getElementById(id);
      if (!tbl) return;
      tbl.replaceChildren(...rows.map(([k, label, f]) => {
        const tr = document.createElement('tr');
        const th = document.createElement('th'); th.textContent = label;
        const td = document.createElement('td'); const v = (D[k] || [])[i];
        td.textContent = v === null || v === undefined ? '--' : f(v, i);
        tr.append(th, td); return tr;
      }));
    };
    fill('rec-table', ROWS);
    fill('rec-obs-table', OBS_ROWS);
    try { history.replaceState(null, '', '#' + i); } catch (e) { /* ignore */ }
    if (cfg.onSelect) cfg.onSelect(i, D);
  }

  function buildTable(force) {
    const tbl = document.getElementById('all-table');
    if (tbl.rows.length && !force) return;
    tbl.replaceChildren();
    if (OBS_ROWS.length) {
      // Group headings so the observatory columns read apart from the conditions.
      const grp = document.createElement('tr'); grp.className = 'grp-head';
      for (const [text, span, cls] of [['Conditions', ROWS.length + 1, ''], ['Observatory', OBS_ROWS.length, 'grp']]) {
        const th = document.createElement('th'); th.textContent = text; th.colSpan = span;
        if (cls) th.className = cls;
        grp.appendChild(th);
      }
      tbl.appendChild(grp);
    }
    const head = document.createElement('tr');
    ['Time', ...ROWS.map(r => r[1]), ...OBS_ROWS.map(r => r[1])].forEach((h, c) => {
      const th = document.createElement('th'); th.textContent = h;
      if (OBS_ROWS.length && c === ROWS.length + 1) th.className = 'grp';
      head.appendChild(th);
    });
    tbl.appendChild(head);
    const frag = document.createDocumentFragment();
    D.t.forEach((tt, i) => {
      const tr = document.createElement('tr');
      tr.className = 'clickable';
      tr.onclick = () => { select(i); document.getElementById('record').scrollIntoView({ behavior: 'smooth' }); };
      const td0 = document.createElement('td'); td0.textContent = fmtTime.format(tt); tr.appendChild(td0);
      for (const [k, , f] of ROWS) {
        const td = document.createElement('td'); const v = D[k][i];
        td.textContent = v === null || v === undefined ? '--' : f(v, i); td.className = 'num';
        tr.appendChild(td);
      }
      OBS_ROWS.forEach(([k, , f], c) => {
        const td = document.createElement('td'); const v = (D[k] || [])[i];
        td.textContent = v === null || v === undefined ? '--' : f(v, i);
        td.className = c ? 'num' : 'num grp';
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    });
    tbl.appendChild(frag);
  }

  async function init(options) {
    cfg = options;
    OBS = cfg.obs || [];
    OBS_ROWS = OBS.map(row => [row.key, row.label, row.detail
      ? (v, i) => { const d = (D[row.detail] || [])[i]; return d ? `${v} (${d})` : v; }
      : v => v]);
    fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: cfg.tz });
    legend('leg-temp', CHARTS[0].series.map(s => [s.color, s.label]));
    legend('leg-rain', [[C.dry, 'Dry', true], [C.wet, 'Raining', true]]);
    D = await (await fetch(cfg.seriesUrl)).json();
    computeGap();
    drawAll();
    obsLegend();
    updateTiles();
    const h = parseInt(location.hash.slice(1), 10);
    select(Number.isInteger(h) && h >= 0 && h < D.t.length ? h : D.t.length - 1);
    window.addEventListener('resize', drawAll);
    document.getElementById('rec-prev').onclick = () => select(sel - 1);
    document.getElementById('rec-next').onclick = () => select(sel + 1);
    document.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT') return;
      if (e.key === 'ArrowLeft') { select(sel - 1); e.preventDefault(); }
      if (e.key === 'ArrowRight') { select(sel + 1); e.preventDefault(); }
    });
    document.getElementById('tbl-details').addEventListener('toggle', e => { if (e.target.open) buildTable(); });
    if (cfg.live) startPolling();
  }

  function computeGap() {
    const diffs = D.t.slice(1).map((v, i) => v - D.t[i]).sort((a, b) => a - b);
    gapMs = diffs.length ? diffs[diffs.length >> 1] * 3 : Infinity;
  }

  // ── summary tiles (recomputed client-side so they track live updates) ───
  function updateTiles() {
    const set = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
    const vals = k => D[k].filter(v => v !== null && v !== undefined);
    const n = D.t.length;
    set('st-records', n);
    if (n) set('st-span', fmtTime.format(D.t[0]) + '–' + fmtTime.format(D.t[n - 1]));
    const cloud = vals('cloud_cover'), sky = vals('sky_temp'), sqm = vals('sqm');
    set('st-cloud', cloud.length ? (cloud.reduce((a, b) => a + b, 0) / cloud.length).toFixed(0) + ' %' : '--');
    set('st-sky', sky.length ? Math.min(...sky).toFixed(1) + ' °C' : '--');
    set('st-sqm', sqm.length ? Math.max(...sqm).toFixed(2) : '--');
    set('st-rain', D.raining.filter(v => v).length + ' min');
  }

  // ── live updates for the day still being recorded ───────────────────────
  let pollTimer = null, lastOk = 0;
  function liveStatus(text, stale) {
    const el = document.getElementById('live-status');
    el.hidden = false;
    el.classList.toggle('stale', !!stale);
    const dot = document.createElement('span'); dot.className = 'dot';
    el.replaceChildren(dot, document.createTextNode(text));
  }

  async function poll() {
    if (document.hidden) return;
    try {
      const sep = cfg.seriesUrl.includes('?') ? '&' : '?';
      const r = await fetch(cfg.seriesUrl + sep + 'since=' + D.t.length, { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const u = await r.json();
      if (cfg.follow && u.latest_day !== cfg.day) { location.reload(); return; }
      if (u.since !== D.t.length || u.count < D.t.length) {
        // File was rewritten underneath us – reload the whole series.
        D = await (await fetch(cfg.seriesUrl, { cache: 'no-store' })).json();
        computeGap();
      } else if (u.t.length) {
        const following = sel === D.t.length - 1;
        for (const k of Object.keys(D)) if (Array.isArray(D[k]) && Array.isArray(u[k])) D[k].push(...u[k]);
        computeGap();
        if (following) { select(D.t.length - 1); } else { drawAll(); select(sel); }
        if (document.getElementById('tbl-details').open) buildTable(true);
      }
      updateTiles();
      obsLegend();
      lastOk = Date.now();
      if (!u.open) {
        clearInterval(pollTimer); pollTimer = null;
        const b = document.getElementById('open-badge'); if (b) b.remove();
        liveStatus('Day complete', false);
        if (cfg.follow) setTimeout(() => location.reload(), 5000);
        return;
      }
      liveStatus('Live · ' + D.t.length + ' records · last ' + fmtTime.format(D.t[D.t.length - 1]), false);
    } catch (e) {
      liveStatus('Live updates paused – retrying', true);
    }
  }

  function startPolling() {
    liveStatus('Live · ' + D.t.length + ' records · last ' + fmtTime.format(D.t[D.t.length - 1]), false);
    pollTimer = setInterval(poll, cfg.pollSec * 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden && pollTimer) poll(); });
  }

  return { init };
})();
