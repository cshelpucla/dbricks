# Per-post topic classifier

Status as of 2026-11-19: **version 2 is implemented and tested in code but not yet deployed.** The enrichment job (`bluesky-enrich`) still runs version 1. Section 6 lists what version 2 changes, and [TOPIC_IDENTIFICATION.md](TOPIC_IDENTIFICATION.md) section 6 gives the deploy steps.

The classifier gives each post an IPTC Media Topics subject on its own, without looking at other posts. It is one of two topic layers. The other is the topic map, which clusters posts and names each cluster; see [TOPIC_IDENTIFICATION.md](TOPIC_IDENTIFICATION.md) for how the two fit together. Code: `bluesky/src/enrich_lib.py` (logic) and `bluesky/src/enrich_job.py` (`Enricher.load_taxonomy` and the "topics" block of `Enricher.process`). Tests: `bluesky/tests/test_enrich_lib.py`. Contract: [ENRICHMENT_SPEC.md](ENRICHMENT_SPEC.md) section 7.

## 1. What it produces

| Output | Content |
|---|---|
| `post_enriched.topic_qcode`, `topic_label`, `topic_score` | The best-matching IPTC concept, its English label and its cosine similarity. Null when the post got no topic. |
| `post_enriched.topic_l1` | The subject (top-level IPTC concept) reported for that concept. This is normally the root of the concept's path; section 2.7 lists the exceptions. |
| `post_topics` rows with `topic_type = 'iptc'` | Up to three concepts per post, `rank` 1 to 3, each with its `score`. |

The same job also writes `post_topics` rows of type `hashtag`, `person` and `company`. Those come from exact matching, not from this classifier, and are described in ENRICHMENT_SPEC.md sections 7.4 and 7.5.

## 2. How a post is classified

### 2.1 Which posts are considered

1. **Clean posts only.** Posts with `is_filtered` set (bots and spam, ENRICHMENT_SPEC.md section 5) are skipped.
2. **Embeddable text.** `embed_text()` must return a string (section 2.2), which means at least 12 characters after cleaning.
3. **Sampling cap.** At most `EMBED_MAX_PER_WINDOW` posts (default 600) are embedded per 120-second window. When more are eligible, a uniform random sample is taken, seeded by the window start. On 2026-11-19, 62% of clean posts were embedded. Posts that are not embedded never get an IPTC topic.

### 2.2 The text that is embedded

`enrich_lib.clean_text()` and `embed_text()`:

- Remove links that start with `http://`, `https://` or `www.` (`URL_RE`).
- **Version 2:** also remove links without a scheme, as clients display them in the post text, such as `en.wikipedia.org/wiki/Roger_C...` or `youtu.be/abc`. `BARE_URL_RE` matches a dotted host followed by a path. Phrases such as "U.S./China", "24/7" or "and/or" are not links and are left alone. `URL_RE` itself is unchanged, so the bot rules and geo TLD signals behave as before.
- Collapse whitespace.
- If the cleaned text is under 40 characters and the post has a link card, append `" - " + card title`. A link post's subject usually lives in its card.
- Truncate to 500 characters. Return nothing if the result is under 12 characters.

### 2.3 The embedding call

- Endpoint `databricks-qwen3-embedding-0-6b` (`EMBED_ENDPOINT`): 1,024 dimensions, multilingual.
- The input is prefixed with `EMBED_INSTRUCTION`: `Instruct: Identify which news topic category this social media post is about\nQuery: `. Qwen3 embeddings are asymmetric. Queries carry an instruction and documents (the concept texts) do not. On 2,000 real posts the instruction measurably improved matching. Because the stored vector is topic-oriented, it also suits the topic map.
- Requests carry 32 inputs each and run on 6 threads, with 3 attempts per batch and exponential backoff. If more than half of a window's batches fail, the window fails and is retried, so a broken endpoint cannot silently produce posts with no embedding.
- Vectors are L2-normalised and stored in `post_enriched.embedding`.

### 2.4 The taxonomy

