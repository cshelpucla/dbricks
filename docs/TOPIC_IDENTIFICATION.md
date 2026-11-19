# Topic identification

Status as of 2026-11-19: reviewed, and three improvements implemented in code with tests passing (48 bluesky tests). **Nothing in this document is deployed yet**: uploading to the workspace was blocked by the session's permission check, so section 6 lists the commands to run. The per-post classifier has its own document, [POST_CLASSIFIER.md](POST_CLASSIFIER.md). Parent documents: [SPEC.md](SPEC.md) sections 5.5 and 7, and [ENRICHMENT_SPEC.md](ENRICHMENT_SPEC.md) section 7.

## 1. Overview

A post gets topics from two layers, which run in different jobs:

| Layer | Job (code) | How | Output |
|---|---|---|---|
| **Per-post classifier** | `bluesky-enrich`, continuous (`enrich_job.py`, `enrich_lib.py`) | Embeds the post and takes the nearest IPTC Media Topics concepts, unless a "no topic" anchor is nearer. | `post_enriched.topic_qcode`, `topic_label`, `topic_l1`, `topic_score`; `post_topics` rows of type `iptc` |
| **Topic map** | `bluesky-topic-map`, every 2 hours (`map_job.py`, `map_lib.py`) | Lays out a sample of embeddings in 2D, clusters the islands, names each cluster with an LLM (label plus subject), then places every other embedded post in the window through its nearest sampled neighbours. | `post_map` (one row per post: position, cluster, **`cluster_topic`**), `topic_clusters` (one row per cluster) |

Hashtags, people and companies are a third, exact-match kind of topic, written to `post_topics` by the enrichment job (ENRICHMENT_SPEC.md sections 7.4 and 7.5).

**Which topic counts:** a post's main topic is its cluster's subject, `post_map.cluster_topic`. That subject is chosen by an LLM that reads the cluster's posts, and the review (section 4) found it far more reliable than single-post matching. The IPTC concept from the classifier is the detail beneath it. The app shows them as a post's topic and subtopic, and its topic filter uses the cluster subject. A post in no cluster, or in a chatter cluster, has the topic "no clear topic".

## 2. Per-post classifier

See [POST_CLASSIFIER.md](POST_CLASSIFIER.md) for every step, setting, measurement and the roadmap. In short: Qwen3 0.6B embeddings with a topic instruction are compared with 988 IPTC concepts (levels 1 to 3) and 12 chatter anchors. The top 3 concepts at or above 0.62 are kept, unless an anchor is nearest. The concept's top-level ancestor is the subject, apart from two remapped economy branches (version 2).

## 3. Topic map

Each run of `map_job.py`:

1. **Sample.** Up to 40,000 clean, embedded posts from the last 24 hours (`MAP_MAX_POINTS`, `MAP_HOURS`), in random order, de-duplicated by `uri`.
2. **Centre languages** *(new, section 5.1)*. Compute each language's mean vector in the sample. Posts with no language tag form their own group, and languages with fewer than 30 sampled posts share one pooled mean. Subtract 0.7 of the post's language mean (`LANG_CENTRE`) and re-normalise.
3. **Reduce and lay out.** PCA to 50 dimensions, then UMAP to 2D (cosine, 15 neighbours, `min_dist` 0.05), scaled to 0 to 100. The fitted PCA is kept for step 7.
4. **Cluster.** HDBSCAN on the 2D layout (minimum size max(30, points / 500), `min_samples` 10), so clusters are the islands people see. Ids are renumbered by size, 0 being the largest; -1 is noise.
5. **Summarise.** For each cluster: the 12 posts nearest its centre; its top hashtags; and its majority IPTC subject with share, plus the most common concept *within that subject* *(changed, section 5.3)*.
6. **Name.** `databricks-claude-haiku-5-5` reads the samples and hints and returns a 2 to 5 word label plus one of the 17 IPTC subjects, or "none" for chatter. The IPTC hint is shown only when its subject covers at least 15% of the cluster *(changed, section 5.3)*. If the LLM fails, the cluster falls back to the IPTC concept (share at least 15%), then to a hashtag used by at least 10% of its posts, then to "Cluster N".
7. **Place the rest** *(new, section 5.2)*. Every other clean, embedded post in the window is read in hash partitions of about 40,000 posts (`--assign-chunk`). Each is centred with the sample's language means and projected with the sample's PCA. It then takes the label with the largest cosine-weighted vote among its 15 most similar sampled posts (`knn_assign`). It joins that cluster when the label is not noise and holds at least half the vote; otherwise it is unclustered. Its x and y are the mean position of the neighbours that voted for the winner.
8. **Write.** Overwrite `topic_clusters`, then `post_map`. The app joins the two on `computed_at`.

