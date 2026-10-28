"use strict";
// memecoin-agent dashboard. Vanilla JS, no build step. All data-derived text is
// inserted with textContent (token names come from public APIs and are untrusted).

const TOKEN = document.querySelector('meta[name="ui-token"]').content;
const state = {
  view: null,
  tab: "overview",
  range: "24h",
  chartMode: "line",
  equity: [],
  equityTable: false,
  scanFilter: "all",
  scanSearch: "",
  logFilter: "all",
  logAfter: 0,
  logs: [],
  configLoaded: false,
  backtest: null,
  failures: 0,
  lastTicks: -1,
  lastTickWall: Date.now(),  // browser time when the tick counter last advanced
};

// ---------------------------------------------------------------- helpers
const $ = (sel) => document.querySelector(sel);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined) continue;
    if (k === "text") el.textContent = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c) el.append(c);
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "X-UI-Token": TOKEN, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { return null; }
  return value;
}

const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function usd(v, { compact = false, sign = false } = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  let body;
  if (compact && a >= 1e6) body = `${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M`;
  else if (compact && a >= 1e4) body = `${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}K`;
  else body = nf2.format(a);
  const pre = v < 0 ? "−" : sign && v > 0 ? "+" : "";
  return `${pre}$${body}`;
}
function price(v) {
  if (!v) return "—";
  if (v >= 1) return `$${nf2.format(v)}`;
  // Memecoin prices are tiny: keep 4 significant figures.
  return `$${Number(v.toPrecision(4)).toString()}`;
}
function pct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const pre = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${pre}${Math.abs(v).toFixed(digits)}%`;
}
function num(v, digits = 2) {
  return v === null || v === undefined ? "—" : Number(v).toFixed(digits);
}
function delta(v, text) {
  // Direction is carried by arrow + sign, not color alone.
  const cls = v > 0 ? "delta-up" : v < 0 ? "delta-down" : "delta-flat";
  const arrow = v > 0 ? "▲ " : v < 0 ? "▼ " : "";
  return h("span", { class: cls, text: arrow + text });
}
function duration(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  if (minutes < 1) return "<1m";
  const total = Math.round(minutes);  // round once, so 119.6 min is "2h 0m", never "1h 60m"
  if (total < 60) return `${total}m`;
  const hrs = Math.floor(total / 60);
  if (hrs < 48) return `${hrs}h ${total % 60}m`;
  return `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
}
function ago(iso, now) {
  if (!iso) return "never";
  const secs = (new Date(now || Date.now()) - new Date(iso)) / 1000;
  if (secs < 5) return "just now";
  if (secs < 60) return `${Math.round(secs)}s ago`;
  return `${duration(secs / 60)} ago`;
}
function timeLabel(iso, withDate = false) {
  const d = new Date(iso);
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return withDate ? `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${t}` : t;
}
function safeUrl(u) {
  return typeof u === "string" && /^https:\/\//.test(u) ? u : null;
}
function shortMint(m) {
  return m && m.length > 12 ? `${m.slice(0, 4)}…${m.slice(-4)}` : m || "";
}
function tokenCell(symbol, mint, url) {
  const link = safeUrl(url);
  return h("div", { class: "token" },
    link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", class: "token-symbol", text: symbol || "?" })
      : h("span", { class: "token-symbol", text: symbol || "?" }),
    h("span", { class: "token-mint", title: mint, text: shortMint(mint) }));
}
function table(columns, rows, empty) {
  if (!rows.length) return h("div", { class: "empty", text: empty });
  return h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : null, scope: "col", text: c.label })))),
    h("tbody", {}, rows.map((r) => h("tr", {}, columns.map((c) => {
      const v = c.render(r);
      return h("td", { class: c.num ? "num" : c.cls || null }, v);
    })))));
}
function replace(el, ...children) {
  el.replaceChildren(...children.flat().filter(Boolean));
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = isError ? "toast error" : "toast";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

function confirmDialog(title, body, okLabel) {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  $("#confirm-ok").textContent = okLabel || "Confirm";
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
    dlg.returnValue = "cancel";
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- charts
function niceTicks(min, max, count = 4) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => span / st <= count) || 10 * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

function axisUsd(ticks) {
  const whole = ticks.every((t) => Math.abs(t - Math.round(t)) < 1e-9);
  return (v) => (whole ? `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : usd(v, { compact: true }));
}

// Equity chart, styled like a trading app: a readout above the plot (value, change since the start
// of the range) and either a smooth line or candles. The line is a monotone cubic through the last
// real sample of each time bucket, so it flows without inventing values: flat stretches stay flat
// and the curve never overshoots a real high or low. Hovering scrubs: the readout follows the
// pointer and the line is colored up to it, gray after.
const RANGE_LABELS = { "1h": "past hour", "24h": "past 24 hours", "7d": "past 7 days", all: "all time" };
const CANDLE_STEPS = [1, 5, 15, 30, 60, 120, 240, 720, 1440].map((min) => min * 60000);
let chartSeq = 0;

function smoothPath(pts) {
  const n = pts.length;
  const f = (v) => v.toFixed(1);
  if (n < 3) return pts.map((p, i) => `${i ? "L" : "M"}${f(p[0])},${f(p[1])}`).join("");
  const dx = [], sl = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0];
    sl[i] = dx[i] ? (pts[i + 1][1] - pts[i][1]) / dx[i] : 0;
  }
  // Fritsch-Carlson tangents: zero at flats and turning points, which is what keeps it monotone.
  const tan = [sl[0]];
  for (let i = 1; i < n - 1; i++) {
    tan[i] = sl[i - 1] * sl[i] <= 0 ? 0
      : (3 * (dx[i - 1] + dx[i])) / ((2 * dx[i] + dx[i - 1]) / sl[i - 1] + (dx[i] + 2 * dx[i - 1]) / sl[i]);
  }
  tan[n - 1] = sl[n - 2];
  let d = `M${f(pts[0][0])},${f(pts[0][1])}`;
  for (let i = 0; i < n - 1; i++) {
    const k = dx[i] / 3;
    d += `C${f(pts[i][0] + k)},${f(pts[i][1] + k * tan[i])} ${f(pts[i + 1][0] - k)},${f(pts[i + 1][1] - k * tan[i + 1])} `
      + `${f(pts[i + 1][0])},${f(pts[i + 1][1])}`;
  }
  return d;
}

function resample(xs, ys, buckets) {
  // The last real sample in each time bucket (plus the very first): exact values, few enough to flow.
  if (xs.length <= buckets) return xs.map((x, i) => [x, ys[i]]);
  const x0 = xs[0], span = xs[xs.length - 1] - x0 || 1;
  const out = [[xs[0], ys[0]]];
  let cur = -1;
  for (let i = 1; i < xs.length; i++) {
    const b = Math.min(buckets - 1, Math.floor(((xs[i] - x0) / span) * buckets));
    if (b === cur) out[out.length - 1] = [xs[i], ys[i]];
    else { out.push([xs[i], ys[i]]); cur = b; }
  }
  return out;
}

function toCandles(xs, ys, step) {
  // Each candle opens at the previous close, so consecutive candles connect.
  const out = [];
  let c = null;
  for (let i = 0; i < xs.length; i++) {
    const t = Math.floor(xs[i] / step) * step;
    if (!c || c.t !== t) {
      if (c) out.push(c);
      const o = c ? c.c : ys[i];
      c = { t, o, h: Math.max(o, ys[i]), l: Math.min(o, ys[i]), c: ys[i] };
    } else {
      c.h = Math.max(c.h, ys[i]); c.l = Math.min(c.l, ys[i]); c.c = ys[i];
    }
  }
  if (c) out.push(c);
  return out;
}

function trendOf(d) {
  return d > 0.005 ? "up" : d < -0.005 ? "down" : "flat";
}

function equityChart(container, points, { mode = "line", rangeLabel = "", emptyText = "No data yet" } = {}) {
  container.replaceChildren();
  if (!points || points.length < 2) {
    container.append(h("div", { class: "chart-empty", text: emptyText }));
    return;
  }
  const uid = ++chartSeq;
  const xs = points.map((p) => new Date(p[0]).getTime());
  const ys = points.map((p) => p[1]);
  const base = ys[0], last = ys[ys.length - 1];
  const withDate = (xs[xs.length - 1] - xs[0]) / 36e5 > 20;

  const W = Math.max(container.clientWidth, 280);
  const H = W < 560 ? 210 : 270;
  const m = { top: 10, right: 12, bottom: 26, left: 52 };
  const plotW = W - m.left - m.right, bottom = H - m.bottom;

  const candles = mode === "candles";
  const step = CANDLE_STEPS.find((st) => (xs[xs.length - 1] - xs[0]) / st <= 100) || CANDLE_STEPS[CANDLE_STEPS.length - 1];
  const bars = candles ? toCandles(xs, ys, step) : null;
  const line = candles ? null : resample(xs, ys, Math.max(30, Math.round(plotW / 12)));
  const lows = candles ? bars.map((b) => b.l) : line.map((p) => p[1]);
  const highs = candles ? bars.map((b) => b.h) : lows;
  const lo = Math.min(base, ...lows), hi = Math.max(base, ...highs);
  const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.005 || 1;
  const ticks = niceTicks(lo - pad, hi + pad, 4);
  const yMin = ticks[0], yMax = ticks[ticks.length - 1];
  const tx0 = candles ? bars[0].t : xs[0];
  const tx1 = candles ? bars[bars.length - 1].t + step : xs[xs.length - 1];
  const X = (t) => m.left + ((t - tx0) / Math.max(tx1 - tx0, 1)) * plotW;
  const Y = (v) => m.top + (1 - (v - yMin) / (yMax - yMin)) * (bottom - m.top);

  // Readout: value, then change since the start of the range (arrow + sign, not color alone).
  const readValue = h("div", { class: "readout-value" });
  const readDelta = h("div", { class: "readout-delta" });
  function setReadout(value, label) {
    const d = value - base;
    readValue.textContent = usd(value);
    replace(readDelta, delta(d, `${usd(d, { sign: true })} (${pct(base ? (d / base) * 100 : 0, 2)})`),
      h("span", { class: "muted", text: ` · ${label}` }));
  }

  const overall = trendOf(last - base);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", class: `trend-${overall}`,
    "aria-label": `Equity ${candles ? "candlestick" : "line"} chart, ${rangeLabel}: ${usd(base)} to ${usd(last)}` });
  for (const t of ticks) {
    svg.append(s("line", { class: "grid", x1: m.left, x2: W - m.right, y1: Y(t), y2: Y(t) }));
    svg.append(s("text", { x: m.left - 8, y: Y(t) + 4, "text-anchor": "end", text: axisUsd(ticks)(t) }));
  }
  const nX = Math.max(1, Math.min(5, Math.floor(plotW / 110)));
  for (let i = 0; i <= nX; i++) {
    const t = tx0 + ((tx1 - tx0) * i) / nX;
    svg.append(s("text", { x: X(t), y: H - 8, "text-anchor": i === 0 ? "start" : i === nX ? "end" : "middle",
      text: timeLabel(new Date(t).toISOString(), withDate) }));
  }
  // Where the range started: above it is a gain for the period, below it a loss.
  svg.append(s("line", { class: "ref-line", x1: m.left, x2: W - m.right, y1: Y(base), y2: Y(base) }));

  const cross = s("line", { class: "crosshair", y1: m.top, y2: bottom, visibility: "hidden" });
  const hoverDot = s("circle", { class: "series-dot", r: 4, visibility: "hidden" });
  let marks, show, hide;

  if (!candles) {
    const pts = line.map(([t, v]) => [X(t), Y(v)]);
    const d = smoothPath(pts);
    const area = `${d}L${pts[pts.length - 1][0].toFixed(1)},${bottom}L${pts[0][0].toFixed(1)},${bottom}Z`;
    const clip = s("rect", { x: 0, y: 0, width: W, height: H });
    svg.append(
      s("defs", {},
        s("linearGradient", { id: `eq-fill-${uid}`, x1: 0, y1: 0, x2: 0, y2: 1 },
          s("stop", { offset: "0%", class: "fill-top" }), s("stop", { offset: "100%", class: "fill-bottom" })),
        s("clipPath", { id: `eq-clip-${uid}` }, clip)),
      s("path", { class: "series-line series-rest", d }),
      s("g", { "clip-path": `url(#eq-clip-${uid})` },
        s("path", { class: "series-wash", d: area, fill: `url(#eq-fill-${uid})` }),
        s("path", { class: "series-line", d })),
      s("circle", { class: "series-dot", cx: pts[pts.length - 1][0], cy: pts[pts.length - 1][1], r: 4 }));
    marks = line;
    show = (i) => {
      const [t, v] = line[i];
      const cx = X(t), cy = Y(v);
      clip.setAttribute("width", cx);
      svg.setAttribute("class", `trend-${trendOf(v - base)}`);
      hoverDot.setAttribute("cx", cx); hoverDot.setAttribute("cy", cy); hoverDot.setAttribute("visibility", "visible");
      setReadout(v, timeLabel(new Date(t).toISOString(), withDate));
      return cx;
    };
    hide = () => { clip.setAttribute("width", W); svg.setAttribute("class", `trend-${overall}`); };
  } else {
    const slot = plotW / Math.max(1, (tx1 - tx0) / step);
    const bw = Math.max(1, Math.min(12, slot - 2)); // a 2px surface gap between neighbours
    const groups = bars.map((b) => {
      const cx = X(b.t + step / 2);
      const top = Y(Math.max(b.o, b.c)), height = Math.max(1, Math.abs(Y(b.o) - Y(b.c)));
      return s("g", { class: `candle candle-${trendOf(b.c - b.o)}` },
        s("line", { class: "candle-wick", x1: cx, x2: cx, y1: Y(b.h), y2: Y(b.l) }),
        s("rect", { class: "candle-body", x: cx - bw / 2, y: top, width: bw, height, rx: Math.min(1.5, bw / 4) }));
    });
    svg.append(...groups);
    marks = bars.map((b) => [b.t + step / 2, b.c]);
    let lit = null;
    show = (i) => {
      const b = bars[i];
      if (lit) lit.classList.remove("hover");
      lit = groups[i]; lit.classList.add("hover");
      const range = `${timeLabel(new Date(b.t).toISOString(), withDate)}–${timeLabel(new Date(b.t + step).toISOString())}`;
      setReadout(b.c, `${range} · O ${usd(b.o)} H ${usd(b.h)} L ${usd(b.l)}`);
      return X(b.t + step / 2);
    };
    hide = () => { if (lit) lit.classList.remove("hover"); lit = null; };
  }

  // Hover / keyboard layer: the crosshair snaps to the nearest point or candle.
  const hit = s("rect", { x: m.left, y: 0, width: plotW, height: H, fill: "transparent" });
  svg.append(cross, hoverDot, hit);
  const readout = h("div", { class: "chart-readout" }, readValue, readDelta);
  container.append(readout, svg);
  container.tabIndex = 0;
  setReadout(last, rangeLabel);
  let idx = marks.length - 1;
  function focusOn(i) {
    idx = Math.max(0, Math.min(marks.length - 1, i));
    const cx = show(idx);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
  }
  function release() {
    hide();
    cross.setAttribute("visibility", "hidden"); hoverDot.setAttribute("visibility", "hidden");
    setReadout(last, rangeLabel);
  }
  function nearest(clientX) {
    const r = svg.getBoundingClientRect();
    const t = tx0 + ((((clientX - r.left) * W) / r.width - m.left) / plotW) * (tx1 - tx0);
    let a = 0, z = marks.length - 1;
    while (z - a > 1) { const mid = (a + z) >> 1; if (marks[mid][0] < t) a = mid; else z = mid; }
    return t - marks[a][0] < marks[z][0] - t ? a : z;
  }
  hit.addEventListener("pointermove", (e) => focusOn(nearest(e.clientX)));
  hit.addEventListener("pointerleave", release);
  container.onfocus = () => focusOn(idx);
  container.onblur = release;
  container.onkeydown = (e) => {
    const keys = { ArrowLeft: idx - 1, ArrowRight: idx + 1, Home: 0, End: marks.length - 1 };
    if (e.key in keys) { focusOn(keys[e.key]); e.preventDefault(); }
  };
}

