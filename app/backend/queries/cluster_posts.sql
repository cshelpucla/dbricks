SELECT uri, text, topic_l1, topic_label, topic_score, lang, geo_country, geo_place, bot_score,
       to_json(hashtags) AS hashtags, link_domain, age_h
FROM base
WHERE cluster_id = :cid
ORDER BY event_time DESC
LIMIT {limit}
