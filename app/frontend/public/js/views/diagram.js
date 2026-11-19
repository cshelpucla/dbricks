/** Architecture tab: the multi-page draw.io diagram, one button per page, rendered with the vendored viewer. */
import { clear, el } from "../lib/dom.js";

const FILE = "diagrams/pipelines.drawio";
const VIEWER = "vendor/drawio-viewer.min.js"; // draw.io viewer-static 32.6.9 (Apache 2.0), loaded on first visit

let viewerReady = null;
function loadViewer() {
  viewerReady ??= new Promise((resolve, reject) => {
    const s = el("script", { src: VIEWER, async: true });
    s.onload = () => (window.GraphViewer ? resolve(window.GraphViewer) : reject(new Error("The draw.io viewer did not initialise.")));
    s.onerror = () => { viewerReady = null; reject(new Error("Could not load the draw.io viewer.")); };
    document.head.append(s);
  });
  return viewerReady;
}

// The app's theme (auto follows the OS), mapped onto the viewer's dark-mode option.
const isDark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
};

export function init({ root, onError }) {
  const pages = el("div", { class: "seg pages", role: "group", "aria-label": "Diagram page" });
  const download = el("a", { class: "btn", href: FILE, download: "pipelines.drawio" }, "Download .drawio");
  const full = el("button", { class: "btn", type: "button" }, "Full screen");
  const stage = el("div", { class: "arch-stage" });
  const caption = el("p", { class: "muted arch-caption" });
  root.append(
    el("div", { class: "arch-head" }, pages, el("div", { class: "arch-actions" }, full, download)),
    caption, stage);

  let xml = null, names = [], page = 0, loaded = false;

  async function render() {
    const GraphViewer = await loadViewer();
    const host = el("div", { class: "arch-graph" });
    clear(stage).append(host);
    // A fresh viewer per page and theme: the viewer has no page-switch call, and recreating is cheap.
    new GraphViewer(host, xml.documentElement, {
      page, "dark-mode": isDark() ? "dark" : "light", border: 16,
      "auto-fit": true, responsive: true, resize: true, nav: true, lightbox: false,
      toolbar: "zoom", "toolbar-nohide": true, "toolbar-position": "top",
    });
    host.setAttribute("aria-label", `Architecture diagram, page ${page + 1} of ${names.length}: ${names[page]}`);
    host.setAttribute("role", "img");
  }

  function select(i) {
    page = i;
    pages.querySelectorAll("button").forEach((b, j) => b.setAttribute("aria-pressed", String(j === i)));
    caption.textContent = `Page ${i + 1} of ${names.length}: ${names[i]}. Zoom with the toolbar above the diagram; Full screen gives it the whole window.`;
    render().catch((e) => onError(e.message));
  }

  pages.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) select(Number(b.dataset.page)); });
  full.addEventListener("click", () => (document.fullscreenElement ? document.exitFullscreen() : stage.requestFullscreen?.()));
  document.addEventListener("themechange", () => { if (loaded && !root.hidden) render().catch((e) => onError(e.message)); });

  return {
    async activate() {
      if (loaded) return;
      try {
        const res = await fetch(FILE);
        if (!res.ok) throw new Error(`Diagram not found (${res.status}): add app/frontend/public/${FILE}.`);
        xml = new DOMParser().parseFromString(await res.text(), "text/xml");
        names = [...xml.getElementsByTagName("diagram")].map((d, i) => d.getAttribute("name") || `Page ${i + 1}`);
        if (!names.length) throw new Error("The diagram file has no pages.");
        pages.append(...names.map((n, i) => el("button", { type: "button", "data-page": i, "aria-pressed": "false" }, n)));
        loaded = true;
        select(0);
      } catch (e) {
        clear(stage).append(el("div", { class: "placeholder" }, el("h2", { text: "Architecture diagram" }), el("p", {}, e.message)));
      }
    },
  };
}
