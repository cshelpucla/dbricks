/** Taxonomies tab: the vocabularies that tag posts (IPTC topics, people, companies) and name the map's clusters,
 *  each as a browsable tree with how many posts in the period each entry tagged. */
import { getJSON } from "../api.js";
import { clear, el, fmt } from "../lib/dom.js";
import { singleSelect } from "../lib/controls.js";
import { entityBadge, industryIcon } from "../lib/badges.js";

const RANGES = [[1, "Last hour"], [6, "Last 6 hours"], [24, "Last 24 hours"], [168, "Last 7 days"]];
const KEYS = ["iptc", "people", "companies", "clusters"];
const CHILD_CAP = 100;          // children listed under one parent before "Show all"
const MATCH_CAP = 400;          // search results shown
const NOUN = { iptc: ["concept", "concepts"], people: ["person", "people"], companies: ["company", "companies"], clusters: ["cluster", "clusters"] };
const count = (n, k) => `${fmt.int(n)} ${NOUN[k][n === 1 ? 0 : 1]}`;

export function init({ root, onError, setBadge }) {
  let mode = null, hours = 24, key = "iptc", overview = null, tree = null, loaded = false;
  let sort = "posts", q = "", seenOnly = false, selected = null, openTop = false;
  const expanded = new Set(), showAll = new Set(), trees = new Map();

  const controls = el("div", { class: "row-controls" });
  const fresh = el("span", { class: "muted small tax-fresh" });
  const cards = el("div", { class: "tax-cards", role: "group", "aria-label": "Taxonomy" });
  const note = el("div", { class: "note", hidden: true }, "Sample taxonomies with invented counts. They show the layout only.");
  const search = el("input", { type: "search", placeholder: "Search names, aliases, codes", "aria-label": "Search this taxonomy" });
  const sortSeg = el("div", { class: "seg", role: "group", "aria-label": "Sort by" });
  const seen = el("input", { type: "checkbox" });
  const metricHint = el("span", { class: "hint" });
  const head = el("div", { class: "tree-head" });
  const treeEl = el("div", { class: "tree", role: "tree", "aria-label": "Taxonomy entries" });
  const detail = el("section", { class: "card", "aria-live": "polite" });
  const about = el("section", { class: "card tax-about" });
  root.append(controls, cards, note, el("main", { class: "stage tax-stage" },
    el("section", { class: "viz" },
      el("div", { class: "toolbar" }, el("div", { class: "field" }, search), sortSeg,
        el("label", { class: "check" }, seen, "Only entries seen in this period"), metricHint),
      head, el("div", { class: "tree-scroll" }, treeEl)),
    el("aside", { class: "side", "aria-label": "Entry details and matching rules" }, detail, about)));

  controls.append(singleSelect({ label: "Period", options: RANGES.map(([value, label]) => ({ value, label })), value: hours,
    onChange: (v) => { hours = v; trees.clear(); load(); } }).el, fresh);
  let timer = null;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { q = search.value.trim().toLowerCase(); paintTree(); }, 150); });
  seen.addEventListener("change", () => { seenOnly = seen.checked; paintTree(); });

  async function load() {
    try {
      const params = new URLSearchParams({ hours: String(hours) });
      const [o] = await Promise.all([getJSON("/api/taxonomies", params), loadTree()]);
      overview = o; mode = o.mode; loaded = true; onError(null); setBadge(mode);
      note.hidden = mode !== "demo";
      fresh.textContent = o.window.newest ? `Newest tagged post: ${fmt.ago(o.window.newest)}. Counts cover posts the enrichment job has processed.` : "";
      paintCards();
    } catch (err) { onError(`Could not load taxonomies: ${err.message}`); }
  }

  async function loadTree() {
    const id = `${key}:${hours}`;
    if (!trees.has(id)) trees.set(id, index(await getJSON(`/api/taxonomies/${key}`, new URLSearchParams({ hours: String(hours) }))));
    tree = trees.get(id);
    if (openTop) { tree.roots.forEach((r) => expanded.add(r.id)); openTop = false; }
    if (selected && !tree.byId.has(selected)) selected = null;
    paintSort(); paintTree(); paintDetail(); paintAbout();
  }

  async function pick(k) {
    if (k === key) return;
    key = k; selected = null; q = ""; search.value = ""; expanded.clear(); showAll.clear();
    openTop = k === "clusters";          // subjects open by default: the point is how clusters map onto them
    paintCards();
    try { await loadTree(); onError(null); } catch (err) { onError(`Could not load ${k}: ${err.message}`); }
  }

  /** Parent/child links, depth, subtree totals (a node's own count plus its descendants') and leaf counts. */
  function index(res) {
    const byId = new Map(res.nodes.map((n) => [n.id, { ...n, kids: [] }])), roots = [];
    for (const n of byId.values()) (n.parent && byId.has(n.parent) ? byId.get(n.parent).kids : roots).push(n);
    const maxAt = [];
    const walk = (n, depth) => {
      n.depth = depth; n.total = n.posts; n.leaves = n.kids.length ? 0 : 1;
      for (const c of n.kids) { walk(c, depth + 1); n.total += c.total; n.leaves += c.leaves; }
      maxAt[depth] = Math.max(maxAt[depth] || 0, n.total);
    };
    roots.forEach((r) => walk(r, 0));
    return { ...res, byId, roots, maxAt };
  }

  // ---- overview cards ----------------------------------------------------------------------------------------
  function cardFacts(k) {
    const s = overview.sizes[k] || {}, w = overview.window, ago = s.loaded_at ? `loaded ${fmt.ago(s.loaded_at)}` : "";
    if (!s.available) return { big: "–", unit: "not loaded yet", lines: [s.error ? "Table missing or unreadable" : "Table is empty"] };
    const share = (a, b) => (b ? fmt.pct(a / b) : "–");
    if (k === "iptc") return { big: fmt.int(s.size), unit: `concepts on ${s.levels} levels`,
      lines: [`${share(w.with_topic, w.embedded)} of embedded posts got a topic`, `${fmt.int(w.concepts_seen)} concepts used · ${ago}`] };
    if (k === "people") return { big: fmt.int(s.size), unit: `people in ${s.groups} groups`,
      lines: [`${fmt.int(w.with_person)} posts mention one (${share(w.with_person, w.clean)})`, `${fmt.int(w.people_seen)} people seen · ${ago}`] };
    if (k === "companies") return { big: fmt.int(s.size), unit: `companies in ${s.sectors} sectors`,
      lines: [`${fmt.int(w.with_company)} posts mention one (${share(w.with_company, w.clean)})`, `${fmt.int(w.companies_seen)} seen · ${fmt.int(s.industries)} industries · ${ago}`] };
    return { big: fmt.int(s.size), unit: "clusters in the latest map",
      lines: [`${share(s.posts_with_subject, s.posts)} of mapped posts sit in a cluster with a subject`, s.loaded_at ? `computed ${fmt.ago(s.loaded_at)}` : ""] };
  }

  function paintCards() {
    if (!overview) return;
    clear(cards).append(...KEYS.map((k) => {
      const f = cardFacts(k);
      return el("button", { type: "button", class: "tax-card", "aria-pressed": String(k === key), onclick: () => pick(k) },
        el("span", { class: "t" }, overview.about[k].name),
        el("span", { class: "v" }, f.big, " ", el("span", { class: "u" }, f.unit)),
        ...f.lines.filter(Boolean).map((l) => el("span", { class: "l" }, l)));
    }));
  }

  // ---- tree --------------------------------------------------------------------------------------------------
  function paintSort() {
    const opts = [["posts", tree.about.metric === "mentions" ? "Mentions" : "Posts"], ["az", "A–Z"]];
    if (key === "people" || key === "companies") opts.push(["fame", "Wikipedia editions"]);
    if (!opts.some(([v]) => v === sort)) sort = "posts";
    clear(sortSeg).append(...opts.map(([v, label]) => el("button", { type: "button", "aria-pressed": String(v === sort),
      onclick: () => { sort = v; paintSort(); paintTree(); } }, label)));
    metricHint.textContent = `Count = ${tree.about.metric_note}${key === "clusters" ? "" : `, ${periodText()}`}.`;
  }

  const periodText = () => RANGES.find(([h]) => h === hours)[1].toLowerCase();
  const fame = (n) => n.info.sitelinks ?? Math.max(0, ...n.kids.map(fame));
  function sorted(list) {
    const by = sort === "az" ? (a, b) => a.label.localeCompare(b.label)
      : sort === "fame" ? (a, b) => fame(b) - fame(a) || b.total - a.total
      : (a, b) => b.total - a.total || a.label.localeCompare(b.label);
    return [...list].sort(by);
  }
  const visible = (n) => !seenOnly || n.total > 0;
  const isOpen = (n) => expanded.has(n.id);

  function matches(n) {
    if (n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q)) return true;
    return (n.info.aliases || []).some((a) => a.toLowerCase().includes(q));
  }

  function paintTree() {
    clear(head).append(el("span", {}, "Name"), el("span", { class: "num" }, `${tree.about.metric === "mentions" ? "Mentions" : "Posts"}${key === "clusters" ? ", latest map" : `, ${periodText()}`}`));
    clear(treeEl);
    const rows = [];
    if (q.length >= 2) {
      const hits = [...tree.byId.values()].filter((n) => visible(n) && matches(n)), hitIds = new Set(hits.map((n) => n.id));
      const keep = new Set();
      for (const n of hits.slice(0, MATCH_CAP)) for (let p = n; p; p = tree.byId.get(p.parent)) keep.add(p.id);
      const add = (n) => { rows.push(row(n, keep.has(n.id) && n.kids.some((c) => keep.has(c.id)), hitIds.has(n.id)));
        for (const c of sorted(n.kids)) if (keep.has(c.id)) add(c); };
      sorted(tree.roots).filter((r) => keep.has(r.id)).forEach(add);
      treeEl.append(...rows);
      if (!hits.length) treeEl.append(el("p", { class: "empty-row" }, `No entry matches “${search.value.trim()}”.`));
      else if (hits.length > MATCH_CAP) treeEl.append(el("p", { class: "empty-row" }, `Showing the first ${MATCH_CAP} of ${fmt.int(hits.length)} matches. Type more to narrow.`));
    } else {
      const add = (n) => {
        rows.push(row(n, isOpen(n)));
        if (!isOpen(n)) return;
        const kids = sorted(n.kids.filter(visible)), cap = showAll.has(n.id) ? kids.length : CHILD_CAP;
        kids.slice(0, cap).forEach(add);
        if (kids.length > cap) rows.push(el("div", { class: "tree-more", style: `padding-left:${(n.depth + 1) * 18 + 30}px` },
          el("button", { type: "button", class: "link", onclick: () => { showAll.add(n.id); paintTree(); } }, `Show all ${fmt.int(kids.length)}`)));
      };
      sorted(tree.roots.filter(visible)).forEach(add);
      treeEl.append(...rows);
      if (!rows.length) treeEl.append(el("p", { class: "empty-row" }, "Nothing in this period."));
    }
    const focusable = treeEl.querySelector(`[data-id="${CSS.escape(selected || "")}"]`) || treeEl.querySelector("[role=treeitem]");
    if (focusable) focusable.tabIndex = 0;
  }

  function sub(n) {
    if (key === "iptc") {
      const parts = [`level ${n.info.level}`];
      if (n.kids.length) parts.push(`${n.kids.length} below`);
      if (!n.info.matched) parts.push(n.info.level === 0 ? "rolled up" : "not matched");
      return parts.join(" · ");
    }
    if (n.kind === "group") return count(n.leaves, key);
    if (n.kind === "cluster") return n.info.iptc_label ? `most common post topic: ${n.info.iptc_label}` : "";
    return n.info.description || "";
  }

  // A person's portrait or a company's industry icon before the name; sector and industry rows get their icon too.
  function pic(n, size) {
    if (n.kind === "entity" && (key === "people" || key === "companies")) return entityBadge(key, { label: n.label, ...n.info }, size);
    if (key === "companies" && n.kind === "group") {
      const e = n.info.level === "industry" ? { industry: n.label, sector: tree.byId.get(n.parent)?.label } : { sector: n.label };
      return industryIcon(e, Math.round(size * 0.75));
    }
    return null;
  }

  function row(n, open, hit = false) {
    const max = tree.maxAt[n.depth] || 1, has = n.kids.length > 0;
    const r = el("div", { class: `tree-row${n.id === selected ? " sel" : ""}${hit ? " hit" : ""}${n.total ? "" : " zero"}`,
      role: "treeitem", "aria-level": String(n.depth + 1), "aria-selected": String(n.id === selected), "aria-expanded": has ? String(open) : null,
      tabindex: "-1", "data-id": n.id, style: `padding-left:${n.depth * 18 + 6}px`,
      onclick: () => { if (has && !q) flip(n); select(n.id); } },
      el("span", { class: "caret", "aria-hidden": "true" }, has ? (open ? "▾" : "▸") : ""),
      el("span", { class: "nm" }, pic(n, 20), el("span", { class: "lb" }, n.label), el("span", { class: "sb" }, sub(n))),
      el("span", { class: "tree-val", title: `${fmt.int(n.total)} ${tree.about.metric}` },
        el("span", { class: "track" }, el("span", { class: "b", style: `width:${n.total ? Math.max(2, Math.round((n.total / max) * 100)) : 0}%` })),
        el("span", { class: "n" }, fmt.int(n.total))));
    r.addEventListener("keydown", (e) => keys(e, n));
    return r;
  }

  const flip = (n) => { if (isOpen(n)) expanded.delete(n.id); else expanded.add(n.id); };
  function toggle(n) { flip(n); paintTree(); refocus(n.id); }
  function select(id) { selected = id; paintTree(); paintDetail(); refocus(id); }
  function refocus(id) { const r = treeEl.querySelector(`[data-id="${CSS.escape(id)}"]`); if (r) { treeEl.querySelectorAll("[role=treeitem]").forEach((x) => { x.tabIndex = -1; }); r.tabIndex = 0; r.focus({ preventScroll: false }); } }

  function keys(e, n) {
    const items = [...treeEl.querySelectorAll("[role=treeitem]")], i = items.indexOf(e.currentTarget);
    const go = (j) => { const t = items[Math.max(0, Math.min(items.length - 1, j))]; if (t) refocus(t.dataset.id); };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(items.length - 1);
    else if (e.key === "ArrowRight") { if (n.kids.length && !isOpen(n)) toggle(n); else go(i + 1); }
    else if (e.key === "ArrowLeft") { if (n.kids.length && isOpen(n) && !q) toggle(n); else if (n.parent) refocus(n.parent); }
    else if (e.key === "Enter" || e.key === " ") select(n.id);
    else return;
    e.preventDefault();
  }

  // ---- side panels -------------------------------------------------------------------------------------------
  function link(n) {
    if (n.id.startsWith("medtop:")) return [`https://cv.iptc.org/newscodes/mediatopic/${n.id.slice(7)}`, `IPTC ${n.id.slice(7)}`];
    if (/^Q\d+$/.test(n.id)) return [`https://www.wikidata.org/wiki/${n.id}`, `Wikidata ${n.id}`];
    return null;
  }
  const stat = (v, k) => el("div", { class: "stat" }, el("div", { class: "v" }, v), el("div", { class: "k" }, k));

  function paintDetail() {
    clear(detail);
    const n = selected && tree.byId.get(selected);
    if (!n) {
      detail.append(el("h2", {}, "Details"), el("p", {}, "Select an entry to see its definition, where it sits and how many posts it tagged."));
      return;
    }
    const path = [];
    for (let p = tree.byId.get(n.parent); p; p = tree.byId.get(p.parent)) path.unshift(p.label);
    const metric = tree.about.metric === "mentions" ? "mentions" : "posts";
    if (path.length) detail.append(el("div", { class: "crumb" }, path.join(" › ")));
    const picture = pic(n, 44);
    detail.append(el("h2", { class: picture ? "tax-title with-pic" : "tax-title" }, picture, n.label));
    const l = link(n);
    const stats = el("div", { class: "stats" });
    if (key === "iptc") {
      stats.append(stat(fmt.int(n.total), n.kids.length ? `${metric}, incl. below` : `${metric}, best match`),
        stat(fmt.int(n.info.top3), "posts with it in their top 3"), stat(String(n.info.level), "level"));
    } else if (n.kind === "entity") {
      stats.append(stat(fmt.int(n.total), metric), stat(fmt.int(n.info.sitelinks), "Wikipedia editions"), stat(fmt.int(n.info.n_aliases), "aliases"));
    } else if (n.kind === "cluster") {
      stats.append(stat(fmt.int(n.total), "posts"), stat(n.info.label_source === "llm" ? "LLM" : n.info.label_source, "named by"));
    } else {
      stats.append(stat(fmt.int(n.total), metric), stat(fmt.int(n.leaves), NOUN[key][1]),
        stat(fmt.int(countSeen(n)), key === "clusters" ? "with posts" : "seen in period"));
    }
    detail.append(stats);
    const text = n.info.definition || n.info.description;
    if (text) detail.append(el("p", { class: "def" }, text));
    if (key === "iptc" && !n.info.matched) detail.append(el("p", {}, n.info.level === 0
      ? "Top-level subject: posts are not matched to it directly; its count adds up the concepts below. The map's cluster names pick from these 17."
      : "Below level 3, so posts are never matched to it directly."));
    if (n.info.aliases?.length) detail.append(el("p", {}, el("strong", {}, "Also matched as: "), n.info.aliases.join(", "),
      n.info.n_aliases > n.info.aliases.length ? ` (+${n.info.n_aliases - n.info.aliases.length} more)` : ""));
    if (n.info.source === "pageviews") detail.append(el("p", {}, "In the list because of recent Wikipedia page views, not the Vital Articles list."));
    if (n.kind === "cluster") {
      detail.append(el("p", {}, el("strong", {}, "Subject chosen for the cluster: "), n.info.subject || "none (no shared subject)"));
      if (n.info.iptc_label) detail.append(el("p", {}, el("strong", {}, "Per-post IPTC tags in this cluster: "),
        `most common topic “${n.info.iptc_label}”; most common subject “${n.info.iptc_l1}”, on ${fmt.pct(n.info.iptc_l1_share)} of its posts.`));
      if (n.info.top_hashtags?.length) detail.append(el("div", { class: "pills" }, n.info.top_hashtags.map((t) => el("span", { class: "pill" }, "#" + t))));
    }
    if (n.kids.length) {
      const top = sorted(n.kids).slice(0, 8);
      detail.append(el("p", { class: "kids-h" }, el("strong", {}, sort === "posts" ? "Largest below" : "Below")),
        el("div", { class: "kids" }, top.map((c) => el("button", { type: "button", class: "leg-row", onclick: () => { expanded.add(n.id); select(c.id); } },
          el("span", {}, c.label), el("span", { class: "n" }, fmt.int(c.total))))));
    }
    if (l) detail.append(el("p", {}, el("a", { href: l[0], target: "_blank", rel: "noopener" }, l[1] + " ↗")));
  }
  const countSeen = (n) => (n.kids.length ? n.kids.reduce((s, c) => s + countSeen(c), 0) : n.total > 0 ? 1 : 0);

  function paintAbout() {
    const a = tree.about;
    clear(about).append(el("h2", {}, "How it's applied"),
      el("p", {}, el("strong", {}, "Source: "), a.source_url ? el("a", { href: a.source_url, target: "_blank", rel: "noopener" }, a.source) : a.source),
      el("p", {}, el("strong", {}, "Refresh: "), a.refresh, " Table: ", el("code", {}, a.table), "."),
      el("ol", { class: "steps" }, a.steps.map((s) => el("li", {}, s))),
      el("p", { class: "muted" }, a.limits));
    if (a.anchors) about.append(el("details", {}, el("summary", {}, `The ${a.anchors.length} “no topic” anchors`),
      el("ul", {}, a.anchors.map((s) => el("li", {}, s)))));
  }

  return { activate() { if (!loaded) return load(); }, badge: () => mode };
}
