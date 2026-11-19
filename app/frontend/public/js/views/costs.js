/** Costs tab: DBU spend from the billing system tables, as list-price estimates. */
import { getJSON } from "../api.js";
import { chartCard } from "../lib/charts.js";
import { clear, el, fmt } from "../lib/dom.js";
import { singleSelect } from "../lib/controls.js";

const RANGES = [[7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"]];
const GROUPS = [["product", "Product"], ["sku", "SKU"], ["resource", "Job, pipeline or warehouse"]];

export function init({ root, onError, setBadge }) {
  let mode = null, days = 30, group = "product", loaded = false;
  const controls = el("div", { class: "row-controls" });
  const body = el("div", { class: "detail-stack" });
  root.append(controls, body);
  controls.append(
    singleSelect({ label: "Period", options: RANGES.map(([value, label]) => ({ value, label })), value: days, onChange: (v) => { days = v; load(); } }).el,
    singleSelect({ label: "Break down by", options: GROUPS.map(([value, label]) => ({ value, label })), value: group, onChange: (v) => { group = v; load(); } }).el);

  const tile = (v, k) => el("div", { class: "kpi" }, el("div", { class: "v" }, v), el("div", { class: "k" }, k));

  async function load() {
    try {
      const res = await getJSON("/api/costs", new URLSearchParams({ days: String(days), group }));
      onError(null); loaded = true; mode = res.mode; setBadge(mode); render(res);
    } catch (err) { onError(`Could not load costs: ${err.message}`); }
  }

  function render(res) {
    clear(body);
    if (!res.available) {
      body.append(el("div", { class: "note" }, el("h2", { text: res.reason === "permission" ? "Billing tables are not accessible yet" : "Cost data is not available" }),
        el("p", { class: "muted", text: res.reason === "permission"
          ? "The app reads the Databricks billing system tables, and this identity has no access to them."
          : "The billing query did not succeed." }),
        el("pre", { text: res.help }), el("p", { class: "muted", text: `Detail: ${res.message}` })));
      return;
    }
    const k = res.kpis;
    body.append(el("div", { class: "kpis" }, tile(fmt.usd(k.total), `spend, last ${res.days} days`), tile(fmt.usd(k.per_day), "average per day"),
      tile(fmt.usd(k.projected_30d), "projected next 30 days (last 7 days' rate)"), tile(fmt.num(k.dbus), "DBUs consumed")));
    const x = res.daily.map((d) => d.date);
    const series = res.series.map((name) => ({ name, values: res.daily.map((d) => d.values[name] || 0) }));
    const chart = chartCard("Daily spend", `List-price estimate by ${GROUPS.find((g) => g[0] === group)[1].toLowerCase()}`,
      { kind: "bars", x, series, xFormat: (d) => d.slice(5), yFormat: fmt.usd, axisFormat: (v) => "$" + fmt.num(v), label: "Daily spend" });
    body.append(chart.el, breakdown(res), el("p", { class: "muted", text: "Estimates are DBUs multiplied by list price. They exclude negotiated discounts and any cloud-provider charges for non-serverless compute, so they will not match the invoice exactly." }));
    if (res.mode === "demo") body.prepend(el("div", { class: "note" }, "Sample numbers. They show the layout only and are not your spend."));
  }

  function breakdown(res) {
    const top = Math.max(1e-9, ...res.breakdown.map((b) => b.cost));
    return el("div", { class: "card" }, el("h2", { text: "Breakdown" }), el("table", {},
      el("thead", {}, el("tr", {}, ["Name", "Spend", "Share", "DBUs"].map((h, i) => el("th", { class: i ? "num" : "" }, h)))),
      el("tbody", {}, res.breakdown.map((b) => el("tr", {}, el("td", {}, b.name), el("td", { class: "num" }, el("div", { class: "bar-cell" },
        el("span", { class: "b", style: `width:${Math.round((b.cost / top) * 90)}px` }), fmt.usd(b.cost))), el("td", { class: "num" }, fmt.pct(b.share)), el("td", { class: "num" }, fmt.num(b.dbus)))))));
  }

  return { activate() { if (!loaded) return load(); }, badge: () => mode };
}