- Source: IPTC Media Topics JSON (`https://cv.iptc.org/newscodes/mediatopic/?lang=x-all&format=json`, CC BY 4.0). `parse_iptc()` keeps active concepts, resolves each concept's first active parent, and builds `level`, `path` and `path_codes` (root first). The release used has 1,059 active concepts: 17 / 120 / 504 / 364 / 49 / 5 at levels 0 to 5.
- Each concept is embedded once as `"<label>: <definition>"` (en-GB, no instruction) and stored with its row in `topic_taxonomy`. That cost 23,792 tokens. The job reuses the table on later starts. To reload the taxonomy, empty the table and restart the job.

### 2.5 The matching matrix

Built in `Enricher.load_taxonomy` at job start:

- **Concepts:** all concepts at `MATCH_LEVELS` = levels 1 to 3, which is 988 rows. Level 0 is too broad to be a useful match, and levels 4 and 5 are too sparse.
- **"No topic" anchors:** the 12 `ANCHORS` phrases describing chatter (greetings, small talk, short replies, emoji, jokes, follower questions, self-promotion, personal updates, venting, thanks, vague opinions, status updates). They are embedded at each job start and appended as the last 12 rows.

### 2.6 Scoring

`assign_topics(E, T, top_k=3, min_score=TOPIC_MIN_SCORE, n_anchors=12)` takes the dot product of each post vector with every matrix row (cosine similarity, since both sides are normalised). Then:

1. If the single best row is an anchor, the post is chatter and gets no topic.
2. Otherwise the 3 best concepts are kept if they score at least `TOPIC_MIN_SCORE` (0.62). Rank 1 becomes `topic_qcode`.
3. In `enrich_job.py`, a post whose embedded text is under `TOPIC_MIN_CHARS` (25) gets no topic, even when it matched. **Version 2** measures the embedded text, which has links removed and the card title added. Version 1 measured the raw text, so a bare link counted towards the 25 characters.

### 2.7 The reported subject (`topic_l1`)

`topic_l1` is the label of `subject_qcode(path_codes)`, which is normally the concept's root. **Version 2** adds `SUBJECT_OVERRIDES` because IPTC files several consumer subjects under *economy, business and finance > products and services*. Under version 1 a post about an album or an app was reported as economy:

| Branch (with everything below it) | Reported under | Why |
|---|---|---|
| media and entertainment industry (`medtop:20000304`): music industry, film industry, books and publishing, streaming service, television industry, news industry, podcast, radio industry, satellite and cable service | arts, culture, entertainment and media (`medtop:01000000`) | Posts are about the music or film, not the business. "Music industry" alone matched over 3,000 posts a day, mostly now-playing bots and album talk. |
| computing and information technology (`medtop:20000225`): software and applications, hardware, telecommunication service, semiconductors, wireless, satellite technology | science and technology (`medtop:13000000`) | Posts about apps, security patches and devices. The cluster namer already filed them under science and technology. |
| *exceptions:* advertising (`medtop:20000305`), public relations (`medtop:20000312`) | stay under economy | They are business functions. |

Other economy branches, such as consumer goods, transport and energy, keep their root. Their posts are split between business and other subjects, and the cluster namer handles them case by case.

The concept itself (`topic_qcode`, `topic_label`, `post_topics.topic_id`) is never changed. Only the subject it is reported under changes.

## 3. Configuration

| Setting | Default | Where |
|---|---|---|
| `EMBED_ENDPOINT` | `databricks-qwen3-embedding-0-6b` | `bluesky/config.env` (job parameter `--embed-endpoint`) |
| `EMBED_MAX_PER_WINDOW` | 600 | `config.env` (`--embed-max-per-window`) |
| `TOPIC_MIN_SCORE` | 0.62 | `config.env` (`--topic-min-score`) |
| `TOPIC_MIN_CHARS`, `MATCH_LEVELS`, `ANCHORS`, `EMBED_INSTRUCTION`, `SUBJECT_OVERRIDES`, `SUBJECT_KEEP`, `BARE_URL_RE` | see section 2 | `enrich_lib.py` constants |

