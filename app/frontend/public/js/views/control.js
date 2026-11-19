/** Control tab: pause or resume each pipeline resource, or all of them, with a confirmation step. */
import { getJSON } from "../api.js";
import { clear, el } from "../lib/dom.js";

const STATUS = { scheduled: "Scheduled", running: "Running", paused: "Paused", stopped: "Stopped", failed: "Failed", "not deployed": "Not deployed" };
const RUNNING = new Set(["running", "scheduled", "starting", "deploying", "initializing", "recovering", "resetting"]);
const pill = (s) => el("span", { class: `pill ${s.replace(" ", "")}` }, el("i"), STATUS[s] || s[0].toUpperCase() + s.slice(1));

async function post(path) {
  const res = await fetch(path, { method: "POST", headers: { "X-Requested-With": "bluesky-analytics", accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status}`);
  return body;
}

export function init({ root, onError, setBadge }) {
  let mode = null, list = [], timer = null, busy = false;
  const intro = el("div", { class: "note" },
    el("h2", { text: "Pause and resume" }),
    el("p", { class: "muted", text: "Paused resources stop using compute. Pausing is safe for data except for ingestion paused longer than 36 hours. "
      + "This app itself is not listed: it cannot stop itself (use the Databricks Apps page)." }));
  const allRow = el("div", { class: "row-controls" });
  const cards = el("div", { class: "control-grid" });
  const logBox = el("div", { class: "card" }, el("h2", { text: "Actions this session" }), el("p", { class: "muted", text: "None yet." }));
  const dialog = el("dialog", { class: "confirm" });
  root.append(intro, allRow, cards, logBox, dialog);

  allRow.append(
    el("button", { class: "btn danger", type: "button", onclick: () => confirmAct("all", "pause") }, "Pause all"),
    el("button", { class: "btn", type: "button", onclick: () => confirmAct("all", "resume") }, "Resume all"),
    el("button", { class: "btn ghost", type: "button", onclick: () => refresh() }, "Refresh status"));

  async function refresh() {
    try {
      const res = await getJSON("/api/control");
      list = res.resources; mode = res.mode; setBadge(mode); onError(null); render();
    } catch (err) { onError(`Could not load resource status: ${err.message}`); }
  }

  function render() {
    clear(cards);
    for (const r of list) {
      const deployed = r.status !== "not deployed";
      const running = RUNNING.has(r.status);
      cards.append(el("div", { class: "card" },
        el("div", { class: "card-head" }, el("div", {}, el("h2", { text: r.name }), el("p", { class: "muted", text: `${r.kind} · ${r.layer}` })), pill(r.status)),
        el("p", { class: "muted", text: r.impact }),
        el("div", { class: "row-controls" },
          el("button", { class: "btn danger", type: "button", disabled: !deployed || !running || busy, onclick: () => confirmAct(r.key, "pause") }, "Pause"),
          el("button", { class: "btn", type: "button", disabled: !deployed || running || busy, onclick: () => confirmAct(r.key, "resume") }, "Resume"),
          r.url ? el("a", { class: "link", href: r.url, target: "_blank", rel: "noopener noreferrer" }, "Open in Databricks") : null)));
    }
  }

  function confirmAct(key, action) {
    const targets = key === "all" ? list.filter((r) => r.status !== "not deployed") : list.filter((r) => r.key === key);
    const verb = action === "pause" ? "Pause" : "Resume";
    clear(dialog).append(
      el("h2", { text: `${verb} ${key === "all" ? "all resources" : targets[0]?.name}?` }),
      el("ul", {}, targets.map((r) => el("li", {}, el("strong", {}, r.name), action === "pause" ? ` — ${r.impact}` : ""))),
      action === "resume" ? el("p", { class: "muted", text: "Resumed resources start using compute again and bill until paused." }) : null,
      el("div", { class: "row-controls" },
        el("button", { class: "btn ghost", type: "button", onclick: () => dialog.close() }, "Cancel"),
        el("button", { class: `btn ${action === "pause" ? "danger" : ""}`, type: "button", onclick: () => { dialog.close(); run(key, action); } }, verb)));
    dialog.showModal();
  }

  async function run(key, action) {
    busy = true; render();
    try {
      const res = await post(key === "all" ? `/api/control/all/${action}` : `/api/control/${key}/${action}`);
      const first = logBox.querySelector("p.muted"); if (first) first.remove();
      for (const r of res.results) {
        const name = list.find((x) => x.key === r.key)?.name || r.key;
        logBox.append(el("p", {}, `${new Date().toLocaleTimeString()} · ${action} ${name}: ${r.ok ? r.message : "failed — " + r.message} (by ${res.actor})`));
      }
    } catch (err) { onError(`${action} failed: ${err.message}`); }
    busy = false;
    // states take a little while to settle (runs cancelling, pipelines stopping): poll briefly
    for (let i = 0; i < 6; i++) { await refresh(); await new Promise((r) => setTimeout(r, 5000)); }
  }

  return {
    activate() { refresh(); clearInterval(timer); timer = setInterval(refresh, 20000); },
    deactivate() { clearInterval(timer); },
    badge: () => mode,
  };
}
