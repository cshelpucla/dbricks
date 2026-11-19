"""Costs tab: DBU usage and list-price spend from the Databricks billing system tables.

Figures are list-price estimates (DBUs x list price). They exclude negotiated discounts and any
cloud-provider charges for classic compute, so treat them as a tracking signal, not an invoice.
"""
import random
import re
from datetime import date, timedelta

from .. import config
from . import sqlexec

_TABLE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){1,2}$")
GROUPS = ("product", "sku", "resource")
MAX_SERIES = 7   # beyond this, small groups fold into "Other" (series colours are a fixed, small set)

GROUP_SQL = {
    "product": "coalesce(u.billing_origin_product, 'UNKNOWN')",
    "sku": "u.sku_name",
    "resource": ("coalesce(u.usage_metadata.job_id, u.usage_metadata.dlt_pipeline_id, "
                 "u.usage_metadata.warehouse_id, u.usage_metadata.endpoint_name, 'unattributed')"),
}
GRANT_HELP = ("An account admin needs to enable the billing system schema and grant read access:\n"
              "  databricks system-schemas enable <metastore-id> billing\n"
              "  GRANT USE CATALOG ON CATALOG system TO <principal>;\n"
              "  GRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO <principal>;")


def shape(rows, days: int, group: str, names: dict | None = None) -> dict:
    """rows: (date 'YYYY-MM-DD', group value, dbus, cost). Returns the Costs tab payload."""
    names = names or {}
    totals: dict[str, float] = {}
    dbus: dict[str, float] = {}
    for d, g, q, c in rows:
        g = names.get(g, g)
        totals[g] = totals.get(g, 0) + c
        dbus[g] = dbus.get(g, 0) + q
    ranked = sorted(totals, key=lambda g: -totals[g])
    keep = ranked[:MAX_SERIES] if len(ranked) > MAX_SERIES + 1 else ranked
    fold = lambda g: g if g in keep else "Other"
    start = date.today() - timedelta(days=days - 1)
    daily = {(start + timedelta(days=i)).isoformat(): {} for i in range(days)}
    for d, g, q, c in rows:
        slot = daily.get(str(d)[:10])
        if slot is not None:
            k = fold(names.get(g, g))
            slot[k] = slot.get(k, 0) + c
    series = keep + (["Other"] if len(keep) < len(ranked) else [])
    total = sum(totals.values())
    last7 = sum(sum(v.values()) for v in list(daily.values())[-7:])
    return {
        "available": True, "days": days, "group": group, "series": series,
        "daily": [{"date": d, "values": {k: round(v, 2) for k, v in vals.items()}} for d, vals in daily.items()],
        "kpis": {"total": round(total, 2), "dbus": round(sum(dbus.values()), 1),
                 "per_day": round(total / days, 2), "projected_30d": round(last7 / min(7, days) * 30, 2)},
        "breakdown": [{"name": g, "cost": round(totals[g], 2), "dbus": round(dbus[g], 1),
                       "share": round(totals[g] / total, 3) if total else 0} for g in ranked],
    }


class LiveCosts:
    mode = "live"

    def __init__(self):
        for t in (config.COST_USAGE_TABLE, config.COST_PRICES_TABLE):
            if not _TABLE.match(t):
                raise ValueError(f"not a table name: {t!r}")

    def _names(self) -> dict:
        """Map job / pipeline / warehouse ids from usage metadata to readable names."""
        out = {}
        try:
            w = sqlexec.client()
            for j in w.jobs.list(limit=100):
                out[str(j.job_id)] = j.settings.name if j.settings else str(j.job_id)
            for p in w.pipelines.list_pipelines(max_results=100):
                out[p.pipeline_id] = p.name
            for wh in w.warehouses.list():
                out[wh.id] = wh.name
        except Exception:
            pass            # names are a nicety; ids still work
        return out

    def costs(self, days: int, group: str) -> dict:
        sql = f"""
SELECT cast(u.usage_date AS STRING) AS d, {GROUP_SQL[group]} AS g,
       sum(u.usage_quantity) AS dbus,
       sum(u.usage_quantity * coalesce(p.pricing.default, 0)) AS cost
FROM {config.COST_USAGE_TABLE} u
LEFT JOIN {config.COST_PRICES_TABLE} p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud
 AND u.usage_start_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
WHERE u.usage_date > date_sub(current_date(), :days) AND u.usage_unit = 'DBU'
GROUP BY 1, 2"""
        try:
            rows = sqlexec.run(sql, {"days": ("INT", days)})
        except RuntimeError as e:
            msg = str(e)
            reason = ("permission" if "INSUFFICIENT_PERMISSIONS" in msg or "USE SCHEMA" in msg
                      else "missing" if "TABLE_OR_VIEW_NOT_FOUND" in msg or "SCHEMA_NOT_FOUND" in msg else "error")
            return {"available": False, "reason": reason, "message": msg[:300], "help": GRANT_HELP}
        parsed = [(d, g, float(q or 0), float(c or 0)) for d, g, q, c in rows]
        return shape(parsed, days, group, self._names() if group == "resource" else None)


class DemoCosts:
    mode = "demo"
    PRODUCTS = {   # product: (sku, resource, dollars per day)
        "JOBS": ("PREMIUM_JOBS_SERVERLESS_COMPUTE_AZURE", "bluesky-jetstream-ingest", 38.0),
        "DLT": ("PREMIUM_DLT_SERVERLESS_COMPUTE_AZURE", "bluesky-silver", 52.0),
        "SQL": ("PREMIUM_SERVERLESS_SQL_COMPUTE_AZURE", "SQL warehouse", 14.0),
        "MODEL_SERVING": ("PREMIUM_SERVERLESS_REAL_TIME_INFERENCE_AZURE", "embeddings endpoint", 9.0),
        "INTERACTIVE": ("PREMIUM_ALL_PURPOSE_SERVERLESS_COMPUTE_AZURE", "notebooks", 6.0),
    }
    DBU_PRICE = 0.37

    def costs(self, days: int, group: str) -> dict:
        rng = random.Random(f"costs{days}")
        rows = []
        for i in range(days):
            d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
            for product, (sku, resource, base) in self.PRODUCTS.items():
                ramp = 1.0 if product != "MODEL_SERVING" else 0.2 + 1.6 * i / max(1, days - 1)
                cost = base * ramp * rng.uniform(0.85, 1.15)
                key = {"product": product, "sku": sku, "resource": resource}[group]
                rows.append((d, key, cost / self.DBU_PRICE, cost))
        return shape(rows, days, group)
