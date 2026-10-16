// Calibration page: star counts vs the IR sensor's cloud cover over many nights.
// Data: /api/calibration/summary/<night>.json – per dark minute, the region Δ
// (ambient − pixel) quantiles, which replay the sensor's per-pixel cloud cover
// for any thresholds.  Chart look follows charts.js / the sensor's Trends page.
'use strict';

(() => {
  const C = { plot: '#0f3460', grid: '#2d3561', axis: '#8899aa', text: '#e0e0e0',
              line: '#dfe6e9', whatif: '#ffeaa7', fit: '#fd79a8' };
  // Star-count bands: one-hue ordinal ramp, cloud (light) → clear (deep blue),
  // like the sensor's ClearDarkSky palette.  All ≥ 2.8:1 on the plot colour.
  const BANDS = [
    { label: 'Overcast', color: '#cde2fb' },
    { label: 'Mostly cloudy', color: '#86b6ef' },
    { label: 'Partly cloudy', color: '#5598e7' },
    { label: 'Clear', color: '#2a78d6' },
  ];
  const P = { t: 12, r: 16, b: 34, l: 48 };
  const $ = id => document.getElementById(id);
  const fmtDay = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: CAL.tz });
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: CAL.tz });

  let all = {};        // night -> summary
  let M = null;        // flattened, filtered minutes
  let labels = {};
  let xKey = 'ambient';
  let hover = -1;

  // ── data ───────────────────────────────────────────────────────────────
  function bandOf(s) {
    const clear = +$('f-clear').value, over = +$('f-over').value;
    if (s === null || s === undefined) return -1;
    if (s < over) return 0;
    if (s >= clear) return 3;
    return s < (over + clear) / 2.5 ? 1 : 2;   // splits ~50-300 / 300-800 at the defaults
  }

  // Band names with their star ranges for the current thresholds.
  function bandLabel(k) {
    const clear = +$('f-clear').value, over = +$('f-over').value, mid = Math.round((over + clear) / 2.5);
    return [`${BANDS[0].label} (< ${over} stars)`, `${BANDS[1].label} (${over}–${mid - 1})`,
            `${BANDS[2].label} (${mid}–${clear - 1})`, `${BANDS[3].label} (≥ ${clear})`][k];
  }

  function legend() {
    $('sc-legend').replaceChildren(...BANDS.map((b, k) => {
      const s = document.createElement('span'); const i = document.createElement('i');
      i.className = 'box'; i.style.background = b.color; s.append(i, document.createTextNode(bandLabel(k))); return s;
    }), ...[[C.line, 'Sensor thresholds', 'line'], [C.whatif, 'What-if', 'line'], [C.fit, 'Clear-sky fit', 'line']].map(([c, t, cls]) => {
      const s = document.createElement('span'); const i = document.createElement('i');
      i.className = cls; i.style.background = c; s.append(i, document.createTextNode(t)); return s;
    }));
  }

  async function load() {
    const n = +$('f-range').value;
    const want = n ? CAL.nights.slice(0, n) : CAL.nights;
    const missing = want.filter(d => !all[d]);
    $('cal-status').textContent = missing.length ? `Loading ${missing.length} nights…` : '';
    for (let k = 0; k < missing.length; k += 6) {
      await Promise.all(missing.slice(k, k + 6).map(async d => {
        const r = await fetch(CAL.summaryBase + d + '.json');
        if (r.ok) all[d] = await r.json();
      }));
      $('cal-status').textContent = `Loading… ${Math.min(k + 6, missing.length)} / ${missing.length}`;
    }
    $('cal-status').textContent = '';
    flatten(want);
  }

  function flatten(nights) {
    const moonDown = $('f-moon').checked;
    const keys = ['t', 'ambient', 'dew', 'd_med', 'dq', 'cloud', 'clear_delta', 'overcast_delta', 'amb_slope', 'amb_ref', 'stars', 'moon_alt', 'moon_illum'];
    M = Object.fromEntries(keys.map(k => [k, []]));
    M.night = []; M.band = [];
    for (const d of [...nights].reverse()) {   // oldest first: the last minute is the newest
      const s = all[d];
      if (!s) continue;
      for (let i = 0; i < s.t.length; i++) {
        if (s.dq[i] === null || s.d_med[i] === null) continue;
        if (moonDown && s.moon_alt[i] > 0 && s.moon_illum[i] > 10) continue;
        for (const k of keys) M[k].push(s[k] ? s[k][i] : (k === 'amb_slope' || k === 'amb_ref' ? 0 : null));
        M.night.push(d);
        M.band.push(bandOf(s.stars[i]));
      }
    }
    if ($('w-clear').value === '') useCurrent();
    render();
  }

  // Per-pixel cloud % from the Δ quantiles (trapezoid mean of the clipped ramp).
  function cloudOf(dq, clear, over) {
    const span = clear - over;
    if (!(span > 0.01)) return 50;
    let sum = 0;
    const n = dq.length;
    for (let k = 0; k < n; k++) {
      let t = (dq[k] - over) / span;
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      sum += (k === 0 || k === n - 1 ? 0.5 : 1) * (1 - t);
    }
    return sum / (n - 1) * 100;
  }

  // What-if cloud % for one minute: thresholds shifted by slope × (ambient − ref).
  function wi() {
    return { c: +$('w-clear').value, o: +$('w-over').value, k: +$('w-slope').value || 0, ref: +$('w-ref').value || 0 };
  }
  function cloudAt(dq, amb, p) {
    const off = p.k && amb !== null && amb !== undefined ? p.k * (amb - p.ref) : 0;
    return cloudOf(dq, p.c + off, p.o + off);
  }
  function wiName(p) {
    return `${fmt(p.c, 1)} / ${fmt(p.o, 1)}` + (p.k ? ` + ${p.k} × (T − ${p.ref})` : '');
  }

  const median = a => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y); return b[b.length >> 1]; };
  const pct = (a, p) => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y); return b[Math.min(b.length - 1, Math.floor(p / 100 * b.length))]; };
  const fmt = (v, d = 0, u = '') => v === null || v === undefined || !isFinite(v) ? '--' : v.toFixed(d) + u;

  // ── tiles, what-if, months ─────────────────────────────────────────────
  function render() {
    const n = M.t.length, nights = new Set(M.night).size;
    const clearIdx = [], overIdx = [];
    M.band.forEach((b, i) => { if (b === 3) clearIdx.push(i); else if (b === 0) overIdx.push(i); });
    const cloudyAt = +$('w-cloudy').value;
    $('t-nights').textContent = nights;
    $('t-minutes').textContent = n;
    $('t-clear').textContent = clearIdx.length;
    $('t-over').textContent = overIdx.length;
    if (n) $('t-thr').textContent = `${M.clear_delta[n - 1] ?? '--'} / ${M.overcast_delta[n - 1] ?? '--'}` +
                                    (M.amb_slope[n - 1] ? ` + ${M.amb_slope[n - 1]}×T` : '');
    const falseRep = clearIdx.filter(i => M.cloud[i] >= cloudyAt).length;
    $('t-false').textContent = clearIdx.length ? (100 * falseRep / clearIdx.length).toFixed(1) + ' %' : '--';
    whatIf(clearIdx, overIdx, cloudyAt);
    months(cloudyAt);
    legend();
    draw();
    review();
  }

  function whatIf(clearIdx, overIdx, cloudyAt) {
    const p = wi(), c = p.c, o = p.o;
    const ok = c - o > 0.01;
    $('w-msg').textContent = ok ? '' : 'Clear Δ must be larger than Overcast Δ.';
    const W = ok ? M.dq.map((q, i) => cloudAt(q, M.ambient[i], p)) : M.dq.map(() => null);
    const tbl = $('w-table');
    const row = (cells, head) => {
      const tr = document.createElement('tr');
      cells.forEach((v, k) => { const e = document.createElement(head || k === 0 ? 'th' : 'td'); e.textContent = v; if (k) e.className = 'num'; tr.appendChild(e); });
      return tr;
    };
    const rows = [row(['', 'Minutes', 'Reported cloud (median)', `What-if ${wiName(p)} (median)`], true)];
    BANDS.forEach((b, k) => {
      const idx = M.band.map((v, i) => v === k ? i : -1).filter(i => i >= 0);
      const tr = row([bandLabel(k), idx.length, fmt(median(idx.map(i => M.cloud[i]).filter(v => v !== null)), 0, ' %'),
                      fmt(median(idx.map(i => W[i]).filter(v => v !== null)), 0, ' %')]);
      const sw = document.createElement('i'); sw.className = 'sw'; sw.style.background = b.color;
      tr.firstChild.prepend(sw);
      rows.push(tr);
    });
    const rate = (idx, arr, pred) => idx.length ? 100 * idx.filter(i => arr[i] !== null && pred(arr[i])).length / idx.length : null;
    rows.push(row([`Clear minutes read ≥ ${cloudyAt} %`, clearIdx.length,
                   fmt(rate(clearIdx, M.cloud, v => v >= cloudyAt), 1, ' %'), fmt(rate(clearIdx, W, v => v >= cloudyAt), 1, ' %')]));
    rows.push(row([`Overcast minutes read < ${cloudyAt} %`, overIdx.length,
                   fmt(rate(overIdx, M.cloud, v => v < cloudyAt), 1, ' %'), fmt(rate(overIdx, W, v => v < cloudyAt), 1, ' %')]));
    tbl.replaceChildren(...rows);
  }

  // The sensor's settings right now (read from its /setup page), else the
  // newest recorded ones.
  function useCurrent() {
    const n = M.t.length;
    $('w-clear').value = CAL.current?.clear ?? (n ? M.clear_delta[n - 1] : null) ?? 26;
    $('w-over').value = CAL.current?.overcast ?? (n ? M.overcast_delta[n - 1] : null) ?? 16;
    $('w-slope').value = CAL.current?.slope ?? (n ? M.amb_slope[n - 1] : 0) ?? 0;
    $('w-ref').value = CAL.current?.ref ?? (n ? M.amb_ref[n - 1] : 0) ?? 0;
  }

  function useRecalc() {
    const r = CAL.recalc; if (!r) return;
    $('w-clear').value = r.clear_delta; $('w-over').value = r.overcast_delta;
    $('w-slope').value = r.amb_slope; $('w-ref').value = r.amb_ref;
  }

  // Grid search: thresholds whose replay best matches the star count's clear
  // fraction (mean absolute error), on up to 4000 minutes with stars.
  function bestFit() {
    const idx = M.band.map((b, i) => b >= 0 ? i : -1).filter(i => i >= 0);
    if (idx.length < 50) { $('w-msg').textContent = 'Not enough minutes with star counts.'; return; }
    const step = Math.max(1, Math.floor(idx.length / 4000));
    const use = idx.filter((_, k) => k % step === 0);
    const target = use.map(i => Math.max(0, Math.min(1, 1 - M.stars[i] / CAL.clearStars)) * 100);
    let best = null;
    const k0 = wi().k, ref = wi().ref;
    const off = use.map(i => k0 * (M.ambient[i] - ref));
    for (let c = 16; c <= 36; c += 0.5) for (let o = 0; o <= c - 4; o += 0.5) {
      let err = 0;
      for (let k = 0; k < use.length; k++) err += Math.abs(cloudOf(M.dq[use[k]], c + off[k], o + off[k]) - target[k]);
      err /= use.length;
      if (!best || err < best.err) best = { c, o, err };
    }
    $('w-clear').value = best.c; $('w-over').value = best.o;
    $('w-msg').textContent = `Best fit ${best.c} / ${best.o}${k0 ? ` with slope ${k0}` : ''} (mean error ${best.err.toFixed(1)} % vs the star estimate, ${use.length} minutes). A starting point – check it against the clear/overcast rows and the labels.`;
    render();
  }

  function months(cloudyAt) {
    const by = {};
    M.t.forEach((t, i) => {
      const m = M.night[i].slice(0, 7);
      (by[m] = by[m] || { nights: new Set(), clear: [] }).nights.add(M.night[i]);
      if (M.band[i] === 3) by[m].clear.push(i);
    });
    const tbl = $('m-table');
    const head = document.createElement('tr');
    ['Month', 'Nights', 'Clear min', 'Clear: ambient range', 'Clear: Δ p5 / median', `Clear read ≥ ${cloudyAt} %`, 'Sensor Clear / Overcast Δ'].forEach(h => {
      const th = document.createElement('th'); th.textContent = h; head.appendChild(th);
    });
    const rows = [head];
    let coldest = Infinity;
    for (const m of Object.keys(by).sort()) {
      const b = by[m], amb = b.clear.map(i => M.ambient[i]), d = b.clear.map(i => M.d_med[i]);
      const lo = amb.length ? Math.min(...amb) : null;
      const colder = lo !== null && coldest !== Infinity && lo < coldest - 2;
      if (lo !== null) coldest = Math.min(coldest, lo);
      const last = b.clear.length ? b.clear[b.clear.length - 1] : null;
      const cells = [m + (colder ? ' ⚠' : ''), b.nights.size, b.clear.length,
        amb.length ? `${lo.toFixed(1)} … ${Math.max(...amb).toFixed(1)} °C` : '--',
        d.length ? `${pct(d, 5).toFixed(1)} / ${median(d).toFixed(1)}` : '--',
        b.clear.length ? (100 * b.clear.filter(i => M.cloud[i] >= cloudyAt).length / b.clear.length).toFixed(1) + ' %' : '--',
        last !== null ? `${M.clear_delta[last] ?? '--'} / ${M.overcast_delta[last] ?? '--'}` : '--'];
      const tr = document.createElement('tr');
      cells.forEach((v, k) => { const td = document.createElement('td'); td.textContent = v; if (k) td.className = 'num'; tr.appendChild(td); });
      if (colder) tr.title = 'Clear nights colder than any earlier month';
      rows.push(tr);
    }
    tbl.replaceChildren(...rows);
  }

  // ── scatter ────────────────────────────────────────────────────────────
  let geom = null;
  function draw() {
    const el = $('sc');
    let cv = el.querySelector('canvas');
    if (!cv) {
      cv = document.createElement('canvas'); el.appendChild(cv);
      const tip = document.createElement('div'); tip.className = 'tip'; tip.hidden = true; el.appendChild(tip);
      cv.addEventListener('mousemove', e => { const i = nearest(e.offsetX, e.offsetY); if (i !== hover) { hover = i; draw(); } });
      cv.addEventListener('mouseleave', () => { hover = -1; draw(); });
    }
    const dpr = window.devicePixelRatio || 1, W = el.clientWidth, H = +el.dataset.h;
    cv.style.width = W + 'px'; cv.style.height = H + 'px'; cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = C.plot; ctx.fillRect(0, 0, W, H);
    const X = M[xKey], Y = M.d_med;
    const ok = i => X[i] !== null && Y[i] !== null && M.band[i] >= 0;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let i = 0; i < X.length; i++) if (ok(i)) { x0 = Math.min(x0, X[i]); x1 = Math.max(x1, X[i]); y0 = Math.min(y0, Y[i]); y1 = Math.max(y1, Y[i]); }
    const wp = wi(), wc = wp.c, wo = wp.o;
    const n = M.t.length, sc = n ? M.clear_delta[n - 1] : null, so = n ? M.overcast_delta[n - 1] : null;
    const sk = n ? M.amb_slope[n - 1] || 0 : 0, sref = n ? M.amb_ref[n - 1] || 0 : 0;
    if (!isFinite(x0)) {
      ctx.fillStyle = C.axis; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('No minutes with star counts in this range', W / 2, H / 2); geom = null; return;
    }
    for (const v of [wc, wo, sc, so]) if (v !== null && isFinite(v)) { y0 = Math.min(y0, v); y1 = Math.max(y1, v); }
    const xp = (x1 - x0) * 0.05 || 1, yp = (y1 - y0) * 0.05 || 1;
    x0 -= xp; x1 += xp; y0 -= yp; y1 += yp;
    const cw = W - P.l - P.r, ch = H - P.t - P.b;
    const px = x => P.l + (x - x0) / (x1 - x0) * cw, py = y => P.t + (1 - (y - y0) / (y1 - y0)) * ch;
    geom = { px, py };
    // grid + axes
    ctx.font = '10px monospace'; ctx.lineWidth = 1;
    const ticks = (a, b) => { const s = niceStep((b - a) / 6); const out = []; for (let v = Math.ceil(a / s) * s; v <= b; v += s) out.push(+v.toFixed(6)); return out; };
    for (const v of ticks(y0, y1)) { ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(P.l, py(v)); ctx.lineTo(P.l + cw, py(v)); ctx.stroke(); ctx.fillStyle = C.axis; ctx.textAlign = 'right'; ctx.fillText(v, P.l - 4, py(v) + 3); }
    for (const v of ticks(x0, x1)) { ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(px(v), P.t); ctx.lineTo(px(v), P.t + ch); ctx.stroke(); ctx.fillStyle = C.axis; ctx.textAlign = 'center'; ctx.fillText(v, px(v), P.t + ch + 13); }
    ctx.fillStyle = C.axis; ctx.font = '11px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText((xKey === 'dew' ? 'Dew point' : 'Ambient') + ' (°C)', P.l + cw / 2, H - 4);
    ctx.save(); ctx.translate(12, P.t + ch / 2); ctx.rotate(-Math.PI / 2); ctx.fillText('Δ (°C)', 0, 0); ctx.restore();
    // points: overcast first so clear dots sit on top
    for (let b = 0; b < 4; b++) {
      ctx.fillStyle = BANDS[b].color; ctx.globalAlpha = 0.75;
      for (let i = 0; i < X.length; i++) if (M.band[i] === b && ok(i)) { ctx.beginPath(); ctx.arc(px(X[i]), py(Y[i]), 2.2, 0, 2 * Math.PI); ctx.fill(); }
    }
    ctx.globalAlpha = 1;
    // threshold lines, direct-labelled
    // Clear labels sit above their line, overcast labels below, so a nearby
    // pair of lines never runs through a label.
    const hline = (v, color, dash, text, right, below) => {
      if (v === null || !isFinite(v)) return;
      ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(P.l, py(v)); ctx.lineTo(P.l + cw, py(v)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = C.text; ctx.font = '10px sans-serif'; ctx.textAlign = right ? 'right' : 'left';
      ctx.fillText(text, right ? P.l + cw - 4 : P.l + 4, py(v) + (below ? 12 : -4));
    };
    // Ambient-compensated thresholds are sloped lines on the ambient axis;
    // on the dew-point axis they can't be drawn, so they're left out.
    const sline = (v, k, ref, color, dash, text, right, below) => {
      if (v === null || !isFinite(v)) return;
      if (!k) return hline(v, color, dash, text, right, below);
      if (xKey !== 'ambient') return;
      const ya = v + k * (x0 - ref), yb = v + k * (x1 - ref);
      ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(px(x0), py(ya)); ctx.lineTo(px(x1), py(yb)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = C.text; ctx.font = '10px sans-serif'; ctx.textAlign = right ? 'right' : 'left';
      ctx.fillText(text, right ? P.l + cw - 4 : P.l + 4, py(right ? yb : ya) + (below ? 12 : -4));
    };
    const tag = (k, ref) => k ? ` + ${k}×(T−${ref})` : '';
    sline(sc, sk, sref, C.line, [], `Sensor Clear Δ ${sc}${tag(sk, sref)}`, false, false);
    sline(so, sk, sref, C.line, [], `Sensor Overcast Δ ${so}${tag(sk, sref)}`, false, true);
    if (wc !== sc || wo !== so || wp.k !== sk || wp.ref !== sref) {
      sline(wc, wp.k, wp.ref, C.whatif, [5, 4], `What-if Clear ${wc}${tag(wp.k, wp.ref)}`, true, false);
      sline(wo, wp.k, wp.ref, C.whatif, [5, 4], `What-if Overcast ${wo}${tag(wp.k, wp.ref)}`, true, true);
    }
    // clear-sky fit line
    const f = fitClear();
    if (f) {
      ctx.strokeStyle = C.fit; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px(x0), py(f.a + f.b * x0)); ctx.lineTo(px(x1), py(f.a + f.b * x1)); ctx.stroke();
      ctx.fillStyle = C.text; ctx.textAlign = 'left'; ctx.font = '10px sans-serif';
      ctx.fillText('Clear-sky fit', P.l + 4, py(f.a + f.b * x0) + 12);
      $('sc-fit').textContent = `Clear-sky fit: Δ ≈ ${f.a.toFixed(1)} ${f.b >= 0 ? '+' : '−'} ${Math.abs(f.b).toFixed(2)} × ${xKey === 'dew' ? 'dew point' : 'ambient'} (${f.n} clear minutes, scatter ±${f.sd.toFixed(1)} °C).`;
    } else $('sc-fit').textContent = '';
    // hover
    const tip = el.querySelector('.tip');
    if (hover >= 0) {
      ctx.beginPath(); ctx.arc(px(X[hover]), py(Y[hover]), 5, 0, 2 * Math.PI);
      ctx.fillStyle = BANDS[M.band[hover]].color; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = C.plot; ctx.stroke();
      tip.replaceChildren();
      const h = document.createElement('div'); h.className = 'tip-time'; h.textContent = `${fmtDay.format(M.t[hover])} ${fmtTime.format(M.t[hover])}`; tip.appendChild(h);
      for (const [k, v] of [['Stars', M.stars[hover]], ['Δ', fmt(Y[hover], 1, ' °C')], [xKey === 'dew' ? 'Dew point' : 'Ambient', fmt(X[hover], 1, ' °C')], ['Reported cloud', fmt(M.cloud[hover], 0, ' %')]]) {
        const r = document.createElement('div'); const l = document.createElement('span'); l.textContent = k + ' '; const b = document.createElement('b'); b.textContent = v; r.append(l, b); tip.appendChild(r);
      }
      tip.hidden = false;
      const x = px(X[hover]);
      tip.style.left = (x + 12 + tip.offsetWidth > W ? x - 12 - tip.offsetWidth : x + 12) + 'px';
      tip.style.top = Math.max(0, py(Y[hover]) - 30) + 'px';
    } else tip.hidden = true;
  }

  function niceStep(raw) { const p = 10 ** Math.floor(Math.log10(raw)); const f = raw / p; return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p; }

  function nearest(mx, my) {
    if (!geom) return -1;
    let best = -1, bd = 64;   // within 8 px
    const X = M[xKey], Y = M.d_med;
    for (let i = 0; i < X.length; i++) {
      if (X[i] === null || Y[i] === null || M.band[i] < 0) continue;
      const dx = geom.px(X[i]) - mx, dy = geom.py(Y[i]) - my, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  function fitClear() {
    const X = M[xKey], idx = M.band.map((b, i) => b === 3 && X[i] !== null && M.d_med[i] !== null ? i : -1).filter(i => i >= 0);
    if (idx.length < 30) return null;
    let sx = 0, sy = 0, sxx = 0, sxy = 0;
    for (const i of idx) { sx += X[i]; sy += M.d_med[i]; sxx += X[i] * X[i]; sxy += X[i] * M.d_med[i]; }
    const n = idx.length, den = n * sxx - sx * sx;
    if (Math.abs(den) < 1e-9) return null;
    const b = (n * sxy - sx * sy) / den, a = (sy - b * sx) / n;
    const sd = Math.sqrt(idx.reduce((s, i) => s + (M.d_med[i] - a - b * X[i]) ** 2, 0) / n);
    return { a, b, n, sd };
  }

  // ── image review ───────────────────────────────────────────────────────
  function shownNights() { const n = +$('f-range').value; return n ? CAL.nights.slice(0, n) : CAL.nights; }

  function review() {
    const sel = $('r-night'), cur = sel.value;
    const nights = shownNights().filter(d => all[d] && all[d].images.length);
    sel.replaceChildren(new Option('All shown nights', ''), ...nights.map(d => new Option(`${d} (${all[d].images.length})`, d)));
    sel.value = nights.includes(cur) ? cur : '';
    const only = $('r-unlabelled').checked;
    const items = [];
    for (const d of (sel.value ? [sel.value] : nights)) for (const im of all[d].images) items.push({ night: d, ...im });
    const total = items.length, done = items.filter(it => labels[key(it)]).length;
    $('r-progress').textContent = total ? `· ${done} of ${total} labelled` : '';
    const grid = $('r-grid');
    const shown = items.filter(it => !only || !labels[key(it)]);
    grid.replaceChildren(...shown.map(card));
    if (!shown.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = total ? 'Everything here is labelled.' : 'No sampled images yet – they are saved after each night closes.'; grid.appendChild(p); }
    labelTable(items);
  }

  const key = it => `${it.night}/${it.stamp}`;

  function card(it) {
    const fig = document.createElement('figure'); fig.className = 'review-card';
    const imgs = document.createElement('button'); imgs.type = 'button'; imgs.className = 'review-imgs zoomable';
    imgs.setAttribute('aria-label', 'Enlarge');
    const sky = new Image(); sky.loading = 'lazy'; sky.src = CAL.mediaBase + it.night + '/' + it.stamp + '.jpg'; sky.alt = 'Allsky';
    const ir = new Image(); ir.loading = 'lazy'; ir.src = CAL.mediaBase + it.night + '/' + it.stamp + '.ir.png'; ir.alt = 'IR sky';
    imgs.append(sky, ir);
    imgs.onclick = () => zoom(it);
    const cap = document.createElement('figcaption');
    const t = new Date(it.time);
    cap.innerHTML = '';
    const line = document.createElement('div');
    line.textContent = `${fmtDay.format(t)} ${fmtTime.format(t)} · ${it.stars ?? '--'} stars · sensor ${fmt(it.cloud_cover, 0, ' %')}`;
    const why = document.createElement('div'); why.className = 'muted';
    why.textContent = { disagreement: 'Sensor and stars disagree', band: 'Star-count band sample', random: 'Random sample', labelled: 'Kept: labelled' }[it.reason] || it.reason;
    cap.append(line, why);
    const btns = document.createElement('div'); btns.className = 'label-btns';
    const cur = labels[key(it)];
    for (const l of CAL.labels) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'tbtn' + (cur && cur.label === l ? ' act' : '');
      b.textContent = l; b.setAttribute('aria-pressed', cur && cur.label === l ? 'true' : 'false');
      b.onclick = () => saveLabel(it, cur && cur.label === l ? null : l, note.value);
      btns.appendChild(b);
    }
    const note = document.createElement('input'); note.type = 'text'; note.placeholder = 'Note (optional)'; note.className = 'label-note';
    note.value = cur ? cur.note || '' : '';
    note.onchange = () => { const c = labels[key(it)]; if (c) saveLabel(it, c.label, note.value); };
    fig.append(imgs, cap, btns, note);
    return fig;
  }

  async function saveLabel(it, label, note) {
    try {
      const r = await fetch(CAL.labelUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: JSON.stringify({ key: key(it), label, note }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      if (j.saved) labels[key(it)] = j.saved; else delete labels[key(it)];
      $('cal-status').textContent = label ? `Saved: ${label}` : 'Label removed';
    } catch (e) {
      $('cal-status').textContent = 'Label not saved: ' + e.message;
    }
    review();
  }

  function labelTable(items) {
    const p = wi();
    const tbl = $('r-table');
    const head = document.createElement('tr');
    ['Label', 'Images', 'Sensor reported (median)', `What-if ${wiName(p)} (median)`, 'Stars (median)'].forEach(h => { const th = document.createElement('th'); th.textContent = h; head.appendChild(th); });
    const rows = [head];
    for (const l of CAL.labels) {
      const its = items.filter(it => labels[key(it)]?.label === l);
      if (!its.length) continue;
      // What-if needs the minute's quantiles: find it in the night summary by time.
      const wi = its.map(it => {
        const s = all[it.night]; const t = Date.parse(it.time); const i = s.t.indexOf(t);
        return i >= 0 && s.dq[i] ? cloudAt(s.dq[i], s.ambient[i], p) : null;
      }).filter(v => v !== null);
      const tr = document.createElement('tr');
      [l, its.length, fmt(median(its.map(it => it.cloud_cover).filter(v => v !== null)), 0, ' %'), fmt(median(wi), 0, ' %'),
       fmt(median(its.map(it => it.stars).filter(v => v !== null)))].forEach((v, k) => {
        const td = document.createElement(k ? 'td' : 'th'); td.textContent = v; if (k) td.className = 'num'; tr.appendChild(td);
      });
      rows.push(tr);
    }
    if (rows.length === 1) { const tr = document.createElement('tr'); const td = document.createElement('td'); td.colSpan = 5; td.className = 'muted'; td.textContent = 'No labels yet.'; tr.appendChild(td); rows.push(tr); }
    tbl.replaceChildren(...rows);
  }

  const zoomEl = $('zoom');
  function zoom(it) {
    const t = new Date(it.time);
    $('zoom-title').textContent = `${fmtDay.format(t)} ${fmtTime.format(t)} · ${it.stars ?? '--'} stars · sensor ${fmt(it.cloud_cover, 0, ' %')}`;
    $('zoom-sky').src = CAL.mediaBase + it.night + '/' + it.stamp + '.jpg'; $('zoom-sky').alt = 'Allsky image';
    $('zoom-ir').src = CAL.mediaBase + it.night + '/' + it.stamp + '.ir.png'; $('zoom-ir').alt = 'IR sky image';
    zoomEl.showModal();
  }
  $('zoom-close').onclick = () => zoomEl.close();
  zoomEl.addEventListener('click', e => { if (e.target === zoomEl) zoomEl.close(); });

  // ── nights table ───────────────────────────────────────────────────────
  async function nightsTable() {
    const r = await fetch(CAL.nightsUrl); if (!r.ok) return;
    const list = await r.json();
    const tbl = $('n-table');
    const head = document.createElement('tr');
    ['Night', 'Dark min', 'With stars', 'Images', 'Allsky data from', 'Notes', ''].forEach(h => { const th = document.createElement('th'); th.textContent = h; head.appendChild(th); });
    tbl.replaceChildren(head, ...list.map(n => {
      const tr = document.createElement('tr');
      [n.night, n.minutes, n.with_stars, n.images, n.history_source === 'file' ? 'saved HA history' : n.history_source === 'homeassistant' ? 'Home Assistant' : n.history_source,
       (n.notes || []).join('; ')].forEach((v, k) => { const td = document.createElement('td'); td.textContent = v ?? '--'; if (k && k < 4) td.className = 'num'; tr.appendChild(td); });
      const td = document.createElement('td'); const a = document.createElement('a'); a.href = CAL.downloadBase + n.night + '.jsonl.gz'; a.textContent = 'Download'; td.appendChild(a); tr.appendChild(td);
      return tr;
    }));
  }

  // ── wiring ─────────────────────────────────────────────────────────────
  $('f-range').onchange = load;
  for (const id of ['f-moon', 'f-clear', 'f-over']) $(id).onchange = () => flatten(shownNights());
  for (const id of ['w-clear', 'w-over', 'w-slope', 'w-ref', 'w-cloudy']) $(id).oninput = render;
  if ($('w-recalc')) $('w-recalc').onclick = () => { useRecalc(); render(); };
  $('w-current').onclick = () => { useCurrent(); render(); };
  $('w-fit').onclick = () => { $('w-msg').textContent = 'Searching…'; setTimeout(bestFit, 20); };
  $('r-night').onchange = review; $('r-unlabelled').onchange = review;
  document.querySelectorAll('[data-x]').forEach(b => b.onclick = () => {
    xKey = b.dataset.x; document.querySelectorAll('[data-x]').forEach(x => x.classList.toggle('act', x === b));
    $('sc-xname').textContent = xKey === 'dew' ? 'dew point' : 'ambient'; hover = -1; draw();
  });
  window.addEventListener('resize', () => { if (M) draw(); });

  (async () => {
    try { labels = await (await fetch(CAL.labelsUrl)).json(); } catch (e) { labels = {}; }
    await load();
    nightsTable();
  })();
})();
