/** Small SVG charts: stacked bars and lines on one y-axis, with hover tooltip, legend and a table view. */
import { el, svg, clear } from "./dom.js";

const SERIES_VARS = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5", "--series-6", "--series-7", "--series-8"];
export const seriesVar = (i) => `var(${SERIES_VARS[i % SERIES_VARS.length]})`;
const M = { l: 56, r: 12, t: 8, b: 26 };

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v)), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

/**
 * opts: { kind: "bars"|"lines", x: [label|ISO string], series: [{name, values, color?}],
 *         yFormat(v), xFormat(x, i), height, label, stacked }
 * Colour follows the series name, so a filter that drops a series never repaints the others.
 */
export function createChart(host, opts) {
  let o = opts, width = 600;
  const wrap = el("div", { class: "chart" });
  const legend = el("div", { class: "legend-row" });
  const stage = el("div", { class: "chart-stage" });
  const tip = el("div", { class: "tip", hidden: true });
  const table = el("div", { class: "chart-table", hidden: true });
  stage.append(tip);
  wrap.append(legend, stage, table);
  host.append(wrap);
  new ResizeObserver(() => { const w = stage.clientWidth; if (w && Math.abs(w - width) > 1) { width = w; render(); } }).observe(stage);

  const colorOf = (s, i) => s.color || seriesVar(i);

  function render() {
    const { kind = "bars", x, series, yFormat = String, xFormat = String, height = 220, stacked = kind === "bars" } = o;
    const axisFormat = o.axisFormat || yFormat;
    clear(legend);
    if (series.length > 1) {
      series.forEach((s, i) => legend.append(el("span", { class: "lg" },
        el("i", { class: kind === "lines" ? "line" : "box", style: `background:${colorOf(s, i)}` }), s.name)));
    }
    stage.querySelector("svg")?.remove();
    const h = height, w = width, pw = w - M.l - M.r, ph = h - M.t - M.b, n = x.length;
    const root = svg("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, role: "img", "aria-label": o.label || "chart" });
    stage.prepend(root);
    if (!n || !series.length) { root.append(svg("text", { x: w / 2, y: h / 2, "text-anchor": "middle", class: "ax" }, "No data in this window")); return; }

    const totals = x.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
    const maxV = niceMax(stacked ? Math.max(...totals) : Math.max(...series.flatMap((s) => s.values.map((v) => v || 0))));
    const y = (v) => M.t + ph - (v / maxV) * ph;
    const bw = pw / n, xAt = (i) => M.l + bw * (i + 0.5);

    for (let t = 0; t <= 4; t++) {                                    // recessive grid
      const v = (maxV / 4) * t, yy = y(v);
      root.append(svg("line", { x1: M.l, x2: w - M.r, y1: yy, y2: yy, class: t === 0 ? "axis" : "grid" }));
      root.append(svg("text", { x: M.l - 8, y: yy + 4, "text-anchor": "end", class: "ax" }, axisFormat(v)));
    }
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(pw / 90))));
    x.forEach((xv, i) => { if (i % every === 0) root.append(svg("text", { x: xAt(i), y: h - 8, "text-anchor": "middle", class: "ax" }, xFormat(xv, i))); });

    const hover = svg("rect", { class: "hover-band", y: M.t, height: ph, width: Math.max(bw, 2), visibility: "hidden" });
    const cross = svg("line", { class: "cross", y1: M.t, y2: M.t + ph, visibility: "hidden" });
    root.append(hover, cross);

    if (kind === "bars") {
      const gap = Math.min(2, bw * 0.15), barW = Math.max(1, bw - Math.max(gap, bw > 6 ? 2 : 0));
      x.forEach((_, i) => {
        let acc = 0;
        series.forEach((s, si) => {
          const v = s.values[i] || 0; if (!v) return;
          const y0 = y(acc), y1 = y(acc + v); acc += v;
          root.append(svg("rect", { x: xAt(i) - barW / 2, y: y1, width: barW, height: Math.max(0, y0 - y1 - (series.length > 1 ? 2 : 0)),
            rx: Math.min(3, barW / 2), fill: colorOf(s, si), class: "mark" }));
        });
      });
    } else {
      const ends = [];
      series.forEach((s, si) => {
        const pts = s.values.map((v, i) => `${xAt(i)},${y(v || 0)}`).join(" ");
        root.append(svg("polyline", { points: pts, fill: "none", stroke: colorOf(s, si), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
        ends.push({ s, si, yy: y(s.values[n - 1] || 0) });
      });
      if (series.length <= 4 && series.length > 1) {                  // direct labels at line ends, nudged apart
        ends.sort((a, b) => a.yy - b.yy);
        for (let i = 1; i < ends.length; i++) ends[i].yy = Math.max(ends[i].yy, ends[i - 1].yy + 13);
        ends.forEach((e) => root.append(svg("text", { x: w - M.r - 4, y: e.yy - 6, "text-anchor": "end", class: "end-label" }, e.s.name)));
      }
    }

    const hit = svg("rect", { x: M.l, y: M.t, width: pw, height: ph, fill: "transparent" });
    root.append(hit);
    hit.addEventListener("pointermove", (ev) => {
      const r = root.getBoundingClientRect();
      const i = Math.max(0, Math.min(n - 1, Math.floor((ev.clientX - r.left - M.l) / bw)));
      hover.setAttribute("x", M.l + bw * i); hover.setAttribute("visibility", kind === "bars" ? "visible" : "hidden");
      cross.setAttribute("x1", xAt(i)); cross.setAttribute("x2", xAt(i)); cross.setAttribute("visibility", kind === "lines" ? "visible" : "hidden");
      showTip(i, xAt(i), ev.clientY - r.top);
    });
    hit.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
    renderTable();
  }

  function showTip(i, px, py) {
    const { x, series, yFormat = String, xFormat = String, stacked = (o.kind || "bars") === "bars" } = o;
    clear(tip);
    tip.append(el("div", { class: "s" }, xFormat(x[i], i)));
    if (stacked && series.length > 1) tip.append(el("div", { class: "v" }, yFormat(series.reduce((a, s) => a + (s.values[i] || 0), 0)), el("span", { class: "s" }, " total")));
    series.forEach((s, si) => tip.append(el("div", { class: "row" }, el("i", { class: "line", style: `background:${colorOf(s, si)}` }),
      el("span", { class: "v" }, yFormat(s.values[i] || 0)), el("span", { class: "s" }, series.length > 1 ? s.name : ""))));
    tip.hidden = false;
    const left = px + 14 + tip.offsetWidth > width ? px - tip.offsetWidth - 14 : px + 14;
    tip.style.left = `${Math.max(0, left)}px`; tip.style.top = `${Math.max(0, Math.min(py, 180))}px`;
  }

  function renderTable() {
    clear(table);
    const { x, series, yFormat = String, xFormat = String } = o;
    const rows = x.map((xv, i) => el("tr", {}, el("td", {}, xFormat(xv, i)), series.map((s) => el("td", { class: "num" }, yFormat(s.values[i] || 0)))));
    table.append(el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, ""), series.map((s) => el("th", { class: "num" }, s.name)))), el("tbody", {}, rows)));
  }

  return {
    update(next) { o = next; render(); },
    setTable(show) { table.hidden = !show; stage.hidden = show; },
    root: wrap,
  };
}

/** A titled card holding a chart, with a Table toggle for the same data. */
export function chartCard(title, subtitle, opts) {
  const host = el("div", { class: "card chart-card" });
  const toggle = el("button", { class: "link", type: "button", "aria-pressed": "false", text: "Table" });
  host.append(el("div", { class: "card-head" }, el("div", {}, el("h2", { text: title }), subtitle && el("p", { text: subtitle })), toggle));
  const chart = createChart(host, opts);
  let on = false;
  toggle.addEventListener("click", () => { on = !on; toggle.setAttribute("aria-pressed", String(on)); toggle.textContent = on ? "Chart" : "Table"; chart.setTable(on); });
  return { el: host, update: (o) => chart.update(o) };
}
