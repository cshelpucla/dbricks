# Enrichment job: detailed specification

Status: **deployed** 2026-11-19 as the continuous job `bluesky-enrich` (setup.sh). First hour live: 36,158 posts enriched, 11,400 embedded, 3,302 with a topic, newest row 88 s old. Parent document: [SPEC.md](SPEC.md) section 5. This document is the contract for the code in `bluesky/src/enrich_job.py` and `bluesky/src/enrich_lib.py`; where they disagree, fix one of them.

## 1. Purpose

Turn raw Bluesky post events into analytics-ready rows: which posts are bots or spam, where a post is probably about or from, what topic it belongs to, and a vector that places it on the semantic map. The job runs continuously, a couple of minutes behind the live stream.

Out of scope for this job: the 2D layout (`post_map`, a separate batch job), likes/reposts/follows, any change to bronze or silver.

## 2. Inputs

| Input | Use |
|---|---|
| `datawranglers.bluesky.events_raw` (bronze) | Source of post-create events. Read directly rather than silver, because silver is a CDC target (not append-only) and drops deletes. |
| Serving endpoint `databricks-qwen3-embedding-0-6b` | Text embeddings, 1,024 dimensions, multilingual. |
| IPTC Media Topics JSON (`cv.iptc.org/newscodes/mediatopic/`, CC BY 4.0) | Topic hierarchy. 1,059 active concepts; levels 0 to 5 (17 / 120 / 504 / 364 / 49 / 5). Parent links are in each concept's `broader` field as URIs; retired concepts carry a `retired` date and are excluded. |
| GeoNames `cities15000.zip`, `countryInfo.txt`, `admin1CodesASCII.txt` (CC BY 4.0) | Gazetteer: about 34k cities of 15k+ people, country list with capitals, state/province names. |

Bronze event fields used (all via the VARIANT column `event`): `record.text`, `record.langs`, `record.embed` (type, external uri/title, and the same under `media` for quote-with-media), `record.createdAt`, `record.reply.parent.uri`, `record.facets`, `record.tags`, `record.labels.values`, `record.via`, `record.bridgyOriginalUrl`, `record.fediverseId`.

## 3. Outputs

All in `datawranglers.bluesky`, created by the job if missing (idempotent DDL). Every table carries `event_date` for clustering.

### 3.1 `post_enriched` (one row per post, including filtered ones)

| Column | Type | Meaning |
|---|---|---|
| `uri` | string | `at://did/app.bsky.feed.post/rkey`. Logical primary key. |
| `did` | string | Author account. |
| `time_us` | bigint | Jetstream receive time in microseconds. The job's cursor. |
| `event_time`, `event_date` | timestamp, date | Arrival time (use this for all timing, not `created_at`). |
| `created_at` | timestamp | Client-claimed time (can be backdated). |
| `text` | string | Post text. |
| `lang` | string | First entry of `langs`, else null. |
| `langs` | array<string> | All language tags. |
| `is_reply` | boolean | Has a reply parent. |
| `embed_type` | string | Attachment type. |
| `link_url`, `link_domain` | string | External link card, if any. |
| `hashtags` | array<string> | Lower-cased tags from facets and `tags`. |
| `bot_score` | int | Sum of rule points (section 5). |
| `bot_reasons` | array<string> | Rules that fired. |
| `is_filtered` | boolean | `bot_score >= 4` or an adult label. These posts are never embedded or mapped. Image-only posts (empty text) are not filtered; they simply have no embedding. |
| `geo_country`, `geo_place` | string | Best location candidate (ISO country code; place name if city or state level). |
| `geo_lat`, `geo_lon` | double | Coordinates of the best candidate. |
| `geo_kind` | string | `coords`, `city`, `admin1`, `country`, `lang_region`, `flag`, `tld` or `lang`. |
| `geo_confidence` | double | 0 to 1. |
| `topic_qcode`, `topic_label` | string | Best IPTC concept (level 1 or deeper, see 7.3). |
| `topic_l1` | string | Label of that concept's top-level ancestor. |
| `topic_score` | double | Cosine similarity to the best concept. |
| `embedding` | array<float> | 1,024 values, L2-normalised. Null when not embedded. |
| `embedded` | boolean | Whether an embedding was requested and returned. |
| `enrich_version` | int | Logic version (section 12), so rows can be recomputed after a rule change. |
| `enriched_at` | timestamp | When this row was written. |

