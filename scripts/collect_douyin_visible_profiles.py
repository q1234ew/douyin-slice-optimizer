#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_LIBRARY = ROOT / "data/douyin_capture/douyin_account_library_latest.json"


EXTRACT_JS = r"""
(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const tagRe = /#[^#\s]+/g;
  const links = Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="/note/"]'));
  const works = links.map(a => {
    const href = a.href || '';
    const text = clean(a.innerText || a.textContent || a.getAttribute('aria-label') || '');
    const idMatch = href.match(/\/(?:video|note)\/(\d+)/);
    const tags = Array.from(new Set(text.match(tagRe) || []));
    return {
      href,
      aweme_id: idMatch ? idMatch[1] : '',
      visible_count: null,
      title_tags_text: text,
      tags
    };
  }).filter(item => item.href && item.title_tags_text);
  return JSON.stringify({
    title: document.title,
    url: location.href,
    y: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
    height: Math.round(document.documentElement.scrollHeight || 0),
    visible_work_count: works.length,
    works
  });
})()
""".strip()


SCROLL_JS = """
(() => {
  const amount = Math.max(900, Math.floor((window.innerHeight || 900) * 0.9));
  window.scrollBy(0, amount);
  return JSON.stringify({
    y: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
    height: Math.round(document.documentElement.scrollHeight || 0)
  });
})()
""".strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect visible Douyin profile work cards via Chrome Apple Events.")
    parser.add_argument("--accounts", nargs="*", help="Account keys to collect. Defaults to new matrix accounts.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of accounts.")
    parser.add_argument("--target-works", type=int, default=60, help="Stop after this many unique visible works.")
    parser.add_argument("--scroll-steps", type=int, default=10, help="Maximum scroll steps per profile.")
    parser.add_argument("--page-delay", type=float, default=8.0, help="Initial page load delay in seconds.")
    parser.add_argument("--scroll-delay", type=float, default=1.6, help="Delay after each scroll in seconds.")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_visible_profile_scroll"))
    args = parser.parse_args()

    accounts = _select_accounts(args.accounts, limit=args.limit)
    report: dict[str, Any] = {
        "contract_version": "douyin_visible_profile_scroll.v1",
        "run_id": args.run_id,
        "generated_at": _now_iso(),
        "target_works": args.target_works,
        "scroll_steps": args.scroll_steps,
        "accounts": [],
    }

    for account in accounts:
        result = collect_account(
            account,
            run_id=args.run_id,
            target_works=args.target_works,
            scroll_steps=args.scroll_steps,
            page_delay=args.page_delay,
            scroll_delay=args.scroll_delay,
        )
        report["accounts"].append(result)
        print(json.dumps(_brief(result), ensure_ascii=False), flush=True)

    report["summary"] = {
        "account_count": len(report["accounts"]),
        "success": sum(1 for item in report["accounts"] if item.get("status") == "captured"),
        "empty": sum(1 for item in report["accounts"] if item.get("status") == "empty"),
        "failed": sum(1 for item in report["accounts"] if item.get("status") == "failed"),
        "visible_work_count": sum(int(item.get("visible_work_count") or 0) for item in report["accounts"]),
    }
    out_dir = ROOT / "outputs/douyin_followed_recollect_20260630"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"visible_profile_collection_{args.run_id}.json"
    latest_path = out_dir / "visible_profile_collection_latest.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "report": str(report_path)}, ensure_ascii=False, indent=2))


def collect_account(
    account: dict[str, Any],
    *,
    run_id: str,
    target_works: int,
    scroll_steps: int,
    page_delay: float,
    scroll_delay: float,
) -> dict[str, Any]:
    account_key = str(account.get("account_key") or "").strip()
    profile_url = str(account.get("profile_url") or "").strip()
    if not account_key or not profile_url:
        return {"account_key": account_key, "status": "failed", "error": "missing_account_key_or_profile_url"}

    observed_at = _now_iso()
    raw_dir = ROOT / "data/douyin_capture" / _slug(account_key) / f"raw_{run_id}"
    clean_dir = ROOT / "data/douyin_capture" / _slug(account_key) / f"clean_{run_id}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clean_dir.mkdir(parents=True, exist_ok=True)

    works: dict[str, dict[str, Any]] = {}
    title = ""
    page_url = profile_url
    try:
        _chrome_open(profile_url, delay=page_delay)
        for step in range(max(1, scroll_steps + 1)):
            page = _chrome_json(EXTRACT_JS)
            title = page.get("title") or title
            page_url = page.get("url") or page_url
            for work in page.get("works") or []:
                key = str(work.get("aweme_id") or work.get("href") or work.get("title_tags_text") or "").strip()
                if key and key not in works:
                    works[key] = work
            if len(works) >= target_works or step >= scroll_steps:
                break
            _chrome_json(SCROLL_JS)
            time.sleep(max(0.2, scroll_delay))

        snapshot = {
            "observed_at": observed_at,
            "page": {
                "title": title,
                "url": page_url,
                "source": "douyin_profile_visible_dom_scroll",
                "label": account_key,
            },
            "account": {
                "nickname": account.get("nickname") or "",
                "profile_url": profile_url,
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
            "visible_works": list(works.values()),
        }
        raw_path = raw_dir / f"douyin_profile_visible_{account_key}_{run_id}.json"
        raw_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "account_key": account_key,
            "nickname": account.get("nickname") or "",
            "status": "captured" if works else "empty",
            "profile_url": profile_url,
            "visible_work_count": len(works),
            "raw_dir": str(raw_dir),
            "clean_dir": str(clean_dir),
            "raw_snapshot": str(raw_path),
            "page_title": title,
            "page_url": page_url,
        }
    except Exception as exc:
        return {
            "account_key": account_key,
            "nickname": account.get("nickname") or "",
            "status": "failed",
            "profile_url": profile_url,
            "visible_work_count": len(works),
            "raw_dir": str(raw_dir),
            "clean_dir": str(clean_dir),
            "error": str(exc),
        }


def _select_accounts(keys: list[str] | None, *, limit: int) -> list[dict[str, Any]]:
    library = json.loads(ACCOUNT_LIBRARY.read_text(encoding="utf-8"))
    if keys:
        key_set = set(keys)
        selected = [account for account in library if account.get("account_key") in key_set]
    else:
        selected = [account for account in library if account.get("source_kind") == "tianci7_matrix_xlsx"]
    selected = [account for account in selected if account.get("profile_url")]
    return selected[:limit] if limit and limit > 0 else selected


def _chrome_open(url: str, *, delay: float) -> None:
    script = f"""
tell application "Google Chrome"
  activate
  open location {_apple_quote(url)}
  delay {max(0, float(delay))}
end tell
"""
    subprocess.check_output(["osascript"], input=script, text=True, stderr=subprocess.STDOUT, timeout=max(20, int(delay) + 20))


def _chrome_json(js: str) -> dict[str, Any]:
    script = f"""
tell application "Google Chrome"
  set js to {_apple_quote(js)}
  set resultText to execute active tab of front window javascript js
  return resultText
end tell
"""
    text = subprocess.check_output(["osascript"], input=script, text=True, stderr=subprocess.STDOUT, timeout=30)
    return json.loads(text or "{}")


def _apple_quote(value: str) -> str:
    return json.dumps(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "account"


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_key": result.get("account_key"),
        "status": result.get("status"),
        "visible_work_count": result.get("visible_work_count"),
        "error": result.get("error", ""),
    }


if __name__ == "__main__":
    main()
