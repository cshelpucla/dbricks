/** Canvas bubble map: one bubble per topic cluster, pan and zoom, collision-free labels. */
const MIN_R = 4;          // smallest bubble radius in px (8px marker)
const LABEL_FONT = '12px system-ui, -apple-system, "Segoe UI", sans-serif';
const SUB_FONT = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
const BLOCK_R = 6;        // bubbles at least this radius in px (and every bubble with a second label line) keep labels off
const HIT_PAD = 10;       // hit area is at least this radius, bigger than a small bubble
const MIN_CORE_R = 2.5;   // a core for a handful of posts in a big cluster still shows as a dot

export class MapView {
  constructor(canvas, handlers) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.h = handlers;
    this.clusters = [];
    this.view = { cx: 0, cy: 0, k: 1 };
    this.kFit = 1;
    this.rScale = 0;
    this.style = { fill: () => ({ color: "#888", alpha: 0.5 }), tokens: null };
    this.hoverId = null;
    this.selectedId = null;
    this.screen = [];
    this.drag = null;
    new ResizeObserver(() => this.resize()).observe(canvas);
    canvas.addEventListener("pointerdown", (e) => this.down(e));
    canvas.addEventListener("pointermove", (e) => this.move(e));
    canvas.addEventListener("pointerup", (e) => this.up(e));
    canvas.addEventListener("pointerleave", () => this.leave());
    canvas.addEventListener("wheel", (e) => this.wheel(e), { passive: false });
    canvas.addEventListener("keydown", (e) => this.key(e));
    this.resize();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const r = this.canvas.getBoundingClientRect();
    this.w = Math.max(1, r.width);
    this.hgt = Math.max(1, r.height);
    this.dpr = dpr;
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.hgt * dpr);
    if (this.clusters.length && !this.fitted) this.fit();
    this.draw();
  }

  /** Bubble sizes keep one scale across filter changes so a filtered map shows smaller bubbles, not rescaled ones. */
  setClusters(clusters, { refit = false } = {}) {
    this.clusters = clusters;
    if (clusters.length && (refit || !this.rScale)) {
      const b = this.bounds(false);
      const extent = Math.max(b.maxx - b.minx, b.maxy - b.miny, 1e-6);
      this.rScale = (0.06 * extent) / Math.sqrt(clusters[0].n);
      this.fit();
    }
    this.draw();
  }

  setStyle(style) { this.style = style; this.draw(); }
  select(id) { this.selectedId = id; this.draw(); }
  radiusData(c) { return this.rScale * Math.sqrt(c.n); }

  bounds(pad) {
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (const c of this.clusters) {
      const r = pad ? this.radiusData(c) : 0;
      minx = Math.min(minx, c.x - r); maxx = Math.max(maxx, c.x + r);
      miny = Math.min(miny, c.y - r); maxy = Math.max(maxy, c.y + r);
    }
    return { minx, miny, maxx, maxy };
  }

  fit() {
    if (!this.clusters.length) return;
    const b = this.bounds(true), m = 56;
    const bw = Math.max(b.maxx - b.minx, 1e-6), bh = Math.max(b.maxy - b.miny, 1e-6);
    this.kFit = Math.min((this.w - 2 * m) / bw, (this.hgt - 2 * m) / bh);
    this.view = { cx: (b.minx + b.maxx) / 2, cy: (b.miny + b.maxy) / 2, k: this.kFit };
    this.fitted = true;
    this.draw();
  }

  zoomBy(f, px = this.w / 2, py = this.hgt / 2) {
    const v = this.view;
    const k = Math.min(this.kFit * 80, Math.max(this.kFit * 0.6, v.k * f));
    // keep the data point under the pointer fixed
    const dx = (px - this.w / 2) / v.k, dy = (py - this.hgt / 2) / v.k;
    this.view = { k, cx: v.cx + dx - (px - this.w / 2) / k, cy: v.cy + dy - (py - this.hgt / 2) / k };
    this.draw();
  }

  panBy(dx, dy) {
    this.view = { ...this.view, cx: this.view.cx - dx / this.view.k, cy: this.view.cy - dy / this.view.k };
    this.draw();
  }

  toScreen(c) {
    const v = this.view;
    return [(c.x - v.cx) * v.k + this.w / 2, (c.y - v.cy) * v.k + this.hgt / 2];
  }

  draw() {
    const ctx = this.ctx, tk = this.style.tokens;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.hgt);
    this.screen = [];
    if (!tk) return;
    // Biggest first, so small clusters stay on top and stay hittable.
    for (const c of this.clusters) {
      const [sx, sy] = this.toScreen(c);
      const r = Math.max(MIN_R, this.radiusData(c) * this.view.k);
      if (sx + r < 0 || sx - r > this.w || sy + r < 0 || sy - r > this.hgt) continue;
      const { color, alpha } = this.style.fill(c);
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, 2 * Math.PI);
      ctx.globalAlpha = alpha; ctx.fillStyle = color; ctx.fill();
      ctx.globalAlpha = 0.45; ctx.lineWidth = 1; ctx.strokeStyle = tk.muted; ctx.stroke();
      ctx.globalAlpha = 1; ctx.lineWidth = 2; ctx.strokeStyle = tk.surface; ctx.beginPath(); ctx.arc(sx, sy, r + 1, 0, 2 * Math.PI); ctx.stroke(); // surface ring on overlaps
      const parts = this.style.core?.(c);
      if (parts?.length) this.drawCore(sx, sy, r, c.n, parts, tk);
      this.screen.push({ c, sx, sy, r });
    }
    for (const id of [this.hoverId, this.selectedId]) {
      const e = this.screen.find((s) => s.c.id === id);
      if (!e) continue;
      ctx.beginPath(); ctx.arc(e.sx, e.sy, e.r + 1, 0, 2 * Math.PI);
      ctx.lineWidth = id === this.selectedId ? 2.5 : 1.5; ctx.strokeStyle = tk.ink; ctx.stroke();
    }
    this.drawLabels(tk);
  }

  /** Part of a cluster as a disc at the bubble's centre, on the same area scale as the bubble: parts [{color, n}] are
   *  counts of the cluster's posts (e.g. those mentioning a highlighted person); several parts split the disc into wedges. */
  drawCore(sx, sy, r, total, parts, tk) {
    const ctx = this.ctx, sum = parts.reduce((a, p) => a + p.n, 0);
    const cr = Math.min(r, Math.max(MIN_CORE_R, r * Math.sqrt(Math.min(1, sum / total))));
    let a = -Math.PI / 2;                                         // wedges start at 12 o'clock, in colour-slot order
    for (const p of parts) {
      const da = (2 * Math.PI * p.n) / sum;
      ctx.beginPath();
      if (parts.length > 1) ctx.moveTo(sx, sy);
      ctx.arc(sx, sy, cr, a, a + da); ctx.closePath();
      ctx.fillStyle = p.color; ctx.fill();
      if (cr >= 6) { ctx.lineWidth = 1.5; ctx.strokeStyle = tk.surface; ctx.stroke(); }   // surface gap between wedges and round the core
      a += da;
    }
  }

  /** Cluster names, plus an optional second line from style.sublabel(c) -> {text, color, weight, once?} (e.g. the
   *  highlighted person a bubble is coloured by). Bubbles with a second line are labelled first, heaviest first, whatever
   *  their size, then the rest, biggest first. A second line with a `once` key (e.g. a topic's name) is drawn only on the
   *  first bubble whose label fits; the key's other bubbles keep their priority but get the name line only. */
  drawLabels(tk) {
    const ctx = this.ctx, sub = this.style.sublabel;
    ctx.textBaseline = "middle"; ctx.textAlign = "center"; ctx.lineJoin = "round";
    const placed = [];
    const hit = (b) => placed.some((p) => b.x < p.x + p.w && b.x + b.w > p.x && b.y < p.y + p.h && b.y + b.h > p.y);
    const halo = (text, x, y, fill) => {
      ctx.lineWidth = 3; ctx.strokeStyle = tk.surface; ctx.strokeText(text, x, y);
      ctx.fillStyle = fill; ctx.fillText(text, x, y);          // text stays in ink, never the series colour
    };
    const items = this.screen.map((e) => ({ e, s: sub ? sub(e.c) : null }));
    items.sort((a, b) => (b.s?.weight ?? -1) - (a.s?.weight ?? -1));   // stable: unlabelled stay biggest first
    // A label never covers another bubble (bar specks), so it cannot be read as belonging to it.
    const blockers = items.filter(({ e, s }) => e.r >= BLOCK_R || s).map(({ e }) => e);
    const covers = (b, self) => blockers.some((o) => o !== self &&
      Math.hypot(o.sx - Math.min(Math.max(o.sx, b.x), b.x + b.w), o.sy - Math.min(Math.max(o.sy, b.y), b.y + b.h)) < o.r - 1);
    const named = new Set();                                       // `once` keys already drawn
    let count = 0;
    for (const { e, s: s0 } of items) {
      const s = s0?.once != null && named.has(s0.once) ? null : s0;
      if (!e.c.label || (e.r < 10 && !s0) || count >= 80) continue;
      const text = e.c.label.length > 30 ? e.c.label.slice(0, 29) + "…" : e.c.label;
      ctx.font = LABEL_FONT;
      const tw = ctx.measureText(text).width;
      ctx.font = SUB_FONT;
      const sw = s ? ctx.measureText(s.text).width + 12 : 0;   // 12px for the colour dot
      const w = Math.max(tw, sw) + 4, h = s ? 30 : 16;
      // keep a centred label inside the canvas even when its bubble sits at the edge
      const x = Math.min(Math.max(e.sx - w / 2, 2), this.w - w - 2);
      // inside the bubble when it fits, else below, above, right or left of it: the first spot that is free
      const spots = w + 4 <= e.r * 1.8 && h + 4 <= e.r * 1.6 ? [{ x, y: e.sy - h / 2 }] : [
        { x, y: e.sy + e.r + 2 }, { x, y: e.sy - e.r - 2 - h },
        { x: e.sx + e.r + 4, y: e.sy - h / 2 }, { x: e.sx - e.r - 4 - w, y: e.sy - h / 2 }];
      const box = spots.map((p) => ({ ...p, w, h }))
        .find((b) => b.x >= 0 && b.x + b.w <= this.w && !hit(b) && !covers(b, e));
      if (!box) continue;                                          // no unambiguous spot: zoom in to see it
      placed.push(box); count++;
      if (s?.once != null) named.add(s.once);
      const cx = box.x + w / 2, top = box.y;
      ctx.font = LABEL_FONT; halo(text, cx, top + 8, tk.ink);
      if (s) {
        const y = top + 22, x0 = cx - sw / 2;
        ctx.beginPath(); ctx.arc(x0 + 4, y, 4, 0, 2 * Math.PI); ctx.fillStyle = s.color; ctx.fill();
        ctx.lineWidth = 1; ctx.strokeStyle = tk.surface; ctx.stroke();
        ctx.font = SUB_FONT; halo(s.text, cx + 6, y, tk.ink2);
      }
    }
  }

  pick(px, py) {
    for (let i = this.screen.length - 1; i >= 0; i--) {
      const e = this.screen[i];
      if (Math.hypot(px - e.sx, py - e.sy) <= Math.max(e.r, HIT_PAD)) return e;
    }
    return null;
  }

  local(ev) {
    const r = this.canvas.getBoundingClientRect();
    return [ev.clientX - r.left, ev.clientY - r.top];
  }

  down(ev) {
    this.canvas.setPointerCapture(ev.pointerId);
    const [x, y] = this.local(ev);
    this.drag = { x, y, moved: 0 };
  }

  move(ev) {
    const [x, y] = this.local(ev);
    if (this.drag) {
      const dx = x - this.drag.x, dy = y - this.drag.y;
      this.drag.moved += Math.abs(dx) + Math.abs(dy);
      this.drag.x = x; this.drag.y = y;
      if (this.drag.moved > 4) { this.panBy(dx, dy); this.h.onHover(null); return; }
    }
    const e = this.pick(x, y);
    const id = e ? e.c.id : null;
    if (id !== this.hoverId) { this.hoverId = id; this.draw(); }
    this.h.onHover(e, x, y);
  }

  up(ev) {
    const d = this.drag; this.drag = null;
    if (!d || d.moved > 4) return;
    const [x, y] = this.local(ev);
    const e = this.pick(x, y);
    this.h.onSelect(e ? e.c : null);
  }

  leave() { this.hoverId = null; this.h.onHover(null); this.draw(); }

  wheel(ev) {
    ev.preventDefault();
    const [x, y] = this.local(ev);
    this.zoomBy(Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)), x, y);
  }

  key(ev) {
    const step = 60, map = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (map[ev.key]) { this.panBy(...map[ev.key]); ev.preventDefault(); }
    else if (ev.key === "+" || ev.key === "=") this.zoomBy(1.3);
    else if (ev.key === "-") this.zoomBy(1 / 1.3);
    else if (ev.key === "0" || ev.key === "Home") this.fit();
  }
}