function barChart(container, items, { valueFmt = (v) => String(v) } = {}) {
  container.replaceChildren();
  if (!items.length) { container.append(h("div", { class: "chart-empty", text: "No trades" })); return; }
  const W = Math.max(container.clientWidth, 280);
  const labelW = Math.min(150, W * 0.38), barH = 18, gap = 12, valueW = 44;
  const H = items.length * (barH + gap) - gap + 4;
  const max = Math.max(...items.map((i) => i.value), 1);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Bar chart" });
  const tip = h("div", { class: "tooltip", hidden: true });
  items.forEach((it, i) => {
    const y = i * (barH + gap) + 2;
    const len = Math.max(((W - labelW - valueW) * it.value) / max, 2);
    const x0 = labelW, x1 = labelW + len, r = Math.min(4, len / 2);
    // Rounded data end, square at the baseline.
    const path = `M${x0},${y}H${x1 - r}Q${x1},${y} ${x1},${y + r}V${y + barH - r}Q${x1},${y + barH} ${x1 - r},${y + barH}H${x0}Z`;
    const bar = s("path", { class: "series-bar", d: path });
    const hitbox = s("rect", { x: 0, y: y - gap / 2, width: W, height: barH + gap, fill: "transparent", tabindex: 0 });
    svg.append(
      s("text", { class: "bar-label", x: labelW - 10, y: y + barH / 2 + 4, "text-anchor": "end", text: it.label }),
      bar,
      s("text", { class: "bar-value", x: x1 + 6, y: y + barH / 2 + 4, text: valueFmt(it.value) }),
      hitbox);
    const show = () => {
      bar.classList.add("hover");
      replace(tip, h("div", { class: "tooltip-value", text: valueFmt(it.value) }),
        h("div", { class: "tooltip-label", text: it.detail || it.label }));
      tip.hidden = false;
      const scale = container.clientWidth / W;
      tip.style.left = `${Math.min(x1 * scale + 8, container.clientWidth - tip.offsetWidth)}px`;
      tip.style.top = `${Math.max(y * scale - tip.offsetHeight - 4, 0)}px`;
    };
    const hide = () => { bar.classList.remove("hover"); tip.hidden = true; };
    hitbox.addEventListener("pointerenter", show);
    hitbox.addEventListener("pointerleave", hide);
    hitbox.addEventListener("focus", show);
    hitbox.addEventListener("blur", hide);
  });
  container.append(svg, tip);
}