### 3.2 `post_topics` (post x topic, clean posts only)

`uri, event_time, event_date, topic_type ('iptc' | 'hashtag'), topic_id, topic_label, rank, score`. For `iptc`: the top 3 concepts at or above the score threshold, rank 1 to 3. For `hashtag`: one row per distinct tag, rank null, score null. Key: `(uri, topic_type, topic_id)`.

### 3.3 `post_geo` (post x location candidate)

`uri, event_time, event_date, place, country_code, lat, lon, kind, confidence, matched_text`. Every candidate with confidence 0.25 or more, one row per distinct `(kind, place)` per post. Filtered posts get none.

### 3.4 `topic_taxonomy`

`qcode, label, definition, parent_qcode, level, path (array<string> of labels from the root), path_codes, embedding, loaded_at`. Loaded once, then reused. Reloaded when the table is empty or the source's `dateReleased` changes.

### 3.5 `bot_accounts`

`did, score, reasons (array<string>), first_seen`. Append-only; an account appears once.

## 4. Processing loop

State is derived from the tables (no checkpoint files).

1. `tail` = newest `time_us` among post events in bronze (looking back one day). `cursor` = `max(time_us)` in `post_enriched`; if the table is empty, `tail` minus `START_MINUTES`.
2. Window = `(cursor, min(tail - LAG_MARGIN, cursor + WINDOW_SECONDS)]`. The margin keeps the job behind the ingest job's write batches so late rows are not skipped. If the window is empty, sleep `INTERVAL_SECONDS` and retry.
3. Read post-create events in the window (partition-pruned with `event_date`), deduplicate on `(did, rkey, rev)`.
4. Compute, in this order: normalised fields and hashtags; bot scoring (needs the whole window); geo candidates for non-filtered posts; embedding for a capped sample of embeddable non-filtered posts; topic assignment; rows for each output table.
5. Write `post_geo`, `post_topics`, `bot_accounts`, then `post_enriched` last. The cursor lives in `post_enriched`, so a crash before the last write re-processes the window; duplicates in the satellite tables are possible and consumers deduplicate on `uri`.
6. If more backlog remains (window upper bound is behind `tail - LAG_MARGIN`), continue immediately without sleeping.

Failure handling: a window is retried up to 3 times with 10 s, 20 s backoff; then the exception propagates and the continuous job restarts the task, which resumes from the cursor. Embedding calls retry individually; if more than half of a window's embedding batches fail, the window fails (so an endpoint outage is loud, not a silent run of null vectors).

## 5. Bot and spam scoring

Computed per window with per-account aggregates. `is_filtered` when the total is 4 or more.

| Rule (reason code) | Points |
|---|---|
| `rate`: account posts at least 6 per minute in the window | +3 |
| `links`: at least 5 posts in the window and at least 90% are link cards | +2 |
| `nolang`: at least 5 posts in the window and at least 90% have no language tag | +1 |
| `dup_text`: the same normalised text (20 or more characters) at least 3 times in the window, any accounts | +4 (filters on its own) |
| `dup_self`: the same account repeats identical text | +2 |
| `backfill`: `created_at` more than 7 days before arrival | +4 |
| `future`: `created_at` more than 1 hour after arrival | +1 |
| `tag_stuffing`: 8 or more hashtags | +2 |
| `mention_stuffing`: 6 or more mentions | +2 |
| `spam_words`: matches the spam phrase list (airdrop, "dm me", telegram/whatsapp promotion, "onlyfans", "click link in bio", and similar) | +2 |
| `label_bot`: self-label `bot` | +4 |
| `via_bot`: `via` contains "bot" | +2 |
| `autoposter`: link domain in dlvr.it, ift.tt, buff.ly, ow.ly | +1 |
| `bridged`: bridged from another network | +1 |
| `adult`: self-label porn, sexual, nudity or adult | +2, and always filtered |
| `known_bot`: account already in `bot_accounts` | +4 |
| `empty`: text empty or under 3 characters | 0 (informational: image-only posts are normal, they just cannot be embedded) |

