# Bluesky Analytics: Specification

Status as of 2026-11-19. Items are marked **[built]**, **[designed]** (decided, not yet coded) or **[open]**.

## 1. Goal

Turn the live Bluesky firehose into original, explorable analytics on Databricks:

1. **Semantic map**: every post as a point on a 2D plane, similar topics clustering together; the follow network laid out the same way.
2. **Topics**: extracted as posts arrive, into their own table, organised under a public hierarchy.
3. **Geography**: as much location as can be inferred from post content, shown on real maps.
4. **Clean signal**: bot and spam traffic filtered out of all analytics.
5. Served through a **Databricks App** (section 9).

Later extensions (not in scope now): combining with other live feeds (Wikipedia edits, Polymarket, European grid data, seismic).

## 2. Architecture

```
Jetstream (v2) --> [ingest job] --> events_raw (bronze) --> [silver pipeline] --> posts / likes / reposts / follows
                                         |
                                         +--> [enrich job] --> post_enriched, post_topics, post_geo, bot_accounts
                                                                    ^
                                      topic_taxonomy (IPTC) --------+
                                                                    |
                                          [map job, batch] --> post_map
                                                                    |
                                                        Databricks App (SQL warehouse reads)
```

All objects live in `datawranglers.bluesky` (Unity Catalog). Resource names and tuning live in `bluesky/config.env`.

## 3. Ingestion and silver layer **[built]**

- `bluesky-jetstream-ingest`: continuous serverless job. Reads Jetstream v2 (hosts `jetstream.us-east` / `us-west`, failover), collections `post`, `like`, `repost`, `follow`. Resumes from the bronze table's newest `time_us` minus a 10 s rewind; Jetstream keeps a 36 h lookback.
- `events_raw` (bronze): `seq, time_us, event_time, event_date, kind, did, collection, operation, rkey, rev, event (VARIANT), ingested_at`. Clustered by `(event_date, collection)`. At-least-once delivery, so duplicates are possible.
- `bluesky-silver`: continuous Lakeflow pipeline producing current-state tables (SCD1, deletes applied): `posts`, `likes`, `reposts`, `follows`.
- Observed volume (2026-11-19): about 270 events/s, of which ~53 posts/s, ~220 likes/s, ~32 reposts/s, ~20 follows/s.
- Operations: `setup.sh` deploys/restarts, `stop.sh` pauses, `status.sh` reports, `teardown.sh` deletes. Both resources bill 24/7 while running.

## 4. Data characteristics that shape the design (measured)

| Finding | Consequence |
|---|---|
| 20 accounts made 37% of posts in a 30-minute window; 97% of their posts are link cards and 95% have no language tag | Account-level bot detection is essential; without it every aggregate is skewed |
| Excluding them: 16% link cards, 14% no language | Normal baseline for thresholds |
| 4 accounts posted 17,546 posts with `createdAt` more than 30 days before arrival (archive imports) | Use `event_time` for timing, flag backdated posts |
| Deletions are common (hourly: ~4.5k unfollows, ~4.1k unlikes, ~2.6k post deletes) | Silver drops them; raw table is the source for deletion analytics |
| 75 distinct languages in one hour; `langs` sometimes carries a region (`en-US`, `pt-BR`) | Region subtags are a usable geo signal |
| Bluesky has no location field, no profiles, no handles in this stream | Geo must be inferred; profiles (`app.bsky.actor.profile`) would need to be added to `COLLECTIONS` |

## 5. Enrichment job **[built and running; full contract in [ENRICHMENT_SPEC.md](ENRICHMENT_SPEC.md)]**

A separate continuous serverless job, `bluesky-enrich`, reading new post-create events from bronze in time windows.

### 5.1 Processing loop