`post_map` columns:

| Column | Meaning |
|---|---|
| `uri` | the post |
| `x`, `y` | layout position: from UMAP for sampled posts; the neighbour mean for placed posts |
| `cluster_id` | cluster, or null for none |
| `in_sample` | true for the up to 40,000 posts the layout was built from *(new)* |
| `cluster_share` | for placed posts, the winning label's share of the neighbour vote, 0 to 1; null for sampled posts *(new)* |
| `cluster_topic` | the cluster's subject, the post's main topic; null for chatter clusters and unclustered posts *(new)* |
| `computed_at` | the map run |

`topic_clusters.n` counts sampled posts only. The app counts cluster sizes from `post_map`, so they include placed posts.

## 4. Review (2026-11-19)

Measured on the live tables over the last 24 hours, version 1 classifier and the 14:00 UTC map. Enrichment was catching up after a stall that started at 09:22 UTC, so the "24 hours" held fewer recent posts than usual.

### 4.1 Findings

| # | Finding | Evidence |
|---|---|---|
| F1 | **Language islands.** The embedding separates languages more strongly than subjects. Chatter forms one island per language, and topical non-English posts are pulled into those islands instead of their subject's island. | Share of each language's map posts in subject-less ("chatter") clusters: ja 99.0%, ko 98.9%, fr 86.0%, de 83.5%, nl 83.8%, pt 65.1%, en 21.7%. Even German posts with a confident classifier match (score at least 0.68) sat in chatter clusters 47% of the time. |
| F2 | **Most of the map has no subject.** | 46% of mapped posts were in chatter clusters, 33% in topical clusters and 21% in no cluster. The largest cluster, "Casual Japanese Personal Chatter", held 7,227 posts (18%). |
| F3 | **The app dropped unclustered posts.** `_base.sql` inner-joined `topic_clusters`. | 8,514 mapped posts (21%) never reached totals, topic counts or people and company counts. 72% of them were English, and 36% had a classifier topic. |
| F4 | **Only the sample had a cluster topic.** | The map covers 40,000 posts; 150,000 to 215,000 posts were embedded in the 24 hours (the higher figure later in the day, as the backlog was processed). |
| F5 | **Per-post topics are weak**, with an economy bias, link-only matches and a language bias. | Details in POST_CLASSIFIER.md section 4.2: ~55% precision, 17% coverage of clean posts, and ja/ko 5% coverage against 39% for en. 75% of economy matches were consumer subjects (7,513 of 10,078; "music industry" alone 3,152). |
| F6 | **The two layers disagreed.** | Per-post subject = cluster subject for only 60% of topical-cluster posts with a topic. Much of the gap was F5's economy bias. |
| F7 | **The namer got incoherent hints.** The majority concept and majority subject were counted separately, and both were shown however small their share. | Cluster 0 was told "fashion (sport)" at a 1.5% share; "Short Thank-You Replies" was told "wildfire" at 0.8%. |
| F8 | **Cluster ids don't persist between runs.** | Ids are renumbered by size every run, so trends and new-topic detection aren't possible (not addressed yet, section 7). |

