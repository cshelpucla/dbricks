/** Small pictures beside names: a person's portrait, a company's industry icon, a country's flag. */
import { el, svg } from "./dom.js";

// A flag is the pair of regional-indicator emoji for an ISO 3166-1 alpha-2 code ("FR" -> U+1F1EB U+1F1F7). Windows has
// no flag glyphs and would show two letters, so app.css loads Twemoji Country Flags for exactly these code points.
export const flag = (cc) => (/^[A-Za-z]{2}$/.test(cc ?? "") ? String.fromCodePoint(...[...cc.toUpperCase()].map((c) => 0x1f1a5 + c.charCodeAt(0))) : "");
export const withFlag = (cc, text) => (flag(cc) ? `${flag(cc)} ${text}` : text);

// A person: their Wikipedia portrait (a Commons thumbnail URL from the API), over their initials, which show while it
// loads and stay if there is none or it fails.
export function avatar(e, size = 22) {
  const words = (e?.label ?? "").split(/\s+/).filter((w) => /^\p{L}/u.test(w));
  const initials = (words.length > 1 ? words[0][0] + words[words.length - 1][0] : words[0]?.[0] ?? "?").toUpperCase();
  const box = el("span", { class: "avatar", style: `--sz:${size}px`, "aria-hidden": "true" }, initials);
  if (e?.image) {
    const img = el("img", { src: e.image, alt: "", loading: "lazy", decoding: "async", referrerpolicy: "no-referrer" });
    img.addEventListener("error", () => img.remove());
    box.append(img);
  }
  return box;
}

// A company: an icon for its Wikidata industry when one of these rules knows it (tried in order on the industry, then
// the description), else one for its sector (entity_lib.SECTOR_RULES), else a plain building.
const INDUSTRY_ICONS = [
  [/video game|gaming|esport/i, "gamepad-2"],
  [/artificial intelligence|machine learning/i, "brain-circuit"],
  [/surveillance|security|cyber/i, "lock"],
  [/consumer electronics|smartphone|mobile phone/i, "smartphone"],
  [/semiconductor|electronic|electrical|computer hardware|^computers?$/i, "cpu"],
  [/software|information technology|information and communications|\bit services|cloud computing/i, "code"],
  [/e-commerce|online (?:retail|shop)/i, "shopping-cart"],
  [/social (?:media|network)/i, "message-circle"],
  [/digital distribution/i, "cloud-download"],
  [/internet|search engine|\bweb\b/i, "globe"],
  [/telecom|wireless|mobile network/i, "radio-tower"],
  [/\bfilm|cinema|movie|animation/i, "clapperboard"],
  [/television|broadcast|streaming/i, "tv"],
  [/wrestling|\bsports?\b|football|basketball|baseball/i, "trophy"],
  [/music|record label|audio/i, "music"],
  [/book|publishing/i, "book-open"],
  [/news|media|journalism|magazine/i, "newspaper"],
  [/space|rocket|aerospace|satellite/i, "rocket"],
  [/airline|air transport|aviation|airport/i, "plane"],
  [/railway|\brail\b|train/i, "train-front"],
  [/\bship|maritime|cruise|naval/i, "ship"],
  [/automotive|automobile|\bcars?\b|motor vehicle/i, "car"],
  [/logistic|freight|courier|delivery|truck|transport/i, "truck"],
  [/insurance/i, "shield-check"],
  [/bank|financ|payment|invest|asset management|stock exchange|credit|capital/i, "landmark"],
  [/\barms\b|defen[cs]e|weapon|military/i, "shield"],
  [/pharma|drug|biotech/i, "pill"],
  [/health|medic|hospital|life science/i, "heart-pulse"],
  [/caf[eé]|coffee/i, "coffee"],
  [/beer|brew/i, "beer"],
  [/beverage|drink/i, "cup-soda"],
  [/food|restaurant|catering/i, "utensils"],
  [/supermarket|grocer/i, "shopping-basket"],
  [/retail|department store|\bstores?\b/i, "store"],
  [/hotel|hospitality|resort/i, "bed-double"],
  [/cloth|fashion|apparel|textile|footwear|luxury/i, "shirt"],
  [/camera|photograph|optic/i, "camera"],
  [/petroleum|\boil\b|natural gas|fuel|refin/i, "fuel"],
  [/electricity|power generation|utilit|renewable|solar|wind power/i, "zap"],
  [/chemi/i, "flask-conical"],
  [/mining|metal|steel|alumin|gold/i, "pickaxe"],
  [/real estate|propert|construction/i, "building-2"],
  [/engineering|machinery/i, "cog"],
  [/manufactur|industr/i, "factory"],
];
const SECTOR_ICONS = {
  Technology: "cpu", "Communication Services": "radio-tower", "Consumer Discretionary": "shopping-bag", "Consumer Staples": "shopping-basket",
  Financials: "landmark", "Health Care": "heart-pulse", Energy: "fuel", Utilities: "zap", Industrials: "factory", Transportation: "truck",
  Materials: "pickaxe", "Real Estate": "building-2",
};
export function industryIconName(e) {
  for (const text of [e?.industry, e?.description]) {
    const hit = text && INDUSTRY_ICONS.find(([rx]) => rx.test(text));
    if (hit) return hit[1];
  }
  return SECTOR_ICONS[e?.sector] ?? "building";
}
export function industryIcon(e, size = 16) {
  return svg("svg", { class: "ind-icon", width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2,
                      "stroke-linecap": "round", "stroke-linejoin": "round", role: "img", "aria-label": e?.industry || e?.sector || "Company" },
    svg("title", {}, [e?.industry, e?.sector].filter(Boolean).join(" · ") || "Company"),
    ICONS[industryIconName(e)].map((p) => (typeof p === "string" ? svg("path", { d: p }) : svg(p[0], p[1]))));
}