// ---------------------------------------------------------------- header
function renderHeader(v) {
  const badge = $("#mode-badge");
  badge.textContent = v.synthetic ? "SIMULATED" : v.mode.toUpperCase();
  badge.className = `badge ${v.mode === "live" ? "badge-live" : v.synthetic ? "badge-sim" : ""}`;
  document.title = `${usd(v.equity)} · Memecoin Agent`;

  // The view is only republished by the agent thread, so judge staleness by wall-clock time
  // since the tick counter last moved. Network-heavy ticks can take a while; allow slack.
  const sinceTickS = (Date.now() - state.lastTickWall) / 1000;
  const stale = sinceTickS > (3 * v.tick_seconds) / (v.speed || 1) + 30;
  let cls = "ok", label = "Trading";
  if (stale) { cls = "bad"; label = "Agent stalled"; }
  else if (v.kill_switch) { cls = "bad"; label = "Kill switch on"; }
  else if (v.entries_paused) { cls = "paused"; label = "Entries paused"; }
  else if (v.entry_block) { cls = "paused"; label = "Entries blocked"; }
  const pill = $("#status-pill");
  pill.className = `status-pill ${cls}`;
  pill.title = v.entry_block || "";
  replace(pill, h("span", { class: "dot", "aria-hidden": "true" }), label);

  const bits = [`Last tick ${v.last_tick_at ? ago(new Date(state.lastTickWall).toISOString()) : "pending"}`];
  if (v.synthetic) bits.push(`sim time ${timeLabel(v.now, true)} (${v.speed}× speed)`);
  if (v.feed) bits.push(v.feed.connected ? `launch feed connected · ${v.feed.buffered} tokens` : "launch feed reconnecting");
  if (v.recorder && v.recorder.active) bits.push(`recording · ${v.recorder.tracked} tokens`);
  if (v.gmgn) bits.push(`GMGN ${[v.gmgn.discovery && "discovery", v.gmgn.safety && "safety"].filter(Boolean).join(" + ")}`);
  if (v.larpcheck) {
    bits.push(v.larpcheck.on_unchecked === "check"
      ? `LarpCheck gate · ${v.larpcheck.checks_left_today} checks left today` : "LarpCheck gate on");
  }
  if (v.telegram) bits.push(v.telegram.state === "ok" ? `Telegram ${v.telegram.bot || "bot"}` : `Telegram: ${v.telegram.state}`);
  if (v.tick_errors) bits.push(`${v.tick_errors} tick error${v.tick_errors > 1 ? "s" : ""}`);
  $("#meta").textContent = bits.join(" · ");

  const pause = $("#btn-pause");
  pause.textContent = v.entries_paused ? "Resume entries" : "Pause entries";
  pause.setAttribute("aria-pressed", v.entries_paused ? "true" : "false");
  $("#btn-close-all").disabled = !v.positions.length;
  const passed = v.scan.candidates.filter((c) => c.passed).length;
  $("#tab-scanner-count").textContent = v.scan.candidates.length ? `${passed}/${v.scan.candidates.length}` : "";

  const banner = $("#banner");
  if (state.failures >= 2) {
    banner.hidden = false; banner.className = "banner";
    banner.textContent = "Lost connection to the agent. Retrying… (is the process still running?)";
  } else if (v.mode === "live") {
    banner.hidden = false; banner.className = "banner warn";
    banner.textContent = "LIVE TRADING: buttons on this page move real funds.";
  } else banner.hidden = true;
}

// ---------------------------------------------------------------- overview
function tile(label, value, sub) {
  return h("div", { class: "card tile" },
    h("div", { class: "tile-label", text: label }),
    h("div", { class: "tile-value" }, value),
    sub ? h("div", { class: "tile-sub" }, sub) : null);
}

