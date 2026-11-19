"""Map data source that serves real data once the topic map tables exist, and labelled demo data until then."""
import time

from .. import config
from . import sqlexec

RECHECK_SECONDS = 60


class AutoSource:
    def __init__(self, live, demo):
        self.live, self.demo = live, demo
        self._checked = 0.0
        self._ready = False

    def _all_have_rows(self, tables: tuple[str, ...]) -> bool:
        """One statement for all tables (each warehouse round trip is a few hundred ms); a missing table fails it."""
        t = f"{config.CATALOG}.{config.SCHEMA}"
        try:
            row = sqlexec.run("SELECT " + ", ".join(f"(SELECT 1 FROM {t}.{name} LIMIT 1)" for name in tables))[0]
        except RuntimeError:
            return False
        return all(v is not None for v in row)

    def _pick(self):
        if time.time() - self._checked > RECHECK_SECONDS:
            self._ready = self._all_have_rows(("post_map", "topic_clusters", "post_enriched"))
            self._checked = time.time()
        return self.live if self._ready else self.demo

    def current(self):
        """The source serving requests right now (live once the gold tables have rows)."""
        return self._pick()

    @property
    def mode(self):
        return self._pick().mode

    def meta(self, hours):
        return self._pick().meta(hours)

    def clusters(self, f):
        return self._pick().clusters(f)

    def cluster_posts(self, cluster_id, f, limit=25):
        return self._pick().cluster_posts(cluster_id, f, limit)
