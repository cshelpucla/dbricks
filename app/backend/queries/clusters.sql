SELECT cluster_id,
       count(*) AS n,
       avg(x) AS x,
       avg(y) AS y,
       sqrt(var_pop(x) + var_pop(y)) AS spread,
       max(cluster_label) AS label,
       max(cluster_topic) AS topic,
       avg(topic_score) AS score,
       -- newer half of the data actually present (not of the window, which may predate the data)
       sum(CASE WHEN age_h < (SELECT max(age_h) FROM base) / 2 THEN 1 ELSE 0 END) AS recent
FROM base
WHERE cluster_id IS NOT NULL
GROUP BY cluster_id
