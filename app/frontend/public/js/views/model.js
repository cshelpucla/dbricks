/** Data model tab: entity-relationship diagram of the bronze, silver and gold tables. */
import { getJSON } from "../api.js";
import { clear, el, svg } from "../lib/dom.js";

const W = 250, HEAD = 34, ROW = 18, GAP = 26, PAD = 20, COL_GAP = 120, TOP = 46;
const LANES = [["BRONZE", 0, 0], ["SILVER", 1, 1], ["GOLD", 2, 3]];

export function init({ root, onError }) {
  const intro = el("p", { class: "muted", text: "Click a table to highlight its relationships. Dashed outlines are tables that do not exist yet." });
  const host = el("div", { class: "erd" });
  root.append(intro, host);
  let spec = null, existing = new Set(), focus = null;

  function layout() {
    const ys = {}, pos = {};
    for (const t of spec.tables) {
      ys[t.col] ??= TOP;
      const h = HEAD + t.columns.length * ROW + 8;
      pos[t.name] = { x: PAD + t.col * (W + COL_GAP), y: ys[t.col], h, t };
      ys[t.col] += h + GAP;
    }
    return { pos, width: PAD * 2 + 4 * W + 3 * COL_GAP, height: Math.max(...Object.values(ys)) + PAD };
  }

  const anchor = (pos, [tbl, col], towardsRight) => {
    const p = pos[tbl], i = Math.max(0, p.t.columns.findIndex((c) => c[0] === col));
    return { x: towardsRight ? p.x + W : p.x, y: p.y + HEAD + i * ROW + ROW / 2 };
  };

  function draw() {
    const { pos, width, height } = layout();
    const root_ = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": "Entity relationship diagram of the bronze, silver and gold tables" });
    for (const [label, c0, c1] of LANES) {
      const x = PAD + c0 * (W + COL_GAP) - 10, w = (c1 - c0 + 1) * W + (c1 - c0) * COL_GAP + 20;
      root_.append(svg("rect", { x, y: 6, width: w, height: height - 12, rx: 12, class: "lane" }), svg("text", { x: x + 12, y: 28, class: "lane-t" }, label));
    }
    const touches = (r, name) => r.from[0] === name || r.to[0] === name;
    // relationships first, so tables sit on top
    for (const r of spec.relations) {
      const a = pos[r.from[0]], b = pos[r.to[0]];
      const hot = focus && touches(r, focus);
      let s, e, d;
      if (a.t.col === b.t.col) {                                   // same column: loop out on the left side
        s = anchor(pos, r.from, false); e = anchor(pos, r.to, false);
        const bulge = Math.max(24, Math.abs(s.y - e.y) * 0.25);
        d = `M${s.x},${s.y} C${s.x - bulge},${s.y} ${e.x - bulge},${e.y} ${e.x},${e.y}`;
      } else {
        const fwd = a.t.col < b.t.col;
        s = anchor(pos, r.from, fwd); e = anchor(pos, r.to, !fwd);
        const dx = Math.max(40, Math.abs(e.x - s.x) / 2) * (fwd ? 1 : -1);
        d = `M${s.x},${s.y} C${s.x + dx},${s.y} ${e.x - dx},${e.y} ${e.x},${e.y}`;
      }
      root_.append(svg("path", { d, class: `rel ${hot ? "hot" : ""}`, opacity: focus && !hot ? 0.2 : 1 }),
        svg("circle", { cx: e.x, cy: e.y, r: 3.5, fill: hot ? "var(--series-1)" : "var(--muted)", opacity: focus && !hot ? 0.2 : 1 }));
    }
    for (const { x, y, h, t } of Object.values(pos)) {
      const planned = !existing.has(t.name);
      const g = svg("g", { class: "t", tabindex: "0", role: "button", "aria-label": `${t.name} table${planned ? ", not created yet" : ""}`, opacity: focus && focus !== t.name ? 0.55 : 1 });
      g.append(svg("rect", { x, y, width: W, height: h, rx: 8, class: `tbl ${planned ? "planned" : ""}` }),
        svg("path", { d: `M${x},${y + HEAD} V${y + 8} a8,8 0 0 1 8,-8 H${x + W - 8} a8,8 0 0 1 8,8 V${y + HEAD} Z`, class: "th" }),
        svg("text", { x: x + 12, y: y + 22, class: "tn" }, t.name), svg("text", { x: x + W - 10, y: y + 22, "text-anchor": "end", class: "tag" }, planned ? "NOT CREATED" : ""));
      t.columns.forEach(([n, type, key], i) => {
        const cy = y + HEAD + i * ROW + 13;
        g.append(svg("text", { x: x + 12, y: cy, class: `cn ${key ? "key" : ""}` }, (key === "pk" ? "PK  " : key === "fk" ? "FK  " : "") + n),
          svg("text", { x: x + W - 10, y: cy, "text-anchor": "end", class: "ct" }, type));
      });
      g.append(svg("title", {}, t.note));
      const toggle = () => { focus = focus === t.name ? null : t.name; draw(); };
      g.addEventListener("click", toggle); g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      root_.append(g);
    }
    clear(host).append(root_);
  }

  return {
    async activate() {
      try {
        if (!spec) spec = await getJSON("data/erd.json");
        existing = new Set((await getJSON("/api/model/status")).existing);
        onError(null); draw();
      } catch (err) { onError(`Could not load the data model: ${err.message}`); }
    },
  };
}
