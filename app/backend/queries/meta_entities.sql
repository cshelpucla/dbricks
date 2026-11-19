-- Most-mentioned people and companies in the window (entity matches live in post_topics, topic_type person/company).
SELECT kind, qid, label, description, sector, industry, c FROM (
  SELECT t.topic_type AS kind, t.topic_id AS qid, max(t.topic_label) AS label, max(x.description) AS description, max(x.category) AS sector, max(x.industry) AS industry, count(DISTINCT b.uri) AS c,
         row_number() OVER (PARTITION BY t.topic_type ORDER BY count(DISTINCT b.uri) DESC) AS rk
  FROM base b
  JOIN {catalog}.{schema}.post_topics t ON t.uri = b.uri AND t.topic_type IN ('person', 'company')
  LEFT JOIN (SELECT qid, description, category, industry FROM {catalog}.{schema}.taxonomy_people
             UNION ALL SELECT qid, description, category, industry FROM {catalog}.{schema}.taxonomy_companies) x ON x.qid = t.topic_id
  GROUP BY t.topic_type, t.topic_id
) WHERE rk <= {limit}
ORDER BY kind, c DESC
