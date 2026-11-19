/** World map tab: bubbles sized by post volume at each inferred location, on an Equal Earth projection. */
import { getJSON } from "../api.js";
import { clear, el, fmt, svg } from "../lib/dom.js";
import { multiSelect, singleSelect } from "../lib/controls.js";
import { entityBadge, withFlag } from "../lib/badges.js";

const TIME = [[1, "Last hour"], [6, "Last 6 hours"], [24, "Last 24 hours"], [72, "Last 3 days"], [168, "Last 7 days"]];
const CONF = [[0.4, "0.4 or more (default)"], [0.5, "0.5 or more"], [0.6, "0.6 or more"], [0.3, "0.3 or more (looser)"]];
const LEVELS = [["place", "Places"], ["country", "Countries"]];
const R_MAX = 30, R_MIN = 2.5;
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "–");
const countryName = (c) => { try { return new Intl.DisplayNames(["en"], { type: "region" }).of(c) || c; } catch { return c; } };

// Equal Earth projection (Šavrič, Patterson, Jenny 2018). Output roughly x in [-2.71, 2.71], y in [-1.32, 1.32].
const A1 = 1.340264, A2 = -0.081106, A3 = 0.000893, A4 = 0.003796, M = Math.sqrt(3) / 2;
function project(lon, lat) {
  const l = (lon * Math.PI) / 180, th = Math.asin(M * Math.sin((lat * Math.PI) / 180));
  const t2 = th * th, t6 = t2 * t2 * t2;
  return [(l * Math.cos(th)) / (M * (A1 + 3 * A2 * t2 + t6 * (7 * A3 + 9 * A4 * t2))), -th * (A1 + A2 * t2 + t6 * (A3 + A4 * t2))];
}

/** TopoJSON (world-atlas) to SVG path strings in projected units. */
function topoPaths(topo) {
  const [sx, sy] = topo.transform.scale, [tx, ty] = topo.transform.translate;
  const arcs = topo.arcs.map((arc) => { let x = 0, y = 0; return arc.map(([dx, dy]) => { x += dx; y += dy; return [x * sx + tx, y * sy + ty]; }); });
  const ring = (ids) => {
    const pts = [];
    for (const id of ids) {
      const a = id < 0 ? [...arcs[~id]].reverse() : arcs[id];
      a.forEach((p, i) => { if (i || !pts.length) pts.push(p); });
    }
    // break the path where a ring crosses the 180° meridian, instead of drawing a line across the map
    let d = "";
    pts.forEach(([lon, lat], i) => {
      const jump = i && Math.abs(lon - pts[i - 1][0]) > 180;
      d += (i === 0 || jump ? "M" : "L") + project(lon, lat).map((v) => v.toFixed(4)).join(",");
    });
    return d + "Z";
  };
  return topo.objects.countries.geometries.filter((g) => g.properties?.name !== "Antarctica").map((g) => ({
    name: g.properties?.name || "",
    d: (g.type === "Polygon" ? [g.arcs] : g.type === "MultiPolygon" ? g.arcs : []).map((poly) => poly.map(ring).join("")).join(""),
  }));
}

