# Douyin Collection Standard

This standard defines how Douyin data should be collected, classified, cleaned, stored, and judged for quality in this project.

It is designed for three data sources:

- **Official API**: user-authorized data from Douyin Open Platform.
- **Browser Visible**: read-only data visible in the user's logged-in browser.
- **Browser Media Asset**: user-approved, read-only collection of playable video/cover resources visible to the logged-in browser for local research.
- **Manual Import**: CSV/XLSX/JSON data exported or manually prepared by the user.

The project should prefer official API data when available, then use browser-visible data only for discovery, public context, and competitive observation.

## 1. Compliance Boundary

Allowed:

- Read data explicitly visible to the user in the browser.
- Use user-authorized official APIs for the user's own/authorized account data.
- Store raw snapshots, cleaned records, quality reports, and analysis outputs locally.
- Capture account/profile/video/work metadata that is visibly shown on the page.
- Download visible video, cover, frame, and audio-derived assets for explicitly approved local research samples, with account-scoped local storage and quality reports.

Not allowed:

- Do not read cookies, LocalStorage, browser session stores, passwords, tokens, or private browser profile files.
- Do not bypass login, CAPTCHA, anti-bot, paywall, age-gate, or platform safety interstitials.
- Do not perform bulk scraping that stresses the platform.
- Do not click like, follow, unfollow, comment, share, delete, publish, or any state-changing control during collection.
- Do not treat browser-visible counts as official truth when API metrics are available.
- Do not collect media assets without a bounded sample plan, per-account output separation, and a success/failure report.

## 2. Source Priority

### S1 Official API

Use for authorized, metric-grade data:

- User profile/public info.
- Video list and video basic info.
- Video interaction metrics such as play, like, comment, share, average play duration.
- User interaction aggregates such as fans, likes, comments, shares, homepage visits when authorized.

Data quality target: `A` or `B`.

### S2 Browser Visible

Use for visible discovery and competitor context:

- Follow/feed visible account profile.
- Public profile URL and visible follower/like counts.
- Visible work cards.
- Visible tags, related searches, titles, topic anchors.
- Visible unlabeled interaction numbers, marked as unlabeled unless mapped by reliable UI container.

Data quality target: `C` for clean samples; `D` when only one account or high duplicate rate.

Filtering rule:

- Treat recommended, hot, search, or same-category program cards as possible mixed-in DOM content, not as evidence that the target account grid has reached the end.
- Keep a work only when it can be tied back to the target account by a reliable signal such as API author identity, profile-scoped endpoint, target account URL context, stable account key, or target-specific title/tag evidence.
- Do not use mixed-in recommendation text as a stopping condition. Use explicit pagination signals (`has_more=0`), repeated API cursors, stable no-new-work windows, or user-visible end states instead.

### S2M Browser Media Asset

Use for local research and model-feature experiments after the user explicitly approves a bounded sample plan:

- Video files exposed as playable media resources in the logged-in browser.
- Cover images exposed by visible page metadata or image resources.
- Extracted frames created locally from downloaded video.
- Extracted audio created locally from downloaded video.

Required boundaries:

- Only run against planned samples with `account_id`, `aweme_id`, `source_url`, `stage`, and `run_id`.
- Save assets under `data/douyin_media_assets/<account_id>/<run_id>/`.
- Generate a JSON/Markdown report for every run.
- Stop the run when the page enters login, verification, non-target recommendation, or other blocked states.
- Do not read cookies, LocalStorage, SessionStorage, browser profile files, tokens, or passwords.

Data quality target: `B` for smoke/pilot runs when more than 80% of planned samples produce valid `ffprobe` metadata and at least one visual asset. `C` or lower when media success rate falls below the threshold.

### S3 Manual Import

Use for data supplied by the user:

- CSV/XLSX exported from a platform dashboard.
- Manually curated competitor account lists.
- Experiment notes and annotations.

Data quality target depends on source and schema completeness.

## 3. Entity Model

### Collection Run

One browser/API/manual collection attempt.

Required fields:

- `run_id`
- `source_method`: `official_api | browser_visible | manual_import`
- `started_at`
- `finished_at`
- `operator`
- `source_url`
- `status`: `completed | partial | blocked | failed`
- `quality_grade`
- `quality_score`
- `notes`

### Account

One Douyin account/profile.

Required fields:

- `platform`: `douyin`
- `account_key`: stable internal key, preferably profile URL or open_id when authorized.
- `nickname`
- `profile_url`
- `source_method`
- `observed_at`

Optional fields:

- `avatar_url`
- `bio`
- `douyin_id_visible`
- `followers_visible`
- `following_visible`
- `likes_received_visible`
- `account_type`: `own | followed | competitor | source_program | artist | media | unknown`
- `content_domain`: `music_variety | entertainment | education | ecommerce | local_life | other | unknown`
- `confidence`

### Follow Relationship

One visible relationship from the user's account to another account.

Required fields:

- `observer_account`
- `target_account_key`
- `target_profile_url`
- `following_state_visible`
- `observed_at`
- `source_url`

### Work / Video

One visible published work or API video.

Required fields:

- `work_key`
- `account_key`
- `platform`: `douyin`
- `source_method`
- `observed_at`
- `source_url`

At least one of:

- `aweme_id`
- `video_url`
- `normalized_title + account_key`

Content fields:

- `title`
- `normalized_title`
- `description`
- `tags`
- `related_searches`
- `cover_url`
- `publish_time_visible`
- `duration_visible`
- `media_type`: `video | image_text | live_replay | unknown`
- `is_pinned_visible`

Classification fields:

- `content_category`: `music_variety | performance_clip | interview | reaction | behind_the_scenes | news | commentary | other | unknown`
- `program_name`
- `artist_names`
- `song_title`
- `hook_type`: `high_note | emotional_story | judge_comment | contrast | conflict | chorus | funny | visual | unknown`
- `slice_structure`: `setup_to_payoff | chorus_first | reaction_first | quote_first | pure_highlight | unknown`
- `commercial_intent`: `none | soft_promo | hard_promo | ecommerce | unknown`
- `rights_risk`: `low | medium | high | unknown`

Metric fields:

- `visible_like_count`
- `visible_comment_count`
- `visible_share_count`
- `visible_favorite_count`
- `visible_play_count`
- `visible_count_unlabeled`
- `metric_window`: `realtime_visible | 6h | 24h | 7d | 30d | lifetime | unknown`
- `metric_source`: `official_api | browser_visible_labeled | browser_visible_unlabeled | manual_import`

### Topic / Tag

Normalized hashtag or related search.

Required fields:

- `tag_text`
- `tag_type`: `hashtag | related_search | music | poi | unknown`
- `first_observed_at`
- `source_work_key`
- `source_account_key`

### Media Asset

One locally stored media artifact derived from a planned work/video sample.

Required fields:

- `asset_id`
- `account_key`
- `run_id`
- `aweme_id`
- `source_url`
- `asset_type`: `video | cover | frame | audio | transcript | ocr | feature`
- `local_path`
- `created_at`
- `source_method`: `browser_media_asset`
- `status`: `planned | success | partial | failed`

Recommended fields:

- `file_size_bytes`
- `sha256`
- `duration_seconds`
- `width`
- `height`
- `codec_name`
- `frame_time_seconds`
- `errors`

## 4. Classification Taxonomy

### Account Type

- `own`: user's own account.
- `followed`: account visible in the user's follow/feed surface.
- `competitor`: account explicitly marked for benchmarking.
- `source_program`: show/program official account.
- `artist`: singer/performer account.
- `media`: publisher/media account.
- `unknown`: not enough evidence.

### Work Category

- `music_variety`: music variety show clip.
- `performance_clip`: stage performance or singing clip.
- `judge_comment`: mentor/judge comment, scoring, selection.
- `reaction`: audience/mentor/artist reaction.
- `behind_the_scenes`: backstage, rehearsal, interview.
- `compilation`: montage or multi-scene mix.
- `commentary`: commentary/explainer.
- `promo`: explicit promotion.
- `other`
- `unknown`