function renderOverview(v) {
  $("#hero-value").textContent = usd(v.equity);
  const ret = v.baseline_equity ? (v.equity / v.baseline_equity - 1) * 100 : 0;
  replace($("#hero-delta"), delta(ret, `${pct(ret, 2)} `),
    h("span", { class: "muted", text: `vs ${usd(v.baseline_equity)} ${v.mode === "live" ? "at first sample" : "start"}` }));

  const todayPct = v.day_start_equity ? (v.today_pnl / v.day_start_equity) * 100 : 0;
  const sess = v.session;
  replace($("#tiles"),
    tile("Today", delta(v.today_pnl, usd(v.today_pnl, { sign: true })), pct(todayPct, 2)),
    tile("Realized PnL", delta(v.realized_pnl, usd(v.realized_pnl, { sign: true })), `today ${usd(v.today_realized, { sign: true })}`),
    tile("Unrealized", delta(v.unrealized_pnl, usd(v.unrealized_pnl, { sign: true })), `${v.positions.length} open`),
    tile("Cash", usd(v.cash), `${usd(v.positions_value)} in positions`),
    tile("Win rate", sess.win_rate_pct === null ? "—" : `${sess.win_rate_pct.toFixed(0)}%`,
      `${sess.wins}/${sess.round_trips} round trips this session`));

  const free = v.positions.length - v.risk.open_positions;  // dust, runners, long-term and manual don't take a slot
  $("#positions-count").textContent = `${v.risk.open_positions} / ${v.risk.max_open_positions}${free ? ` + ${free} outside the limit` : ""}`;
  replace($("#positions"), table([
    { label: "Token", render: (p) => h("div", { class: "token-row" }, tokenCell(p.symbol, p.mint, p.url), positionPills(p)) },
    { label: "Value", num: true, render: (p) => usd(p.value_usd) },
    { label: "PnL", num: true, render: (p) => delta(p.pnl_pct, pct(p.pnl_pct)) },
    { label: "Peak", num: true, render: (p) => pct(p.peak_pct) },
    { label: "Plan", cls: "reason", render: (p) => planCell(p, v) },
    { label: "Liquidity", num: true, render: (p) => pct(p.liquidity_change_pct, 0) },
    { label: "LarpCheck", render: larpCell },
    { label: "Held", num: true, render: (p) => h("span", { title: p.stage === "runner" ? "Runners have no time stop" : `Time stop at ${duration(p.max_hold_minutes)}` }, duration(p.held_minutes)) },
    { label: "", render: (p) => positionActions(p) },
  ], v.positions, "No open positions"));
  renderLarpReasons(v.positions);

  const r = v.risk;
  const lossPct = r.daily_loss_limit_usd ? (r.daily_loss_usd / r.daily_loss_limit_usd) * 100 : 0;
  const meter = (label, value, frac, severity = false) => {
    const sev = !severity ? "" : frac >= 0.85 ? "crit" : frac >= 0.6 ? "warn" : "";
    const fill = h("div", { class: "meter-fill" });
    fill.style.width = `${Math.min(100, Math.max(0, frac * 100)).toFixed(1)}%`;
    return h("div", { class: "meter" },
      h("div", { class: "meter-head" }, h("span", { text: label }),
        h("span", { class: "meter-value", text: (sev === "crit" ? "⚠ " : "") + value })),
      h("div", { class: `meter-track ${sev}`, role: "meter", "aria-label": label,
        "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(frac * 100) }, fill));
  };
  let entry = "✓ Opening new positions when signals appear";
  if (v.kill_switch) entry = "✕ Kill switch file present — no new entries (exits still run)";
  else if (v.entries_paused) entry = "‖ Paused from the dashboard — exits still run";
  else if (v.entry_block) entry = `‖ New entries blocked: ${v.entry_block}`;
  replace($("#risk"),
    meter("Daily loss limit", `${usd(r.daily_loss_usd)} of ${usd(r.daily_loss_limit_usd)} (${r.daily_loss_limit_pct}%)`, lossPct / 100, true),
    meter("Open positions", `${r.open_positions} of ${r.max_open_positions}`, r.open_positions / r.max_open_positions),
    meter("Entries in the last hour", `${r.entries_last_hour} of ${r.max_entries_per_hour}`, r.entries_last_hour / r.max_entries_per_hour),
    h("div", { class: "entry-state", text: entry }));

  replace($("#recent-trades"), tradesTable(v.trades.slice(0, 8), false));
}

const LANE_LABELS = { copy: "Copy", longterm: "Long-term", manual: "Manual", launch: "Launch" };
const CHAIN_LABELS = { solana: "SOL", ethereum: "ETH", bsc: "BSC", base: "Base", arbitrum: "ARB", robinhood: "Robinhood" };

function positionPills(p) {
  const pills = [];
  if (p.chain && p.chain !== "solana") pills.push(h("span", { class: "pill pill-chain", text: CHAIN_LABELS[p.chain] || p.chain }));
  if (LANE_LABELS[p.lane]) pills.push(h("span", { class: "pill", text: LANE_LABELS[p.lane] }));
  if (p.dust) pills.push(h("span", { class: "pill pill-dust", title: "Kept after a LarpCheck exit; left alone until you close it", text: "dust" }));
  if (p.stage === "runner") pills.push(h("span", { class: "pill pill-runner", title: "Initials and half the profit are out; the rest runs", text: "runner" }));
  if (p.stage === "dip watch") pills.push(h("span", { class: "pill pill-dip", title: p.review_note || "", text: "dip watch" }));
  return pills;
}

function planCell(p, v) {
  const next = p.next_review_at ? ` · next check ${timeLabel(p.next_review_at)}` : "";
  let text, tip = p.review_note || null;
  if (!p.managed) text = "You trade it: no automatic sells";
  else if (p.dust) text = "None (dust)";
  else if (p.style !== "let_it_run") text = `Exit below ${price(p.stop_price_usd)}${p.trailing_armed ? " ↟" : ""} · take profit ${p.tp_hit}/${p.tp_total}`;
  else if (p.stage === "runner") {
    text = `Runner: ${pct(-(p.runner_below_peak_pct || 0), 0)} from its peak; sells only ${v.exits.runner_drawdown_pct}% below it${next}`;
  } else if (p.stage === "dip watch") {
    text = `Dip watch: held while it consolidates; stop ${price(p.stop_price_usd)}${next}`;
  } else {
    text = `Initials out at ${price(p.secure_price_usd)} (+${v.exits.secure_at_pct}%) · stop ${price(p.stop_price_usd)}`;
  }
  return h("span", { title: tip }, text, p.review_note && p.stage !== "securing"
    ? h("div", { class: "muted small", text: p.review_note }) : null);
}

function positionActions(p) {
  const sell = (fraction, label) => h("button", { class: "btn btn-small", type: "button",
    title: `Sell ${Math.round(fraction * 100)}% now`, onclick: () => sellPart(p, fraction), text: label });
  const toggle = h("label", { class: "agent-toggle", title: p.managed ? "The agent's exit rules apply" : "Only you sell it" },
    h("input", { type: "checkbox", class: "switch", checked: p.managed || null,
      onchange: (e) => control("set_managed", { mint: p.mint, managed: e.target.checked }) }),
    h("span", { text: "Agent" }));
  return h("div", { class: "row-actions" }, toggle, sell(0.25, "25%"), sell(0.5, "50%"),
    h("button", { class: "btn btn-small btn-danger", type: "button", onclick: () => closePosition(p), text: "Sell all" }));
}

async function sellPart(p, fraction) {
  const live = state.view && state.view.mode === "live";
  const ok = await confirmDialog(`Sell ${Math.round(fraction * 100)}% of ${p.symbol}?`,
    `About ${usd(p.value_usd * fraction)} at market now${live ? " — a real on-chain sale" : ""}.`, "Sell");
  if (ok) control("sell", { mint: p.mint, fraction });
}

const LARP_BAD = new Set(["LARP", "SCAM", "LIKELY LARP"]);

function larpCell(p) {
  const l = p.larp;
  if (!l) return h("span", { class: "muted", text: "—" });
  const bad = LARP_BAD.has(l.verdict) || l.coin_stamp === "RIGGED";
  const label = l.verdict + (l.coin_stamp ? ` · ${l.coin_stamp}` : "");
  const tip = [l.summary, ...((l.reasoning && l.reasoning.risks) || []).map((r) => `• ${r.title}`)].filter(Boolean).join("\n");
  const link = safeUrl(l.url);
  const badge = h("span", { class: `verdict ${bad ? "v-unsafe" : "v-pass"}`, title: tip || null },
    h("span", { class: "verdict-icon", "aria-hidden": "true", text: bad ? "✗" : "✓" }), h("span", { text: label }));
  return link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", class: "plain-link" }, badge) : badge;
}

// LarpCheck's reasoning for every held coin it flagged: why, the risk findings with evidence, what to watch.
function renderLarpReasons(positions) {
  const flagged = positions.filter((p) => p.larp && p.larp.reasoning);
  replace($("#larp-reasons"), flagged.map((p) => {
    const l = p.larp, r = l.reasoning;
    return h("details", { class: "larp-reason", open: true },
      h("summary", {}, h("strong", { text: `Why LarpCheck flagged ${p.symbol}` }),
        h("span", { class: "muted", text: ` · ${l.verdict}${l.coin_stamp ? " · " + l.coin_stamp : ""} · ${l.trigger || "flagged"}${r.source === "full report" ? " · full report" : ""}` })),
      r.summary ? h("p", { text: r.summary }) : null,
      r.claim ? h("p", { class: "muted", text: `Claim: ${r.claim} (${r.claim_status || "unverified"})` }) : null,
      r.coin_line ? h("p", { class: "muted", text: `Coin: ${r.coin_line}` }) : null,
      r.risks && r.risks.length ? h("ul", {}, r.risks.map((f) => h("li", {},
        f.kind === "missing" ? h("span", { class: "pill", text: "missing" }) : null, " ", h("strong", { text: f.title }),
        f.detail ? ` ${f.detail}` : "",
        safeUrl(f.evidence) ? h("a", { href: f.evidence, target: "_blank", rel: "noopener noreferrer", class: "pill", text: "evidence" }) : null))) : null,
      r.watch ? h("p", { class: "muted", text: `What would change the picture: ${r.watch}` }) : null,
      safeUrl(l.url) ? h("a", { href: l.url, target: "_blank", rel: "noopener noreferrer", text: "Full check on LarpCheck →" }) : null);
  }));
}

function tradesTable(trades, full) {
  const cols = [
    { label: "Time", cls: "nowrap", render: (t) => timeLabel(t.time, true) },
    { label: "Side", render: (t) => h("span", { class: `side side-${t.side}`, text: t.side.toUpperCase() }) },
    { label: "Token", render: (t) => tokenCell(t.symbol, t.mint, null) },
    { label: "Amount", num: true, render: (t) => usd(t.usd_value) },
    { label: "Price", num: true, render: (t) => price(t.price_usd) },
    { label: "Realized", num: true, render: (t) => (t.side === "sell" ? delta(t.realized_pnl_usd, usd(t.realized_pnl_usd, { sign: true })) : "") },
    { label: "Reason", cls: "reason", render: (t) => t.reason },
  ];
  if (full) {
    cols.splice(5, 0, { label: "Fee", num: true, render: (t) => usd(t.fee_usd) });
    cols.push({ label: "Tx", render: (t) => (t.signature
      ? h("a", { href: `https://solscan.io/tx/${encodeURIComponent(t.signature)}`, target: "_blank", rel: "noopener noreferrer", text: shortMint(t.signature) })
      : "") });
  }
  return table(cols, trades, "No trades yet");
}

async function closePosition(p) {
  const live = state.view && state.view.mode === "live";
  const ok = await confirmDialog(`Close ${p.symbol}?`,
    `Sell the entire position (about ${usd(p.value_usd)}) at market now${live ? " — this is a real on-chain sale" : ""}.`, "Sell now");
  if (ok) control("close", { mint: p.mint });
}

// ---------------------------------------------------------------- scanner
function renderScanner(v) {
  const all = v.scan.candidates;
  const q = state.scanSearch.trim().toLowerCase();
  const rows = all.filter((c) => (state.scanFilter === "all" || (state.scanFilter === "passed") === c.passed)
    && (!q || (c.symbol || "").toLowerCase().includes(q) || c.mint.toLowerCase().includes(q)));
  const passed = all.filter((c) => c.passed).length;
  $("#scan-summary").textContent = v.scan.at
    ? `Last scan ${ago(v.scan.at, v.now)} · ${all.length} candidates · ${passed} passed filters · scans every ${duration(v.scan.every_s / 60)}`
    : "No scan yet";
  replace($("#scan-table"), table([
    { label: "Token", render: (c) => tokenCell(c.symbol, c.mint, c.url) },
    { label: "Score", num: true, render: (c) => num(c.score) },
    { label: "Liquidity", num: true, render: (c) => usd(c.liquidity_usd, { compact: true }) },
    { label: "Mkt cap", num: true, render: (c) => usd(c.market_cap_usd, { compact: true }) },
    { label: "5m", num: true, render: (c) => pct(c.price_change_m5) },
    { label: "1h", num: true, render: (c) => pct(c.price_change_h1) },
    { label: "Buys/sells 1h", num: true, render: (c) => num(c.buy_sell_ratio_h1) },
    { label: "Age", num: true, render: (c) => duration(c.age_minutes) },
    { label: "Verdict", cls: "reason", render: verdict },
  ], rows, all.length ? "No candidates match" : "Waiting for the first scan…"));
}

function verdict(c) {
  let cls = "v-fail", icon = "✗", text;
  let evidence = null;
  if (c.outcome === "bought") { cls = "v-buy"; icon = "●"; text = "Bought"; }
  else if (c.outcome === "unsafe: larpcheck check running") { cls = "v-pass"; icon = "…"; text = "Waiting for LarpCheck verdict"; }
  else if (c.outcome.startsWith("waiting: ")) { cls = "v-pass"; icon = "…"; text = "Waiting: " + c.outcome.slice(9); }
  else if (/^unsafe: larpcheck( unavailable|:| required:)/.test(c.outcome)) {
    // No verdict to go on: never bought (require_verdict).
    icon = "–"; text = "No LarpCheck verdict: " + c.outcome.replace(/^unsafe: larpcheck( required)?:? ?/, "");
  }
  else if (c.outcome.startsWith("unsafe")) {
    cls = "v-unsafe"; icon = "✗"; text = c.outcome.replace(/^unsafe: /, "Unsafe: ");
    const m = text.match(/\s\((https:\/\/[^\s)]+)\)$/);  // LarpCheck appends its evidence page
    if (m && safeUrl(m[1])) { text = text.slice(0, m.index); evidence = m[1]; }
  }
  else if (c.outcome) { text = c.outcome.charAt(0).toUpperCase() + c.outcome.slice(1); }
  else if (c.passed) { cls = "v-pass"; icon = "✓"; text = "Passed filters"; }
  else {
    text = c.reasons.slice(0, 2).join(" · ");
    if (c.reasons.length > 2) text += ` · +${c.reasons.length - 2} more`;
  }
  const launch = c.lane === "launch"
    ? h("span", { class: "pill", title: `Launch lane: ${usd(c.volume_h1, { compact: true })} traded in its first minutes`, text: "Launch" }) : null;
  const kind = c.kind
    ? h("span", { class: "pill", title: c.kind === "meme" ? "Meme: LarpCheck's free lookup only" : "Tech: full LarpCheck gate",
      text: c.kind === "meme" ? "Meme" : "Tech" }) : null;
  return h("span", { class: `verdict ${cls}`, title: c.reasons.join("\n") || null },
    h("span", { class: "verdict-icon", "aria-hidden": "true", text: icon }), launch, kind, h("span", { text }),
    evidence ? h("a", { href: evidence, target: "_blank", rel: "noopener noreferrer", class: "pill", text: "evidence" }) : null);
}

// ---------------------------------------------------------------- manual trades
const trade = { results: [], picked: null, searching: false };

function renderTrade(v) {
  $("#trade-cash").textContent = `${usd(v.cash)} paper cash`;
  const off = !v.manual_trading;
  $("#lookup-go").disabled = off;
  $("#trade-note").textContent = off
    ? (v.mode === "live" ? "Manual trades are paper-only; this agent is trading live." : "Token search isn't available in this mode.")
    : "Any chain DexScreener covers (Solana, Ethereum, BSC, Base, Robinhood…). Buys fill at the current price with paper fees. "
      + "Searching by name? Fake copies share names: check the contract before you buy.";
  $("#buy-managed-note").textContent = $("#buy-managed").checked
    ? `On: the agent's exits apply (initials and half the profit out at +${v.exits.secure_at_pct}%, the rest runs; stop at −${v.exits.hard_stop_pct}%).`
    : "Off: the agent never sells it; you sell from Open positions or Telegram.";
}

function renderLookup() {
  const rows = trade.results;
  replace($("#lookup-results"), trade.searching ? h("div", { class: "empty", text: "Searching…" }) : table([
    { label: "Token", render: (t) => h("div", { class: "token-row" }, tokenCell(t.symbol, t.mint, t.url),
      h("span", { class: "pill pill-chain", text: CHAIN_LABELS[t.chain] || t.chain })) },
    { label: "Name", cls: "reason", render: (t) => t.name },
    { label: "Price", num: true, render: (t) => price(t.price_usd) },
    { label: "Liquidity", num: true, render: (t) => usd(t.liquidity_usd, { compact: true }) },
    { label: "Mkt cap", num: true, render: (t) => usd(t.market_cap_usd, { compact: true }) },
    { label: "24h vol", num: true, render: (t) => usd(t.volume_h24, { compact: true }) },
    { label: "24h", num: true, render: (t) => pct(t.price_change_h24, 0) },
    { label: "Age", num: true, render: (t) => duration(t.age_minutes) },
    { label: "", render: (t) => h("button", { class: "btn btn-small btn-primary", type: "button", onclick: () => pickToken(t), text: "Select" }) },
  ], rows, "Paste a contract address or a name, then Search"));
}

async function lookup(q) {
  trade.searching = true; renderLookup();
  try {
    const res = await api(`/api/lookup?q=${encodeURIComponent(q)}`);
    trade.results = res.results || [];
    if (trade.results.length === 1) pickToken(trade.results[0]);
  } catch (e) { toast(e.message, true); trade.results = []; }
  trade.searching = false; renderLookup();
}

function pickToken(t) {
  trade.picked = t;
  $("#buy-card").hidden = false;
  $("#buy-title").textContent = `Buy ${t.symbol} on ${CHAIN_LABELS[t.chain] || t.chain}`;
  const link = $("#buy-link");
  if (safeUrl(t.url)) { link.href = t.url; link.textContent = "DexScreener →"; } else { link.removeAttribute("href"); link.textContent = ""; }
  const fact = (label, value) => h("div", { class: "fact" }, h("span", { class: "muted", text: label }), h("strong", { text: value }));
  replace($("#buy-facts"),
    fact("Price", price(t.price_usd)), fact("Liquidity", usd(t.liquidity_usd, { compact: true })),
    fact("Market cap", usd(t.market_cap_usd, { compact: true })), fact("24h volume", usd(t.volume_h24, { compact: true })),
    fact("24h buys / sells", `${t.buys_h24} / ${t.sells_h24}`), fact("Pool age", duration(t.age_minutes)),
    fact("Contract", shortMint(t.mint)));
  replace($("#buy-warnings"));
  $("#buy-status").textContent = "";
  $("#buy-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function checkPicked() {
  const t = trade.picked;
  if (!t) return;
  $("#buy-status").textContent = "Checking…";
  try {
    const res = await api("/api/control", { method: "POST", body: JSON.stringify({ action: "manual_check", chain: t.chain,
      address: t.mint, usd: Number($("#buy-usd").value) || 50 }) });
    const w = res.warnings || [];
    replace($("#buy-warnings"), w.length ? w.map((x) => h("li", { class: "warn", text: x }))
      : h("li", { class: "ok", text: "No honeypot or tax flags found (not a guarantee)." }));
    $("#buy-status").textContent = "";
  } catch (e) { $("#buy-status").textContent = ""; toast(e.message, true); }
}

async function buyPicked() {
  const t = trade.picked;
  const amount = Number($("#buy-usd").value);
  if (!t || !(amount > 0)) { toast("Enter an amount in USD", true); return; }
  const managed = $("#buy-managed").checked;
  const ok = await confirmDialog(`Paper-buy ${usd(amount)} of ${t.symbol}?`,
    `On ${t.chain} at about ${price(t.price_usd)}. ${managed ? "The agent will manage the exit." : "Only you will sell it."}`, "Buy");
  if (!ok) return;
  await control("manual_buy", { chain: t.chain, address: t.mint, usd: amount, managed });
}

// ---------------------------------------------------------------- copy lane and long-term lane
function walletLink(w) {
  const chain = w.chain === "sol" ? "sol" : w.chain;
  return h("a", { href: `https://gmgn.ai/${encodeURIComponent(chain)}/address/${encodeURIComponent(w.address)}`,
    target: "_blank", rel: "noopener noreferrer", title: w.address, text: w.short });
}

function renderLanes(v) {
  const c = v.copy;
  if (!c) {
    $("#copy-status").textContent = "off";
    $("#copy-hint").textContent = "The copy lane needs a GMGN API key (GMGN_API_KEY). Without it the agent buys no low caps.";
    replace($("#copy-wallets"));
  } else {
    $("#copy-status").textContent = `${c.wallets.length} wallet(s) on ${c.chains.join(", ")} · vetted ${c.last_refresh ? ago(new Date(c.last_refresh * 1000).toISOString()) : "soon"}`
      + (c.errors ? ` · ${c.errors} error(s)` : "");
    $("#copy-hint").textContent = "Wallets with a 10–30% win rate over 30 days, over 2 months old, with real losses. "
      + "A low cap is bought only when one of them buys it (never transfers in, never its own launch)."
      + (c.last_error ? ` Last error: ${c.last_error}` : "");
    replace($("#copy-wallets"), table([
      { label: "Chain", render: (w) => h("span", { class: "pill pill-chain", text: w.chain }) },
      { label: "Wallet", render: (w) => h("div", { class: "token" }, walletLink(w), w.pinned ? h("span", { class: "pill", text: "yours" }) : null) },
      { label: "Win rate", num: true, render: (w) => (w.win_rate === null ? "—" : `${(w.win_rate * 100).toFixed(0)}%`) },
      { label: "30d return", num: true, render: (w) => (w.pnl === null ? "—" : delta(w.pnl, pct(w.pnl * 100, 0))) },
      { label: "Trades", num: true, render: (w) => w.trades },
      { label: "Losses", num: true, render: (w) => w.losses },
      { label: "Age", num: true, render: (w) => (w.age_days === null ? "—" : `${Math.round(w.age_days)}d`) },
      { label: "Last buy", cls: "reason", render: (w) => (w.last_buy_at ? `${w.last_buy_symbol || "?"} · ${ago(new Date(w.last_buy_at * 1000).toISOString())}` : "—") },
      { label: "Found in", cls: "reason", render: (w) => w.source },
    ], c.wallets, "Finding wallets… (the first vetting pass takes a few minutes)"));
  }
  replace($("#lane-outcomes"), table([
    { label: "Time", cls: "nowrap", render: (o) => timeLabel(o.at, true) },
    { label: "Lane", render: (o) => h("span", { class: "pill", text: LANE_LABELS[o.lane] || o.lane }) },
    { label: "Token", render: (o) => h("div", { class: "token-row" }, tokenCell(o.symbol, o.mint, null),
      h("span", { class: "pill pill-chain", text: CHAIN_LABELS[o.chain] || o.chain })) },
    { label: "Outcome", cls: "reason", render: (o) => o.outcome },
  ], v.lane_outcomes || [], "Nothing yet"));

  const lt = v.longterm;
  if (!lt) {
    $("#lt-status").textContent = "off";
    replace($("#lt-table"));
    return;
  }
  const ok = lt.tokens.filter((t) => t.ok).length;
  $("#lt-status").textContent = lt.scanning ? "scanning…"
    : lt.last_scan ? `${ok} qualify of ${lt.tokens.length} · scanned ${ago(new Date(lt.last_scan * 1000).toISOString())}` : "first scan soon";
  $("#lt-hint").textContent = "Over a year old (your watchlist skips the age rule), far below its high but wearing down gradually, "
    + "still traded every day, holders with $1,000+ still in. Bought with its own budget; no time stop.";
  replace($("#lt-table"), table([
    { label: "Token", render: (t) => h("div", { class: "token-row" }, tokenCell(t.symbol, t.address, t.facts.url),
      h("span", { class: "pill pill-chain", text: CHAIN_LABELS[t.chain] || t.chain }), t.watchlist ? h("span", { class: "pill", text: "watchlist" }) : null) },
    { label: "Age", num: true, render: (t) => (t.facts.age_days !== undefined ? `${t.facts.age_days}d` : "—") },
    { label: "Below high", num: true, render: (t) => (t.facts.below_high_pct !== undefined ? `${t.facts.below_high_pct}%` : "—") },
    { label: "vs 1–3 mo ago", num: true, render: (t) => (t.facts.vs_1_3_months_pct !== undefined ? pct(t.facts.vs_1_3_months_pct, 0) : "—") },
    { label: "24h vol", num: true, render: (t) => usd(t.facts.volume_h24_usd, { compact: true }) },
    { label: "Buys 24h", num: true, render: (t) => (t.facts.buys_h24 ?? "—") },
    { label: "Holders (large)", num: true, render: (t) => (t.facts.holders ? `${t.facts.holders} (${t.facts.large_holders})` : "—") },
    { label: "Verdict", cls: "reason", render: (t) => h("span", { class: `verdict ${t.ok ? "v-pass" : "v-fail"}` },
      h("span", { class: "verdict-icon", "aria-hidden": "true", text: t.ok ? "✓" : "✗" }),
      h("span", { text: t.ok ? "Qualifies" : t.reasons.join(" · ") })) },
  ], lt.tokens, "No candidates yet: add tokens to longterm.watchlist, or set a GMGN key for discovery"));
}

// ---------------------------------------------------------------- trades
function renderTrades(v) {
  const sells = v.trades.filter((t) => t.side === "sell");
  const realized = sells.reduce((a, t) => a + t.realized_pnl_usd, 0);
  $("#trades-summary").textContent = `${v.trades.length} most recent trades · ${sells.length} sells · realized ${usd(realized, { sign: true })}`;
  replace($("#trades-table"), tradesTable(v.trades, true));
  const dl = v.downloads;
  if (dl && dl.session_started_at) {
    $("#downloads-note").textContent = `This session started ${timeLabel(dl.session_started_at, true)}`;
  }
  setDownloadLinks();
}

// Downloads are slices of the one all-time trade file: this session, the last N hours (1-24), or all.
function downloadUrl(range) {
  return `/api/trades.csv?range=${encodeURIComponent(range)}&token=${encodeURIComponent(TOKEN)}`;
}

function setDownloadLinks() {
  $("#dl-session").href = downloadUrl("session");
  $("#dl-hours-go").href = downloadUrl(`${$("#dl-hours").value || 1}h`);
  $("#dl-all").href = downloadUrl("all");
}

// ---------------------------------------------------------------- activity
async function pollLog() {
  try {
    const { entries } = await api(`/api/log?after=${state.logAfter}`);
    if (entries.length) {
      state.logs.push(...entries);
      state.logs = state.logs.slice(-1500);
      state.logAfter = entries[entries.length - 1].id;
      if (state.tab === "activity") renderLog();
    }
  } catch { /* the view poll reports connection problems */ }
}

function renderLog() {
  const el = $("#log");
  const isTrade = (e) => /^(BUY|SELL)\b/.test(e.msg);
  const rows = state.logs.filter((e) => state.logFilter === "all"
    || (state.logFilter === "trades" ? isTrade(e) : e.level !== "INFO" && e.level !== "DEBUG"));
  replace(el, rows.length ? rows.slice(-600).map((e) => h("div", { class: "log-line" },
    h("span", { class: "log-time", text: new Date(e.t).toLocaleTimeString() }),
    h("span", { class: `lvl lvl-${e.level}`, text: e.level }),
    h("span", { class: "log-source", text: e.source }),
    h("span", { class: "log-msg", text: e.msg }))) : h("div", { class: "empty", text: "Nothing logged yet" }));
  if ($("#log-follow").checked) el.scrollTop = el.scrollHeight;
}

// ---------------------------------------------------------------- backtest
async function pollBacktest() {
  try {
    state.backtest = await api("/api/backtest");
    renderBacktest();
  } catch { /* ignore */ }
}

function renderBacktest() {
  const b = state.backtest;
  if (!b) return;
  const status = $("#bt-status");
  $("#bt-run").disabled = b.status === "running";
  if (b.status === "running") status.textContent = `Running since ${timeLabel(b.started_at)}…`;
  else if (b.status === "error") status.textContent = `✗ ${b.error}`;
  else if (b.status === "done") status.textContent = `✓ Finished in ${b.elapsed_s}s · ${b.records.toLocaleString()} records`;
  else status.textContent = "";
  const results = $("#bt-results");
  if (b.status !== "done") { if (b.status !== "running") results.hidden = true; return; }
  results.hidden = false;
  const st = b.stats;
  replace($("#bt-tiles"),
    tile("Return", delta(st.return_pct, pct(st.return_pct, 2)), `${usd(b.starting_equity)} → ${usd(b.final_equity)}`),
    tile("Max drawdown", pct(-st.max_drawdown_pct, 2)),
    tile("Round trips", String(st.round_trips), `avg hold ${duration(st.avg_hold_min)}`),
    tile("Win rate", `${st.win_rate_pct.toFixed(1)}%`, `avg win ${pct(st.avg_win_pct)} · loss ${pct(st.avg_loss_pct)}`),
    tile("Profit factor", st.profit_factor === null ? "∞" : num(st.profit_factor), "gross wins ÷ gross losses"),
    tile("Fees", usd(st.fees_usd), `realized ${usd(st.realized_pnl_usd, { sign: true })}`));
  $("#bt-period").textContent = `${timeLabel(b.start, true)} → ${timeLabel(b.end, true)} · ${st.period_hours.toFixed(1)}h`;
  equityChart($("#bt-chart"), b.equity, { rangeLabel: "over the backtest", emptyText: "Not enough data" });
  const exits = Object.entries(st.exit_reasons).map(([label, value]) => ({
    label, value, detail: `${label}: ${value} of ${st.round_trips} round trips` }));
  barChart($("#bt-exits"), exits);
  $("#bt-report").textContent = b.report;
  const closed = [...b.closed].sort((a, z) => z.closed_at.localeCompare(a.closed_at));
  $("#bt-closed-count").textContent = `${closed.length}${closed.length >= 500 ? " (latest 500)" : ""}`;
  replace($("#bt-closed"), table([
    { label: "Token", render: (c) => tokenCell(c.symbol, c.mint, null) },
    { label: "Opened", cls: "nowrap", render: (c) => timeLabel(c.opened_at, true) },
    { label: "Held", num: true, render: (c) => duration(c.hold_minutes) },
    { label: "Cost", num: true, render: (c) => usd(c.cost_usd) },
    { label: "PnL", num: true, render: (c) => delta(c.pnl_usd, `${usd(c.pnl_usd, { sign: true })} (${pct(c.pnl_pct)})`) },
    { label: "Exit", cls: "reason", render: (c) => c.exit_reason },
  ], closed, "No round trips"));
}

// ---------------------------------------------------------------- settings
async function loadSettings() {
  if (state.configLoaded) return;
  try {
    const cfg = await api("/api/config");
    state.configLoaded = true;
    if (!$("#bt-dir").value) {
      $("#bt-dir").value = (state.view && state.view.recorder && state.view.recorder.dir)
        || (cfg.recorder && cfg.recorder.dir) || "recordings";
    }
    const obj = (o) => Object.entries(o).map(([k, v]) => `${k}: ${v}`).join(", ");
    const fmt = (v) => (Array.isArray(v) ? (v.length ? v.map((x) => (x && typeof x === "object" ? obj(x) : x))
      .join(v.some((x) => x && typeof x === "object") ? "\n" : ", ") : "none")
      : v === null ? "—" : typeof v === "object" ? obj(v) : String(v));
    const row = ([k, v]) => {
      const text = fmt(v);
      return [h("dt", { text: k }), h("dd", { class: text.length > 14 || text.includes("\n") ? "wide" : null, text })];
    };
    replace($("#settings"), Object.entries(cfg).filter(([, sec]) => sec && typeof sec === "object").map(([name, sec]) =>
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", { text: name })),
        h("dl", {}, Object.entries(sec).flatMap(row)))));
  } catch (e) { toast(`Could not load settings: ${e.message}`, true); }
}

// ---------------------------------------------------------------- equity
async function pollEquity() {
  try {
    const { points } = await api(`/api/equity?range=${state.range}&points=2000`);
    state.equity = points;
    if (state.tab === "overview") renderEquity();
  } catch { /* ignore */ }
}

function renderEquity() {
  equityChart($("#equity-chart"), state.equity, { mode: state.chartMode, rangeLabel: RANGE_LABELS[state.range] || "",
    emptyText: "Collecting equity samples…" });
  const tableEl = $("#equity-table");
  tableEl.hidden = !state.equityTable;
  if (state.equityTable) {
    const rows = state.equity.slice(-60).reverse();
    replace(tableEl, table([
      { label: "Time", render: (p) => timeLabel(p[0], true) },
      { label: "Equity", num: true, render: (p) => usd(p[1]) },
    ], rows, "No samples"));
  }
}

// ---------------------------------------------------------------- controls
async function control(action, extra = {}) {
  try {
    const res = await api("/api/control", { method: "POST", body: JSON.stringify({ action, ...extra }) });
    if (res.message) toast(res.message);
    await pollView();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- loop & wiring
function bytes(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
}

function renderRecorder(v) {
  const r = v.recorder;
  if (!r) return;
  const btn = $("#rec-toggle");
  btn.hidden = !r.available;
  btn.textContent = r.active ? "Stop recording" : "Start recording";
  btn.className = r.active ? "btn btn-small" : "btn btn-small btn-primary";
  const now = r.active
    ? h("p", {}, h("span", { class: "rec-dot", "aria-hidden": "true" }),
      `Recording every ${r.interval_s}s since ${timeLabel(r.since, true)} — tracking ${r.tracked} tokens, `
      + `${r.records_written.toLocaleString()} records this session. Shares this session's market connections.`)
    : h("p", { class: "muted", text: "Not recording. Start it to collect real market data for backtests; it runs inside this "
      + "session (same data connections, no second process)." });
  const d = r.on_disk;
  const disk = d
    ? h("p", { text: `On disk in ${r.dir}: ${d.files} hourly file${d.files === 1 ? "" : "s"} (${bytes(d.bytes)}), `
      + `${timeLabel(d.first_hour, true)} → ${timeLabel(d.last_hour, true)}. Aim for several days before trusting a backtest.` })
    : h("p", { class: "muted", text: `No recordings in ${r.dir} yet.` });
  replace($("#rec-status"), now, disk);
}

function render() {
  const v = state.view;
  if (!v) return;
  renderHeader(v);
  if (state.tab === "overview") renderOverview(v);
  else if (state.tab === "scanner") renderScanner(v);
  else if (state.tab === "trades") renderTrades(v);
  else if (state.tab === "trade") renderTrade(v);
  else if (state.tab === "lanes") renderLanes(v);
  else if (state.tab === "backtest") renderRecorder(v);
}

async function pollView() {
  try {
    state.view = await api("/api/view");
    state.failures = 0;
    if (state.view.ticks !== state.lastTicks) { state.lastTicks = state.view.ticks; state.lastTickWall = Date.now(); }
  } catch (e) {
    state.failures += 1;
    if (state.view) renderHeader(state.view);
    else $("#status-pill").textContent = `Can't reach the agent: ${e.message}`;
    return;
  }
  render();
  if (state.view.backtest_status === "running" || (state.backtest && state.backtest.status === "running")) pollBacktest();
}

function selectTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", t.dataset.tab === name ? "true" : "false"));
  document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  store("tab", name);
  if (name === "activity") renderLog();
  if (name === "settings") loadSettings();
  if (name === "backtest") { loadSettings(); pollBacktest(); }
  if (name === "overview") renderEquity();
  render();
}

function segmented(id, key, onChange) {
  const group = $(id);
  group.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
    onChange(btn.dataset[key]);
  });
}

function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  $("#btn-theme").title = `Theme: ${t || "auto"} (click to change)`;
}

