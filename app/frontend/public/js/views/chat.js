/** Ask tab: chat with a model that queries the data (polars) and draws charts (Plotly or Chart.js). */
import { clear, el, fmt } from "../lib/dom.js";

const SUGGESTIONS = [
  "What are the top 10 topics in the last 24 hours?",
  "Show posts per hour by language for the top 5 languages",
  "Pivot topic by language as a heatmap",
  "Which countries post the most about sport?",
];
const loaded = {};
function loadScript(src) {
  return loaded[src] ||= new Promise((res, rej) => {
    const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = () => rej(new Error(`could not load ${src}`));
    document.head.append(s);
  });
}
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const palette = () => Array.from({ length: 8 }, (_, i) => css(`--series-${i + 1}`));

/** Minimal markdown: paragraphs, "-" bullets, **bold**, `code`. Built from DOM nodes, never innerHTML. */
function inline(text) {
  const out = [];
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) out.push(el("strong", { text: part.slice(2, -2) }));
    else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) out.push(el("code", { text: part.slice(1, -1) }));
    else if (part) out.push(part);
  }
  return out;
}
function markdown(text) {
  const box = el("div"); let list = null;
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*[-*]\s+(.*)/);
    if (m) { if (!list) box.append(list = el("ul")); list.append(el("li", {}, inline(m[1]))); continue; }
    list = null;
    if (line.trim()) box.append(el("p", {}, inline(line.replace(/^#+\s*/, ""))));
  }
  return box;
}

function tableCard(t) {
  const shown = t.rows.length;
  const num = t.rows[0]?.map((v) => typeof v === "number") || [];
  const card = el("details", { class: "chat-table" }, el("summary", { text: `Data: ${t.id}, ${fmt.int(t.total_rows)} rows${shown < t.total_rows ? ` (first ${shown})` : ""}` }));
  card.append(el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, t.columns.map((c, i) => el("th", { class: num[i] ? "num" : "", text: c })))),
    el("tbody", {}, t.rows.map((r) => el("tr", {}, r.map((v, i) => el("td", { class: num[i] ? "num" : "", text: v == null ? "–" : Number.isInteger(v) ? fmt.int(v) : String(v) }))))))));
  return card;
}

/** Charts are re-drawn on theme change, so colours always come from the current CSS tokens. */
function chartCard(a, registry) {
  const host = el("div", { class: "chart-card chat-chart", role: "img", "aria-label": a.title || "Chart" });
  const draw = async () => {
    try {
      if (a.library === "plotly") {
        await loadScript("vendor/plotly.min.js");
        const spec = structuredClone(a.spec);
        const ink = css("--ink-2"), grid = css("--grid");
        spec.layout = { ...spec.layout, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", colorway: palette(), margin: { l: 56, r: 16, t: spec.layout.title?.text ? 40 : 16, b: 48 },
          font: { color: ink, family: getComputedStyle(document.body).fontFamily, size: 12 }, title: { ...spec.layout.title, font: { color: css("--ink"), size: 14 } } };
        for (const k of ["xaxis", "yaxis"]) if (spec.layout[k]) spec.layout[k] = { gridcolor: grid, linecolor: css("--axis"), automargin: true, ...spec.layout[k] };
        let plot = host.querySelector(".plot");
        if (!plot) host.append(plot = el("div", { class: "plot" }));
        await window.Plotly.react(plot, spec.data, spec.layout, { responsive: true, displaylogo: false });
      } else {
        await loadScript("vendor/chart.umd.js");
        const spec = structuredClone(a.spec), pal = palette(), Chart = window.Chart;
        const pie = spec.type === "doughnut";
        spec.data.datasets.forEach((d, i) => {
          if (pie) { d.backgroundColor = d.data.map((_, j) => pal[j % pal.length]); d.borderColor = css("--surface"); }
          else { d.backgroundColor = spec.type === "line" && !d.fill ? pal[i % pal.length] : pal[i % pal.length] + (d.fill ? "55" : ""); d.borderColor = pal[i % pal.length]; d.tension = 0.25; d.pointRadius = spec.type === "line" ? 2 : 3; }
        });
        Chart.defaults.color = css("--ink-2"); Chart.defaults.borderColor = css("--grid");
        spec.options = { responsive: true, maintainAspectRatio: false, ...spec.options };
        const title = spec.options.plugins?.title; if (title) title.color = css("--ink");
        registry.chart?.destroy();
        host.querySelector(".cjs")?.remove();
        const wrap = el("div", { class: "cjs" }, el("canvas")); host.append(wrap);
        registry.chart = new Chart(wrap.firstChild, spec);
      }
    } catch (err) { host.replaceChildren(el("p", { class: "muted", text: `Chart could not be drawn: ${err.message}` })); }
  };
  registry.redraw = draw; draw();
  return host;
}

