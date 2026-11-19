-- The most-mentioned people and companies in each cluster (top 5 per kind).
SELECT cluster_id, kind, qid, label, c FROM (
  SELECT b.cluster_id, t.topic_type AS kind, t.topic_id AS qid, max(t.topic_label) AS label, count(DISTINCT b.uri) AS c,
         row_number() OVER (PARTITION BY b.cluster_id, t.topic_type ORDER BY count(DISTINCT b.uri) DESC) AS rk
  FROM base b
  JOIN {catalog}.{schema}.post_topics t ON t.uri = b.uri AND t.topic_type IN ('person', 'company')
  WHERE b.cluster_id IS NOT NULL
  GROUP BY b.cluster_id, t.topic_type, t.topic_id
) WHERE rk <= 5