`config.env` changes take effect when `setup.sh` re-creates the job. Constant changes take effect when the source file is uploaded and the job restarted (section 7).

## 4. Measured quality

### 4.1 Calibration (2026-11-19, 2,000 real posts, instruction embeddings)

| `TOPIC_MIN_SCORE` | Share of embedded posts given a topic |
|---|---|
| 0.58 | 53% |
| **0.62** (chosen) | 31% |
| 0.66 | 12% |

Without anchors, nearest-concept matching gave 93% of posts a topic at 0.35, and most of the extra ones were wrong (chatter forced into "hobby" or "human mishap"). Subtracting each concept's average score, to damp concepts that attract everything, was tried and rejected because it swapped generic matches for obscure ones.

### 4.2 Production review (2026-11-19, last 24 hours, version 1)

- **Coverage:** 17% of clean posts and 28% of embedded posts got a topic.
- **Precision:** a hand check of 45 random assignments found about 25 sensible (~55%). This matches the earlier check of 22 posts (55 to 65%).
- **Narrow score band:** accepted scores sit between 0.62 and 0.75, with a median around 0.65. Right and wrong matches are hard to separate by score alone.
- **Strong language bias:** the concept texts are English, so non-English posts score lower against them.

| Language | Embedded posts | Given a topic | Median accepted score |
|---|---|---|---|
| en | 65,186 | 39.3% | 0.654 |
| ja | 23,927 | 5.1% | 0.637 |
| (no tag) | 20,877 | 34.5% | 0.651 |
| de | 9,229 | 16.9% | 0.646 |
| es | 7,044 | 22.2% | 0.645 |
| fr | 5,330 | 20.6% | 0.642 |
| ko | 3,323 | 5.1% | 0.634 |

Failure modes seen in the hand check:

| Failure | Share of sample | Example | Addressed by |
|---|---|---|---|
| Chatter passes the anchors | ~22% | "Cool mini fridge! Where's the beer?" → beverage and grocery; "I only needed one, the fascist owner." → hijacking | not fixed: needs the trained classifier (section 8) |
| Link-only text | ~7% | "en.wikipedia.org/wiki/Roger_C..." → competitive sport | version 2 (section 2.2) |
| Subject wrong although the concept is right | ~7% | album post → music industry → *economy* | version 2 (section 2.7) |
| Concept plausible but too specific | several | F1 Lego minifigures → track cycling | not fixed |

Estimated version 2 effect on the same 24 hours, recomputed from stored concepts:
- **Subject remap:** economy falls from 14,272 to 8,121 posts, and arts, culture, entertainment and media rises from 5,583 to 11,148.
- **Link fix:** 573 embedded posts a day had nothing left after link removal and no card; they will no longer be embedded. 237 accepted topics sat on text under 25 characters once links are removed; they will be dropped.

## 5. Relationship to the topic map

The classifier's output feeds the map in one place: each cluster's majority subject and its most common concept within that subject are shown to the cluster namer as a hint. Since 2026-11-19 the hint is shown only when that subject covers at least 15% of the cluster (`map_lib.IPTC_MIN_SHARE`).

Under version 1, the per-post subject agreed with its cluster's subject 60% of the time. **Since 2026-11-19 the cluster subject (`post_map.cluster_topic`) is the main topic of a post, and this classifier's concept is the detail beneath it.** The app shows it as the post's subtopic.

## 6. Versions

`enrich_lib.VERSION` is stored on every `post_enriched` row as `enrich_version`.

| Version | Change |
|---|---|
| 1 | First deployment (2026-11-19). |
| 2 | Links without a scheme removed before embedding; the 25-character topic gate measured on the embedded text; `SUBJECT_OVERRIDES` for the media and computing branches. Rows written before the upgrade keep version 1 values, since nothing recomputes written rows. To reprocess a time range, delete its rows from `post_enriched` and its satellite tables; the job re-reads that range from bronze (ENRICHMENT_SPEC.md section 12). |

