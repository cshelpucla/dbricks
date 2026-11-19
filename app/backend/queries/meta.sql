SELECT 'topic' AS kind, cluster_topic AS value, count(*) AS c FROM base GROUP BY 2
UNION ALL
SELECT 'lang', lang, count(*) FROM base WHERE lang IS NOT NULL GROUP BY 2
UNION ALL
SELECT 'country', geo_country, count(*) FROM base WHERE geo_country IS NOT NULL GROUP BY 2
UNION ALL
SELECT 'total', NULL, count(*) FROM base
