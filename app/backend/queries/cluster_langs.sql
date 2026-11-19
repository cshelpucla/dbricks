SELECT cluster_id, lang, c FROM (
  SELECT cluster_id, lang, count(*) AS c,
         row_number() OVER (PARTITION BY cluster_id ORDER BY count(*) DESC) AS rk
  FROM base WHERE lang IS NOT NULL AND cluster_id IS NOT NULL GROUP BY cluster_id, lang
) WHERE rk <= 3