### 4.2 Offline experiments

Run on 8,000 posts from the 14:00 map (embeddings, languages and classifier topics downloaded from `post_map` joined to `post_enriched`). The scripts used the same `map_lib` functions as the job, with local numpy, scikit-learn and umap-learn.

**Language centring, nearest-neighbour view** (15 nearest neighbours in PCA-50 space, the graph UMAP is built from):

| | Same-language neighbours (non-English posts) | Japanese | Neighbours sharing the post's IPTC subject |
|---|---|---|---|
| Raw embeddings | 0.655 | 0.909 | 0.703 |
| Full centring | 0.252 | 0.414 | 0.709 |

**Whole map pipeline** (PCA, UMAP, HDBSCAN with the job's settings for 8,000 points). "Subject coherence" is the share of a cluster's posts with a classifier topic that agree on its majority subject, weighted by size:

| Language mean removed | `min_samples` | Clusters | Unclustered (topical posts / others) | Japanese posts in Japanese-majority clusters | Subject coherence |
|---|---|---|---|---|---|
| 0 (old) | 10 | 40 | 9.9% (13.1% / 8.7%) | 98.7% | 0.435 |
| 0 | 5 | 55 | 13.5% | 98.7% | 0.488 |
| **0.7 (chosen)** | **10** | **51** | **22.3% (16.5% / 24.6%)** | **30.8%** | **0.665** |
| 0.7 | 5 | 69 | 20.8% (15.6% / 22.8%) | 24.7% | 0.664 |
| 1.0 | 10 | 62 | 27.7% (26.4% / 28.2%) | 11.7% | 0.644 |
| 1.0 | 5 | 75 | 24.6% | 14.1% | 0.661 |

With 0.7 centring, no Japanese, Korean, Spanish or Dutch posts sat in clusters that were at least 80% one non-English language; before, 98.8% of Japanese and 97.8% of Korean posts did. German fell from 73% to 16%. French went the other way, from 1% to 24%: one French-language cluster formed. Some language-specific subjects (national politics, local media) legitimately cluster by language.

**Placing posts outside the sample:**
- A kNN classifier on PCA-50 predicted a clustered post's cluster subject with 94.0% accuracy (5-fold CV, distance-weighted, raw embeddings), and its exact cluster with 91.1%. Logistic regression reached 86.5% and 85.9%.
- `knn_assign` with the final settings (0.7 centring, k = 15, half the vote), on 20% of posts held out from their clusters: 79.5% went to their own cluster, 19.7% were left unclustered and 0.9% went to a wrong cluster. When it places a post, it is right 99% of the time.
- Of the noise posts, 36% had at least 60% of their clustered neighbours in one cluster. Most noise really lies between islands, so it stays unclustered rather than being forced into a cluster.

## 5. Changes

### 5.1 Language centring before layout (F1, F2)

- **Change.** `map_lib.language_means()` and `centre_languages()` (with `LANG_CENTRE` = 0.7), applied in `map_job.py` before `reduce()`. `reduce()` now also returns the fitted PCA.
- **Why 0.7, not 1.0.** Full centring broke up the language islands further but pushed unclustered topical posts to 26%. At 0.7, unclustered topical posts rose only from 13% to 16.5%, and subject coherence was highest. The extra unclustered posts are mostly chatter, which has no subject anyway.
- **Trade-off.** Chatter no longer forms one tidy island per language, and the language filter is the way to see one language's posts. The unclustered share rises, from 10% to 22% on the 8,000-post test; production at 40,000 posts started from 21% and will rise too. The kNN placement and the app change (5.2) keep those posts counted.
- **Tests.** `test_language_centring_lets_subjects_cross_languages`.

### 5.2 Cluster topic for every embedded post; unclustered posts kept (F3, F4, F6)

- **Change.** `map_lib.knn_assign()`, and `map_job.assign_rest()` reading the window in hash partitions. `post_map` gains `in_sample`, `cluster_share` and `cluster_topic`, and holds every clean, embedded post of the window, not only the sample. It is written with `overwriteSchema`.
- **App change.** `_base.sql` left-joins `topic_clusters`, so unclustered posts count in totals and filter counts as "no clear topic". `clusters.sql`, `cluster_langs.sql` and `cluster_entities.sql` skip them, since they have no bubble. The `/api/clusters` response shape is unchanged.
- **Cost.** Reading embeddings for the rest of the window (150,000 to 215,000 posts a day measured, at most 432,000 under the embedding cap) adds roughly 1 to 3 minutes per run. There are no extra model calls.
- **Tests.** `test_knn_assign_places_posts_inside_their_island`.

### 5.3 Fixes to the classifier and the namer's hint (F5, F7)

- **Subject remap** (classifier version 2): the media and entertainment branch goes to arts, and the computing branch to science and technology (POST_CLASSIFIER.md section 2.7). Expected over 24 hours: economy 14,272 → 8,121 posts, arts 5,583 → 11,148.
- **Links without a scheme removed before embedding**, and the 25-character topic gate measured on the embedded text (version 2). Expected: about 570 link-only posts a day no longer embedded, and about 240 topics a day on near-empty text dropped.
- **Consistent hint for the namer:** the concept is now taken from within the majority subject, and the hint is shown only at a 15% or higher subject share (`IPTC_MIN_SHARE`, also used by the fallback name).
- **Tests.** `test_links_without_a_scheme_are_not_embedded`, `test_subject_remap_for_consumer_subjects`, `test_summarize_takes_the_concept_from_the_majority_subject`, `test_prompt_lists_topics_and_posts`.

### 5.4 Files changed

| File | Change |
|---|---|
| `bluesky/src/map_lib.py` | language centring, `reduce()` returns the PCA, `knn_assign()`, consistent IPTC pair, hint gate |
| `bluesky/src/map_job.py` | reads `lang`, centres, places the rest of the window, new `post_map` columns, `--assign-chunk` |
| `bluesky/src/enrich_lib.py` | `VERSION` 2, `BARE_URL_RE` in `clean_text()`, `SUBJECT_OVERRIDES` / `subject_qcode()` |
| `bluesky/src/enrich_job.py` | topic gate on embedded text; `topic_l1` through `subject_qcode()` |
| `bluesky/tests/test_map_lib.py`, `test_enrich_lib.py` | tests above |
| `app/backend/queries/_base.sql`, `clusters.sql`, `cluster_langs.sql`, `cluster_entities.sql` | unclustered posts kept in the base and skipped by cluster queries |
| `app/backend/services/warehouse.py` | `_map_stamp()`: the map's sample size counts `in_sample` rows (section 7 item 3) |

## 6. Deploy and verify

From `~/dbricks`. Upload only these files: other sessions have undeployed local edits elsewhere in `app/` (including `app/backend/services/warehouse.py`), so don't run `deploy.sh` or upload the whole folder.

```bash
W=/Users/alex.rovinsky@datawranglers.biz
# 1. jobs: map and enrichment code
for f in map_lib.py map_job.py enrich_lib.py enrich_job.py; do
  databricks workspace import "$W/bluesky/src/$f" --file bluesky/src/$f --format AUTO --overwrite
done
databricks jobs cancel-all-runs --job-id 391032803835690   # bluesky-enrich: the continuous trigger restarts it on the new code
databricks jobs run-now --job-id 557741817366142 --no-wait # bluesky-topic-map: or wait for the next even hour
# 2. app: the four query files, then redeploy from the workspace copy
for f in _base.sql clusters.sql cluster_langs.sql cluster_entities.sql; do
  databricks workspace import "$W/bluesky-analytics-app/backend/queries/$f" --file app/backend/queries/$f --format AUTO --overwrite
done
databricks apps deploy bluesky-analytics --source-code-path "/Workspace$W/bluesky-analytics-app"
```

The app queries work with both the old and the new `post_map`, so the order of steps 1 and 2 doesn't matter.

**Checks after the map run:**

```sql
-- composition: expect many more rows than 40,000, in_sample = 40,000, unclustered around 20 to 30%
SELECT count(*) AS posts, count_if(in_sample) AS sampled, avg(CASE WHEN cluster_id IS NULL THEN 1 ELSE 0 END) AS unclustered,
       avg(CASE WHEN cluster_topic IS NULL THEN 1 ELSE 0 END) AS no_subject
FROM datawranglers.bluesky.post_map;

-- language islands: share of each language's posts in chatter clusters (before: ja 0.99, de 0.84, fr 0.86)
SELECT e.lang, count(*) AS n,
       avg(CASE WHEN m.cluster_id IS NOT NULL AND m.cluster_topic IS NULL THEN 1 ELSE 0 END) AS in_chatter,
       avg(CASE WHEN m.cluster_topic IS NOT NULL THEN 1 ELSE 0 END) AS with_subject
FROM datawranglers.bluesky.post_map m JOIN datawranglers.bluesky.post_enriched e ON e.uri = m.uri
GROUP BY 1 ORDER BY n DESC LIMIT 10;
```

Also read the run log's "placed N of M posts outside the sample" line, and look at the new cluster labels in `topic_clusters`.

**Checks after the enrichment restart:** see POST_CLASSIFIER.md section 7, step 6.

**Rollback:**
- Set `LANG_CENTRE = 0` in `map_lib.py` and re-upload to restore the old layout.
- To undo the placement and the schema change, re-upload the previous `map_job.py`. The next run overwrites `post_map` with the old five columns; `overwriteSchema` handles it, and the app queries don't use the new columns.
- The app change is independent and can stay.

## 7. Open items and next steps

1. **Trained per-post classifier** (F5): POST_CLASSIFIER.md section 8. This is the main remaining quality gap.
2. **Stable cluster ids** (F8): match each run's clusters to the previous run's by centroid similarity (1,024-dimension means, Hungarian matching above a minimum similarity). Carry the id and label forward, and keep a `topic_history` table of (topic id, run, size, label). This enables trend lines, a "new topic" badge, and skipping the LLM call for clusters that haven't changed.
3. **Sample-count wording in the app:** done in code. `WarehouseSource._map_stamp()` in `warehouse.py` counts `in_sample` rows for "map built from a sample of N posts", and falls back to `count(*)` while `post_map` still has the old columns. The same file holds another session's undeployed people and company thumbnail changes, so it ships when those are approved. Until then the app shows the total placed posts as the sample size, which is cosmetic.
4. **Ask tab:** `services/chatdata.py` exposes `cluster_id` and the per-post `topic_l1`. Exposing `post_map.cluster_topic` would give chat answers the main topic.
5. **Large clusters:** split clusters over about 5% of the map into named sub-clusters (re-run HDBSCAN within the cluster, or use leaf selection). Draw the 12 naming samples from several sub-regions, not only the centre.
6. **Clustering dimension:** clustering on a 5 to 10 dimension UMAP and displaying a separate 2D layout (the BERTopic approach) usually gives cleaner clusters. The cost is that clusters no longer match the islands exactly. Worth a measured trial.
7. **`min_samples` 5:** gave about 1.5 points less noise at the same coherence in the offline test. Try it if the production unclustered share is high.
8. **Duplicate `uri` rows in `post_enriched`:** a few posts appear twice, one row without an embedding (4 of 8,000 sampled; the app's base counted 40,005 rows for 40,000 map posts). The map job now de-duplicates its own input. The app's join still counts duplicates.
9. **Diagrams:** the Architecture diagram (`app/frontend/public/diagrams/pipelines.drawio`, topic map page) and the Data model tab describe `post_map` with its old columns. Update them in draw.io.