### Hook Type

- `high_note`
- `chorus`
- `emotional_story`
- `judge_comment`
- `surprise`
- `conflict`
- `nostalgia`
- `celebrity_pairing`
- `funny`
- `visual`
- `unknown`

### Confidence

- `high`: stable ID, labeled metrics, low duplicate risk.
- `medium`: stable account and title, inferred video ID or partially labeled metrics.
- `low`: no stable video ID, unlabeled metrics, repeated feed-window data.

## 5. Required Quality Gates

### Batch-Level Quality

Grade `A`:

- Official API or verified manual import.
- Stable IDs for accounts and videos.
- Duplicate ratio under `5%`.
- Metrics labeled and time-windowed.
- More than 95% required fields present.

Grade `B`:

- Browser-visible or API mixed data.
- Stable account IDs/profile URLs.
- Most works have video IDs or strong dedupe keys.
- Duplicate ratio under `20%`.
- Metrics mostly labeled or explicitly marked as unlabeled.

Grade `C`:

- Browser-visible sample.
- More than one account represented.
- Duplicate ratio under `50%`.
- Counts parse successfully.
- Some video IDs may be inferred.

Grade `D`:

- Single-account sample when the target is followed-account coverage.
- Duplicate ratio over `50%`.
- Current video IDs are inferred from noisy page context.
- Metrics are unlabeled or mixed.

### Record-Level Flags

Use these flags instead of hiding ambiguity:

- `multiple_raw_aweme_ids`
- `current_aweme_inferred_from_link`
- `count_recovered_from_prefix`
- `html_unescaped`
- `deduped_across_snapshots`
- `same_title_multiple_visible_counts`
- `missing_visible_count`
- `browser_visible_unlabeled_metrics`
- `needs_manual_review`

## 6. Deduplication Rules

### Account Key

Priority:

1. Official `open_id` or authorized account ID.
2. Douyin profile URL.
3. Visible Douyin ID.
4. Normalized nickname plus profile context.

### Work Key

Priority:

1. Official `video_id` / `aweme_id`.
2. Canonical video URL.
3. `account_key + normalized_title + primary_tags`.
4. `account_key + normalized_title`.

Do not use visible counts in the work key. Counts change over time.

### Count Selection

When duplicate work cards expose several counts:

- Preserve `all_visible_counts`.
- Use `best_visible_count` as the maximum parsed count for visible popularity ranking.
- Store `snapshot_count` and `source_files`.
- Add `same_title_multiple_visible_counts` if counts differ.

### Formal Historical Sample Store

The formal learning table `historical_capture_samples` uses a stricter deduplication rule than raw capture files:

- If `platform_item_id` exists, dedupe by `account_id + platform + platform_item_id`.
- If `platform_item_id` is missing, dedupe by a stable normalized title key.
- When the same video appears in several capture batches, keep the newer dataset date first, then the higher view count.
- Keep `dataset_id` and `program_key` on the retained row for source isolation.
- Do not create physical `dataset_id=all` rows. `all` is an aggregate view only.

For reporting, keep these counts separate:

- `raw_rows`: rows read from source files.
- source `sample_count`: valid rows in source files.
- `unique_count`: source rows deduped by work key.
- historical `sample_count`: rows physically stored after global historical dedupe.

## 7. Browser Visible Capture Standard

Each raw snapshot should contain:

- `observed_at`
- `page.title`
- `page.url`
- `page.source`
- `account`
- `current_video`
- `visible_works`
- `raw_stats`

Minimum browser-visible extraction per snapshot:

- Account nickname and profile URL.
- Account visible follower/like counts when present.
- Current video inferred ID.
- Current visible tags and related searches.
- Work card title text.
- Work card visible count.
- Work card tags.
- Source file name and timestamp.

Capture cadence:

