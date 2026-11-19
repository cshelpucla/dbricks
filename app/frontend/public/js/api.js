export async function getJSON(path, params) {
  const url = params ? `${path}?${params}` : path;
  const res = await fetch(url, { headers: { accept: "application/json" } });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    throw new Error(`${res.status} ${detail || res.statusText}`);
  }
  return res.json();
}

/** Filter state to query string; empty filters are omitted. */
export function filterParams(s) {
  const p = new URLSearchParams({ hours: String(s.hours) });
  if (s.topics.length) p.set("topic", s.topics.join(","));
  if (s.langs.length) p.set("lang", s.langs.join(","));
  if (s.countries.length) p.set("country", s.countries.join(","));
  if (s.people?.length) p.set("person", s.people.join(","));
  if (s.companies?.length) p.set("company", s.companies.join(","));
  if (s.minScore > 0) p.set("min_score", String(s.minScore));
  if (s.hasGeo) p.set("has_geo", "true");
  if (s.q) p.set("q", s.q);
  return p;
}
