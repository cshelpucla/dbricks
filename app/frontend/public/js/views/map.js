/** Topic map tab: filters above a bubble map of topic clusters, with legend and cluster details. */
import { getJSON, filterParams } from "../api.js";
import { readTokens, trendColor } from "../colors.js";
import { clear, el, fmt, svg } from "../lib/dom.js";
import { multiSelect, singleSelect } from "../lib/controls.js";
import { entityBadge, withFlag } from "../lib/badges.js";
import { MapView } from "../mapview.js";

const TIME = [[1, "Last hour"], [6, "Last 6 hours"], [24, "Last 24 hours"], [72, "Last 3 days"], [168, "Last 7 days"]];
const NO_TOPIC = "no clear topic";      // clusters of chatter the namer gave no subject
const MAX_HIGHLIGHTS = 3;   // scatter-style marks keep distinguishable colours only up to three categories
const KINDS = { people: "person", companies: "company" };   // colour modes that highlight entities
// A cluster takes a highlighted person's or company's colour only when they are a real presence in it, not a passing mention.
const MIN_MENTIONS = 3, MIN_MENTION_SHARE = 0.01;
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const langName = (c) => { try { return new Intl.DisplayNames(["en"], { type: "language" }).of(c) || c; } catch { return c; } };
const countryName = (c) => { try { return new Intl.DisplayNames(["en"], { type: "region" }).of(c) || c; } catch { return c; } };
const trendWord = (t) => (t > 0.15 ? "Heating up" : t < -0.15 ? "Cooling" : "Steady");
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

