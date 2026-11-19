-- What the panels show beside a person's or company's name, one row per taxonomy entry. image (people only, a Commons
-- file name) is read through to_json so this still runs on a taxonomy_people written before that column existed.
SELECT qid, description, category AS sector, industry, get_json_object(to_json(struct(*)), '$.image') AS image
FROM {catalog}.{schema}.taxonomy_people
UNION ALL
SELECT qid, description, category, industry, NULL FROM {catalog}.{schema}.taxonomy_companies
