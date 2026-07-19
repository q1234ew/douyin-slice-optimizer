# Douyin Visible Collection Flow

This workflow is for user-approved, read-only collection of visible Douyin web data. It does not inspect cookies, local storage, passwords, or private browser session stores.

For the complete schema, taxonomy, quality gates, and target data model, see [Douyin Collection Standard](../guides/douyin-collection-standard.md).

## Flow

1. Capture visible page snapshots from the logged-in browser.
   - Save raw snapshots as `data/douyin_capture/douyin_follow_visible_<timestamp>.json`.
   - Keep raw snapshots immutable so parsing rules can be improved later.

2. Normalize and clean snapshots.
   - Run:
     ```bash
     PYTHONPATH=src python3 -m dso.cli douyin-visible-clean \
       --input-dir data/douyin_capture \
       --output-dir data/douyin_capture
     ```

3. Use the cleaned outputs.
   - `douyin_visible_records_clean_latest.json/csv`: all cleaned current-video and work-card records.
   - `douyin_visible_works_dedup_latest.json/csv`: deduped work cards for analysis.
   - `douyin_current_videos_clean_latest.json/csv`: per-snapshot current-video records with inferred current video IDs.
   - `douyin_collection_quality_latest.json`: coverage, duplicate rate, quality flags, and next-step recommendations.

4. For multi-account runs, save each account separately before creating any combined workbook.
   - Raw data: `data/douyin_capture/<account_slug>/raw_<run_id>/...`
   - Clean data: `data/douyin_capture/<account_slug>/clean_<run_id>/...`
   - Account workbook: `outputs/<collection_run>/accounts/<account_slug>/<account_slug>_douyin_collection_latest.xlsx`
   - Combined workbook: only a summary/index that reads from account-level outputs.

## Quality Gates

- `account_count` should be greater than 1 before treating a batch as a followed-account sample.
- `estimated_duplicate_ratio` should be below `0.2` before using raw work-card counts for analysis.
- Prefer `douyin_visible_works_dedup_latest.*` for ranking and summaries.
- If `multiple_raw_aweme_ids` appears, trust `current_aweme_id` over raw `aweme_ids_visible`; the cleaner infers it from visible hashtag links.
- If `count_recovered_from_prefix` appears, the visible count was recovered from prefixes such as `共创 2.4万` or `热点 6.7万`.
- Recommended, hot, search, or same-category program cards can appear in the page DOM while collecting a target account. Filter them by account/profile/API context and target-specific title/tag evidence; do not treat their appearance as proof that the target account grid has reached the end.

## Recommended Next Capture Strategy

The current `douyin.com/follow` view behaves like a feed plus a right-side works panel. Scrolling can repeatedly expose the same account, so the next collection pass should be two-stage:

1. Build a followed-account library from visible account/profile URLs.
2. Visit each account page at low frequency and collect visible work cards per account.

This avoids treating feed-window repeats as new works and gives the dashboard a stable competitor-account dataset.

Execution order:

1. Capture 20+ followed/competitor accounts into an account library.
2. Collect recent/top visible works per account.
3. Save raw captures under the matching account directory.
4. Run `douyin-visible-clean` per account.
5. Review each account's `douyin_collection_quality_latest.json`.
6. Use each account's `douyin_visible_works_dedup_latest.*` for analysis and dashboard views.
7. Generate combined summaries only after account-level files have been written and verified.