function init() {
  applyTheme(store("theme"));
  $("#btn-theme").addEventListener("click", () => {
    const order = [null, "light", "dark"];
    const cur = store("theme");
    const next = order[(order.indexOf(cur === "light" || cur === "dark" ? cur : null) + 1) % 3];
    store("theme", next || "auto");
    applyTheme(next);
    if (state.tab === "overview") renderEquity();
  });
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));
  document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.goto)));
  segmented("#range-select", "range", (r) => { state.range = r; pollEquity(); });
  state.chartMode = store("chartMode") === "candles" ? "candles" : "line";
  document.querySelectorAll("#chart-mode button").forEach((b) =>
    b.setAttribute("aria-pressed", b.dataset.mode === state.chartMode ? "true" : "false"));
  segmented("#chart-mode", "mode", (mode) => { state.chartMode = mode; store("chartMode", mode); renderEquity(); });
  segmented("#scan-filter", "filter", (f) => { state.scanFilter = f; render(); });
  segmented("#log-filter", "filter", (f) => { state.logFilter = f; renderLog(); });
  $("#scan-search").addEventListener("input", (e) => { state.scanSearch = e.target.value; render(); });
  $("#equity-table-toggle").addEventListener("click", (e) => {
    state.equityTable = !state.equityTable;
    e.currentTarget.setAttribute("aria-pressed", state.equityTable ? "true" : "false");
    renderEquity();
  });
  $("#btn-pause").addEventListener("click", () => control(state.view && state.view.entries_paused ? "resume" : "pause"));
  $("#btn-scan").addEventListener("click", () => control("scan_now"));
  $("#rec-toggle").addEventListener("click", () =>
    control(state.view && state.view.recorder && state.view.recorder.active ? "record_stop" : "record_start"));
  $("#btn-close-all").addEventListener("click", async () => {
    const v = state.view;
    if (!v || !v.positions.length) return;
    const ok = await confirmDialog("Close every position?",
      `Sell all ${v.positions.length} open position(s), about ${usd(v.positions_value)}, at market now${v.mode === "live" ? " — real on-chain sales" : ""}. New entries keep running unless you also pause them.`,
      "Sell everything");
    if (ok) control("close_all");
  });
  $("#bt-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const overrides = $("#bt-overrides").value.split("\n").map((l) => l.trim()).filter(Boolean);
    try {
      state.backtest = await api("/api/backtest", { method: "POST", body: JSON.stringify({
        dir: $("#bt-dir").value.trim(), overrides, assume_safe: $("#bt-assume-safe").checked }) });
      renderBacktest();
      const poll = setInterval(async () => {
        await pollBacktest();
        if (!state.backtest || state.backtest.status !== "running") clearInterval(poll);
      }, 1000);
    } catch (err) { toast(err.message, true); }
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.tab === "overview") renderEquity();
      if (state.tab === "backtest") renderBacktest();
    }, 150);
  });

  const saved = store("tab");
  const hours = $("#dl-hours");
  for (let n = 1; n <= 24; n++) hours.append(h("option", { value: n, text: `${n} hour${n > 1 ? "s" : ""}` }));
  const savedHours = store("dlHours");
  hours.value = /^([1-9]|1\d|2[0-4])$/.test(savedHours || "") ? savedHours : "1";
  hours.addEventListener("change", () => { store("dlHours", hours.value); setDownloadLinks(); });
  setDownloadLinks();

  $("#lookup-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#lookup-q").value.trim();
    if (q) lookup(q);
  });
  $("#buy-form").addEventListener("submit", (e) => { e.preventDefault(); buyPicked(); });
  $("#buy-check").addEventListener("click", checkPicked);
  $("#buy-managed").addEventListener("change", () => state.view && renderTrade(state.view));
  renderLookup();

  selectTab(["overview", "scanner", "trade", "lanes", "trades", "activity", "backtest", "settings"].includes(saved) ? saved : "overview");
  pollView(); pollEquity(); pollLog();
  setInterval(pollView, 2000);
  setInterval(pollEquity, 10000);
  setInterval(pollLog, 2500);
}

document.addEventListener("DOMContentLoaded", init);