## 7. Changing the classifier safely

1. Change `enrich_lib.py`. Keep the logic there, free of Spark, so it stays testable.
2. Add or extend tests in `bluesky/tests/test_enrich_lib.py`, then run `python -m pytest bluesky/tests`.
3. If outputs change, bump `VERSION`.
4. For threshold or anchor changes, measure on real posts first. Pull a sample of embeddings and texts from `post_enriched`, re-score offline against `topic_taxonomy.embedding`, and hand-check at least 50 posts at the candidate setting.
5. Deploy: upload `enrich_lib.py` and `enrich_job.py` to the workspace folder (`databricks workspace import ... --format AUTO --overwrite`), then `databricks jobs cancel-all-runs --job-id <bluesky-enrich id>`. The continuous trigger starts a new run on the new code within about 30 seconds, and the job resumes from its cursor. There is no need to rerun `setup.sh`, which would also restart ingest and silver.
6. After the restart, check:
   - `SELECT enrich_version, count(*) FROM post_enriched WHERE event_time > now() - INTERVAL 1 HOUR GROUP BY 1`
   - the `topic_l1` distribution of the new rows;
   - duplicate `post_topics` rows for the last hour. A cancel that lands between the `post_topics` and `post_enriched` writes makes the new run redo that window.

## 8. Roadmap: a trained classifier

Nearest-concept matching is zero-shot. It compares a post with short English concept descriptions, and three of its weaknesses can't be fixed by tuning: chatter that gets past the anchors, the narrow score band, and the language bias. The proposed replacement is a classifier trained on the embeddings the job already stores. It is not built yet.

**Design**

1. **Labels.** Ask `databricks-claude-haiku-5-5` to label about 5,000 posts once. Stratify by language (at least 300 each for ja, de, fr, es, ko and pt) and by version 1 score band. The label is one of the 17 IPTC subjects or "none" (chatter), optionally with a level-1 concept for the large subjects. Send about 20 posts per request to keep the prompt overhead small. This is roughly 250 requests and 0.5M tokens, one-off.
2. **Model.** A multinomial logistic regression (a linear probe) on the stored 1,024-dimension embeddings, with class weights for rare subjects. Use 5-fold cross-validation for the regularisation strength. On a related task, a linear model on these embeddings predicted a post's cluster subject with 87% accuracy (kNN 94%), so the embeddings carry enough subject signal.
3. **Decision rule.** Pick the class with the highest probability. Assign a subject only when its probability exceeds a per-class threshold chosen for about 85% precision on held-out labels; otherwise give no topic. This replaces both the 0.62 cut-off and the anchors.
4. **Evaluation.** Hold out 20% of the labelled posts, plus a hand check of 200, and report precision, recall and coverage per language. The bar to ship: at least 75% precision overall and non-English coverage within 10 points of English.
5. **Serving.** Store the weights (18 × 1,024 floats) in a small table or Volume file. The enrichment job loads it at start and applies it as one matrix multiply per window, with no per-post model cost. Keep the IPTC concept match for the detail (`topic_label`) and use the classifier for `topic_l1` and for the yes/no topic decision. This becomes `VERSION 3`.
6. **Refresh.** Relabel a fresh sample monthly, or when the language mix shifts, and compare against the held-out set before swapping the weights.

**Alternatives considered**

- **LLM re-ranking per post** (the first planned improvement in ENRICHMENT_SPEC.md section 7.3): send the top 5 concepts to Haiku for every post that passes the gate. That is about 60,000 posts a day, roughly 15M tokens a day, indefinitely, and it adds a model call to the enrichment loop's latency.
- **Translating posts before embedding:** it doubles the model calls per post, and the language-bias part of the gap can be closed more cheaply by training on multilingual labels.
- **Per-language thresholds on the current scores:** cheap, but they only rescale the bias; they can't make a Japanese post's nearest English concept the right one.
