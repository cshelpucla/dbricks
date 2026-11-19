/** Filter controls: multi-select and single-select popovers, built with plain DOM. */
import { el, svg, clear } from "./dom.js";

const check = () => svg("svg", { class: "tick", viewBox: "0 0 16 16", "aria-hidden": "true" },
  svg("path", { d: "M3 8.5l3.2 3.2L13 5", fill: "none", stroke: "currentColor", "stroke-width": 2.2, "stroke-linecap": "round", "stroke-linejoin": "round" }));

function popover(field, button, build) {
  const pop = el("div", { class: "pop", hidden: true });
  field.append(pop);
  const close = () => { pop.hidden = true; button.setAttribute("aria-expanded", "false"); };
  button.setAttribute("aria-haspopup", "true"); button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = pop.hidden; document.dispatchEvent(new Event("pop-close"));
    if (open) { pop.hidden = false; button.setAttribute("aria-expanded", "true"); build(pop); }
  });
  document.addEventListener("pop-close", close);
  document.addEventListener("click", (e) => { if (!field.contains(e.target)) close(); });
  field.addEventListener("keydown", (e) => { if (e.key === "Escape") { close(); button.focus(); } });
  return { pop, close };
}

/** options: [{value, count?, label?}]. Calls onChange(selectedValues). Empty selection means "all".
 *  iconOf(value), if given, returns a small node (portrait, icon) shown before each option's text. */
export function multiSelect({ label, fmtOpt = (v) => v, iconOf = null, onChange, searchable = false }) {
  let options = [], selected = [];
  const field = el("div", { class: "field" }), button = el("button", { class: "btn", type: "button" });
  field.append(el("span", { class: "lbl" }, label), button);
  const summary = () => { button.textContent = !selected.length ? "All" : selected.length === 1 ? fmtOpt(selected[0]) : `${selected.length} selected`; };
  const { pop } = popover(field, button, (p) => paint(p));
  function paint(p = pop, q = "") {
    clear(p);
    if (searchable) {
      const s = el("input", { type: "search", placeholder: `Search ${label.toLowerCase()}`, "aria-label": `Search ${label}`, value: q });
      s.addEventListener("input", () => { paint(p, s.value); p.querySelector("input").focus(); });
      p.append(s);
    }
    for (const o of options.filter((o) => fmtOpt(o.value).toLowerCase().includes(q.toLowerCase()))) {
      const on = selected.includes(o.value);
      p.append(el("div", { class: "opt", role: "checkbox", "aria-checked": String(on), tabindex: "0",
        onclick: () => toggle(o.value, p, q), onkeydown: (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(o.value, p, q); } } },
        on ? check() : el("span", { class: "tick" }), iconOf?.(o.value), fmtOpt(o.value), o.count != null && el("span", { class: "n" }, o.count.toLocaleString())));
    }
    p.append(el("div", { class: "foot" }, el("button", { class: "link", type: "button", onclick: () => { selected = []; summary(); onChange(selected); paint(p, q); } }, "Clear")));
  }
  function toggle(v, p, q) {
    selected = selected.includes(v) ? selected.filter((x) => x !== v) : [...selected, v];
    summary(); onChange(selected); paint(p, q);
  }
  summary();
  return { el: field, setOptions(o) { options = o; if (!pop.hidden) paint(); }, setSelected(s) { selected = s; summary(); } };
}

/** Single choice with a check mark, like a date-range preset list. options: [{value, label}] */
export function singleSelect({ label, options, value, onChange }) {
  let current = value;
  const field = el("div", { class: "field" }), button = el("button", { class: "btn", type: "button" });
  field.append(el("span", { class: "lbl" }, label), button);
  const text = () => options.find((o) => o.value === current)?.label ?? "";
  button.textContent = text();
  const { close } = popover(field, button, (p) => {
    clear(p);
    for (const o of options) {
      p.append(el("div", { class: "opt", role: "radio", "aria-checked": String(o.value === current), tabindex: "0",
        onclick: () => pick(o.value), onkeydown: (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); pick(o.value); } } },
        o.value === current ? check() : el("span", { class: "tick" }), o.label));
    }
  });
  function pick(v) { current = v; button.textContent = text(); close(); onChange(v); }
  return { el: field };
}
