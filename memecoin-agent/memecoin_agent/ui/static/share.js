"use strict";

const shareToken = document.querySelector('meta[name="share-token"]').content;
const $ = (id) => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";

function percent(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const n = Number(value);
  return `${n > 0 ? "+" : ""}${n.toFixed(1)}%`;
}
function displayNumber(value) {
  return Number.isFinite(Number(value)) && value !== null ? String(value) : "—";
}
function setReturn(el, value) {
  el.textContent = percent(value);
  el.classList.toggle("up", value > 0);
  el.classList.toggle("down", value < 0);
}
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text;
  return el;
}
function timeLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Recently" :
    date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
async function get(path) {
  const res = await fetch(path, { headers: { "X-Share-Token": shareToken }, cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
function drawList(id, rows, toItem, emptyLabel) {
  const list = $(id);
  list.replaceChildren();
  if (!rows.length) {
    list.append(node("p", "empty", emptyLabel));
    return;
  }
  for (const row of rows) list.append(toItem(row));
}
function positionItem(p) {
  const row = node("div", "list-row", "");
  const info = node("div", "row-text", "");
  info.append(node("strong", "", String(p.symbol || "Token")));
  info.append(node("span", "", [p.chain, p.lane, p.stage].filter(Boolean).join(" · ")));
  const ret = node("span", "row-return", percent(p.pnl_pct));
  if (p.pnl_pct > 0) ret.classList.add("up");
  if (p.pnl_pct < 0) ret.classList.add("down");
  row.append(info, ret);
  return row;
}
function tradeItem(t) {
  const row = node("div", "list-row", "");
  const info = node("div", "row-text", "");
  info.append(node("strong", "", String(t.symbol || "Token")));
  info.append(node("span", "", timeLabel(t.time)));
  row.append(info, node("span", "trade-side", String(t.side || "").toUpperCase()));
  return row;
}
function drawChart(raw) {
  const holder = $("chart");
  const points = (raw || []).filter((p) => Array.isArray(p) && p.length === 2 && Number.isFinite(Number(p[1])));
  holder.replaceChildren();
  if (points.length < 2) {
    holder.append(node("p", "empty", "Waiting for more recorded points…"));
    return;
  }
  const width = 900, height = 205, pad = 16;
  const values = points.map((p) => Number(p[1]));
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = Math.max(hi - lo, 0.1);
  const xy = values.map((v, i) =>
    `${pad + i * (width - 2 * pad) / (values.length - 1)},${height - pad - (v - lo) / span * (height - 2 * pad)}`);
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Portfolio return over time");
  const line = document.createElementNS(svgNS, "polyline");
  line.setAttribute("points", xy.join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "3");
  line.setAttribute("stroke-linecap", "round");
  line.setAttribute("stroke-linejoin", "round");
  svg.append(line);
  holder.append(svg);
  $("chart-caption").textContent = `${percent(values[0])} → ${percent(values[values.length - 1])}`;
}
async function refresh() {
  try {
    const view = await get("/api/share/view");
    setReturn($("return"), view.return_pct);
    setReturn($("today"), view.today_pct);
    $("trades-count").textContent = displayNumber(view.session?.round_trips);
    $("win-rate").textContent = `Win rate ${percent(view.session?.win_rate_pct)}`;
    $("positions-count").textContent = displayNumber(view.positions?.length);
    $("mode").textContent = `Agent mode ${String(view.mode || "unknown")}`;
    drawList("positions", view.positions || [], positionItem, "No open positions");
    drawList("recent", view.trades || [], tradeItem, "No recent trades");
    const fresh = view.last_tick_at && Date.now() - new Date(view.last_tick_at).getTime() < 120_000;
    $("connection").textContent = fresh ? "Agent active" : "Waiting for a new tick";
    const equity = await get("/api/share/equity");
    drawChart(equity.points);
  } catch {
    $("connection").textContent = "Connection unavailable";
  }
}

refresh();
setInterval(refresh, 10_000);
