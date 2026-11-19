/** Pipelines tab: every pipeline's status, throughput (rows and bytes per time bucket) and table health. */
import { getJSON } from "../api.js";
import { chartCard } from "../lib/charts.js";
import { clear, el, fmt } from "../lib/dom.js";
import { singleSelect } from "../lib/controls.js";

const WINDOWS = [[1, "Last hour"], [6, "Last 6 hours"], [24, "Last 24 hours"], [72, "Last 3 days"], [168, "Last 7 days"]];
const STATUS = { scheduled: "Scheduled", running: "Running", paused: "Paused", stopped: "Stopped", failed: "Failed", "not deployed": "Not deployed" };

const pill = (s) => el("span", { class: `pill ${s.replace(" ", "")}` }, el("i"), STATUS[s] || s);

export function init({ root, onError, setBadge }) {
  let mode = null, hours = 24, list = [], selected = "ingest", timer = null;
  const controls = el("div", { class: "row-controls" });
  const kpis = el("div", { class: "kpis" });
  const tableHost = el("div", { class: "card" });
  const detail = el("div", { class: "detail-stack" });
  root.append(controls, kpis, tableHost, detail);

  controls.append(
    singleSelect({ label: "Time window", options: WINDOWS.map(([value, label]) => ({ value, label })), value: hours, onChange: (h) => { hours = h; refresh(); } }).el,
    el("div", { class: "field" }, el("span", { class: "lbl" }, " "), el("button", { class: "btn plain", type: "button", onclick: () => refresh() }, "Refresh")));

  const xFormat = (iso) => { const d = new Date(iso); return hours > 24 ? `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:00` : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };

  async function refresh() {
    try {
      const res = await getJSON("/api/pipelines", new URLSearchParams({ hours: String(hours) }));
      list = res.pipelines; mode = res.mode; setBadge(mode); onError(null);
      renderKpis(); renderList(); await renderDetail();
    } catch (err) { onError(`Could not load pipelines: ${err.message}`); }
  }

  function renderKpis() {
    const live = list.filter((p) => p.status !== "not deployed");
    const running = list.filter((p) => p.status === "running").length;
    const ingest = list.find((p) => p.key === "ingest");
    clear(kpis).append(
      tile(`${running} of ${live.length}`, "pipelines running"),
      tile(fmt.num(ingest ? ingest.rows_per_sec : 0) + "/s", "rows into bronze (average)"),
      tile(fmt.num(list.reduce((a, p) => a + p.rows, 0)), `rows written, ${WINDOWS.find((w) => w[0] === hours)[1].toLowerCase()}`),
      tile(fmt.bytes(list.reduce((a, p) => a + p.bytes, 0)), "data written"));
  }
  const tile = (v, k) => el("div", { class: "kpi" }, el("div", { class: "v" }, v), el("div", { class: "k" }, k));

  function renderList() {
    const maxRows = Math.max(1, ...list.map((p) => p.rows));
    clear(tableHost).append(el("h2", { text: "Pipelines" }), el("table", {},
      el("thead", {}, el("tr", {}, ["Pipeline", "Layer", "Status", "Rows written", "Rows / s", "Data written", "Last run"].map((h, i) => el("th", { class: i >= 3 && i <= 5 ? "num" : "" }, h)))),
      el("tbody", {}, list.map((p) => el("tr", { class: `sel-row ${p.key === selected ? "sel" : ""}`, tabindex: "0",
        onclick: () => { selected = p.key; renderList(); renderDetail(); }, onkeydown: (e) => { if (e.key === "Enter") { selected = p.key; renderList(); renderDetail(); } } },
        el("td", {}, el("strong", {}, p.name), el("div", { class: "muted" }, `${p.kind} · ${p.tables.join(", ")}`)),
        el("td", {}, p.layer), el("td", {}, pill(p.status)),
        el("td", { class: "num" }, el("div", { class: "bar-cell" }, el("span", { class: "b", style: `width:${Math.round((p.rows / maxRows) * 80)}px` }), fmt.num(p.rows))),
        el("td", { class: "num" }, fmt.num(p.rows_per_sec)), el("td", { class: "num" }, fmt.bytes(p.bytes)), el("td", {}, fmt.ago(p.last_run)))))));
  }

  async function renderDetail() {
    const p = list.find((x) => x.key === selected);
    clear(detail);
    if (!p) return;
    const head = el("div", { class: "card" }, el("div", { class: "card-head" }, el("div", {}, el("h2", { text: p.name }), el("p", { text: p.description })),
      p.url ? el("a", { class: "link", href: p.url, target: "_blank", rel: "noopener noreferrer" }, "Open in Databricks") : null));
    detail.append(head);
    if (p.status === "not deployed") { head.append(el("p", { text: "This pipeline has not been deployed yet, so there are no statistics." })); return; }
    const loading = el("p", { class: "muted", text: "Loading statistics…" });
    head.append(loading);
    let st;
    try { st = await getJSON(`/api/pipelines/${p.key}/stats`, new URLSearchParams({ hours: String(hours) })); }
    catch (err) { loading.remove(); head.append(el("p", { text: `Could not load statistics: ${err.message}` })); return; }
    loading.remove();
    if (selected !== p.key) return;

    const stamps = [...new Set(st.series.map((r) => r.ts))].sort();
    const tables = [...new Set(st.series.map((r) => r.table))];
    const by = (field, scale = 1) => tables.map((t) => ({ name: t, values: stamps.map((ts) => { const r = st.series.find((x) => x.table === t && x.ts === ts); return r && r[field] != null ? r[field] / scale : 0; }) }));
    const per = `per ${st.bucket_minutes >= 60 ? st.bucket_minutes / 60 + " h" : st.bucket_minutes + " min"}`;
    const mk = (title, sub, o) => chartCard(title, sub, { x: stamps, xFormat, ...o });

    const rows = mk("Rows pushed", `Rows written ${per}, by table`, { kind: "bars", series: by("rows"), yFormat: fmt.num, label: "Rows written per time bucket" });
    const bytes = mk("Data volume", `Data written ${per}, by table`, { kind: "lines", series: by("bytes"), yFormat: fmt.bytes, label: "Bytes written per time bucket" });
    const isPipeline = p.kind === "pipeline";
    const perf = isPipeline
      ? mk("Merge time", "Average time to apply a batch, by table", { kind: "lines", series: by("exec_ms", 1000), yFormat: (v) => `${v.toFixed(1)} s`, label: "Average merge seconds" })
      : mk("Commits", `Write commits ${per}, by table`, { kind: "bars", series: by("commits"), yFormat: fmt.int, label: "Commits per time bucket" });
    const grid = el("div", { class: "grid-2" }, rows.el, bytes.el, perf.el, tableHealth(st), runsCard(st));
    detail.append(grid);
  }

  function tableHealth(st) {
    return el("div", { class: "card" }, el("h2", { text: "Tables written" }), el("table", {},
      el("thead", {}, el("tr", {}, ["Table", "Size", "Files", "Last write"].map((h, i) => el("th", { class: i ? "num" : "" }, h)))),
      el("tbody", {}, st.tables.map((t) => el("tr", {}, el("td", {}, t.table), t.exists
        ? [el("td", { class: "num" }, fmt.bytes(t.size_bytes)), el("td", { class: "num" }, fmt.int(t.files)), el("td", { class: "num" }, t.freshness_s == null ? "–" : `${fmt.dur(t.freshness_s)} ago`)]
        : el("td", { colspan: "3", class: "muted" }, "not created yet"))))));
  }

  function runsCard(st) {
    return el("div", { class: "card" }, el("h2", { text: "Recent runs" }), st.runs.length ? el("table", {},
      el("thead", {}, el("tr", {}, ["Run", "State", "Started", "Duration"].map((h, i) => el("th", { class: i === 3 ? "num" : "" }, h)))),
      el("tbody", {}, st.runs.map((r) => el("tr", {}, el("td", {}, r.id), el("td", {}, r.state), el("td", {}, fmt.ago(r.start)), el("td", { class: "num" }, r.seconds == null ? "running" : fmt.dur(r.seconds)))))) : el("p", { text: "No runs recorded." }));
  }

  return {
    activate() { refresh(); clearInterval(timer); timer = setInterval(() => { if (!document.hidden) refresh(); }, 60000); },
    deactivate() { clearInterval(timer); },
    badge: () => mode,
  };
}