export function init({ root, onError, setBadge }) {
  const s = { hours: 24, minConf: 0.4, level: "place", topics: [], people: [], companies: [] };
  const names = new Map();                                          // person or company Wikidata id -> display name
  const nameOf = (id) => names.get(id) || id;
  const info = new Map();                                           // id -> its entry from /api/geo (portrait, industry, sector)
  const badge = (kind, id) => entityBadge(kind, info.get(id) ?? { label: nameOf(id) }, 20);
  let mode = null, data = null, countries = null, view = { x: -2.75, y: -1.36, w: 5.5, h: 2.72 }, base = { ...view };
  const filters = el("div", { class: "filters" });
  const summary = el("div", { class: "summary" });
  const tip = el("div", { class: "tip", hidden: true });
  const host = el("div", { class: "geo-wrap" }, tip);
  const zoom = el("div", { class: "zoom" },
    el("button", { class: "btn", type: "button", "aria-label": "Zoom in", onclick: () => zoomBy(1.5) }, "+"),
    el("button", { class: "btn", type: "button", "aria-label": "Zoom out", onclick: () => zoomBy(1 / 1.5) }, "−"),
    el("button", { class: "btn", type: "button", "aria-label": "Show the whole world", onclick: () => { view = { ...base }; paint(); } }, "Fit"));
  host.append(zoom);
  const legend = el("div", { class: "card" });
  const table = el("div", { class: "card geo-table" });
  root.append(summary, filters, el("main", { class: "stage" }, el("section", { class: "viz geo-viz", "aria-label": "World map of post volume" }, host),
    el("aside", { class: "side" }, legend, table)));

  const topicSel = multiSelect({ label: "Topic", fmtOpt: cap, searchable: true, onChange: (v) => { s.topics = v; load(); } });
  const personSel = multiSelect({ label: "Person", fmtOpt: nameOf, iconOf: (q) => badge("people", q), searchable: true, onChange: (v) => { s.people = v; load(); } });
  const companySel = multiSelect({ label: "Company", fmtOpt: nameOf, iconOf: (q) => badge("companies", q), searchable: true, onChange: (v) => { s.companies = v; load(); } });
  filters.append(
    singleSelect({ label: "Time window", options: TIME.map(([value, label]) => ({ value, label })), value: s.hours, onChange: (v) => { s.hours = v; load(); } }).el,
    singleSelect({ label: "Show", options: LEVELS.map(([value, label]) => ({ value, label })), value: s.level, onChange: (v) => { s.level = v; load(); } }).el,
    singleSelect({ label: "Location confidence", options: CONF.map(([value, label]) => ({ value, label })), value: s.minConf, onChange: (v) => { s.minConf = v; load(); } }).el,
    topicSel.el, personSel.el, companySel.el);

  const svgRoot = svg("svg", { class: "geo", role: "img", "aria-label": "World map. Bubble size is the number of posts at each location; the table lists the same data." });
  const gLand = svg("g"), gBubbles = svg("g");
  svgRoot.append(gLand, gBubbles);
  host.prepend(svgRoot);

  // pan and zoom by moving the viewBox
  let drag = null;
  const pxScale = () => view.w / Math.max(1, host.clientWidth);
  svgRoot.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY }; svgRoot.setPointerCapture(e.pointerId); });
  svgRoot.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const k = pxScale(); view.x -= (e.clientX - drag.x) * k; view.y -= (e.clientY - drag.y) * k; drag = { x: e.clientX, y: e.clientY }; setViewBox();
  });
  svgRoot.addEventListener("pointerup", () => { drag = null; });
  svgRoot.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = svgRoot.getBoundingClientRect(), fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
    zoomBy(Math.exp(-e.deltaY * 0.0015), fx, fy);
  }, { passive: false });
  function zoomBy(f, fx = 0.5, fy = 0.5) {
    const w = Math.min(base.w, Math.max(base.w / 40, view.w / f)), h = (w * base.h) / base.w;
    view = { x: view.x + (view.w - w) * fx, y: view.y + (view.h - h) * fy, w, h }; paint();
  }
  function setViewBox() { svgRoot.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`); }

  async function load() {
    host.classList.add("loading");
    try {
      if (!countries) countries = topoPaths(await getJSON("data/countries-110m.json"));
      const params = new URLSearchParams({ hours: String(s.hours), min_conf: String(s.minConf), level: s.level });
      if (s.topics.length) params.set("topic", s.topics.join(","));
      if (s.people.length) params.set("person", s.people.join(","));
      if (s.companies.length) params.set("company", s.companies.join(","));
      data = await getJSON("/api/geo", params);
      mode = data.mode; setBadge(mode); onError(null);
      for (const p of [...(data.people ?? []), ...(data.companies ?? [])]) { names.set(p.value, p.label); info.set(p.value, p); }
      topicSel.setOptions(data.topics);
      personSel.setOptions((data.people ?? []).map(({ value, count }) => ({ value, count })));
      companySel.setOptions((data.companies ?? []).map(({ value, count }) => ({ value, count })));
      paint(); renderSide();
    } catch (err) { onError(`Could not load the world map: ${err.message}`); }
    finally { host.classList.remove("loading"); }
  }

  function paint() {
    if (!countries || !data) return;
    setViewBox();
    if (!gLand.firstChild) for (const c of countries) gLand.append(svg("path", { d: c.d, class: "land" }, svg("title", {}, c.name)));
    clear(gBubbles);
    const k = pxScale(), max = Math.max(1, ...data.points.map((p) => p.n));
    for (const p of [...data.points].sort((a, b) => b.n - a.n)) {                 // big first, so small stay on top
      const [x, y] = project(p.lon, p.lat);
      const r = Math.max(R_MIN, R_MAX * Math.sqrt(p.n / max)) * k;
      const c = svg("circle", { cx: x, cy: y, r, class: "bubble", tabindex: "0", "stroke-width": 1.5 * k });
      const label = `${p.name && p.level !== "country" && s.level === "place" ? p.name : countryName(p.country || p.name)}`;
      const show = (ev) => {
        clear(tip).append(el("div", { class: "v" }, `${fmt.int(p.n)} posts`),
          el("div", { class: "l" }, withFlag(p.country, s.level === "country" ? countryName(p.country) : `${p.name}${p.country && p.name !== p.country ? ", " + countryName(p.country) : ""}`)),
          el("div", { class: "s" }, `Most common topic: ${cap(p.topic)}`));
        tip.hidden = false;
        const hr = host.getBoundingClientRect(), cx = (ev.clientX ?? hr.left + hr.width / 2) - hr.left, cy = (ev.clientY ?? hr.top + 40) - hr.top;
        tip.style.left = `${cx + 14 + tip.offsetWidth > hr.width ? cx - tip.offsetWidth - 10 : cx + 14}px`; tip.style.top = `${Math.max(4, cy - 10)}px`;
      };
      c.addEventListener("pointermove", show); c.addEventListener("focus", show);
      c.addEventListener("pointerleave", () => { tip.hidden = true; }); c.addEventListener("blur", () => { tip.hidden = true; });
      c.append(svg("title", {}, `${label}: ${p.n} posts`));
      gBubbles.append(c);
    }
  }

  function renderSide() {
    const pct = data.total ? data.located / data.total : 0;
    summary.textContent = `${fmt.int(data.located)} of ${fmt.int(data.total)} posts (${fmt.pct(pct)}) have an inferred location at confidence ${s.minConf} or more · ${data.points.length} ${s.level === "place" ? "places" : "countries"}`;
    // size legend: reference circles for the largest value and two fractions of it
    const max = Math.max(1, ...data.points.map((p) => p.n));
    const refs = [max, Math.round(max / 4), Math.round(max / 16)].filter((v, i, a) => v > 0 && a.indexOf(v) === i);
    const lg = svg("svg", { width: 2 * R_MAX + 120, height: 2 * R_MAX + 8, "aria-hidden": "true" });
    for (const v of refs) {
      const r = Math.max(R_MIN, R_MAX * Math.sqrt(v / max)), cy = 2 * R_MAX + 4 - r;
      lg.append(svg("circle", { cx: R_MAX + 2, cy, r, class: "bubble-key" }), svg("line", { x1: R_MAX + 2, x2: 2 * R_MAX + 14, y1: cy - r, y2: cy - r, class: "key-line" }),
        svg("text", { x: 2 * R_MAX + 18, y: cy - r + 4, class: "key-text" }, `${fmt.int(v)} posts`));
    }
    clear(legend).append(el("h2", { text: "Bubble size" }), el("p", { text: "Area is proportional to the number of posts. Locations are inferred from post text, hashtags, links and language tags." }), lg,
      ...(data.mode === "demo" ? [el("p", { text: "Demo data: synthetic numbers for layout only." })] : []));
    const top = data.points.slice(0, 25), total = data.points.reduce((a, p) => a + p.n, 0) || 1;
    clear(table).append(el("h2", { text: `Top ${s.level === "place" ? "places" : "countries"}` }), el("table", {},
      el("thead", {}, el("tr", {}, ["", s.level === "place" ? "Place and top topic" : "Country and top topic", "Posts", "Share"].map((h, i) => el("th", { class: i >= 2 ? "num" : "" }, h)))),
      el("tbody", {}, top.map((p, i) => el("tr", {}, el("td", { class: "muted" }, String(i + 1)),
        el("td", {}, withFlag(p.country, s.level === "place" && p.name && p.name !== p.country ? p.name : countryName(p.country || p.name)),
          el("div", { class: "muted small" }, cap(p.topic))),
        el("td", { class: "num" }, fmt.int(p.n)), el("td", { class: "num" }, fmt.pct(p.n / total)))))));
  }

  new ResizeObserver(() => paint()).observe(host);
  return { activate() { if (!data) load(); else paint(); }, badge: () => mode };
}