export function init({ root, onError, setBadge }) {
  let mode = null, busy = false, cid = null;
  const charts = [];
  const list = el("nav", { class: "chat-list", "aria-label": "Saved conversations" });
  const newBtn = el("button", { class: "btn", type: "button", text: "New chat", onclick: () => open(null) });
  const log = el("div", { class: "chat-log", "aria-live": "polite" });
  const suggest = el("div", { class: "chat-suggest" }, SUGGESTIONS.map((s) => el("button", { class: "btn", type: "button", text: s, onclick: () => send(s) })));
  const input = el("textarea", { rows: "1", placeholder: "Ask about topics, languages, countries, trends…", "aria-label": "Your question" });
  const sendBtn = el("button", { class: "btn primary", type: "submit", text: "Ask" });
  const form = el("form", { class: "chat-form" }, input, sendBtn);
  root.append(el("div", { class: "chat-shell" }, el("aside", { class: "chat-side" }, newBtn, list), el("div", { class: "chat" }, log, suggest, form)));

  const add = (role, ...kids) => { const m = el("div", { class: `msg ${role}` }, kids); log.append(m); log.scrollTop = log.scrollHeight; return m; };
  const artifactPart = (a) => { if (a.kind !== "chart") return tableCard(a); const reg = {}; charts.push(reg); return chartCard(a, reg); };
  const assistantParts = (m) => [el("div", { class: "bubble" }, markdown(m.content ?? m.reply)), ...(m.artifacts || []).map(artifactPart)];

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) { let d = ""; try { d = (await res.json()).detail; } catch { /* not JSON */ } throw new Error(d || `${res.status} ${res.statusText}`); }
    return res.status === 204 ? null : res.json();
  }

  /** POST that answers with server-sent events; calls onEvent with each parsed `data:` payload. */
  async function stream(path, payload, onEvent) {
    const res = await fetch(path, { method: "POST", headers: { "content-type": "application/json", accept: "text/event-stream" }, body: JSON.stringify(payload) });
    if (!res.ok) { let d = ""; try { d = (await res.json()).detail; } catch { /* not JSON */ } throw new Error(d || `${res.status} ${res.statusText}`); }
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += value;
      for (let i; (i = buf.indexOf("\n\n")) >= 0; buf = buf.slice(i + 2)) {
        const data = buf.slice(0, i).split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
        if (data) onEvent(JSON.parse(data));
      }
    }
  }

  async function refreshList() {
    try {
      const { conversations } = await api("/api/chat/conversations");
      list.replaceChildren(...conversations.map((c) => el("div", { class: `chat-item${c.id === cid ? " on" : ""}` },
        el("button", { class: "chat-open", type: "button", title: c.title, onclick: () => open(c.id) }, el("span", { class: "t", text: c.title || "(untitled)" }), el("span", { class: "s", text: fmt.ago(c.updated_at.endsWith("Z") || c.updated_at.includes("+") ? c.updated_at : c.updated_at.replace(" ", "T") + "Z") })),
        el("button", { class: "chat-del", type: "button", "aria-label": `Delete conversation: ${c.title}`, text: "×", onclick: () => remove(c.id) }))));
      if (!conversations.length) list.replaceChildren(el("p", { class: "muted", text: "Saved conversations appear here." }));
    } catch (err) { onError(`Could not load saved chats: ${err.message}`); }
  }

  async function open(id) {
    if (busy) return;
    charts.length = 0; cid = id; clear(log); onError(null);
    suggest.hidden = !!id;
    if (id) {
      try {
        const { messages } = await api(`/api/chat/conversations/${id}`);
        for (const m of messages) add(m.role, ...(m.role === "user" ? [el("div", { class: "bubble", text: m.content })] : assistantParts(m)));
      } catch (err) { onError(`Could not open that chat: ${err.message}`); cid = null; suggest.hidden = false; }
    }
    refreshList(); input.focus();
  }

  async function remove(id) {
    try { await api(`/api/chat/conversations/${id}`, { method: "DELETE" }); } catch (err) { onError(`Could not delete: ${err.message}`); return; }
    if (id === cid) await open(null); else refreshList();
  }

  async function send(text) {
    text = text.trim(); if (!text || busy) return;
    busy = true; sendBtn.disabled = true; suggest.hidden = true; input.value = ""; input.style.height = "auto"; onError(null);
    add("user", el("div", { class: "bubble", text }));
    // Status text, then each table and chart as soon as it exists; the reply replaces the status when done.
    const status = el("div", { class: "bubble", text: "Reading the data…" });
    const pending = add("assistant pending", status);
    let answered = false;
    try {
      await stream("/api/chat/stream", { message: text, conversation_id: cid }, (ev) => {
        if (ev.type === "status") status.textContent = ev.text;
        else if (ev.type === "artifact") pending.append(artifactPart(ev.artifact));
        else if (ev.type === "error") throw new Error(ev.detail);
        else if (ev.type === "done") {
          answered = true; cid = ev.conversation_id; mode = ev.mode; setBadge(mode);
          pending.className = "msg assistant"; status.replaceChildren(markdown(ev.reply));
          busy = false; sendBtn.disabled = false;
        }
        log.scrollTop = log.scrollHeight;
      });
      if (!answered) throw new Error("the connection closed before the answer was complete");
      refreshList();
    } catch (err) {
      pending.className = "msg assistant err"; pending.replaceChildren(el("div", { class: "bubble", text: `Could not answer: ${err.message}` }));
    } finally { busy = false; sendBtn.disabled = false; log.scrollTop = log.scrollHeight; input.focus(); }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); } });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; });
  document.addEventListener("themechange", () => charts.forEach((c) => c.redraw?.()));

  return {
    async activate() {
      refreshList();
      if (mode) return;
      try { mode = (await api("/api/chat/info")).mode; } catch { /* badge stays hidden */ }
    },
    badge: () => mode,
  };
}
