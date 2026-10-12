// CloudRoll card for Home Assistant: the Data page (day charts + record
// inspector with the matching Allsky / Observatory / IR images) inside a
// Lovelace dashboard, plus a "Now" strip of live readings from HA entities.
//
// Served by CloudRoll at /static/ha/cloudroll-card.js; the HA origin must be in
// config.json web.cors_origins.  Look and chart drawing follow static/charts.js
// and static/style.css.
//
//   type: custom:cloudroll-card
//   url: https://cloudroll.example            # CloudRoll base URL
//   entities:                                 # optional, per reading key
//     cloud_cover: sensor.sky_cloud_cover
//     raining: binary_sensor.sky_rain
//   (readings without an entity show CloudRoll's latest record instead)
'use strict';

(() => {
const VERSION = '0.3.1';

const C = {
  plot: '#0f3460', grid: '#2d3561', axis: '#8899aa', text: '#e0e0e0', muted: '#b2bec3',
  sel: '#4a90d9', cross: '#dfe6e9', wet: '#e17055', dry: '#00b894', surface: '#16213e',
};
const CHARTS = [
  { id: 'temp', title: 'Temperature (°C)', h: 220, unit: '°C', digits: 1, series: [
    { key: 'ambient_temp', label: 'Ambient', color: '#ffeaa7' },
    { key: 'sky_temp', label: 'Sky', color: '#a29bfe' }] },
  { id: 'cloud', title: 'Cloud Cover (%)', h: 150, unit: '%', digits: 0, min: 0, max: 100,
    series: [{ key: 'cloud_cover', label: 'Cloud cover', color: '#dfe6e9' }] },
  { id: 'lux', title: 'Sky Brightness (lux, log scale)', h: 150, unit: 'lx', log: true,
    series: [{ key: 'brightness', label: 'Brightness', color: '#fdcb6e' }] },
  { id: 'sqm', title: 'Sky Quality (mag/arcsec²)', h: 150, unit: 'mag/arcsec²', digits: 2,
    series: [{ key: 'sqm', label: 'SQM', color: '#00cec9' }] },
  { id: 'pres', title: 'Pressure (hPa)', h: 150, unit: 'hPa', digits: 1,
    series: [{ key: 'pressure', label: 'Pressure', color: '#81ecec' }] },
  { id: 'hum', title: 'Humidity (%)', h: 150, unit: '%', digits: 1,
    series: [{ key: 'humidity', label: 'Humidity', color: '#74b9ff' }] },
];
const RAIN_H = 56;
const ROWS = [
  ['cloud_cover', 'Cloud Cover', v => v.toFixed(0) + ' %', '#dfe6e9'],
  ['ambient_temp', 'Ambient Temp', v => v.toFixed(1) + ' °C', '#ffeaa7'],
  ['sky_temp', 'Sky Temp', v => v.toFixed(1) + ' °C', '#a29bfe'],
  ['brightness', 'Brightness', fmtLux, '#fdcb6e'],
  ['sqm', 'SQM', v => v.toFixed(2), '#00cec9'],
  ['pressure', 'Pressure', v => v.toFixed(1) + ' hPa', '#81ecec'],
  ['humidity', 'Humidity', v => v.toFixed(1) + ' %', '#74b9ff'],
  ['raining', 'Rain', v => v ? 'Raining' : 'Dry', null],
];
const P = { t: 10, r: 14, b: 22, l: 56 };

function fmtLux(v) { return v >= 100 ? v.toFixed(0) + ' lx' : Number(v.toPrecision(3)) + ' lx'; }
function fmtVal(c, v) {
  if (v === null || v === undefined) return '--';
  if (c.log) return fmtLux(v);
  return v.toFixed(c.digits ?? 1) + (c.unit === '%' ? ' %' : c.unit === '°C' ? ' °C' : '');
}
const nicedate = day => new Date(day + 'T12:00:00').toLocaleDateString(undefined,
  { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const STYLE = `
:host { display: block; container-type: inline-size; }
* { box-sizing: border-box; }
.root { font-family: Arial, sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 16px;
        border-radius: var(--ha-card-border-radius, 12px); }
h1, h2 { color: #a0c4ff; }
h1 { margin: 0 0 12px; font-size: 2em; }
h1 .sub { font-size: 0.5em; color: #74b9ff; font-weight: normal; letter-spacing: 1px; text-transform: uppercase; }
h2 { margin: 0 0 8px; font-size: 1.5em; }
a { color: #74b9ff; text-decoration: none; }
.card { background: #16213e; border-radius: 8px; padding: 16px; margin-bottom: 16px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.4); }
table { border-collapse: collapse; width: 100%; margin-bottom: 12px; }
th, td { border: 1px solid #2d3561; padding: 8px; text-align: left; }
th { background: #0f3460; width: 1%; white-space: nowrap; }
.nav { margin-bottom: 16px; }
.nav a { display: inline-block; background: #0f3460; color: #e0e0e0; padding: 8px 14px;
         border-radius: 4px; margin: 0 8px 6px 0; }
.nav a:hover { background: #1a4a8a; }
.tbtn { display: inline-block; padding: 6px 12px; margin: 2px; border-radius: 4px; background: #0f3460;
        color: #e0e0e0; cursor: pointer; border: none; font-size: 0.85em; font-family: inherit; }
.tbtn:hover { background: #1a4a8a; }
.tbtn:disabled, .tbtn.off { opacity: 0.35; cursor: default; pointer-events: none; }
.muted { color: #8899aa; font-size: 0.85em; }
.err { color: #ff7675; }
.badge { font-size: 0.6em; background: #7f4f00; color: #ffeaa7; padding: 2px 6px; border-radius: 4px; vertical-align: middle; }
.footer { text-align: center; font-size: 0.75em; color: #636e72; margin-top: 8px; }
.month-nav { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.month-nav h2 { margin: 0; text-align: center; flex: 1; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; }
.stat-box { background: #0f3460; border-radius: 6px; padding: 12px; text-align: center; }
.stat-box.tap { cursor: pointer; }
.stat-box.tap:hover { background: #1a4a8a; }
.stat-label { font-size: 0.75em; color: #74b9ff; text-transform: uppercase; letter-spacing: 1px; }
.stat-value { font-size: 1.4em; font-weight: bold; margin-top: 4px; }
.stat-src { font-size: 0.65em; color: #8899aa; margin-top: 4px; }
#now { grid-template-columns: repeat(auto-fit, minmax(105px, 1fr)); }
.now-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.now-head h2 { margin: 0; }
.dl-row { margin: 14px 0 0; }
.data-layout { display: grid; grid-template-columns: minmax(0, 1fr) 420px; gap: 16px; align-items: start; }
.charts .card h2 { font-size: 1em; margin-bottom: 8px; }
.record { position: sticky; top: calc(var(--header-height, 56px) + 12px);
          max-height: calc(100vh - var(--header-height, 56px) - 24px); overflow-y: auto; }
@container (max-width: 1000px) {
  .data-layout { grid-template-columns: minmax(0, 1fr); }
  .record { position: static; max-height: none; }
}
.chart { position: relative; width: 100%; }
.chart canvas { display: block; border-radius: 4px; cursor: crosshair; touch-action: pan-y; }
.tip { position: absolute; pointer-events: none; background: #16213e; border: 1px solid #2d3561;
       border-radius: 6px; padding: 6px 8px; font-size: 0.78em; color: #e0e0e0; white-space: nowrap;
       box-shadow: 0 2px 8px rgba(0,0,0,0.5); z-index: 2; }
.tip-time { color: #8899aa; margin-bottom: 2px; }
.tip .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.legend { font-size: 0.8em; color: #b2bec3; margin: -4px 0 6px; }
.legend span { margin-right: 14px; white-space: nowrap; }
.legend i { display: inline-block; margin-right: 5px; }
.legend i.line { width: 14px; height: 2px; vertical-align: middle; }
.legend i.box { width: 10px; height: 10px; border-radius: 2px; vertical-align: -1px; }
.rec-nav { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
.rec-table { margin-top: 10px; }
.rec-table th { width: 45%; }
.rec-cams { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 10px; }
.rec-cams .cam { flex: var(--ar, 1.7778) 1 0; min-width: 0; }
.cam { margin: 0; display: flex; flex-direction: column; gap: 4px; }
.cam figcaption { font-size: 0.7em; color: #74b9ff; text-transform: uppercase; letter-spacing: 1px;
                  display: flex; gap: 8px; align-items: baseline; }
.cam-stale { color: #e17055; }
.cam-frame { position: relative; width: 100%; aspect-ratio: var(--ar, 1.7778); padding: 0; border: 0;
             background: #1a1a2e; border-radius: 6px; overflow: hidden; display: block; font: inherit; cursor: zoom-in; }
.cam-frame img { width: 100%; height: 100%; object-fit: contain; display: block; }
.cam-frame.empty { background: #3d4452; cursor: default; }
.cam-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
           text-align: center; padding: 6px; color: #f1f3f5; font-size: 0.78em; font-weight: bold; line-height: 1.3; }
.thermal img { width: 100%; height: auto; border-radius: 6px; display: block; cursor: zoom-in; }
.cam-empty-msg { background: #3d4452; color: #f1f3f5; font-weight: bold; font-size: 0.85em;
                 border-radius: 6px; padding: 24px 14px; margin: 0; text-align: center; }
[hidden] { display: none !important; }
details summary { cursor: pointer; color: #a0c4ff; font-weight: bold; }
details[open] summary { margin-bottom: 10px; }
.table-wrap { overflow-x: auto; }
.data-table th { width: auto; }
.data-table td { font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.clickable { cursor: pointer; }
tr.clickable:hover td { background: #1a4a8a; }
tr.sel td { background: #0f3460; }
.live-status { font-size: 0.8em; color: #8899aa; margin-left: 6px; white-space: nowrap; }
.live-status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #00b894; margin-right: 5px; }
.live-status.stale .dot { background: #e17055; }
dialog.zoom { width: min(96vw, 1600px); max-height: 94vh; padding: 12px 14px 14px; border: 1px solid #2d3561;
              border-radius: 8px; background: #16213e; color: #e0e0e0; }
dialog.zoom::backdrop { background: rgba(5, 8, 20, 0.82); }
.zoom-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; margin-bottom: 10px; }
.zoom-head h2 { margin: 0; font-size: 1.1em; margin-right: auto; }
.zoom-body { display: flex; justify-content: center; }
.zoom-body img { max-width: 100%; max-height: calc(94vh - 90px); object-fit: contain; border-radius: 6px; display: block; }
.zoom-body .cam-empty-msg { width: 100%; padding: 80px 20px; font-size: 1em; }
`;

class CloudRollCard extends HTMLElement {
  setConfig(config) {
    if (!config.url) throw new Error('cloudroll-card: set url to the CloudRoll base URL');
    this._cfg = { ...config, url: config.url.replace(/\/+$/, ''), entities: config.entities || {} };
    if (this._built) this._start();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._built) this._renderNow();
  }

  getCardSize() { return 20; }
  getGridOptions() { return { columns: 'full' }; }

  connectedCallback() {
    if (!this._built) this._build();
    this._onKey = e => this._key(e);
    window.addEventListener('keydown', this._onKey);
    this._onVis = () => { if (!document.hidden) this._poll(); };
    document.addEventListener('visibilitychange', this._onVis);
    this._ro = new ResizeObserver(() => this._drawAll());
    this._ro.observe(this.$.charts);
    // Back on screen after a view switch: reload the day so polling resumes.
    if (this._D) this._loadDay(this._day, this._follow);
    else if (this._cfg) this._start();
  }

  disconnectedCallback() {
    window.removeEventListener('keydown', this._onKey);
    document.removeEventListener('visibilitychange', this._onVis);
    if (this._ro) this._ro.disconnect();
    clearInterval(this._timer); this._timer = null;
  }

  // ── DOM ────────────────────────────────────────────────────────────────
  _build() {
    this._built = true;
    const root = this.attachShadow({ mode: 'open' });
    root.innerHTML = `<style>${STYLE}</style>
<div class="root">
  <h1><span id="title">CloudRoll</span> <span class="sub">IR Sky Timelapse</span></h1>
  <div class="nav" id="nav"></div>
  <div class="card">
    <div class="now-head"><h2>Now</h2><span class="muted" id="now-note"></span></div>
    <div class="stat-grid" id="now"></div>
  </div>
  <p class="cam-empty-msg err" id="error" hidden></p>
  <div id="body" hidden>
    <div class="card">
      <div class="month-nav">
        <button class="tbtn" id="day-prev">&#9664;</button>
        <h2><span id="day-title"></span> <span class="badge" id="open-badge" hidden>open</span></h2>
        <button class="tbtn" id="day-next">&#9654;</button>
      </div>
      <div class="stat-grid">
        <div class="stat-box"><div class="stat-label">Records</div><div class="stat-value" id="st-records">--</div></div>
        <div class="stat-box"><div class="stat-label">Span</div><div class="stat-value" id="st-span">--</div></div>
        <div class="stat-box"><div class="stat-label">Avg Cloud</div><div class="stat-value" id="st-cloud">--</div></div>
        <div class="stat-box"><div class="stat-label">Min Sky Temp</div><div class="stat-value" id="st-sky" style="color:#a29bfe">--</div></div>
        <div class="stat-box"><div class="stat-label">Peak SQM</div><div class="stat-value" id="st-sqm">--</div></div>
        <div class="stat-box"><div class="stat-label">Rain</div><div class="stat-value" id="st-rain">--</div></div>
      </div>
      <p class="dl-row">
        <a class="tbtn" id="dl-csv" target="_blank" rel="noopener">Download CSV</a>
        <a class="tbtn" id="dl-grid" target="_blank" rel="noopener">CSV + grids</a>
        <a class="tbtn" id="dl-day" target="_blank" rel="noopener">Open in CloudRoll &#8599;</a>
        <span class="live-status" id="live-status" hidden></span>
      </p>
    </div>
    <div class="data-layout">
      <div class="charts" id="charts"></div>
      <div class="card record">
        <h2>Record <span id="rec-time">--:--</span></h2>
        <div class="rec-nav">
          <button class="tbtn" id="rec-prev" title="Previous record (←)">&#9664;</button>
          <span id="rec-pos" class="muted"></span>
          <button class="tbtn" id="rec-next" title="Next record (→)">&#9654;</button>
          <a class="tbtn" id="rec-json" target="_blank" rel="noopener">Record JSON</a>
          <a class="tbtn" id="rec-csv" target="_blank" rel="noopener" title="This record's temperatures as a 24 × 32 grid (°C)">Grid CSV</a>
        </div>
        <div class="rec-cams" id="cams"></div>
        <figure class="cam thermal">
          <figcaption>IR sky</figcaption>
          <img id="rec-img" alt="Stored temperature grid" tabindex="0" role="button" aria-label="Enlarge IR sky image">
          <p id="rec-noimg" class="cam-empty-msg" hidden>No temperature grid stored for this record.</p>
        </figure>
        <table class="rec-table" id="rec-table"></table>
        <p class="muted">Click any chart to pick a record; ← / → step through them. Click an image to enlarge it.
          The IR image is re-rendered from the stored 32×24 grid using the colour scale in effect at capture.
          Camera images are kept <span id="keep-days">7</span> days.</p>
      </div>
    </div>
    <div class="card">
      <details id="tbl-details">
        <summary>Table view (all records)</summary>
        <div class="table-wrap"><table class="data-table" id="all-table"></table></div>
      </details>
    </div>
  </div>
  <div class="footer" id="footer"></div>
  <dialog class="zoom" id="zoom">
    <div class="zoom-head">
      <h2 id="zoom-title"></h2>
      <span class="muted">← / → step records · Esc closes</span>
      <a id="zoom-full" class="tbtn" target="_blank" rel="noopener">Open original &#8599;</a>
      <button type="button" class="tbtn" id="zoom-close">Close</button>
    </div>
    <div class="zoom-body">
      <img id="zoom-img" alt="">
      <p id="zoom-msg" class="cam-empty-msg" hidden></p>
    </div>
  </dialog>
</div>`;
    this.$ = new Proxy({}, { get: (_, id) => root.getElementById(id) });
    this._tips = {};
    this._charts = {};
    for (const c of [...CHARTS, { id: 'rain', title: 'Rain', h: RAIN_H }]) {
      const card = el('div', 'card');
      card.append(el('h2', null, c.title));
      if (c.series && c.series.length > 1) card.append(this._legend(c.series.map(s => [s.color, s.label])));
      if (c.id === 'rain') card.append(this._legend([[C.dry, 'Dry', true], [C.wet, 'Raining', true]]));
      const box = el('div', 'chart');
      box.dataset.h = c.h;
      const cv = el('canvas'), tip = el('div', 'tip');
      tip.hidden = true;
      box.append(cv, tip);
      card.append(box);
      this.$.charts.append(card);
      this._charts[c.id] = box;
      this._tips[c.id] = tip;
      cv.addEventListener('mousemove', e => this._setHover(this._idxAt(box, e.offsetX)));
      cv.addEventListener('mouseleave', () => this._setHover(-1));
      cv.addEventListener('click', e => this._select(this._idxAt(box, e.offsetX)));
      cv.addEventListener('touchstart', e => {
        const r = cv.getBoundingClientRect();
        this._select(this._idxAt(box, e.touches[0].clientX - r.left));
      }, { passive: true });
    }
    this.$['day-prev'].onclick = () => this._stepDay(1);
    this.$['day-next'].onclick = () => this._stepDay(-1);
    this.$['rec-prev'].onclick = () => this._select(this._sel - 1);
    this.$['rec-next'].onclick = () => this._select(this._sel + 1);
    this.$['rec-img'].onclick = () => this._zoomOpen('thermal');
    this.$['rec-img'].onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') this._zoomOpen('thermal'); };
    this.$['zoom-close'].onclick = () => this.$.zoom.close();
    this.$.zoom.addEventListener('close', () => { this._zoomed = null; });
    this.$.zoom.addEventListener('click', e => { if (e.target === this.$.zoom) this.$.zoom.close(); });
    this.$['tbl-details'].addEventListener('toggle', e => { if (e.target.open) this._buildTable(); });
  }

  _legend(items) {
    const d = el('div', 'legend');
    for (const [color, label, box] of items) {
      const s = el('span'), sw = el('i', box ? 'box' : 'line');
      sw.style.background = color;
      s.append(sw, document.createTextNode(label));
      d.append(s);
    }
    return d;
  }

  // ── data ───────────────────────────────────────────────────────────────
  async _start() {
    const url = this._cfg.url;
    if (this._started === url) return;
    this._started = url;
    try {
      const r = await fetch(url + '/api/info', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      this._info = await r.json();
    } catch (e) {
      this._started = null;
      return this._fail(`Can't reach CloudRoll at ${url} (${e.message}). Check that it is running and that this ` +
                        `Home Assistant origin (${location.origin}) is listed in its web.cors_origins.`);
    }
    const info = this._info;
    this._fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: info.tz });
    this.$.title.textContent = info.title;
    this.$['keep-days'].textContent = info.frames_days;
    this.$.footer.textContent = `${info.title} v${info.version} · ${info.location.name} ` +
      `(${info.location.latitude.toFixed(4)}, ${info.location.longitude.toFixed(4)}) · card v${VERSION}`;
    const nav = [['Data', '/'], ['Timelapses', '/timelapses'], ['Live', '/live']]
      .map(([label, p]) => [label + ' ↗', url + p]);
    if (info.sensor_url) nav.push(['Sky Sensor ↗', info.sensor_url]);
    this.$.nav.replaceChildren(...nav.map(([label, href]) => {
      const a = el('a', null, label); a.href = href; a.target = '_blank'; a.rel = 'noopener'; return a;
    }));
    this.$.cams.replaceChildren(...info.cams.map(c => {
      const fig = el('figure', 'cam');
      fig.id = 'cam-' + c.name;
      fig.style.setProperty('--ar', c.aspect);
      const cap = el('figcaption', null, c.label + ' ');
      const stale = el('span', 'cam-stale', 'stale'); stale.hidden = true;
      cap.append(stale);
      const frame = el('button', 'cam-frame');
      frame.type = 'button';
      frame.setAttribute('aria-label', 'Enlarge ' + c.label);
      const img = el('img'); img.hidden = true; img.alt = c.label + ' at the selected record';
      const msg = el('span', 'cam-msg', 'Loading…');
      frame.append(img, msg);
      frame.onclick = () => { if (!frame.classList.contains('empty')) this._zoomOpen(c.name); };
      fig.append(cap, frame);
      return fig;
    }));
    if (!info.days.length) return this._fail('CloudRoll has no recorded days yet.');
    await this._loadDay(info.days[0], true);
  }

  _fail(text) {
    this.$.error.textContent = text;
    this.$.error.hidden = false;
    this.$.body.hidden = true;
  }

  async _loadDay(day, follow) {
    this._day = day;
    this._follow = follow;
    clearInterval(this._timer); this._timer = null;
    const url = this._cfg.url;
    try {
      const r = await fetch(`${url}/api/readings/${day}.json`, { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      this._D = await r.json();
    } catch (e) {
      return this._fail(`Couldn't load ${day} from CloudRoll (${e.message}).`);
    }
    if (this._day !== day) return;
    this.$.error.hidden = true;
    this.$.body.hidden = false;
    const base = `${url}/api/readings/${day}`;
    this.$['day-title'].textContent = nicedate(day);
    this.$['open-badge'].hidden = !this._D.open;
    this.$['dl-csv'].href = base + '.csv';
    this.$['dl-grid'].href = base + '.csv?grid=1';
    this.$['dl-day'].href = `${url}/data/${day}`;
    const days = this._info.days, i = days.indexOf(day);
    this.$['day-prev'].classList.toggle('off', i < 0 || i >= days.length - 1);
    this.$['day-next'].classList.toggle('off', i <= 0);
    this._computeGap();
    this._hover = -1;
    this._sel = -1;
    if (this.$['tbl-details'].open) this._buildTable(true); else this.$['all-table'].replaceChildren();
    this._updateTiles();
    this._select(this._D.t.length - 1);
    this._renderNow();
    if (this._D.open) {
      this._liveStatus();
      this._timer = setInterval(() => this._poll(), (this._info.poll_sec || 15) * 1000);
    } else {
      this.$['live-status'].hidden = true;
    }
  }

  _stepDay(dir) {
    const days = this._info.days, i = days.indexOf(this._day) + dir;
    if (i >= 0 && i < days.length) this._loadDay(days[i], i === 0);
  }

  async _poll() {
    const D = this._D;
    if (!this._timer || !D || document.hidden || !this.isConnected) return;
    const day = this._day, url = this._cfg.url;
    try {
      const r = await fetch(`${url}/api/readings/${day}.json?since=${D.t.length}`, { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const u = await r.json();
      if (this._day !== day) return;
      if (this._follow && u.latest_day !== day) {
        // A new day started: refresh the day list and follow it.
        const info = await (await fetch(url + '/api/info', { cache: 'no-store' })).json();
        this._info.days = info.days;
        return this._loadDay(info.days[0], true);
      }
      if (u.since !== D.t.length || u.count < D.t.length) {
        return this._loadDay(day, this._follow);  // file rewritten underneath us
      }
      if (u.t.length) {
        const following = this._sel === D.t.length - 1;
        for (const k of Object.keys(D)) if (Array.isArray(D[k]) && Array.isArray(u[k])) D[k].push(...u[k]);
        this._computeGap();
        this._select(following ? D.t.length - 1 : this._sel);
        if (this.$['tbl-details'].open) this._buildTable(true);
        this._updateTiles();
        this._renderNow();
      }
      if (!u.open) {
        clearInterval(this._timer); this._timer = null;
        this.$['open-badge'].hidden = true;
        this._liveStatus('Day complete');
        return;
      }
      this._liveStatus();
    } catch (e) {
      this._liveStatus('Live updates paused – retrying', true);
    }
  }

  _liveStatus(text, stale) {
    const D = this._D, s = this.$['live-status'];
    text = text || `Live · ${D.t.length} records · last ${this._fmtTime.format(D.t[D.t.length - 1])}`;
    s.hidden = false;
    s.classList.toggle('stale', !!stale);
    s.replaceChildren(el('span', 'dot'), document.createTextNode(text));
  }

  _computeGap() {
    const t = this._D.t;
    const diffs = t.slice(1).map((v, i) => v - t[i]).sort((a, b) => a - b);
    this._gapMs = diffs.length ? diffs[diffs.length >> 1] * 3 : Infinity;
  }

  _updateTiles() {
    const D = this._D, f = this._fmtTime, n = D.t.length;
    const vals = k => D[k].filter(v => v !== null && v !== undefined);
    const set = (id, txt) => { this.$[id].textContent = txt; };
    const cloud = vals('cloud_cover'), sky = vals('sky_temp'), sqm = vals('sqm');
    set('st-records', n);
    set('st-span', n ? f.format(D.t[0]) + '–' + f.format(D.t[n - 1]) : '--');
    set('st-cloud', cloud.length ? (cloud.reduce((a, b) => a + b, 0) / cloud.length).toFixed(0) + ' %' : '--');
    set('st-sky', sky.length ? Math.min(...sky).toFixed(1) + ' °C' : '--');
    set('st-sqm', sqm.length ? Math.max(...sqm).toFixed(2) : '--');
    set('st-rain', D.raining.filter(v => v).length + ' min');
  }

  // ── "Now": live HA entities, falling back to CloudRoll's latest record ─
  _renderNow() {
    if (!this._cfg) return;
    const ents = this._cfg.entities, hass = this._hass;
    if (!this._nowBoxes) {
      this._nowBoxes = {};
      this.$.now.replaceChildren(...ROWS.map(([k, label]) => {
        const box = el('div', 'stat-box');
        const v = el('div', 'stat-value', '--'), src = el('div', 'stat-src');
        box.append(el('div', 'stat-label', label), v, src);
        if (ents[k]) {
          box.classList.add('tap');
          box.title = ents[k];
          box.onclick = () => this.dispatchEvent(new CustomEvent('hass-more-info',
            { bubbles: true, composed: true, detail: { entityId: ents[k] } }));
        }
        this._nowBoxes[k] = { v, src };
        return box;
      }));
    }
    // CloudRoll fallback: the newest record, kept while browsing older days.
    const D = this._D;
    if (D && this._follow && D.t.length) {
      const i = D.t.length - 1;
      this._latest = { t: D.t[i], ...Object.fromEntries(ROWS.map(([k]) => [k, D[k][i]])) };
    }
    const L = this._latest;
    let fromHa = 0, fromCr = 0;
    for (const [k, , fmt, color] of ROWS) {
      const { v, src } = this._nowBoxes[k];
      let val = null, where = '';
      const st = ents[k] && hass && hass.states[ents[k]];
      if (st && !['unavailable', 'unknown'].includes(st.state)) {
        val = k === 'raining' ? st.state === 'on' : parseFloat(st.state);
        if (k !== 'raining' && !isFinite(val)) val = null;
        where = 'HA';
      } else if (L && L[k] !== null && L[k] !== undefined) {
        val = L[k]; where = 'CloudRoll ' + this._fmtTime.format(L.t);
      }
      v.textContent = val === null ? '--' : fmt(val);
      v.style.color = k === 'raining' ? (val === null ? '' : val ? C.wet : C.dry) : color;
      src.textContent = where;
      if (where === 'HA') fromHa++; else if (where) fromCr++;
    }
    this.$['now-note'].textContent = fromCr
      ? `${fromHa} live from Home Assistant · ${fromCr} from CloudRoll's latest record`
      : 'live from Home Assistant';
  }

  // ── charts ─────────────────────────────────────────────────────────────
  _geom(box) {
    const cv = box.querySelector('canvas');
    const dpr = window.devicePixelRatio || 1;
    const W = box.clientWidth, H = +box.dataset.h;
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, W, H, cw: W - P.l - P.r, ch: H - P.t - P.b };
  }
  _t0() { return this._D.t[0]; }
  _tSpan() { const t = this._D.t; return (t[t.length - 1] - t[0]) || 1; }
  _x(g, i) { return P.l + (this._D.t[i] - this._t0()) / this._tSpan() * g.cw; }

  _idxAt(box, px) {
    const t = this._D && this._D.t;
    if (!t || !t.length) return -1;
    const cw = box.clientWidth - P.l - P.r;
    const tt = this._t0() + (px - P.l) / cw * this._tSpan();
    let lo = 0, hi = t.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (t[m] < tt) lo = m; else hi = m; }
    return (tt - t[lo] < t[hi] - tt) ? lo : hi;
  }

  _timeAxis(g) {
    const { ctx, cw, ch } = g, t = this._D.t;
    const hours = this._tSpan() / 3.6e6;
    const step = Math.max(1, Math.ceil(hours / Math.max(2, Math.floor(cw / 70)))) * 3.6e6;
    const first = Math.ceil(this._t0() / 3.6e6) * 3.6e6;
    ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    for (let tt = first; tt <= t[t.length - 1]; tt += step) {
      const x = P.l + (tt - this._t0()) / this._tSpan() * cw;
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, P.t); ctx.lineTo(x, P.t + ch); ctx.stroke();
      ctx.fillStyle = C.axis; ctx.fillText(this._fmtTime.format(tt), x, P.t + ch + 14);
    }
  }

  _markers(g) {
    const { ctx, ch } = g;
    if (this._sel >= 0) {
      ctx.strokeStyle = C.sel; ctx.lineWidth = 2; ctx.setLineDash([4, 3]);
      const x = this._x(g, this._sel);
      ctx.beginPath(); ctx.moveTo(x, P.t); ctx.lineTo(x, P.t + ch); ctx.stroke();
      ctx.setLineDash([]);
    }
    if (this._hover >= 0) {
      ctx.strokeStyle = C.cross; ctx.lineWidth = 1;
      const x = this._x(g, this._hover);
      ctx.beginPath(); ctx.moveTo(x, P.t); ctx.lineTo(x, P.t + ch); ctx.stroke();
    }
  }

  _drawLine(c) {
    const D = this._D, box = this._charts[c.id];
    const g = this._geom(box);
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
      this._tips[c.id].hidden = true;
      return;
    }
    if (c.min !== undefined) { mn = c.min; mx = c.max; }
    else if (c.log) { mn = Math.floor(mn); mx = Math.max(Math.ceil(mx), mn + 1); }
    else { const sp = (mx - mn) || 1; mn -= sp * 0.08; mx += sp * 0.08; }
    const yOf = y => P.t + (1 - (y - mn) / (mx - mn)) * ch;

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
    this._timeAxis(g);

    for (const s of c.series) {
      ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.lineJoin = 'round';
      ctx.beginPath();
      let on = false;
      D[s.key].forEach((v, i) => {
        const y = v === null ? null : tf(v);
        if (y === null || !isFinite(y) || (i > 0 && D.t[i] - D.t[i - 1] > this._gapMs)) { on = false; if (y === null) return; }
        const x = this._x(g, i), yp = yOf(Math.min(mx, Math.max(mn, y)));
        if (!on) { ctx.moveTo(x, yp); on = true; } else ctx.lineTo(x, yp);
      });
      ctx.stroke();
    }
    this._markers(g);

    const hv = this._hover;
    if (hv >= 0) for (const s of c.series) {
      const v = D[s.key][hv]; const y = v === null ? null : tf(v);
      if (y === null || !isFinite(y)) continue;
      ctx.beginPath(); ctx.arc(this._x(g, hv), yOf(y), 4, 0, 2 * Math.PI);
      ctx.fillStyle = s.color; ctx.fill();
      ctx.lineWidth = 2; ctx.strokeStyle = C.plot; ctx.stroke();
    }
    this._tooltip(c.id, g, hv, c.series.map(s => [s.color, s.label, fmtVal(c, D[s.key][hv])]));
  }

  _drawRain() {
    const D = this._D;
    const g = this._geom(this._charts.rain);
    const { ctx, W, H, ch } = g;
    ctx.fillStyle = C.plot; ctx.fillRect(0, 0, W, H);
    const n = D.t.length;
    for (let i = 0; i < n; i++) {
      const v = D.raining[i];
      if (v === null) continue;
      const x1 = this._x(g, i);
      const x2 = i + 1 < n && D.t[i + 1] - D.t[i] <= this._gapMs ? this._x(g, i + 1) : x1 + 1;
      ctx.fillStyle = v ? C.wet : C.dry;
      ctx.fillRect(x1, P.t, Math.max(1, x2 - x1), ch);
    }
    ctx.fillStyle = C.axis; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText('Rain', P.l - 4, P.t + ch / 2 + 3);
    this._timeAxis(g);
    this._markers(g);
    const v = this._hover >= 0 ? D.raining[this._hover] : null;
    this._tooltip('rain', g, this._hover, [[v ? C.wet : C.dry, 'Rain', v === null ? '--' : v ? 'Raining' : 'Dry']]);
  }

  _tooltip(id, g, i, rows) {
    const tip = this._tips[id];
    if (i < 0) { tip.hidden = true; return; }
    tip.replaceChildren(el('div', 'tip-time', this._fmtTime.format(this._D.t[i])));
    for (const [color, label, val] of rows) {
      const r = el('div'), sw = el('span', 'sw');
      sw.style.background = color;
      r.append(sw, el('span', null, label + ' '), el('b', null, val));
      tip.append(r);
    }
    tip.hidden = false;
    const x = this._x(g, i);
    const left = x + 12 + tip.offsetWidth > g.W ? x - 12 - tip.offsetWidth : x + 12;
    tip.style.left = left + 'px'; tip.style.top = P.t + 'px';
  }

  _drawAll() {
    if (!this._D || !this._D.t.length || this.$.body.hidden) return;
    CHARTS.forEach(c => this._drawLine(c));
    this._drawRain();
  }

  _setHover(i) { if (i !== this._hover) { this._hover = i; this._drawAll(); } }

  // ── record inspector ───────────────────────────────────────────────────
  _select(i) {
    const D = this._D;
    if (!D || i < 0 || i >= D.t.length) return;
    this._sel = i;
    this._drawAll();
    const base = `${this._cfg.url}/api/readings/${this._day}`;
    this.$['rec-time'].textContent = this._fmtTime.format(D.t[i]);
    this.$['rec-pos'].textContent = `${i + 1} / ${D.t.length}`;
    this.$['rec-prev'].disabled = i === 0;
    this.$['rec-next'].disabled = i === D.t.length - 1;
    this.$['rec-json'].href = `${base}/${i}.json`;
    this.$['rec-csv'].hidden = !D.grid[i];
    this.$['rec-csv'].href = `${base}/${i}.csv`;
    const img = this.$['rec-img'];
    img.hidden = !D.grid[i];
    this.$['rec-noimg'].hidden = !!D.grid[i];
    this._current = { thermal: D.grid[i] ? { url: `${base}/${i}.png` } : { msg: 'No temperature grid stored for this record.' } };
    if (D.grid[i]) img.src = `${base}/${i}.png`;
    this.$['rec-table'].replaceChildren(...ROWS.map(([k, label, f]) => {
      const tr = el('tr'), v = D[k][i];
      tr.append(el('th', null, label), el('td', null, v === null || v === undefined ? '--' : f(v)));
      return tr;
    }));
    for (const c of this._info.cams) this._showCam(c, D.frame[i], (D['src_' + c.name] || [])[i]);
    const rows = this.$['all-table'].rows;
    for (const r of rows) r.classList.toggle('sel', +r.dataset.i === i);
    if (this._zoomed) this._zoomShow(this._zoomed);
  }

  _showCam(c, stamp, status) {
    const fig = this.shadowRoot.getElementById('cam-' + c.name);
    const frame = fig.querySelector('.cam-frame'), img = fig.querySelector('img'), msg = fig.querySelector('.cam-msg');
    const seq = (+frame.dataset.seq || 0) + 1;
    frame.dataset.seq = seq;
    fig.querySelector('.cam-stale').hidden = status !== 'stale';
    const fail = text => {
      img.hidden = true; img.removeAttribute('src');
      frame.classList.add('empty'); msg.hidden = false; msg.textContent = text;
      this._current[c.name] = { msg: text };
    };
    if (!stamp || !status) return fail('No image for this record');
    if (status.startsWith('error')) return fail('Not captured');
    const url = `${this._cfg.url}/media/sources/${this._day}/${c.name}/${stamp}`;
    this._current[c.name] = { url };
    const next = new Image();
    next.onload = () => {
      if (frame.dataset.seq != seq) return;
      img.src = next.src; img.hidden = false;
      frame.classList.remove('empty'); msg.hidden = true;
      fig.style.setProperty('--ar', (next.naturalWidth / next.naturalHeight).toFixed(4));
    };
    next.onerror = () => {
      if (frame.dataset.seq != seq) return;
      const days = this._info.frames_days;
      fail(`No longer kept (${days} days)`);
      this._current[c.name] = { msg: `This image is no longer kept (camera images are kept ${days} days).` };
    };
    next.src = url + '?w=640';
  }

  _buildTable(force) {
    const tbl = this.$['all-table'], D = this._D;
    if (tbl.rows.length && !force) return;
    const head = el('tr');
    for (const h of ['Time', ...ROWS.map(r => r[1])]) head.append(el('th', null, h));
    const frag = document.createDocumentFragment();
    frag.append(head);
    D.t.forEach((tt, i) => {
      const tr = el('tr', 'clickable' + (i === this._sel ? ' sel' : ''));
      tr.dataset.i = i;
      tr.onclick = () => { this._select(i); this.shadowRoot.querySelector('.record').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); };
      tr.append(el('td', null, this._fmtTime.format(tt)));
      for (const [k, , f] of ROWS) {
        const v = D[k][i];
        tr.append(el('td', null, v === null || v === undefined ? '--' : f(v)));
      }
      frag.append(tr);
    });
    tbl.replaceChildren(frag);
  }

  // ── full-size overlay ──────────────────────────────────────────────────
  _zoomShow(name) {
    const cur = this._current[name] || { msg: 'No image for this record.' };
    const img = this.$['zoom-img'], msg = this.$['zoom-msg'], full = this.$['zoom-full'];
    const label = name === 'thermal' ? 'IR sky' : (this._info.cams.find(c => c.name === name) || {}).label;
    this.$['zoom-title'].textContent = `${label} · ${this._fmtTime.format(this._D.t[this._sel])}`;
    if (cur.url) {
      img.src = cur.url; img.alt = label; img.hidden = false; msg.hidden = true;
      full.href = cur.url; full.hidden = false;
    } else {
      img.hidden = true; img.removeAttribute('src');
      msg.textContent = cur.msg; msg.hidden = false; full.hidden = true;
    }
  }

  _zoomOpen(name) {
    this._zoomed = name;
    this._zoomShow(name);
    if (!this.$.zoom.open) this.$.zoom.showModal();
  }

  // ← / → step records while this card is on screen (not while typing or in an HA dialog).
  _key(e) {
    if (!this._D || this.$.body.hidden || !this.offsetParent || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const target = e.composedPath()[0];
    if (target && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable)) return;
    const inOwnDialog = this.$.zoom.open;
    if (!inOwnDialog && document.querySelector('home-assistant')?.shadowRoot?.querySelector('ha-more-info-dialog, ha-dialog[open]')) return;
    this._select(this._sel + (e.key === 'ArrowLeft' ? -1 : 1));
    e.preventDefault();
  }
}

if (!customElements.get('cloudroll-card')) {
  customElements.define('cloudroll-card', CloudRollCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: 'cloudroll-card', name: 'CloudRoll',
    description: 'CloudRoll IR sky data: day charts with the matching Allsky, Observatory and IR images.' });
  console.info(`%c CLOUDROLL-CARD %c v${VERSION} `, 'background:#4a90d9;color:#fff', 'background:#16213e;color:#a0c4ff');
}
})();
