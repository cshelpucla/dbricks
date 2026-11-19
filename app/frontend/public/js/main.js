/** Tab router and shared chrome: theme, mode badge, error banner. Each tab loads on first visit. */
const TABS = {
  map: () => import("./views/map.js"),
  world: () => import("./views/worldmap.js"),
  pipelines: () => import("./views/pipelines.js"),
  costs: () => import("./views/costs.js"),
  diagram: () => import("./views/diagram.js"),
  model: () => import("./views/model.js"),
  chat: () => import("./views/chat.js"),
  control: () => import("./views/control.js"),
};
const $ = (id) => document.getElementById(id);
const views = {};
let current = null;

function onError(msg) { const e = $("error"); e.textContent = msg || ""; e.hidden = !msg; }

function setBadge(mode) {
  const b = $("badge");
  if (!mode) { b.hidden = true; return; }
  b.hidden = false; b.className = `badge ${mode === "demo" ? "demo" : ""}`;
  b.textContent = mode === "demo" ? "Demo data" : "Live";
  b.title = mode === "demo" ? "Synthetic numbers: the enrichment and map jobs have not produced real data yet" : "Reading Unity Catalog through the SQL warehouse";
}

async function show(name) {
  if (!TABS[name]) name = "map";
  if (current && views[current]?.deactivate) views[current].deactivate();
  current = name; onError(null);
  document.querySelectorAll(".tabs [role=tab]").forEach((t) => { const on = t.dataset.tab === name; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; });
  document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  if (!views[name]) {
    const mod = await TABS[name]();
    views[name] = mod.init({ root: $(`tab-${name}`), onError, setBadge });
    if (views[name].start) await views[name].start();
  }
  await views[name].activate?.();
  setBadge(views[name].badge?.());
}

document.querySelector(".tabs").addEventListener("click", (e) => { const t = e.target.closest("[role=tab]"); if (t) location.hash = t.dataset.tab; });
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  const tabs = [...document.querySelectorAll(".tabs [role=tab]")], i = tabs.indexOf(document.activeElement);
  if (i < 0 || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
  const next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
  next.focus(); location.hash = next.dataset.tab;
});
window.addEventListener("hashchange", () => show(location.hash.slice(1)));

// Theme: auto follows the OS; the button cycles auto -> light -> dark.
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  $("theme").textContent = `Theme: ${t}`;
  try { localStorage.setItem("theme", t); } catch { /* storage unavailable */ }
  requestAnimationFrame(() => document.dispatchEvent(new Event("themechange")));
}
let saved = "auto"; try { saved = localStorage.getItem("theme") || "auto"; } catch { /* ignore */ }
applyTheme(THEMES.includes(saved) ? saved : "auto");
$("theme").addEventListener("click", () => applyTheme(THEMES[(THEMES.indexOf(document.documentElement.dataset.theme || "auto") + 1) % 3]));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => document.dispatchEvent(new Event("themechange")));

show(location.hash.slice(1));