An account whose window-level rules (`rate`, `links`, `nolang`, `dup_self`) sum to 3 or more is appended to `bot_accounts`. Thresholds are starting values (section 11) and are config, not constants.

Baseline from measured data (2026-11-19, 30 minutes): 20 accounts made 37% of posts, 97% of their posts were link cards and 95% untagged; the other accounts had 16% and 14%. A 10-minute sample on the same day showed the busiest account at 14 posts per minute.

## 6. Geo extraction

Each source yields candidates `(place, country_code, lat, lon, kind, confidence, matched_text)`.

| Source | kind | Confidence |
|---|---|---|
| Coordinates in text or link (`@lat,lon`, `?q=lat,lon`, `mlat=&mlon=`, `geo:`, bare decimal pair with 3+ decimals, ranges validated) | `coords` | 1.0 |
| Multi-word city name (e.g. "New York", "Buenos Aires") | `city` | 0.7 |
| Single-word city name | `city` | 0.6 with a location preposition before it (in, at, near, from, to, around, outside, across, of) or a hashtag; 0.5 if population is 1M or more; otherwise not emitted |
| State or province name | `admin1` | 0.5 (centroid: population-weighted mean of its cities) |
| Country name or alias (USA, UK, UAE, "United States", "the Netherlands") | `country` | 0.4 (capital's coordinates) |
| Hashtag that matches a place | as the match type | same as above, no capitalisation needed, tag at least 4 characters |
| Language region subtag (`pt-BR`, `en-GB`) | `lang_region` | 0.5 |
| Flag emoji (pair of regional indicator symbols) | `flag` | 0.3 |
| Country-code top-level domain of the link, excluding generic/hack domains (tv, ly, co, io, ai, me, fm, am, gl, gd, sh, to, cc, ws, nu, ms, vc, ac, ag, dj, st, la, cx, tk, ml, ga, cf, gq, sx) and `youtu.be` | `tld` | 0.25 |
| Language implying a single country (ja, ko, th, vi, id, tr, pl, sv, da, fi, nb, nn, no, cs, hu, el, he, ro, bg, hr, sk, sl, lt, lv, et, is) | `lang` | 0.15 |

Matching rules:
- Latin-script matching tokenises words, builds 1 to 4 word n-grams of adjacent words, takes the longest non-overlapping matches, and requires the surface form to start with a capital letter (skipped for all-caps text and for hashtags).
- A short list of ambiguous names (nice, reading, mobile, turkey, chad, jordan, georgia, orange, bath, wells, hell, normal, mars, nevada city) is only accepted with a location preposition.
- Non-cased scripts (CJK, Arabic, Cyrillic, Thai, Hangul and others) match substrings of 2 to 8 characters against the non-Latin name set, longest first.
- City names come from `name`, `asciiname`, and alternate names: non-Latin alternates for all cities, Latin alternates only for cities with population 200k or more and 5+ characters. Where several places share a name, the most populous wins.
- State names that equal a city or country name are dropped (the city or country wins).

Summary choice: group candidates by country; each additional source agreeing on the same country adds 0.1 (cap 0.95); pick the highest-confidence candidate, preferring the more specific kind on ties (coords, city, admin1, country, others). The winning candidate fills the `geo_*` columns of `post_enriched`. Candidates below 0.15 are dropped; those below 0.25 (a lone language hint) are not written to `post_geo`, but the best candidate at any confidence fills the `geo_*` columns, so consumers choose their own cut-off. The app treats 0.4 or more as "has an inferred location".

## 7. Embeddings and topics

### 7.1 Embedding text

Post text with URLs removed, whitespace collapsed, truncated to 500 characters. If under 40 characters and a link card title exists, append " - " and the title. Posts under 12 characters after cleaning are not embedded. The string sent to the endpoint is prefixed with an instruction (`Instruct: Identify which news topic category this social media post is about\nQuery: `), which measurably improved topic matching on 2,000 real posts; the stored vector therefore encodes topic, which suits the map. Measured on 500 posts: 593 posts per second, about 37 tokens per post bare and 53 with the instruction (about 53M tokens per million posts).

### 7.2 Calling the endpoint

Batches of 32 inputs per request, 6 worker threads, 3 retries per batch with backoff. Vectors are converted to float32 and L2-normalised. The response's token usage is summed and logged per window. A cap of `EMBED_MAX_PER_WINDOW` posts (default 600, about 5 per second) is applied by uniform random sampling of eligible posts; the rest are stored without an embedding. Raise the cap only after the first day's token usage and cost are known.

### 7.3 Topic assignment

The full description of this classifier, with its 2026-11-19 review, version 2 changes and the planned trained replacement, is [POST_CLASSIFIER.md](POST_CLASSIFIER.md). **Version 2** (in code 2026-11-19, not yet deployed) removes links without a scheme before embedding, applies the 25-character gate to the embedded text, and reports the media-and-entertainment and computing branches of *economy > products and services* under arts and under science and technology (`SUBJECT_OVERRIDES`).

- `topic_taxonomy` embeddings are computed from `"<label>: <definition>"` (English, `en-GB`), bare, for all 1,059 active concepts (one-off cost: 23,792 tokens, 2 seconds).
- Matching uses concepts at levels 1 to 3 (988 concepts) plus 12 "no topic" anchor phrases (greetings, small talk, short replies, jokes, vague opinions and similar). Each embedded post is compared with every row by cosine similarity. If the nearest row is an anchor, or the post is under 25 characters, it gets no IPTC topic. Otherwise the top 3 concepts at or above `TOPIC_MIN_SCORE` go to `post_topics`, and the best becomes `topic_qcode`.
- `topic_l1` is the label of the level-0 ancestor, found by walking `parent_qcode`.
- **Calibration (2026-11-19, 2,000 real posts, instruction embeddings):** at 0.62, 31% of posts receive a topic; at 0.58 53%; at 0.66 12%. Plain nearest-label matching without anchors gave topics to 93% of posts at 0.35 and most of the extra were wrong (chatter forced into "hobby", "holiday", "human mishap"). Hand-checking 22 accepted posts at 0.62 found roughly 55 to 65% clearly sensible (earthquakes, astronomy, product ads, geopolitics) and the rest vague or wrong (for example a fursona joke labelled "sport industry"). So per-post topics are a weak signal; **cluster-level labels, taken as the majority of a cluster's posts, are far more reliable**, and the app exposes `topic_score` as a filter.
- Subtracting each concept's average score (to damp concepts that attract everything) was tried and rejected: it replaces generic matches with obscure ones (Zoroastrianism, Walpurgis night).
- **Planned improvement:** retrieve the top 5 concepts by embedding, then have a small LLM (`databricks-claude-haiku-5-5`) choose among them or answer "none". Not in the first build; cost per post would need measuring.

### 7.4 Hashtags

From `facets` features of type `app.bsky.richtext.facet#tag` and the record's `tags` array, lower-cased, de-duplicated, at most 20 per post. Written to `post_topics` as `hashtag` rows for clean posts.

### 7.5 People and companies

Famous people and companies are found by **name matching**, not embeddings (an embedding puts "Trump" near "politics" but cannot tell Trump from Biden). Matches are written to `post_topics` with `topic_type` `person` or `company`, `topic_id` the Wikidata id (for example `Q22686`), `topic_label` the English name and `score` the match confidence. Every clean post is checked, not only the embedded sample.

**Taxonomies, each pulled by its own scheduled job** (`taxonomy_job.py`, schedule in `config.env`, weekly by default) into its own table, so each has separate run history and can be run on demand (`databricks jobs run-now`):

| Job | Table | Source |
|---|---|---|
| `bluesky-taxonomy-people` | `taxonomy_people` | Wikipedia "Vital articles/Level 4/People" (about 1,900 figures editors judge essential, grouped by field, so Leonardo and Michelangelo are in) plus people in the English Wikipedia monthly top-1000 pageviews in at least 2 of the last 6 months with at least 30 Wikipedia editions (so Trump and Musk are in even when they are not "vital"). Confirmed as humans through Wikidata. |
| `bluesky-taxonomy-companies` | `taxonomy_companies` | Wikidata items whose class is business, public company, company, multinational, enterprise, corporation, media company, technology company, airline, bank, car maker, social network or video game company, with at least 30 Wikipedia editions. |

Columns: `qid, kind, label, description, aliases[], sitelinks, category, industry, image, source, loaded_at`. For people `category` is the Vital Articles field (Entertainers, Writers, Sports figures ...) and `image` is the Commons file name of their English Wikipedia article's lead image (the freely licensed page image, usually the infobox portrait, fetched in the same API call that maps titles to Wikidata ids; JPEG, PNG, GIF or WebP only, else null). The app shows it as a 120 px Commons thumbnail beside the name, with the person's initials if there is none or it fails to load. Companies have no image; the app shows an icon for their industry or sector instead. For companies it is the **sector** and `industry` is the company's own Wikidata "industry" (P452) value, giving the hierarchy **sector > industry > company**. The 12 sectors are fixed and GICS-style (Technology, Financials, Health Care, Energy, Utilities, Communication Services, Consumer Discretionary, Consumer Staples, Industrials, Transportation, Materials, Real Estate, plus Other); the sector is chosen by keyword rules over the industry names and, if the company has none, its description (`entity_lib.SECTOR_RULES`, first match wins). Industry is null when Wikidata has none. `sitelinks` (number of Wikipedia editions) is the importance weight. A table is replaced only after a complete pull (at least 500 people or 300 companies, else the job fails and the old table stays). The enrichment job re-reads both tables every 10 minutes and rebuilds its matcher when `loaded_at` changes; until the tables exist it runs without entity matching.

**Matching rules** (`entity_lib.py`, tested in `tests/test_entity_lib.py`): names of two or more words match case-insensitively on word boundaries, accent-insensitively, with possessives removed ("Trump's"); one-word names (surnames, mononyms, short brand names) must be capitalised exactly as written, and a company name or a surname that is also an everyday word ("Apple", "Cook") must also be mid-sentence unless written in capitals, so "Apple pie" and "Cook the pasta" do not match. A one-word name claimed by several entities goes to the best known if it has at least 1.5 times the sitelinks of the runner-up, otherwise it is dropped (so "Bush" matches nobody). Surnames that are also first names, months or weekdays are never used alone. Company legal suffixes are stripped ("Alphabet Inc." also matches "Alphabet"). Hashtags match the name with spaces removed (`#donaldtrump`). At most 5 entities per post. Confidence is 1.0 for a multi-word name, 0.9 for a hashtag and 0.7 for a one-word name.

**Known limits:** English names only (Japanese, Korean and Arabic spellings are not matched); mentions by `@handle` are not used; people and companies whose name equals an ordinary capitalised phrase will occasionally match. Wikimedia asks API clients to identify themselves: set `WIKI_CONTACT` in `config.env`, otherwise their rate limiter is strict and a pull can take several minutes.

## 8. Configuration

In `bluesky/config.env`, passed to the job as arguments:

| Name | Default | Meaning |
|---|---|---|
| `ENRICH_ENABLED` | true | `setup.sh` deploys the job when true. |
| `ENRICH_JOB_NAME` | bluesky-enrich | Job name (the app's Pipelines tab looks for this). |
| `ENRICH_WINDOW_SECONDS` | 120 | Maximum span per processing window. |
| `ENRICH_INTERVAL_SECONDS` | 20 | Sleep when caught up. |
| `ENRICH_LAG_MARGIN_SECONDS` | 60 | How far behind the bronze tail to stay. |
| `ENRICH_START_MINUTES` | 30 | Where to start on first run. |
| `EMBED_ENDPOINT` | databricks-qwen3-embedding-0-6b | Embedding endpoint. |
| `EMBED_MAX_PER_WINDOW` | 600 | Embedding cap per window. |
| `TOPIC_MIN_SCORE` | 0.62 | Minimum similarity for a topic match (calibrated, section 7.3). |
| `BOT_THRESHOLD` | 4 | Score at or above which a post is filtered. |

Rule thresholds in section 5 are constants in `enrich_lib.py` with the same names as the reason codes, so they are easy to find and tune.

## 9. Deployment

- Serverless continuous job `bluesky-enrich`, one task running `enrich_job.py`, environment version 3 (provides pandas, numpy, pyarrow and the Databricks SDK). Created and updated by `setup.sh`, paused by `stop.sh`, reported by `status.sh`, removed by `teardown.sh`.
- Outbound internet is required at start to download the GeoNames and IPTC files. If the GeoNames download fails the job logs a warning and runs without gazetteer matching (coordinates, flags, language and TLD signals still work); if the IPTC load fails and no taxonomy table exists, the job fails.
- Permissions: the job's identity needs `SELECT` on bronze, `CREATE TABLE`/`MODIFY` in the schema, and `CAN QUERY` on the embedding endpoint. The app's service principal already has `SELECT` on the schema.

## 10. Observability

Per window the job logs: window bounds, lag behind the tail, posts read, posts filtered by reason, posts embedded and requests failed, tokens used, geo hit rate (posts with any candidate at or above 0.4), topic hit rate (embedded posts above threshold), and stage timings. The Pipelines tab shows rows written and freshness from Delta history with no extra code, since `post_enriched` is a normal Delta table.

Service levels: window processing at least 3 times faster than real time (so the job catches up after a pause), and `post_enriched` freshness under 5 minutes in steady state.

## 11. Testing and calibration

Automated (`bluesky/tests/test_enrich_lib.py`, no Spark or network): bot rules on crafted accounts (a 14-per-minute poster, a repeat poster, a link-only account, an archive import, a normal user), hashtag extraction, every geo source and the precision rules (for example "I love Nice" vs "Nice weather", "Paris, Texas", Japanese city names, `youtu.be` not counted as Belgium), embedding-text cleaning, topic matrix assignment with synthetic vectors, IPTC hierarchy parsing (level counts 17 / 120 / 504 / 364 / 49 / 5), and cursor-window arithmetic.

Against real data (sample in the scratchpad, 11,391 posts from a 10-minute window): report the filtered share and which rules fire most, the geo hit rate and a hand check of 50 random geo matches for precision, and a hand check of 50 random topic assignments at several score thresholds to choose `TOPIC_MIN_SCORE`.

Acceptance for the first deployment: the job runs for an hour without restarting; `post_enriched` freshness stays under 5 minutes; the filtered share is reported (a 10-minute sample on 2026-11-19 gave 4.5%; investigate if it is above 50% or below 1%); about 30% of embedded posts have a topic and about 30% of clean posts have any geo candidate (7% at confidence 0.4 or more); and token usage per day is known and reported.

## 12. Versioning and reprocessing

`enrich_version` starts at 1 and increases when a rule, threshold or taxonomy changes. To reprocess, delete rows from `post_enriched` and its satellites for the affected time range; the cursor then re-reads that range from bronze (bronze keeps the raw events). Bronze is the source of truth, so enrichment output can always be rebuilt.

## 13. Risks and open questions

1. **Embedding cost:** about 53 tokens per post with the instruction. At the default cap of about 5 posts per second that is roughly 23M tokens a day; the dollar rate is unmeasured, so check the first day's bill before raising the cap.
2. **Topic quality:** measured at roughly 55 to 65% sensible per post (section 7.3). An LLM re-ranking step is the planned fix, and a cluster-then-name step would cover topics IPTC lacks.
3. **Geo precision** from text alone is limited; the confidence column lets consumers choose a cut-off. Known remaining false positives seen in the first run: demonyms that are also place names ("Somali"), street names that share a town's name ("Oak Hill" in a UK roadworks post matched a US town), and the US/UK capital standing in for country-level mentions. Real-data checks during the build removed the worst noise: fragments of Japanese words matching small foreign towns, the Arabic article matching Halle, literal translations ("White House" for Casablanca), person names ("Nancy Pelosi"), generic region words ("South", "East") and all-caps acronyms ("BUY"). Account-level time-zone inference (needs days of history) is the next improvement.
4. **Privacy and retention:** post text and embeddings are stored. A policy for retention and for honouring post deletions (bronze records them) is still to be decided.
5. **Backlog after long pauses:** Jetstream only keeps 36 hours, so the ingest job bounds how far back this job can ever need to go.