export function init({ onError, setBadge }) {
  const $ = (id) => document.getElementById(id);
  const s = { hours: 24, topics: [], langs: [], countries: [], people: [], companies: [], minScore: 0, hasGeo: false, q: "",
              colorBy: "topic", view: "map", selected: null, highlights: new Map(), sort: { key: "n", dir: -1 } };
  const hl = { people: new Map(), companies: new Map() };          // entity id -> colour slot, like s.highlights for topics
  let sector = "";                                                  // Companies panel: show one sector only ("" = all)
  const names = new Map();                                          // entity id -> display name
  const nameOf = (id) => names.get(id) || id;
  const info = new Map();                                           // entity id -> its /api/meta entry (portrait, industry, sector)
  const badge = (kind, id, size) => entityBadge(kind, info.get(id) ?? { label: nameOf(id) }, size);
  // Top mentions with their share of the cluster; in a People/Companies mode the highlighted ones come first.
  const topMentions = (c, k) => [...c[k]].sort((a, b) => (s.colorBy === k && hl[k].has(b[0])) - (s.colorBy === k && hl[k].has(a[0]))).slice(0, 3);
  const mentionText = (c, q, n) => `${nameOf(q)} ${fmt.int(n)} (${fmt.pct(n / c.n)})`;
  const mentions = (c, k) => topMentions(c, k).map(([q, n]) => mentionText(c, q, n));
  const mentionChips = (c, k, list = topMentions(c, k)) => list.map(([q, n]) => el("span", { class: "mention" }, badge(k, q, 18), mentionText(c, q, n)));
  let mode = null, meta = null, data = { clusters: [], topics: [], total: 0 }, tokens = readTokens(), loadToken = 0;

  const map = new MapView($("topicMap"), {
    onHover(e, x, y) {
      const tip = $("tip");
      if (!e) { tip.hidden = true; return; }
      const c = e.c;
      clear(tip).append(el("div", { class: "v" }, `${fmt.int(c.n)} posts`), el("div", { class: "l" }, cap(c.label) || "Unlabelled cluster"),
        el("div", { class: "s" }, `${cap(data.topics[c.topic])} · ${trendWord(c.trend)}`),
        c.langs.length ? el("div", { class: "s" }, c.langs.map(([l, sh]) => `${l} ${fmt.pct(sh)}`).join(" · ")) : null,
        ...["people", "companies"].map((k) => (mentions(c, k).length ? el("div", { class: "s" }, `${cap(k)}: ${mentions(c, k).join(" · ")}`) : null)));
      tip.hidden = false;
      const w = $("canvasWrap").clientWidth;
      tip.style.left = `${x + 16 + tip.offsetWidth > w ? x - tip.offsetWidth - 12 : x + 16}px`;
      tip.style.top = `${Math.max(4, y - 10)}px`;
    },
    onSelect(c) { select(c ? c.id : null); },
  });

  // The highlighted entity mentioned most in this cluster, if it clears the presence threshold: [id, mentions] or null.
  const entityHit = (c) => {
    if (!(s.colorBy in hl)) return null;
    const min = Math.max(MIN_MENTIONS, c.n * MIN_MENTION_SHARE);
    return c[s.colorBy].filter(([q, n]) => hl[s.colorBy].has(q) && n >= min).reduce((a, b) => (!a || b[1] > a[1] ? b : a), null);
  };
  // A topic is what a whole cluster is about, so it colours the whole bubble. People and companies are mentioned by part
  // of a cluster, so their colour fills only a core sized to the posts that mention them; the rest of the bubble stays grey.
  const fillFor = (c) => {
    if (s.colorBy === "trend") return { color: trendColor(c.trend, tokens), alpha: 0.9 };
    if (s.colorBy in hl) return { color: tokens.other, alpha: 1 };
    const slot = s.highlights.get(data.topics[c.topic]);
    return slot === undefined ? { color: tokens.other, alpha: 1 } : { color: tokens.series[slot], alpha: 0.8 };
  };
  const coreFor = (c) => {                                          // [{color, n}] per highlighted entity, in colour-slot order
    if (!(s.colorBy in hl)) return null;
    const sel = hl[s.colorBy];
    return c[s.colorBy].filter(([q]) => sel.has(q)).sort((a, b) => sel.get(a[0]) - sel.get(b[0]))
      .map(([q, n]) => ({ color: tokens.series[sel.get(q)], n }));
  };
  const sublabel = (c) => {
    if (s.colorBy in hl) {                                          // the person or company with the biggest core, and its share
      const best = entityHit(c);
      return best && { text: `${nameOf(best[0])} · ${fmt.int(best[1])} of ${fmt.int(c.n)} posts`, color: tokens.series[hl[s.colorBy].get(best[0])], weight: best[1] };
    }
    const t = data.topics[c.topic], slot = s.highlights.get(t);    // each highlighted topic is named once, on its biggest cluster that has room
    return slot === undefined ? null : { text: cap(t), color: tokens.series[slot], weight: c.n, once: t };
  };
  const mapStyle = () => ({ fill: fillFor, core: coreFor, sublabel: s.colorBy === "trend" ? null : sublabel, tokens });
  const restyle = () => {
    tokens = readTokens(); map.setStyle(mapStyle()); renderKey(); renderLegend(); renderEntityLegends();
    if (s.view === "table") renderTable();
  };
  document.addEventListener("themechange", restyle);

  // ---- filters
  const filters = $("filters");
  const topicSel = multiSelect({ label: "Topic", fmtOpt: cap, searchable: true, onChange: (v) => change({ topics: v }) });
  const langSel = multiSelect({ label: "Language", fmtOpt: langName, searchable: true, onChange: (v) => change({ langs: v }) });
  const countrySel = multiSelect({ label: "Country", fmtOpt: (c) => withFlag(c, countryName(c)), searchable: true, onChange: (v) => change({ countries: v }) });
  const personSel = multiSelect({ label: "Person", fmtOpt: nameOf, iconOf: (q) => badge("people", q, 20), searchable: true, onChange: (v) => change({ people: v }) });
  const companySel = multiSelect({ label: "Company", fmtOpt: nameOf, iconOf: (q) => badge("companies", q, 20), searchable: true, onChange: (v) => change({ companies: v }) });
  const conf = el("input", { type: "range", min: "0", max: "0.9", step: "0.05", value: "0", id: "conf", "aria-label": "Minimum topic confidence" });
  const confOut = el("output", { for: "conf", class: "muted" }, "any");
  conf.addEventListener("input", () => { confOut.textContent = +conf.value ? `≥ ${(+conf.value).toFixed(2)}` : "any"; change({ minScore: +conf.value }, true); });
  const geo = el("input", { type: "checkbox", id: "geo" });
  geo.addEventListener("change", () => change({ hasGeo: geo.checked }));
  const search = el("input", { type: "search", placeholder: "Search post text", "aria-label": "Search post text" });
  search.addEventListener("input", debounce(() => change({ q: search.value.trim() }), 350));
  const reset = el("button", { class: "btn ghost", type: "button", text: "Reset", onclick: () => {
    Object.assign(s, { topics: [], langs: [], countries: [], people: [], companies: [], minScore: 0, hasGeo: false, q: "" });
    [topicSel, langSel, countrySel, personSel, companySel].forEach((x) => x.setSelected([])); conf.value = "0"; confOut.textContent = "any"; geo.checked = false; search.value = "";
    load();
  } });
  filters.append(
    singleSelect({ label: "Time window", options: TIME.map(([value, label]) => ({ value, label })), value: s.hours, onChange: (h) => { s.hours = h; loadMeta().then(load); } }).el,
    topicSel.el, langSel.el, countrySel.el, personSel.el, companySel.el,
    el("div", { class: "field" }, el("label", { for: "conf" }, "Topic confidence"), el("div", { class: "row-controls" }, conf, confOut)),
    el("div", { class: "field" }, el("span", { class: "lbl" }, "Location"), el("label", { class: "check", for: "geo" }, geo, "Has inferred location")),
    el("div", { class: "field" }, el("label", {}, "Search"), search), reset);

  function change(patch, soft) { Object.assign(s, patch); soft ? debouncedLoad() : load(); }
  const debouncedLoad = debounce(() => load(), 250);

  // ---- toolbar
  document.querySelectorAll("[data-color]").forEach((b) => b.addEventListener("click", () => {
    setColorBy(b.dataset.color);
  }));
  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    s.view = b.dataset.view; pressed("[data-view]", b);
    $("canvasWrap").hidden = $("mapKey").hidden = s.view !== "map"; $("tableWrap").hidden = s.view !== "table";
    if (s.view === "map") map.resize(); else renderTable();
  }));
  function setColorBy(mode) {
    s.colorBy = mode;
    document.querySelectorAll("[data-color]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.color === mode)));
    restyle();
  }
  const pressed = (sel, on) => document.querySelectorAll(sel).forEach((b) => b.setAttribute("aria-pressed", String(b === on)));
  $("zoomIn").addEventListener("click", () => map.zoomBy(1.4));
  $("zoomOut").addEventListener("click", () => map.zoomBy(1 / 1.4));
  $("fit").addEventListener("click", () => map.fit());

  // ---- data
  async function loadMeta() {
    meta = await getJSON("/api/meta", new URLSearchParams({ hours: String(s.hours) }));
    mode = meta.mode; setBadge(mode);
    topicSel.setOptions(meta.topics); langSel.setOptions(meta.langs); countrySel.setOptions(meta.countries);
    for (const k of Object.keys(hl)) {
      for (const p of meta[k] ?? []) { names.set(p.value, p.label); info.set(p.value, p); }
      for (const id of [...hl[k].keys()]) if (!(meta[k] ?? []).some((x) => x.value === id)) hl[k].delete(id);
      if (!hl[k].size) (meta[k] ?? []).slice(0, MAX_HIGHLIGHTS).forEach((p, i) => hl[k].set(p.value, i));
    }
    personSel.setOptions((meta.people ?? []).map(({ value, count }) => ({ value, count })));
    companySel.setOptions((meta.companies ?? []).map(({ value, count }) => ({ value, count })));
    for (const t of [...s.highlights.keys()]) if (!meta.topics.some((x) => x.value === t)) s.highlights.delete(t);
    if (!s.highlights.size) meta.topics.filter((t) => t.value !== NO_TOPIC).slice(0, MAX_HIGHLIGHTS).forEach((t, i) => s.highlights.set(t.value, i));
  }

  async function load() {
    const token = ++loadToken;
    $("canvasWrap").classList.add("loading");                       // keep the previous frame while refetching
    try {
      const next = await getJSON("/api/clusters", filterParams(s));
      if (token !== loadToken) return;
      data = next; onError(null);
      for (const [q, label] of Object.entries(data.entities ?? {})) names.set(q, label);
      const first = !map.clusters.length;
      map.setClusters(data.clusters, { refit: first });
      map.setStyle(mapStyle());
      if (s.selected != null && !data.clusters.some((c) => c.id === s.selected)) s.selected = null;
      map.select(s.selected);
      $("empty").hidden = data.clusters.length > 0;
      const built = meta?.map_computed_at ? ` · map built ${fmt.ago(meta.map_computed_at)} from a sample of ${fmt.int(meta.map_points)} posts` : "";
      $("summary").textContent = `${fmt.int(data.total)} posts in ${data.clusters.length} clusters · ${TIME.find((t) => t[0] === s.hours)?.[1].toLowerCase()}${built}`;
      renderKey(); renderLegend(); renderEntityLegends(); renderDetail(); if (s.view === "table") renderTable();
    } catch (err) { if (token === loadToken) onError(`Could not load clusters: ${err.message}`); }
    finally { if (token === loadToken) $("canvasWrap").classList.remove("loading"); }
  }

  // ---- key strip above the map: what the colour summarises in the current mode, and what each colour is
  const TREND_GRAD = "background:linear-gradient(90deg,var(--div-cool),var(--div-mid),var(--div-warm))";
  const glyph = () => svg("svg", { class: "glyph", width: 18, height: 18, viewBox: "0 0 18 18", "aria-hidden": "true" },   // a bubble with a core
    svg("circle", { cx: 9, cy: 9, r: 8, style: "fill:var(--other)" }),
    svg("circle", { cx: 9, cy: 9, r: 4, style: "fill:var(--ink-2);stroke:var(--surface);stroke-width:1.5" }));
  const swatch = (color, text, icon) => el("span", { class: "k-item" }, el("span", { class: "sw on", style: `background:${color}` }), icon, text);
  function renderKey() {
    const box = clear($("mapKey"));
    const head = (mode, what) => el("span", {}, el("strong", {}, mode), ` · ${what}`);
    if (s.colorBy === "trend") {
      box.append(head("Trend", "colour = whether a cluster is growing faster or slower than all posts"),
        el("span", { class: "k-item" }, "Cooling", el("span", { class: "grad", style: TREND_GRAD }), "Heating up"));
    } else if (s.colorBy in hl) {
      const picks = [...hl[s.colorBy]].sort((a, b) => a[1] - b[1]);
      box.append(glyph(), head(cap(s.colorBy), "coloured core = the cluster's posts that mention"),
        ...(picks.length ? picks.map(([q, slot]) => swatch(tokens.series[slot], nameOf(q), badge(s.colorBy, q, 20))) : [el("span", {}, "(none picked: choose in the side panel)")]),
        el("span", {}, "· grey = the rest of the cluster"));
    } else {
      const picks = [...s.highlights].sort((a, b) => a[1] - b[1]);
      box.append(head("Topic", "whole bubble = the cluster's main topic"),
        ...picks.map(([t, slot]) => swatch(tokens.series[slot], cap(t))), swatch(tokens.other, "Other topics"));
    }
  }

  // ---- legend
  // The panel for a colour mode that is not showing folds to one line: what it would highlight, and a way to show it.
  // No swatches there: the colours on the map now mean something else.
  // picks: [name, slot, picture?]
  function folded(box, title, picks, mode) {
    picks.sort((a, b) => a[1] - b[1]);
    box.append(el("div", { class: "card-head" }, el("h2", { text: title }),
        el("button", { class: "link", type: "button", onclick: () => setColorBy(mode) }, "Show on map")),
      !picks.length ? el("p", {}, "None picked")
        : picks.some((p) => p[2]) ? el("p", { class: "mentions" }, picks.map(([t, , pic]) => el("span", { class: "mention" }, pic, t)))
        : el("p", {}, picks.map((p) => p[0]).join(", ")));
  }
  function renderLegend() {
    const box = clear($("legend"));
    box.classList.toggle("active", !(s.colorBy in hl));             // the active key sits at the top of the side panel
    if (s.colorBy === "trend") {
      box.append(el("h2", { text: "Trend" }), el("p", { text: "Posts in the newer half of the window compared with the older half, relative to all topics." }),
        el("div", { class: "grad", style: TREND_GRAD }),
        el("div", { class: "grad-l" }, el("span", {}, "Cooling"), el("span", {}, "Steady"), el("span", {}, "Heating up")));
      return;
    }
    if (s.colorBy in hl) return folded(box, "Highlight topics", [...s.highlights].map(([t, slot]) => [cap(t), slot]), "topic");
    box.append(el("h2", { text: "Highlight topics" }), el("p", { id: "legNote", text: `Pick up to ${MAX_HIGHLIGHTS}. Every cluster on a highlighted topic is coloured whole; other topics stay grey. Hover a bubble for its topic.` }));
    const list = el("div", { class: "leg" });
    for (const t of meta?.topics ?? []) {
      const slot = s.highlights.get(t.value);
      list.append(el("button", { class: "leg-row", type: "button", "aria-pressed": String(slot !== undefined), onclick: () => toggleTopic(t.value) },
        el("span", { class: slot === undefined ? "sw" : "sw on", style: slot === undefined ? "" : `background:${tokens.series[slot]}` }), cap(t.value), el("span", { class: "n" }, fmt.num(t.count))));
    }
    box.append(list);
  }
  function renderEntityLegends() {
    for (const [kind, boxId, title] of [["people", "legendPeople", "Highlight people"], ["companies", "legendCompanies", "Highlight companies"]]) {
      const box = clear($(boxId)), list = meta?.[kind] ?? [];
      box.classList.toggle("active", s.colorBy === kind);
      if (!list.length) { box.hidden = true; continue; }              // nothing yet: the taxonomy jobs have not run
      box.hidden = false;
      if (s.colorBy !== kind) { folded(box, title, [...hl[kind]].map(([q, slot]) => [nameOf(q), slot, badge(kind, q, 20)]), kind); continue; }
      box.append(el("h2", { text: title }), el("p", { id: `${boxId}Note`, text: `Pick up to ${MAX_HIGHLIGHTS}. A cluster's coloured core is the posts in it that mention them, on the same scale as the bubble, so a passing mention is a dot. Clusters where at least ${MIN_MENTIONS} posts and ${fmt.pct(MIN_MENTION_SHARE)} of the cluster mention one are labelled with the count.` }));
      if (kind === "companies") {                                     // sector > industry > company
        const sectors = [...new Set(list.map((t) => t.sector).filter(Boolean))].sort();
        if (sectors.length) {
          const pick = el("select", { "aria-label": "Sector", onchange: (e) => { sector = e.target.value; renderEntityLegends(); } },
            el("option", { value: "" }, "All sectors"), sectors.map((x) => el("option", { value: x, selected: x === sector }, x)));
          box.append(pick);
        }
      }
      const rows = el("div", { class: "leg" });
      for (const t of list.filter((t) => kind !== "companies" || !sector || t.sector === sector)) {
        const slot = hl[kind].get(t.value);
        rows.append(el("button", { class: "leg-row", type: "button", title: t.description || "", "aria-pressed": String(slot !== undefined), onclick: () => toggleEntity(kind, t.value) },
          el("span", { class: slot === undefined ? "sw" : "sw on", style: slot === undefined ? "" : `background:${tokens.series[slot]}` }), entityBadge(kind, t, 24), t.label,
          kind === "companies" && t.industry ? el("span", { class: "sub" }, t.industry) : null, el("span", { class: "n" }, fmt.num(t.count))));
      }
      box.append(rows);
    }
  }
  function toggleEntity(kind, id) {
    const map_ = hl[kind];
    if (map_.has(id)) map_.delete(id);
    else {
      const used = new Set(map_.values()), free = [0, 1, 2].find((i) => !used.has(i));
      if (free === undefined) { $(`${kind === "people" ? "legendPeople" : "legendCompanies"}Note`).textContent = `Already highlighting ${MAX_HIGHLIGHTS}. Deselect one first.`; return; }
      map_.set(id, free);
    }
    setColorBy(kind);                                                 // picking from a panel colours the map by that panel
  }
  function toggleTopic(name) {
    if (s.highlights.has(name)) s.highlights.delete(name);
    else {
      const used = new Set(s.highlights.values()), free = [0, 1, 2].find((i) => !used.has(i));
      if (free === undefined) { $("legNote").textContent = `Already highlighting ${MAX_HIGHLIGHTS} topics. Deselect one first.`; return; }
      s.highlights.set(name, free);                                   // colour follows the topic, never its rank
    }
    if (s.colorBy in hl) setColorBy("topic"); else restyle();
  }

  // ---- details
  function select(id) { s.selected = id; map.select(id); renderDetail(); if (s.view === "table") renderTable(); }
  async function renderDetail() {
    const box = clear($("detail")), c = data.clusters.find((x) => x.id === s.selected);
    if (!c) { box.append(el("h2", { text: "Cluster details" }), el("p", { text: "Click a bubble to see what is in it and its recent posts." })); return; }
    box.append(el("div", { class: "crumb" }, `${cap(data.topics[c.topic])} ›`), el("h2", { text: cap(c.label) || "Unlabelled cluster" }),
      el("div", { class: "stats" },
        el("div", { class: "stat" }, el("div", { class: "v" }, fmt.int(c.n)), el("div", { class: "k" }, "posts")),
        el("div", { class: "stat" }, el("div", { class: "v" }, fmt.pct(c.n / Math.max(1, data.total))), el("div", { class: "k" }, "of shown")),
        el("div", { class: "stat" }, el("div", { class: "v" }, trendWord(c.trend)), el("div", { class: "k" }, "trend"))),
      c.langs.length ? el("p", {}, "Languages: " + c.langs.map(([l, sh]) => `${langName(l)} ${fmt.pct(sh)}`).join(" · ")) : null,
      c.score != null ? el("p", {}, `Average topic confidence ${c.score.toFixed(2)}`) : null,
      ...["people", "companies"].map((k) => (c[k].length ? el("p", { class: "mentions" }, `${cap(k)} mentioned:`, mentionChips(c, k)) : null)));
    const list = el("div", {}, el("p", { text: "Loading recent posts…" }));
    box.append(el("h2", { text: "Recent posts" }), list);
    const id = c.id;
    try {
      const res = await getJSON(`/api/clusters/${id}/posts`, filterParams(s));
      if (s.selected !== id) return;
      clear(list);
      if (!res.posts.length) list.append(el("p", { text: "No posts to show." }));
      for (const p of res.posts) {
        const link = p.url && /^https:\/\/bsky\.app\//.test(p.url) ? el("a", { href: p.url, target: "_blank", rel: "noopener noreferrer" }, "Open") : null;
        list.append(el("div", { class: "post" }, el("div", { class: "t" }, p.text || ""),
          el("div", { class: "m" }, [p.lang && langName(p.lang), p.country ? withFlag(p.country, p.place || countryName(p.country)) : p.place, fmt.ago(new Date(Date.now() - p.age_hours * 3600e3).toISOString())].filter(Boolean).map((t) => el("span", {}, t)), link)));
      }
    } catch (err) { if (s.selected === id) { clear(list); list.append(el("p", { text: `Could not load posts: ${err.message}` })); } }
  }

  // ---- table view (same data as the map, as a list)
  function renderTable() {
    const wrap = clear($("tableWrap"));
    const kind = s.colorBy in hl ? s.colorBy : null;                // People/Companies: the mentions the map's cores show
    const hits = (c) => c[kind].filter(([q]) => hl[kind].has(q));
    if (s.sort.key === "hits" && !kind) s.sort = { key: "n", dir: -1 };
    // [key, header, numeric sort, right-aligned cell]
    const cols = [["label", "Cluster", false], ["topic", "Topic", false], ["n", "Posts", true, true], ["share", "Share", true, true],
      kind && ["hits", `Highlighted ${kind} mentioned`, true], ["trend", "Trend", true, true], ["langs", "Top languages", false], ["score", "Confidence", true, true]].filter(Boolean);
    const val = (c, k) => (k === "label" ? c.label : k === "topic" ? data.topics[c.topic] : k === "share" ? c.n : k === "langs" ? c.langs[0]?.[0] ?? ""
      : k === "hits" ? hits(c).reduce((a, [, n]) => a + n, 0) : c[k] ?? -1);
    const cell = {
      label: (c) => cap(c.label) || "–", topic: (c) => cap(data.topics[c.topic]), n: (c) => fmt.int(c.n), share: (c) => fmt.pct(c.n / Math.max(1, data.total)),
      hits: (c) => (hits(c).length ? el("span", { class: "mentions" }, mentionChips(c, kind, hits(c))) : "–"),
      trend: (c) => trendWord(c.trend), langs: (c) => c.langs.map(([l, sh]) => `${l} ${fmt.pct(sh)}`).join(", "), score: (c) => (c.score == null ? "–" : c.score.toFixed(2)),
    };
    const rows = [...data.clusters].sort((a, b) => { const x = val(a, s.sort.key), y = val(b, s.sort.key); return (x > y ? 1 : x < y ? -1 : 0) * s.sort.dir; });
    wrap.append(el("table", {},
      el("thead", {}, el("tr", {}, cols.map(([k, name, num, right]) => el("th", { class: right ? "num" : "", "aria-sort": s.sort.key === k ? (s.sort.dir > 0 ? "ascending" : "descending") : "none",
        onclick: () => { s.sort = { key: k, dir: s.sort.key === k ? -s.sort.dir : (num ? -1 : 1) }; renderTable(); } }, name)))),
      el("tbody", {}, rows.map((c) => el("tr", { class: c.id === s.selected ? "sel" : "", tabindex: "0", onclick: () => select(c.id), onkeydown: (e) => { if (e.key === "Enter") select(c.id); } },
        cols.map(([k, , , right]) => el("td", { class: right ? "num" : "" }, cell[k](c))))))));
  }

  return {
    activate() { map.resize(); },
    badge: () => mode,
    start: async () => { try { await loadMeta(); await load(); } catch (err) { onError(`Could not load the map: ${err.message}`); } },
  };
}
