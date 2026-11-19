/** Resolves CSS tokens for the canvas, which cannot use var() directly. Re-read after a theme change. */
export function readTokens() {
  const cs = getComputedStyle(document.documentElement);
  const g = (n) => cs.getPropertyValue(n).trim();
  return {
    surface: g("--surface"), ink: g("--ink"), ink2: g("--ink-2"), muted: g("--muted"),
    series: [g("--series-1"), g("--series-2"), g("--series-3")],
    other: g("--other"),
    cool: g("--div-cool"), mid: g("--div-mid"), warm: g("--div-warm"),
  };
}

const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

export function mix(a, b, t) {
  const [r1, g1, b1] = hex(a), [r2, g2, b2] = hex(b);
  const c = (x, y) => Math.round(x + (y - x) * t);
  return `rgb(${c(r1, r2)}, ${c(g1, g2)}, ${c(b1, b2)})`;
}

/** Diverging colour for trend in [-1, 1]: cool for cooling, warm for heating up, gray-ish at steady. */
export function trendColor(trend, tk) {
  const m = Math.min(1, Math.abs(trend) ** 0.6 * 1.15);
  return mix(tk.mid, trend >= 0 ? tk.warm : tk.cool, m);
}
