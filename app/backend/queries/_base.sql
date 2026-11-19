-- Shared CTE: clean posts placed on the current topic map, inside the time window. Cluster names and
-- topics come from topic_clusters of the same map run (joined on computed_at). Posts in no cluster stay
-- in with a null cluster_id and 'no clear topic', so totals and filter counts include them; the cluster
-- queries skip them. Rows of a half-written map refresh (a cluster_id with no matching label) are left
-- out. Optional filter clauses are appended by services/warehouse.py.
WITH base AS (
  SELECT m.cluster_id, m.x, m.y, c.label AS cluster_label, coalesce(c.topic_l1, 'no clear topic') AS cluster_topic,
         e.uri, e.text, e.topic_l1, e.topic_label, e.topic_score, e.lang,
         e.geo_country, e.geo_place, e.bot_score, e.hashtags, e.link_domain, e.event_time,
         timestampdiff(SECOND, e.event_time, current_timestamp()) / 3600.0 AS age_h
  FROM {catalog}.{schema}.post_map m
  LEFT JOIN {catalog}.{schema}.topic_clusters c ON c.cluster_id = m.cluster_id AND c.computed_at = m.computed_at
  JOIN {catalog}.{schema}.post_enriched e ON e.uri = m.uri
  WHERE e.is_filtered = false
    AND (m.cluster_id IS NULL OR c.cluster_id IS NOT NULL)
    AND e.event_time > current_timestamp() - make_interval(0, 0, 0, 0, :hours, 0, 0)
    {filters}
)
