/** Tiny DOM helper. Text always goes in as text nodes, never as HTML. */
export function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, "");
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

const SVG = "http://www.w3.org/2000/svg";
export function svg(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
}

export const clear = (n) => { while (n.firstChild) n.firstChild.remove(); return n; };

export const fmt = {
  num: (v) => (v == null ? "–" : Math.abs(v) >= 1e9 ? (v / 1e9).toFixed(1) + "B" : Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + "M"
    : Math.abs(v) >= 1e4 ? (v / 1e3).toFixed(0) + "k" : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(1) + "k" : String(Math.round(v * 10) / 10)),
  int: (v) => (v == null ? "–" : Math.round(v).toLocaleString()),
  bytes: (v) => {
    if (v == null) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
  },
  usd: (v) => (v == null ? "–" : "$" + (v >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(2))),
  pct: (v) => (v > 0 && v < 0.005 ? "<1%" : `${Math.round(v * 100)}%`),
  dur: (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`),
  ago: (iso) => {
    if (!iso) return "–";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  },
};