- Use low-frequency manual or assisted capture.
- After each scroll/navigation, wait for visible page state to settle.
- Stop when quality report shows high duplicate ratio or repeated same account.
- For followed-account coverage, switch to account-library mode instead of deep-scrolling the same feed item.

## 8. Two-Stage Complete Collection Plan

### Stage 1: Account Library

Goal: build a list of followed/competitor accounts.

Collect:

- `account_key`
- `nickname`
- `profile_url`
- `following_state_visible`
- `followers_visible`
- `likes_received_visible`
- `account_type`
- `content_domain`
- `first_observed_at`
- `last_observed_at`

Quality gate:

- At least 20 accounts or a user-defined target list.
- Profile URL present for 95% of accounts.
- No duplicate profile URLs.

### Stage 2: Account Work Collection

Goal: collect recent visible works per target account.

Collect per account:

- Top/recent visible work cards.
- Titles, tags, counts, pinned state, publish time if visible.
- Video URL or aweme ID if visible.
- Screenshot/source snapshot ID if needed for audit.

Quality gate:

- At least 10 works per account when available.
- Duplicate ratio under 20%.
- Counts parsed for 90% of visible work cards.

### Stage 3: Metrics Enrichment

Goal: enrich records with official or imported metrics.

Use:

- Official API for authorized own-account data.
- Manual import for dashboard exports.
- Browser-visible counts only as directional competitor context.

### Stage 4: Classification

Goal: convert raw records into analysis-ready dimensions.

Classify:

- account type
- content domain
- work category
- hook type
- artist/song/program
- rights risk
- analysis confidence

## 9. Output Files

Account-partitioned storage is mandatory for multi-account collection. A combined workbook or dashboard summary may be generated, but it must be treated as an index/report only, not as the sole source of truth.

Per-account raw and clean data:

- `data/douyin_capture/<account_slug>/raw_<run_id>/...`
- `data/douyin_capture/<account_slug>/clean_<run_id>/douyin_visible_records_clean_latest.json`
- `data/douyin_capture/<account_slug>/clean_<run_id>/douyin_visible_records_clean_latest.csv`
- `data/douyin_capture/<account_slug>/clean_<run_id>/douyin_visible_records_clean_latest_utf8_bom.csv`
- `data/douyin_capture/<account_slug>/clean_<run_id>/douyin_visible_works_dedup_latest.json`
- `data/douyin_capture/<account_slug>/clean_<run_id>/douyin_visible_works_dedup_latest.csv`
- `data/douyin_capture/<account_slug>/clean_<run_id>/douyin_collection_quality_latest.json`

Per-account Excel outputs:

- `outputs/<collection_run>/accounts/<account_slug>/<account_slug>_douyin_collection_latest.xlsx`

Combined outputs:

- `outputs/<collection_run>/three_accounts_douyin_collection_latest.xlsx`
- Combined outputs should read from the per-account clean directories and preserve account identity on every row.
- When the collection grows, shard by account first, then by run date or batch number inside that account directory.

Raw immutable:

- `data/douyin_capture/douyin_follow_visible_<timestamp>.json`

Clean all records:

- `data/douyin_capture/douyin_visible_records_clean_latest.json`
- `data/douyin_capture/douyin_visible_records_clean_latest.csv`
- `data/douyin_capture/douyin_visible_records_clean_latest_utf8_bom.csv`

Analysis-ready works:

- `data/douyin_capture/douyin_visible_works_dedup_latest.json`
- `data/douyin_capture/douyin_visible_works_dedup_latest.csv`

Current video sample:

- `data/douyin_capture/douyin_current_videos_clean_latest.json`
- `data/douyin_capture/douyin_current_videos_clean_latest.csv`

Quality:

- `data/douyin_capture/douyin_collection_quality_latest.json`

## 10. Minimum Dashboard Views

The product dashboard should show:

- Collection health: grade, duplicate ratio, account coverage, last run.
- Account library: account type, domain, followers, likes, last collected.
- Works table: deduped works, tags, counts, hook type, category, confidence.
- Quality issues: rows needing manual review.
- Trend and benchmark views only after enough account and time coverage exists.
