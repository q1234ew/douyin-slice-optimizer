#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ACCOUNT_LIBRARY = ROOT / "data/douyin_capture/douyin_account_library_latest.json"
DEFAULT_OUTPUT_ROOT = ROOT / "data/douyin_capture"
DEFAULT_REPORT_DIR = ROOT / "outputs/douyin_followed_recollect_20260630"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dso.collectors.douyin_accounts import clean_account_api_works  # noqa: E402
from dso.db.session import init_db  # noqa: E402
from dso.learning.historical_samples import historical_sample_summary, import_douyin_history  # noqa: E402


PAGE_META_JS = r"""
(() => JSON.stringify({
  title: document.title || '',
  url: location.href || ''
}))()
""".strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect Douyin account works through the profile post API in the logged-in Chrome page context."
    )
    parser.add_argument("--accounts", nargs="*", help="Account keys to collect. Defaults to all accounts in the library.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum selected accounts. 0 means no limit.")
    parser.add_argument("--account-library", default=str(ACCOUNT_LIBRARY))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_appleevents_api"))
    parser.add_argument("--dataset-suffix", default=None, help="Dataset suffix. Defaults to run date plus appleevents_api.")
    parser.add_argument("--target-per-account", type=int, default=500)
    parser.add_argument("--page-size", type=int, default=35)
    parser.add_argument("--max-pages", type=int, default=18)
    parser.add_argument("--stall-pages", type=int, default=2)
    parser.add_argument("--page-delay", type=float, default=7.0)
    parser.add_argument("--request-delay", type=float, default=0.7)
    parser.add_argument("--request-timeout", type=int, default=60)
    parser.add_argument("--skip-complete", action="store_true", help="Skip accounts already at their target sample count.")
    parser.add_argument("--no-clean", action="store_true", help="Only save raw API outputs.")
    parser.add_argument("--no-import", action="store_true", help="Clean but do not import into historical samples.")
    parser.add_argument("--force-import", action="store_true", help="Force refresh the target dataset on import.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    library = _load_accounts(Path(args.account_library))
    accounts = _select_accounts(library, args.accounts, args.limit)
    plan = [_plan_account(account, args.target_per_account) for account in accounts]
    if args.skip_complete:
        plan = [item for item in plan if int(item["current_count"]) < int(item["target_count"])]

    report: dict[str, Any] = {
        "contract_version": "douyin_post_api_collection.v1",
        "run_id": args.run_id,
        "generated_at": _now_iso(),
        "target_per_account": args.target_per_account,
        "page_size": args.page_size,
        "max_pages": args.max_pages,
        "skip_complete": bool(args.skip_complete),
        "dry_run": bool(args.dry_run),
        "planned_accounts": plan,
        "accounts": [],
    }

    if args.dry_run:
        _write_report(report, Path(args.report_dir), args.run_id)
        print(json.dumps(_report_summary(report), ensure_ascii=False, indent=2))
        return

    for item in plan:
        account = item["account"]
        result = collect_account(
            account,
            account_library=Path(args.account_library),
            output_root=Path(args.output_root),
            run_id=args.run_id,
            dataset_suffix=args.dataset_suffix,
            target_count=int(item["target_count"]),
            page_size=args.page_size,
            max_pages=args.max_pages,
            stall_pages=args.stall_pages,
            page_delay=args.page_delay,
            request_delay=args.request_delay,
            request_timeout=args.request_timeout,
            clean=not args.no_clean,
            import_history=not args.no_import,
            force_import=args.force_import,
        )
        report["accounts"].append(result)
        print(json.dumps(_brief(result), ensure_ascii=False), flush=True)

    _write_report(report, Path(args.report_dir), args.run_id)
    print(json.dumps(_report_summary(report), ensure_ascii=False, indent=2))


def collect_account(
    account: dict[str, Any],
    *,
    account_library: Path,
    output_root: Path,
    run_id: str,
    dataset_suffix: str | None,
    target_count: int,
    page_size: int,
    max_pages: int,
    stall_pages: int,
    page_delay: float,
    request_delay: float,
    request_timeout: int,
    clean: bool,
    import_history: bool,
    force_import: bool,
) -> dict[str, Any]:
    account_key = str(account.get("account_key") or "").strip()
    profile_url = str(account.get("profile_url") or "").strip()
    sec_uid = str(account.get("sec_uid") or "").strip() or _sec_uid_from_profile_url(profile_url)
    if not account_key or not profile_url or not sec_uid:
        return {
            "account_key": account_key,
            "status": "failed",
            "error": "missing_account_key_profile_url_or_sec_uid",
        }

    account_slug = _slug(account_key)
    observed_at = _now_iso()
    account_dir = output_root / account_slug
    raw_dir = account_dir / f"raw_{run_id}"
    raw_dir.mkdir(parents=True, exist_ok=True)

    works_by_id: dict[str, dict[str, Any]] = {}
    manifest: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    cursor = "0"
    status = "captured"
    error = ""
    no_new_pages = 0

    try:
        _chrome_open(profile_url, delay=page_delay)
        page_meta = _chrome_json(PAGE_META_JS, timeout=max(20, int(page_delay) + 15))
        for page in range(max(1, max_pages)):
            if cursor in seen_cursors:
                manifest.append({"page": page, "cursor": cursor, "stop_reason": "repeated_cursor"})
                break
            seen_cursors.add(cursor)

            payload = _fetch_post_page(
                sec_uid=sec_uid,
                cursor=cursor,
                count=page_size,
                page=page,
                timeout=request_timeout,
            )
            works = payload.get("works") or []
            new_count = 0
            for work in works:
                aweme_id = str(work.get("aweme_id") or "").strip()
                if not aweme_id:
                    continue
                if aweme_id not in works_by_id:
                    works_by_id[aweme_id] = work
                    new_count += 1

            manifest.append(
                {
                    "page": page,
                    "status": payload.get("status"),
                    "status_code": payload.get("status_code"),
                    "status_msg": payload.get("status_msg") or "",
                    "cursor": cursor,
                    "next_cursor": str(payload.get("next_cursor") or ""),
                    "has_more": payload.get("has_more"),
                    "count": len(works),
                    "new_count": new_count,
                    "unique_count": len(works_by_id),
                    "len": payload.get("len"),
                    "request_url": payload.get("request_url") or "",
                }
            )

            if payload.get("status") != 200 or int(payload.get("status_code") or 0) != 0:
                status = "partial" if works_by_id else "failed"
                error = str(payload.get("status_msg") or payload.get("head") or "post_api_request_failed")[:500]
                break
            if len(works_by_id) >= target_count:
                break
            if not payload.get("has_more"):
                break
            next_cursor = str(payload.get("next_cursor") or "").strip()
            if not next_cursor:
                manifest.append({"page": page, "cursor": cursor, "stop_reason": "missing_next_cursor"})
                break
            no_new_pages = no_new_pages + 1 if new_count == 0 else 0
            if no_new_pages >= max(1, stall_pages):
                manifest.append({"page": page, "cursor": cursor, "stop_reason": "stall_no_new_works"})
                break
            cursor = next_cursor
            time.sleep(max(0.1, request_delay))

        works_list = list(works_by_id.values())
        raw_path = raw_dir / f"{account_slug}_post_api_works.json"
        manifest_path = raw_dir / f"{account_slug}_post_api_pages_manifest.json"
        snapshot_path = raw_dir / f"douyin_profile_visible_{account_slug}_post_api_appleevents.json"
        raw_path.write_text(json.dumps(works_list, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot_path.write_text(
            json.dumps(
                _profile_snapshot(account, page_meta, works_list, observed_at=observed_at),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        result: dict[str, Any] = {
            "account_key": account_key,
            "nickname": account.get("nickname") or "",
            "status": status if works_list else "empty",
            "target_count": target_count,
            "raw_work_count": len(works_list),
            "raw_dir": str(raw_dir),
            "raw_works": str(raw_path),
            "pages_manifest": str(manifest_path),
            "profile_snapshot": str(snapshot_path),
            "page_count": len([item for item in manifest if "status" in item]),
            "last_cursor": cursor,
            "error": error,
        }

        if clean:
            clean_result = clean_account_api_works(
                account_library=account_library,
                account_key=account_key,
                raw_works=raw_path,
                output_root=output_root,
                run_id=run_id,
                source_method="appleevents_post_api",
                observed_at=observed_at,
            )
            result.update(
                {
                    "clean_dir": str(clean_result.clean_dir),
                    "clean_work_count": len(clean_result.clean_works),
                    "quality_grade": clean_result.quality_report.get("quality_grade"),
                    "quality_score": clean_result.quality_report.get("quality_score"),
                    "author_mismatch_rejected": len(clean_result.rejected_author_mismatch),
                    "clean_paths": clean_result.paths,
                }
            )
            if import_history:
                dataset_id = f"{account_key}_{_dataset_suffix(dataset_suffix, run_id)}"
                imported = import_douyin_history(
                    account_key,
                    clean_result.clean_dir,
                    raw_dir=clean_result.raw_dir,
                    dataset_id=dataset_id,
                    dataset_name=f"{account.get('nickname') or account_key} Douyin post API {run_id}",
                    force=force_import,
                )
                result["history_import"] = imported
                result["final_count"] = int(historical_sample_summary(account_key).get("sample_count") or 0)
        return result
    except Exception as exc:
        return {
            "account_key": account_key,
            "nickname": account.get("nickname") or "",
            "status": "failed",
            "target_count": target_count,
            "raw_work_count": len(works_by_id),
            "raw_dir": str(raw_dir),
            "error": str(exc),
            "pages_manifest": manifest,
        }


def _fetch_post_page(*, sec_uid: str, cursor: str, count: int, page: int, timeout: int) -> dict[str, Any]:
    js = _post_page_js(sec_uid=sec_uid, cursor=cursor, count=count, page=page)
    return _chrome_json(js, timeout=timeout)


def _post_page_js(*, sec_uid: str, cursor: str, count: int, page: int) -> str:
    return f"""
(() => {{
  const clean = value => String(value ?? '').replace(/\\s+/g, ' ').trim();
  const number = value => {{
    if (value === null || value === undefined || value === '') return 0;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }};
  const tagsFrom = text => Array.from(new Set(clean(text).match(/#[^#\\s]+/g) || []));
  const first = (...values) => values.find(value => value !== undefined && value !== null && value !== '') ?? '';
  const normalize = item => {{
    const statistics = item.statistics || item.stats || {{}};
    const author = item.author || {{}};
    const video = item.video || {{}};
    const music = item.music || {{}};
    const images = Array.isArray(item.images) ? item.images : (Array.isArray(item.image_infos) ? item.image_infos : []);
    const awemeId = clean(first(item.aweme_id, item.awemeId, item.id));
    const desc = clean(first(item.desc, item.title, item.item_title));
    const shareInfo = item.share_info || {{}};
    const mediaType = images.length ? 'image_text' : clean(first(item.media_type, item.aweme_type, 'video'));
    return {{
      aweme_id: awemeId,
      desc,
      create_time: number(first(item.create_time, item.createTime)),
      share_url: clean(first(item.share_url, shareInfo.share_url)),
      video_url: awemeId ? `https://www.douyin.com/video/${{awemeId}}` : '',
      digg_count: number(first(statistics.digg_count, statistics.like_count, item.digg_count, item.like_count)),
      comment_count: number(first(statistics.comment_count, item.comment_count)),
      share_count: number(first(statistics.share_count, item.share_count)),
      collect_count: number(first(statistics.collect_count, statistics.favorite_count, item.collect_count, item.favorite_count)),
      play_count: number(first(statistics.play_count, statistics.view_count, item.play_count, item.view_count, 0)),
      duration: number(first(video.duration, item.duration, item.duration_ms)),
      music_title: clean(first(music.title, item.music_title)),
      author_nickname: clean(first(author.nickname, author.name, item.author_nickname)),
      author_sec_uid: clean(first(author.sec_uid, author.secUid, item.author_sec_uid, item.sec_uid)),
      author_uid: clean(first(author.uid, author.user_id, item.author_uid, item.author_user_id, item.uid)),
      is_top: Boolean(first(item.is_top, item.isTop, false)),
      images_count: images.length,
      item_title: clean(item.item_title),
      media_type: mediaType,
      tags: tagsFrom(desc)
    }};
  }};
  const params = new URLSearchParams({{
    device_platform: 'webapp',
    aid: '6383',
    channel: 'channel_pc_web',
    sec_user_id: {json.dumps(sec_uid)},
    max_cursor: {json.dumps(str(cursor))},
    count: String({int(count)}),
    locate_query: 'false',
    show_live_replay_strategy: '1',
    need_time_list: '1',
    time_list_query: '0',
    whale_cut_token: '',
    cut_version: '1'
  }});
  const requestUrl = '/aweme/v1/web/aweme/post/?' + params.toString();
  const xhr = new XMLHttpRequest();
  xhr.open('GET', requestUrl, false);
  xhr.withCredentials = true;
  xhr.setRequestHeader('accept', 'application/json, text/plain, */*');
  xhr.send(null);
  let payload = {{}};
  try {{ payload = JSON.parse(xhr.responseText || '{{}}'); }} catch (error) {{
    return JSON.stringify({{
      page: {int(page)},
      request_url: requestUrl,
      status: xhr.status,
      status_code: -1,
      status_msg: String(error && error.message || 'json_parse_failed'),
      cursor: {json.dumps(str(cursor))},
      next_cursor: '',
      has_more: 0,
      count: 0,
      len: xhr.responseText.length,
      works: [],
      head: xhr.responseText.slice(0, 300)
    }});
  }}
  const rawWorks = Array.isArray(payload.aweme_list) ? payload.aweme_list : [];
  return JSON.stringify({{
    page: {int(page)},
    request_url: requestUrl,
    status: xhr.status,
    status_code: Number(payload.status_code || 0),
    status_msg: clean(payload.status_msg),
    cursor: {json.dumps(str(cursor))},
    next_cursor: clean(first(payload.max_cursor, payload.cursor, '')),
    has_more: Number(payload.has_more || 0),
    count: rawWorks.length,
    len: xhr.responseText.length,
    works: rawWorks.map(normalize),
    head: xhr.responseText.slice(0, 180)
  }});
}})()
""".strip()


def _profile_snapshot(account: dict[str, Any], page_meta: dict[str, Any], works: list[dict[str, Any]], *, observed_at: str) -> dict[str, Any]:
    account_key = account.get("account_key") or ""
    nickname = account.get("nickname") or ""
    return {
        "observed_at": observed_at,
        "page": {
            "title": page_meta.get("title") or "",
            "url": page_meta.get("url") or account.get("profile_url") or "",
            "source": "douyin_profile_post_api_appleevents",
            "label": "post_api_cursor_pages",
        },
        "account": {
            "nickname": nickname,
            "profile_url": account.get("profile_url") or "",
            "followers_visible": str(account.get("follower_count") or ""),
            "likes_received_visible": "",
            "account_type": account.get("account_type") or "unknown",
            "content_domain": account.get("program_key") or "unknown",
            "total_works_visible": str(account.get("aweme_count") or ""),
            "following_state_visible": "已关注",
        },
        "current_video": {
            "aweme_ids_visible": [],
            "hashtag_links": [],
            "visible_metric_numbers_unlabeled": [],
        },
        "visible_works": [
            {
                "href": item.get("video_url") or "",
                "aweme_id": item.get("aweme_id") or "",
                "visible_count": str(item.get("digg_count") or ""),
                "title_tags_text": item.get("desc") or "",
                "tags": item.get("tags") or [],
                "account_key": account_key,
                "account_name": nickname,
                "collected_at": observed_at,
                "source": "profile_post_api_appleevents",
                "api_extra": item,
            }
            for item in works
        ],
    }


def _plan_account(account: dict[str, Any], target_per_account: int) -> dict[str, Any]:
    account_key = account.get("account_key") or ""
    aweme_count = _safe_int(account.get("aweme_count"))
    target = min(target_per_account, aweme_count) if aweme_count > 0 else target_per_account
    current = _current_sample_count(account_key)
    return {
        "account_key": account_key,
        "nickname": account.get("nickname") or "",
        "aweme_count": aweme_count,
        "current_count": current,
        "target_count": target,
        "gap": max(0, target - current),
        "profile_url": account.get("profile_url") or "",
        "account": account,
    }


def _current_sample_count(account_key: str) -> int:
    if not account_key:
        return 0
    try:
        return int(historical_sample_summary(account_key).get("sample_count") or 0)
    except Exception:
        return 0


def _select_accounts(library: list[dict[str, Any]], keys: list[str] | None, limit: int) -> list[dict[str, Any]]:
    if keys:
        wanted = set(keys)
        selected = [account for account in library if account.get("account_key") in wanted]
    else:
        selected = list(library)
    selected = [account for account in selected if account.get("profile_url")]
    selected.sort(key=lambda item: (-_safe_int(item.get("aweme_count")), str(item.get("account_key") or "")))
    return selected[:limit] if limit and limit > 0 else selected


def _load_accounts(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ["accounts", "account_library", "items", "rows"]:
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
    raise ValueError(f"account library must be a list or contain accounts: {path}")


def _chrome_open(url: str, *, delay: float) -> None:
    script = f"""
tell application "Google Chrome"
  activate
  open location {_apple_quote(url)}
  delay {max(0, float(delay))}
end tell
"""
    subprocess.check_output(
        ["osascript"],
        input=script,
        text=True,
        stderr=subprocess.STDOUT,
        timeout=max(20, int(delay) + 20),
    )


def _chrome_json(js: str, *, timeout: int = 60) -> dict[str, Any]:
    script = f"""
tell application "Google Chrome"
  set js to {_apple_quote(js)}
  set resultText to execute active tab of front window javascript js
  return resultText
end tell
"""
    text = subprocess.check_output(["osascript"], input=script, text=True, stderr=subprocess.STDOUT, timeout=timeout)
    return json.loads(text or "{}")


def _write_report(report: dict[str, Any], report_dir: Path, run_id: str) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    report["summary"] = _report_summary(report)
    path = report_dir / f"post_api_collection_{run_id}.json"
    latest = report_dir / "post_api_collection_latest.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(path)


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    accounts = report.get("accounts") or []
    planned = report.get("planned_accounts") or []
    return {
        "planned_account_count": len(planned),
        "account_count": len(accounts),
        "captured": sum(1 for item in accounts if item.get("status") in {"captured", "partial"}),
        "failed": sum(1 for item in accounts if item.get("status") == "failed"),
        "empty": sum(1 for item in accounts if item.get("status") == "empty"),
        "raw_work_count": sum(int(item.get("raw_work_count") or 0) for item in accounts),
        "clean_work_count": sum(int(item.get("clean_work_count") or 0) for item in accounts),
        "imported": sum(int(((item.get("history_import") or {}).get("inserted") or 0)) for item in accounts),
        "updated": sum(int(((item.get("history_import") or {}).get("updated") or 0)) for item in accounts),
    }


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_key": result.get("account_key"),
        "status": result.get("status"),
        "target_count": result.get("target_count"),
        "raw_work_count": result.get("raw_work_count"),
        "clean_work_count": result.get("clean_work_count"),
        "final_count": result.get("final_count"),
        "error": result.get("error", ""),
    }


def _dataset_suffix(value: str | None, run_id: str) -> str:
    if value:
        return _slug(value)
    date = run_id[:8] if len(run_id) >= 8 else datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{date}_appleevents_api"


def _sec_uid_from_profile_url(url: str) -> str:
    return str(url or "").rstrip("/").split("/")[-1]


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if text.endswith("万"):
        return int(float(text[:-1] or 0) * 10000)
    if text.endswith("亿"):
        return int(float(text[:-1] or 0) * 100000000)
    try:
        return int(float(text))
    except Exception:
        return 0


def _slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value)).strip("_")
    return slug or "account"


def _apple_quote(value: str) -> str:
    return json.dumps(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