/** kind "people" or "companies"; e is the entity from /api/meta (or just {label}). */
export const entityBadge = (kind, e, size) => (kind === "people" ? avatar(e, size) : industryIcon(e, size && Math.round(size * 0.75)));

// Lucide icons 0.460.0 (ISC licence, lucide.dev): each is a list of path data strings or [tag, attributes] on a 24x24 grid.
const ICONS = {
  "bed-double": ["M2 20v-8a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v8","M4 10V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v4","M12 4v6","M2 18h20"],
  "beer": ["M17 11h1a3 3 0 0 1 0 6h-1","M9 12v6","M13 12v6","M14 7.5c-1 0-1.44.5-3 .5s-2-.5-3-.5-1.72.5-2.5.5a2.5 2.5 0 0 1 0-5c.78 0 1.57.5 2.5.5S9.44 2 11 2s2 1.5 3 1.5 1.72-.5 2.5-.5a2.5 2.5 0 0 1 0 5c-.78 0-1.5-.5-2.5-.5Z","M5 8v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8"],
  "book-open": ["M12 7v14","M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"],
  "brain-circuit": ["M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z","M9 13a4.5 4.5 0 0 0 3-4","M6.003 5.125A3 3 0 0 0 6.401 6.5","M3.477 10.896a4 4 0 0 1 .585-.396","M6 18a4 4 0 0 1-1.967-.516","M12 13h4","M12 18h6a2 2 0 0 1 2 2v1","M12 8h8","M16 8V5a2 2 0 0 1 2-2",["circle",{"cx":"16","cy":"13","r":".5"}],["circle",{"cx":"18","cy":"3","r":".5"}],["circle",{"cx":"20","cy":"21","r":".5"}],["circle",{"cx":"20","cy":"8","r":".5"}]],
  "building-2": ["M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z","M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2","M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2","M10 6h4","M10 10h4","M10 14h4","M10 18h4"],
  "building": [["rect",{"width":"16","height":"20","x":"4","y":"2","rx":"2","ry":"2"}],"M9 22v-4h6v4","M8 6h.01","M16 6h.01","M12 6h.01","M12 10h.01","M12 14h.01","M16 10h.01","M16 14h.01","M8 10h.01","M8 14h.01"],
  "camera": ["M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z",["circle",{"cx":"12","cy":"13","r":"3"}]],
  "car": ["M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2",["circle",{"cx":"7","cy":"17","r":"2"}],"M9 17h6",["circle",{"cx":"17","cy":"17","r":"2"}]],
  "clapperboard": ["M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z","m6.2 5.3 3.1 3.9","m12.4 3.4 3.1 4","M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"],
  "cloud-download": ["M12 13v8l-4-4","m12 21 4-4","M4.393 15.269A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.436 8.284"],
  "code": [["polyline",{"points":"16 18 22 12 16 6"}],["polyline",{"points":"8 6 2 12 8 18"}]],
  "coffee": ["M10 2v2","M14 2v2","M16 8a1 1 0 0 1 1 1v8a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1h14a4 4 0 1 1 0 8h-1","M6 2v2"],
  "cog": ["M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z","M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z","M12 2v2","M12 22v-2","m17 20.66-1-1.73","M11 10.27 7 3.34","m20.66 17-1.73-1","m3.34 7 1.73 1","M14 12h8","M2 12h2","m20.66 7-1.73 1","m3.34 17 1.73-1","m17 3.34-1 1.73","m11 13.73-4 6.93"],
  "cpu": [["rect",{"width":"16","height":"16","x":"4","y":"4","rx":"2"}],["rect",{"width":"6","height":"6","x":"9","y":"9","rx":"1"}],"M15 2v2","M15 20v2","M2 15h2","M2 9h2","M20 15h2","M20 9h2","M9 2v2","M9 20v2"],
  "cup-soda": ["m6 8 1.75 12.28a2 2 0 0 0 2 1.72h4.54a2 2 0 0 0 2-1.72L18 8","M5 8h14","M7 15a6.47 6.47 0 0 1 5 0 6.47 6.47 0 0 0 5 0","m12 8 1-6h2"],
  "factory": ["M2 20a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8l-7 5V8l-7 5V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z","M17 18h1","M12 18h1","M7 18h1"],
  "flask-conical": ["M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2","M8.5 2h7","M7 16h10"],
  "fuel": [["line",{"x1":"3","x2":"15","y1":"22","y2":"22"}],["line",{"x1":"4","x2":"14","y1":"9","y2":"9"}],"M14 22V4a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v18","M14 13h2a2 2 0 0 1 2 2v2a2 2 0 0 0 2 2a2 2 0 0 0 2-2V9.83a2 2 0 0 0-.59-1.42L18 5"],
  "gamepad-2": [["line",{"x1":"6","x2":"10","y1":"11","y2":"11"}],["line",{"x1":"8","x2":"8","y1":"9","y2":"13"}],["line",{"x1":"15","x2":"15.01","y1":"12","y2":"12"}],["line",{"x1":"18","x2":"18.01","y1":"10","y2":"10"}],"M17.32 5H6.68a4 4 0 0 0-3.978 3.59c-.006.052-.01.101-.017.152C2.604 9.416 2 14.456 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.414-1.414A2 2 0 0 1 9.828 16h4.344a2 2 0 0 1 1.414.586L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.545-.604-6.584-.685-7.258-.007-.05-.011-.1-.017-.151A4 4 0 0 0 17.32 5z"],
  "globe": [["circle",{"cx":"12","cy":"12","r":"10"}],"M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20","M2 12h20"],
  "heart-pulse": ["M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z","M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"],
  "landmark": [["line",{"x1":"3","x2":"21","y1":"22","y2":"22"}],["line",{"x1":"6","x2":"6","y1":"18","y2":"11"}],["line",{"x1":"10","x2":"10","y1":"18","y2":"11"}],["line",{"x1":"14","x2":"14","y1":"18","y2":"11"}],["line",{"x1":"18","x2":"18","y1":"18","y2":"11"}],["polygon",{"points":"12 2 20 7 4 7"}]],
  "lock": [["rect",{"width":"18","height":"11","x":"3","y":"11","rx":"2","ry":"2"}],"M7 11V7a5 5 0 0 1 10 0v4"],
  "message-circle": ["M7.9 20A9 9 0 1 0 4 16.1L2 22Z"],
  "music": ["M9 18V5l12-2v13",["circle",{"cx":"6","cy":"18","r":"3"}],["circle",{"cx":"18","cy":"16","r":"3"}]],
  "newspaper": ["M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2","M18 14h-8","M15 18h-5","M10 6h8v4h-8V6Z"],
  "pickaxe": ["M14.531 12.469 6.619 20.38a1 1 0 1 1-3-3l7.912-7.912","M15.686 4.314A12.5 12.5 0 0 0 5.461 2.958 1 1 0 0 0 5.58 4.71a22 22 0 0 1 6.318 3.393","M17.7 3.7a1 1 0 0 0-1.4 0l-4.6 4.6a1 1 0 0 0 0 1.4l2.6 2.6a1 1 0 0 0 1.4 0l4.6-4.6a1 1 0 0 0 0-1.4z","M19.686 8.314a12.501 12.501 0 0 1 1.356 10.225 1 1 0 0 1-1.751-.119 22 22 0 0 0-3.393-6.319"],
  "pill": ["m10.5 20.5 10-10a4.95 4.95 0 1 0-7-7l-10 10a4.95 4.95 0 1 0 7 7Z","m8.5 8.5 7 7"],
  "plane": ["M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"],
  "radio-tower": ["M4.9 16.1C1 12.2 1 5.8 4.9 1.9","M7.8 4.7a6.14 6.14 0 0 0-.8 7.5",["circle",{"cx":"12","cy":"9","r":"2"}],"M16.2 4.8c2 2 2.26 5.11.8 7.47","M19.1 1.9a9.96 9.96 0 0 1 0 14.1","M9.5 18h5","m8 22 4-11 4 11"],
  "rocket": ["M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z","m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z","M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0","M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"],
  "shield-check": ["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z","m9 12 2 2 4-4"],
  "shield": ["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"],
  "ship": ["M2 21c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1 .6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1","M19.38 20A11.6 11.6 0 0 0 21 14l-9-4-9 4c0 2.9.94 5.34 2.81 7.76","M19 13V7a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v6","M12 10v4","M12 2v3"],
  "shirt": ["M20.38 3.46 16 2a4 4 0 0 1-8 0L3.62 3.46a2 2 0 0 0-1.34 2.23l.58 3.47a1 1 0 0 0 .99.84H6v10c0 1.1.9 2 2 2h8a2 2 0 0 0 2-2V10h2.15a1 1 0 0 0 .99-.84l.58-3.47a2 2 0 0 0-1.34-2.23z"],
  "shopping-bag": ["M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z","M3 6h18","M16 10a4 4 0 0 1-8 0"],
  "shopping-basket": ["m15 11-1 9","m19 11-4-7","M2 11h20","m3.5 11 1.6 7.4a2 2 0 0 0 2 1.6h9.8a2 2 0 0 0 2-1.6l1.7-7.4","M4.5 15.5h15","m5 11 4-7","m9 11 1 9"],
  "shopping-cart": [["circle",{"cx":"8","cy":"21","r":"1"}],["circle",{"cx":"19","cy":"21","r":"1"}],"M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12"],
  "smartphone": [["rect",{"width":"14","height":"20","x":"5","y":"2","rx":"2","ry":"2"}],"M12 18h.01"],
  "store": ["m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7","M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8","M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4","M2 7h20","M22 7v3a2 2 0 0 1-2 2a2.7 2.7 0 0 1-1.59-.63.7.7 0 0 0-.82 0A2.7 2.7 0 0 1 16 12a2.7 2.7 0 0 1-1.59-.63.7.7 0 0 0-.82 0A2.7 2.7 0 0 1 12 12a2.7 2.7 0 0 1-1.59-.63.7.7 0 0 0-.82 0A2.7 2.7 0 0 1 8 12a2.7 2.7 0 0 1-1.59-.63.7.7 0 0 0-.82 0A2.7 2.7 0 0 1 4 12a2 2 0 0 1-2-2V7"],
  "train-front": ["M8 3.1V7a4 4 0 0 0 8 0V3.1","m9 15-1-1","m15 15 1-1","M9 19c-2.8 0-5-2.2-5-5v-4a8 8 0 0 1 16 0v4c0 2.8-2.2 5-5 5Z","m8 19-2 3","m16 19 2 3"],
  "trophy": ["M6 9H4.5a2.5 2.5 0 0 1 0-5H6","M18 9h1.5a2.5 2.5 0 0 0 0-5H18","M4 22h16","M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22","M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22","M18 2H6v7a6 6 0 0 0 12 0V2Z"],
  "truck": ["M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2","M15 18H9","M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.624l-3.48-4.35A1 1 0 0 0 17.52 8H14",["circle",{"cx":"17","cy":"18","r":"2"}],["circle",{"cx":"7","cy":"18","r":"2"}]],
  "tv": [["rect",{"width":"20","height":"15","x":"2","y":"7","rx":"2","ry":"2"}],["polyline",{"points":"17 2 12 7 7 2"}]],
  "utensils": ["M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2","M7 2v20","M21 15V2a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7"],
  "zap": ["M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"],
};