1. `tail` = newest bronze post `time_us`. `cursor` = newest `time_us` already in `post_enriched` (on first run, `tail` minus a start offset).
2. Process posts with `cursor < time_us <= min(tail - 60 s, cursor + window)`. The upper bound stays behind the bronze tail so late-written events are not skipped.
3. Dedupe on `(did, rkey, rev)`; score bots/spam; extract hashtags and geo; embed clean posts; assign topics.
4. Write `post_geo`, `post_topics`, `bot_accounts`, then `post_enriched` last, so the cursor only advances after the other tables are written. A retry after a crash can duplicate sub-table rows; consumers dedupe on `uri`.

### 5.2 Bot and spam filtering

Additive score per post; `is_filtered = bot_score >= 4`. All posts are stored with their score and reasons; only clean posts are embedded and mapped.

| Signal | Points |
|---|---|
| Account posts at least 6 per minute within the window | +3 |
| Account has at least 5 posts in window and at least 90% are link cards | +2 |
| Account has at least 5 posts in window and at least 90% lack a language tag | +1 |
| Same normalised text at least 3 times in window (any accounts) | +3 |
| Same account repeats identical text | +2 |
| `createdAt` older than 7 days vs arrival (archive import) | +4 |
| `createdAt` more than 1 hour in the future | +1 |
| 8 or more hashtags, or 6 or more mentions | +2 |
| Spam phrases (airdrop, "dm me", telegram/whatsapp promotion, etc.) | +2 |
| Self-label `bot` | +4 |
| `via` field contains "bot" | +2 |
| Auto-poster link domains (dlvr.it, ift.tt, buff.ly, ow.ly) | +1 |
| Bridged mirror (`bridgyOriginalUrl`, `fediverseId`) | +1 |
| Adult self-label (`porn`, `sexual`, `nudity`, `adult`) | +2 (kept out of maps) |
| Account already in `bot_accounts` | +4 |
| Text empty or under 3 characters | filtered, reason `empty` |

Accounts with an account-level score of 3 or more in a window are appended to `bot_accounts`. Thresholds are starting values to be calibrated against the sampled data and recorded in config.

### 5.3 Geo extraction

Evidence sources, each producing a candidate with a confidence:

| Source | Confidence |
|---|---|
| Coordinates in text or map links (Google Maps `@lat,lon`, `?q=lat,lon`, `mlat/mlon`, `geo:`, bare decimal pairs) | 1.0 |
| City name match (GeoNames `cities15000`, population 15k+) | 0.5 to 0.7 (higher with a location preposition or multi-word name) |
| State/province name (centroid of its cities, population-weighted) | 0.5 |
| Country name or alias (capital's coordinates) | 0.4 |
| Hashtag that matches a place | as the matching type |
| Language region subtag (`pt-BR`, `en-GB`) | 0.5 |
| Flag emoji | 0.3 |
| Link country-code TLD (excluding generic/shortener TLDs such as `.tv`, `.ly`, `.co`, `.io`, and `youtu.be`) | 0.25 |
| Language alone where it implies one country (ja, ko, th, vi, ...) | 0.15 |

Rules: Latin-script names require capitalisation; a small list of ambiguous names (Nice, Reading, Mobile, Turkey, Chad, Jordan, Georgia, Orange, Bath, Wells) is down-weighted or excluded; names in non-cased scripts (Japanese, Chinese, Korean, Arabic, Thai) match directly with a minimum length. Agreement between sources on the same country adds 0.1 each (cap 0.95). Every candidate goes into `post_geo`; the best one is summarised on `post_enriched`. Gazetteer: GeoNames (CC BY 4.0), downloaded at job start.

**[open]** Account-level time zone inference from posting hours, which needs several days of history and is the most promising way to separate languages shared by many countries.

### 5.4 Embeddings

- Model: serving endpoint `databricks-qwen3-embedding-0-6b` (1,024 dimensions, multilingual; verified: same-meaning sentences in Japanese, Spanish and English scored 0.78 to 0.84, unrelated pairs 0.19 to 0.33).
- Input: post text with URLs removed, whitespace collapsed, truncated to 500 characters; link card title appended when the text is short. Posts under 12 characters after cleaning are not embedded.
- Called in batches of 32 inputs per request with a small thread pool and retries. A per-window cap and sample rate bound cost.
- Stored as `ARRAY<FLOAT>` on `post_enriched`.

### 5.5 Topics

- **Hierarchy: IPTC Media Topics** (CC BY 4.0, released 2026-09-30): 1,059 active concepts under 17 top-level subjects, labels in 13 languages. Chosen over IAB Content Taxonomy 3.1 (704 nodes, 37 top-level, English only) for the multilingual labels and news orientation. IAB remains a possible second view for advertising-style categories.
- `topic_taxonomy` holds each concept: code, label, definition, parent, level, path of labels from the root, and an embedding of "label: definition".
- Each clean post is matched to the nearest concepts by cosine similarity; the top 3 above a calibrated threshold are written to `post_topics` (rank, score). Posts below the threshold get no IPTC topic (casual chatter). Ancestors are derived by joining the taxonomy, not stored per post.
- Hashtags are written to `post_topics` as a second topic type, giving a free-form complement to the fixed taxonomy.
- **[open]** Emerging topics not in IPTC (memes, events): cluster the embeddings and have an LLM name each cluster (`databricks-claude-haiku-5-5`, which rejects a `temperature` parameter).

## 6. Tables

| Table | Grain | Key columns |
|---|---|---|
| `post_enriched` | one row per post | `uri, did, event_time, event_date, time_us, text, lang, langs, is_reply, embed_type, link_url, link_domain, hashtags, bot_score, bot_reasons, is_filtered, geo_country, geo_place, geo_lat, geo_lon, geo_kind, geo_confidence, topic_qcode, topic_label, topic_l1, topic_score, embedding, enriched_at` |
| `post_topics` | post x topic | `uri, event_time, topic_type (iptc or hashtag), topic_id, topic_label, rank, score` |
| `post_geo` | post x geo candidate | `uri, event_time, place, country_code, lat, lon, kind, confidence, matched_text` |
| `topic_taxonomy` | IPTC concept | `qcode, label, definition, parent_qcode, level, path, embedding` |
| `bot_accounts` | account | `did, score, reasons, first_seen` |
| `post_map` | post | `uri, x, y, cluster_id, computed_at` (snapshot, overwritten); in code but not yet deployed: one row per embedded post of the window, plus `in_sample, cluster_share, cluster_topic` |

## 7. Topic map job **[built and running]**

**2026-11-19 review and changes:** [TOPIC_IDENTIFICATION.md](TOPIC_IDENTIFICATION.md). The changes are in code but not yet deployed: languages centred before layout, every embedded post placed in a cluster through its nearest sampled neighbours (`post_map.cluster_topic` becomes a post's main topic), unclustered posts kept in the app's counts, and a consistent IPTC hint for the namer. The steps below describe the deployed version.

Scheduled serverless job `bluesky-topic-map` (`bluesky/src/map_job.py`, logic in `map_lib.py`), every 2 hours at minute 0 UTC (`MAP_SCHEDULE`). Each run:

1. samples up to 40,000 clean, embedded posts from the last 24 hours of `post_enriched` (`MAP_MAX_POINTS`, `MAP_HOURS`);
2. reduces the 1,024-dimension vectors to 50 with PCA, then lays them out in 2D with UMAP (cosine, 15 neighbours, min_dist 0.05), scaled to 0 to 100;
3. finds clusters with HDBSCAN on the 2D layout (minimum size max(30, points / 500)), so clusters match the islands people see; about 12% of posts fall in no cluster;
4. names each cluster with `databricks-claude-haiku-5-5`, which reads the 12 posts nearest the cluster centre, its top hashtags and the majority IPTC match, and returns a 2 to 5 word label plus one IPTC top-level subject or "none" (chatter). Falls back to the majority IPTC label or a hashtag used by 10%+ of the cluster's posts if the LLM fails;
5. overwrites `post_map` (uri, x, y, cluster_id, computed_at) and `topic_clusters` (cluster_id, label, topic_l1, label_source, n, x, y, majority IPTC label and share, top hashtags, computed_at). The app joins them on `computed_at`, so a reader mid-refresh briefly sees no labels rather than mismatched ones.

Measured (2026-11-19): 40,000 posts take about 2 minutes after environment setup (sampling 17 s, layout and clustering 50 s, naming 24 s for 51 clusters). Runs produced 51 and 75 clusters, all LLM-named. Labels are specific ("Commentary on Trump and world politics", "GTA 6 Hype and Opinions", "Automated Radio Now Playing Posts", "Bird of the Day photos"). Chatter forms language islands ("Japanese Fandom Casual Chatter", "German Conversational Reply Fragments"), which the namer gives no subject; about 57% of mapped posts are in such clusters.

Lessons recorded in code comments: serverless environment 3 preinstalls numpy 1.26.4, scipy 1.13.1 and scikit-learn 1.4.2 and rejects upgrades of core packages, so the job pins umap-learn 0.5.7, numba 0.60.0 and pynndescent 0.5.13; and the chat endpoint sometimes answers with a reasoning block first, so naming calls allow 1,000 output tokens and read only text blocks.

Not done: the follow-network layout, and placing posts outside the sample (the map is a sample, refreshed every 2 hours).

## 8. Operations and cost

- The enrich job is a third always-on billing resource, on top of ingest and silver. `stop.sh`, `status.sh` and `teardown.sh` are to be extended to cover it, with a config switch to run it on a schedule instead.
- Variable cost is embedding tokens: bounded by the per-window cap and sample rate. Measure the first day before raising limits.
- Public data only. Posts are public; text is stored in the workspace, so retention and deletion handling need a policy (see open questions).

## 9. Databricks App **[built: topic map (live), world map (live), pipelines, costs, architecture diagram, data model, control]**

Directory `app/` (see its README), deployed as `bluesky-analytics`. Tabs: Topic map, World map (post volume by inferred location, live from `post_enriched`), Pipelines, Costs, Architecture (seven-page draw.io diagram, `app/frontend/public/diagrams/pipelines.drawio`), Data model (ERD), Control (pause or resume each resource or all of them). The map tab was planned with these views:

1. **Semantic map**: pan/zoom scatter of `post_map`, coloured by IPTC top-level topic, with hover text and a filter by language, time window and topic.
2. **Topic explorer**: hierarchy drill-down with volume over time and the fastest-rising topics.
3. **Geo map**: inferred locations with confidence slider; hotspots over the last hour.
4. **Signal quality**: filtered vs kept volume, top bot accounts, rule hit counts.
5. **Network**: follow graph layout (later).

The backend queries Unity Catalog through a SQL warehouse using the app's service principal, which needs read access to the schema.

## 10. Open questions

1. Retention: how long to keep post text and embeddings, and whether to honour deletes by removing rows from enriched tables.
2. Embed every clean post, or sample? (Roughly 35 clean posts/s is about 3 million a day.)
3. Which second taxonomy, if any (IAB), and whether to map hashtags into IPTC.
4. Add `app.bsky.actor.profile` and `app.bsky.graph.block` collections for richer geo (profile text often names a city).
5. Calibration set: hand-label about 200 posts to tune bot thresholds, topic threshold and geo confidence.

## 11. Build order

1. ~~Enrichment job~~ (done 2026-11-19).
2. ~~Topic map job~~ (done 2026-11-19).
3. Verify a day of data; check the embedding and naming token bills; recalibrate thresholds.
3. Map layout job.
4. App skeleton, then views in the order of section 9.

## 12. Attribution

IPTC Media Topics, CC BY 4.0, IPTC. GeoNames, CC BY 4.0, geonames.org. IAB Content Taxonomy, CC BY 3.0, IAB Tech Lab.
