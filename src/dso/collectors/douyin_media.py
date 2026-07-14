from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dso.config import ensure_data_dirs


MEDIA_COLLECTION_VERSION = "douyin_media_collection.v1"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149 Safari/537.36"
)

PAGE_MEDIA_JS = r"""
(() => {
  const urls = Array.from(document.querySelectorAll('video,source'))
    .map(el => el.currentSrc || el.src || '')
    .filter(Boolean);
  const og = document.querySelector('meta[property="og:image"]')?.content
    || document.querySelector('meta[name="og:image"]')?.content
    || document.querySelector('meta[itemprop="image"]')?.content
    || '';
  const images = Array.from(document.querySelectorAll('img,meta[property],meta[name]'))
    .map(el => el.content || el.currentSrc || el.src || '')
    .filter(Boolean);
  const cover = og || images.find(u => /pcweb_cover|AWEME_DETAIL|image-cut|douyinpic|byteimg/i.test(u)) || '';
  const resources = (performance.getEntriesByType('resource') || [])
    .map(e => e.name)
    .filter(u => /douyinvod|idouyinvod|aweme\/v1\/play|mime_type=video_mp4|pcweb_cover|AWEME_DETAIL|douyinpic|byteimg/i.test(u));
  return JSON.stringify({
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    video_src: urls[0] || '',
    video_sources: Array.from(new Set([
      ...urls,
      ...resources.filter(u => /douyinvod|idouyinvod|aweme\/v1\/play|mime_type=video_mp4/i.test(u))
    ])).slice(0, 12),
    cover_url: cover,
    resource_count: resources.length
  });
})()
""".strip()


@dataclass(frozen=True)
class MediaCollectionPaths:
    root: Path
    videos: Path
    covers: Path
    frames: Path
    audio: Path
    transcripts: Path
    ocr: Path
    features: Path


def collect_douyin_media(
    plan_path: str | Path,
    *,
    stage: str | None = "smoke_v1",
    account: str | None = None,
    limit: int = 0,
    output_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    run_id: str = "20260629_test_v1",
    page_delay_seconds: int = 14,
    extra_wait_seconds: int = 5,
    extract_audio: bool = True,
    dry_run: bool = False,
    max_storage_bytes: int = 0,
) -> dict:
    """Collect Douyin video media from a test/pilot plan using Chrome Apple Events.

    The page script is intentionally read-only. It reads DOM media elements,
    meta images, and performance resource URLs. It does not read cookies,
    localStorage, or sessionStorage.
    """

    plan = _read_json(Path(plan_path))
    samples = _select_plan_samples(plan, stage=stage, account=account, limit=limit)
    settings = ensure_data_dirs()
    asset_root = Path(output_root) if output_root else settings.data_dir / "douyin_media_assets"
    reports = Path(report_dir) if report_dir else settings.root / "outputs" / "v0.7_media_collection_test"
    reports.mkdir(parents=True, exist_ok=True)
    storage_limit = max(0, int(max_storage_bytes or 0))

    results: list[dict[str, Any]] = []
    stopped_for_storage = False
    for item in samples:
        row = _initial_result(item)
        paths = _asset_paths(asset_root, item["account_id"], run_id, item["aweme_id"])
        if storage_limit and _directory_size(asset_root) >= storage_limit:
            row.update(
                {
                    "status": "skipped_storage_limit",
                    "storage_limit_bytes": storage_limit,
                    "storage_used_bytes": _directory_size(asset_root),
                    "errors": ["storage_limit_reached_before_sample"],
                }
            )
            results.append(row)
            stopped_for_storage = True
            break
        if dry_run:
            row.update(
                {
                    "status": "planned",
                    "video_path": str(paths.videos / f"{item['aweme_id']}.mp4"),
                    "cover_path": str(paths.covers / f"{item['aweme_id']}.jpg"),
                    "frame_path": str(paths.frames / item["aweme_id"] / "frame_0001.jpg"),
                    "audio_path": str(paths.audio / f"{item['aweme_id']}.wav"),
                }
            )
            results.append(row)
            continue
        result = _collect_one(
            item,
            paths=paths,
            page_delay_seconds=page_delay_seconds,
            extra_wait_seconds=extra_wait_seconds,
            extract_audio=extract_audio,
            storage_root=asset_root,
            max_storage_bytes=storage_limit,
        )
        results.append(result)
        if _close_tab_after_sample_enabled():
            _chrome_close_current_douyin_tab()
        if storage_limit and _directory_size(asset_root) >= storage_limit:
            stopped_for_storage = True
            break

    summary = _summary(results, source_plan=str(plan_path), run_id=run_id, dry_run=dry_run)
    summary["storage"] = {
        "root": str(asset_root),
        "used_bytes": _directory_size(asset_root),
        "limit_bytes": storage_limit,
        "used_gb": round(_directory_size(asset_root) / (1024**3), 4),
        "limit_gb": round(storage_limit / (1024**3), 4) if storage_limit else 0,
        "stopped_for_storage": stopped_for_storage,
    }
    report = {"summary": summary, "results": results}
    suffix = _report_suffix(stage=stage, account=account, dry_run=dry_run)
    json_path = reports / f"media_collection_{suffix}_report.json"
    md_path = reports / f"media_collection_{suffix}_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_report_markdown(report), encoding="utf-8")
    return {
        **summary,
        "report_json": str(json_path),
        "report_md": str(md_path),
        "output_root": str(asset_root),
    }


def _collect_one(
    item: dict[str, Any],
    *,
    paths: MediaCollectionPaths,
    page_delay_seconds: int,
    extra_wait_seconds: int,
    extract_audio: bool,
    storage_root: Path | None = None,
    max_storage_bytes: int = 0,
) -> dict:
    row = _initial_result(item)
    _ensure_asset_dirs(paths)
    try:
        info = _chrome_extract_page_media(item["source_url"], delay=page_delay_seconds)
        if not info.get("video_src") and extra_wait_seconds > 0:
            time.sleep(extra_wait_seconds)
            info = _chrome_read_current_page_media()
        row.update(
            {
                "page_href": info.get("href", ""),
                "page_title": info.get("title", ""),
                "video_src_found": bool(info.get("video_src")),
                "video_source_count": len(info.get("video_sources") or []),
                "cover_url_found": bool(info.get("cover_url")),
                "resource_count": info.get("resource_count", 0),
            }
        )
        video_path = paths.videos / f"{item['aweme_id']}.mp4"
        cover_path = paths.covers / f"{item['aweme_id']}.jpg"
        frame_path = paths.frames / item["aweme_id"] / "frame_0001.jpg"
        audio_path = paths.audio / f"{item['aweme_id']}.wav"
        candidates = list(dict.fromkeys(info.get("video_sources") or ([info.get("video_src")] if info.get("video_src") else [])))
        ok = False
        last_error = ""
        for candidate in candidates[:5]:
            ok, last_error = _download(
                candidate,
                video_path,
                referer=info.get("href") or item["source_url"],
                storage_root=storage_root,
                max_storage_bytes=max_storage_bytes,
            )
            if ok and video_path.stat().st_size > 1024:
                row["download_url"] = candidate
                break
        row["video_path"] = str(video_path) if video_path.exists() else ""
        row["video_downloaded"] = bool(ok and video_path.exists())
        if not row["video_downloaded"]:
            row["errors"].append("video_download_failed: " + last_error)
        if info.get("cover_url"):
            cover_ok, cover_error = _download(
                info["cover_url"],
                cover_path,
                referer=info.get("href") or item["source_url"],
                storage_root=storage_root,
                max_storage_bytes=max_storage_bytes,
            )
            row["cover_path"] = str(cover_path) if cover_ok and cover_path.exists() else ""
            row["cover_downloaded"] = bool(cover_ok and cover_path.exists())
            if not cover_ok:
                row["errors"].append("cover_download_failed: " + cover_error)
        else:
            row["cover_path"] = ""
            row["cover_downloaded"] = False
        if row["video_downloaded"]:
            row["file_size_bytes"] = video_path.stat().st_size
            row["sha256"] = hashlib.sha256(video_path.read_bytes()).hexdigest()
            row["ffprobe"] = _ffprobe(video_path)
            row["duration_seconds"] = float((row["ffprobe"].get("format") or {}).get("duration") or 0)
            frame_ok, frame_error = _extract_frame(video_path, frame_path)
            if frame_ok and _storage_over_limit(storage_root, max_storage_bytes):
                frame_path.unlink(missing_ok=True)
                frame_ok = False
                frame_error = "storage_limit_exceeded_after_frame_extract"
            row["frame_path"] = str(frame_path) if frame_ok else ""
            row["frame_extracted"] = bool(frame_ok)
            if not frame_ok:
                row["errors"].append("frame_extract_failed: " + frame_error)
            if extract_audio:
                audio_ok, audio_error = _extract_audio(video_path, audio_path)
                if audio_ok and _storage_over_limit(storage_root, max_storage_bytes):
                    audio_path.unlink(missing_ok=True)
                    audio_ok = False
                    audio_error = "storage_limit_exceeded_after_audio_extract"
                row["audio_path"] = str(audio_path) if audio_ok else ""
                row["audio_extracted"] = bool(audio_ok)
                if not audio_ok:
                    row["errors"].append("audio_extract_failed: " + audio_error)
        has_media = row.get("video_downloaded") and row.get("duration_seconds", 0) > 0
        has_visual = row.get("cover_downloaded") or row.get("frame_extracted")
        row["status"] = "success" if has_media and has_visual else "partial"
        row["storage_used_bytes"] = _directory_size(storage_root) if storage_root else 0
    except Exception as exc:
        row["status"] = "failed"
        row["errors"].append(str(exc))
    return row


def _select_plan_samples(plan: dict[str, Any], *, stage: str | None, account: str | None, limit: int) -> list[dict[str, Any]]:
    samples = [item for item in plan.get("samples") or [] if isinstance(item, dict)]
    if stage and stage != "all":
        samples = [item for item in samples if item.get("stage") == stage]
    if account:
        samples = [item for item in samples if item.get("account_id") == account]
    samples = [item for item in samples if item.get("aweme_id") and item.get("source_url") and item.get("account_id")]
    samples.sort(key=lambda item: int(item.get("collection_order") or 0))
    return samples[:limit] if limit and limit > 0 else samples


def _initial_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": item.get("sample_id") or "",
        "collection_order": int(item.get("collection_order") or 0),
        "account_id": item.get("account_id") or "",
        "dataset_id": item.get("dataset_id") or "",
        "performance_label": item.get("performance_label") or "",
        "aweme_id": item.get("aweme_id") or "",
        "source_url": item.get("source_url") or "",
        "title": item.get("title") or "",
        "status": "started",
        "errors": [],
    }


def _asset_paths(root: Path, account_id: str, run_id: str, aweme_id: str) -> MediaCollectionPaths:
    base = root / account_id / run_id
    return MediaCollectionPaths(
        root=base,
        videos=base / "videos",
        covers=base / "covers",
        frames=base / "frames",
        audio=base / "audio",
        transcripts=base / "transcripts",
        ocr=base / "ocr",
        features=base / "features",
    )


def _ensure_asset_dirs(paths: MediaCollectionPaths) -> None:
    for path in [paths.videos, paths.covers, paths.frames, paths.audio, paths.transcripts, paths.ocr, paths.features]:
        path.mkdir(parents=True, exist_ok=True)


def _chrome_extract_page_media(url: str, *, delay: int) -> dict[str, Any]:
    script = f"""
tell application "Google Chrome"
  activate
  open location {_apple_quote(url)}
  delay {max(0, int(delay))}
  set js to {_apple_quote(PAGE_MEDIA_JS)}
  set resultText to execute active tab of front window javascript js
  return resultText
end tell
"""
    text = subprocess.check_output(["osascript"], input=script, text=True, stderr=subprocess.STDOUT, timeout=max(30, delay + 25))
    return json.loads(text)


def _chrome_read_current_page_media() -> dict[str, Any]:
    script = f"""
tell application "Google Chrome"
  set js to {_apple_quote(PAGE_MEDIA_JS)}
  set resultText to execute active tab of front window javascript js
  return resultText
end tell
"""
    text = subprocess.check_output(["osascript"], input=script, text=True, stderr=subprocess.STDOUT, timeout=30)
    return json.loads(text)


def _download(
    url: str,
    path: Path,
    *,
    referer: str,
    storage_root: Path | None = None,
    max_storage_bytes: int = 0,
) -> tuple[bool, str]:
    if not url:
        return False, "missing_url"
    if _storage_over_limit(storage_root, max_storage_bytes):
        return False, "storage_limit_reached"
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": referer}
    temp_path = path.with_name(path.name + ".part")
    timeout_seconds = _download_timeout_seconds()
    if shutil.which("curl"):
        return _download_with_curl(
            url,
            path,
            temp_path=temp_path,
            referer=referer,
            storage_root=storage_root,
            max_storage_bytes=max_storage_bytes,
            timeout_seconds=timeout_seconds,
        )
    try:
        deadline = time.monotonic() + timeout_seconds
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=min(20.0, timeout_seconds)) as resp:
            current = _directory_size(storage_root) if storage_root else 0
            content_length = int(resp.headers.get("Content-Length") or 0)
            if max_storage_bytes and content_length and current + content_length > max_storage_bytes:
                return False, "storage_limit_would_exceed"
            downloaded = 0
            path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("wb") as handle:
                while True:
                    if time.monotonic() > deadline:
                        handle.close()
                        temp_path.unlink(missing_ok=True)
                        return False, f"download_timeout_after_{timeout_seconds:.0f}s"
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if max_storage_bytes and current + downloaded > max_storage_bytes:
                        handle.close()
                        temp_path.unlink(missing_ok=True)
                        return False, "storage_limit_would_exceed"
                    handle.write(chunk)
        temp_path.replace(path)
        return True, ""
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return False, str(exc)


def _download_with_curl(
    url: str,
    path: Path,
    *,
    temp_path: Path,
    referer: str,
    storage_root: Path | None,
    max_storage_bytes: int,
    timeout_seconds: float,
) -> tuple[bool, str]:
    current = _directory_size(storage_root) if storage_root else 0
    if max_storage_bytes and current >= max_storage_bytes:
        return False, "storage_limit_reached"
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "curl",
        "--location",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        f"{timeout_seconds:.0f}",
        "--connect-timeout",
        "10",
        "--speed-limit",
        "1024",
        "--speed-time",
        "10",
        "--user-agent",
        DEFAULT_USER_AGENT,
        "--referer",
        referer,
        "--output",
        str(temp_path),
    ]
    if max_storage_bytes:
        remaining = max(1, max_storage_bytes - current)
        command.extend(["--max-filesize", str(remaining)])
    command.append(url)
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout_seconds + 5
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.kill()
                process.wait(timeout=5)
                temp_path.unlink(missing_ok=True)
                return False, f"download_timeout_after_{timeout_seconds:.0f}s"
            time.sleep(0.2)
        if process.returncode != 0:
            temp_path.unlink(missing_ok=True)
            return False, f"curl_exit_{process.returncode}"
        if _storage_over_limit(storage_root, max_storage_bytes):
            temp_path.unlink(missing_ok=True)
            return False, "storage_limit_would_exceed"
        temp_path.replace(path)
        return True, ""
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return False, str(exc)


def _download_timeout_seconds() -> float:
    try:
        return max(5.0, float(os.environ.get("DSO_DOUYIN_MEDIA_DOWNLOAD_TIMEOUT_SECONDS") or 35.0))
    except ValueError:
        return 35.0


def _close_tab_after_sample_enabled() -> bool:
    return str(os.environ.get("DSO_DOUYIN_MEDIA_CLOSE_TAB_AFTER_SAMPLE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _chrome_close_current_douyin_tab() -> None:
    script = """
tell application "Google Chrome"
  if (count of windows) is 0 then return "no_window"
  set t to active tab of front window
  if (URL of t) contains "douyin.com" then
    close t
    return "closed"
  end if
  return "not_douyin"
end tell
"""
    subprocess.run(["osascript"], input=script, text=True, capture_output=True, timeout=10, check=False)


def _directory_size(path: Path | None) -> int:
    if not path or not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _storage_over_limit(storage_root: Path | None, max_storage_bytes: int) -> bool:
    return bool(storage_root and max_storage_bytes and _directory_size(storage_root) >= max_storage_bytes)


def _ffprobe(path: Path) -> dict[str, Any]:
    try:
        text = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size:stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        return json.loads(text)
    except Exception as exc:
        return {"error": str(exc)}


def _extract_frame(video_path: Path, frame_path: Path) -> tuple[bool, str]:
    try:
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", str(video_path), "-frames:v", "1", str(frame_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0 and frame_path.exists() and frame_path.stat().st_size > 0:
            return True, ""
        return False, proc.stderr[-500:]
    except Exception as exc:
        return False, str(exc)


def _extract_audio(video_path: Path, audio_path: Path) -> tuple[bool, str]:
    try:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0 and audio_path.exists() and audio_path.stat().st_size > 0:
            return True, ""
        return False, proc.stderr[-500:]
    except Exception as exc:
        return False, str(exc)


def _summary(results: list[dict[str, Any]], *, source_plan: str, run_id: str, dry_run: bool) -> dict[str, Any]:
    summary = {
        "contract_version": MEDIA_COLLECTION_VERSION,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "source_plan": source_plan,
        "run_id": run_id,
        "dry_run": dry_run,
        "total": len(results),
        "success": sum(1 for row in results if row.get("status") == "success"),
        "partial": sum(1 for row in results if row.get("status") == "partial"),
        "failed": sum(1 for row in results if row.get("status") == "failed"),
        "planned": sum(1 for row in results if row.get("status") == "planned"),
        "video_downloaded": sum(1 for row in results if row.get("video_downloaded")),
        "cover_downloaded": sum(1 for row in results if row.get("cover_downloaded")),
        "frame_extracted": sum(1 for row in results if row.get("frame_extracted")),
        "audio_extracted": sum(1 for row in results if row.get("audio_extracted")),
        "by_account": _bucket_counts(results, "account_id"),
        "by_label": _bucket_counts(results, "performance_label"),
    }
    return summary


def _bucket_counts(results: list[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    bucket: dict[str, dict[str, int]] = {}
    for row in results:
        name = str(row.get(key) or "unknown")
        item = bucket.setdefault(name, {"total": 0, "success": 0, "video_downloaded": 0})
        item["total"] += 1
        item["success"] += int(row.get("status") == "success")
        item["video_downloaded"] += int(bool(row.get("video_downloaded")))
    return bucket


def _report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    rows = report.get("results") or []
    lines = [
        "# 抖音切片视频媒体采集报告",
        "",
        f"- 生成时间: {summary.get('generated_at') or ''}",
        f"- 样本总数: {summary.get('total') or 0}",
        f"- 成功: {summary.get('success') or 0}",
        f"- 部分成功: {summary.get('partial') or 0}",
        f"- 失败: {summary.get('failed') or 0}",
        f"- 下载到视频: {summary.get('video_downloaded') or 0}",
        f"- 抽帧成功: {summary.get('frame_extracted') or 0}",
        f"- 音频抽取成功: {summary.get('audio_extracted') or 0}",
        "",
        "| 顺序 | 账号 | 标签 | aweme_id | 状态 | 时长 | 大小MB | 抽帧 | 音频 | 错误 |",
        "| ---: | --- | --- | --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for row in rows:
        size_mb = round((row.get("file_size_bytes") or 0) / 1024 / 1024, 2)
        err = "; ".join(row.get("errors") or [])[:80]
        lines.append(
            f"| {row.get('collection_order') or 0} | {row.get('account_id') or ''} | "
            f"{row.get('performance_label') or ''} | {row.get('aweme_id') or ''} | "
            f"{row.get('status') or ''} | {round(row.get('duration_seconds') or 0, 2)} | "
            f"{size_mb} | {bool(row.get('frame_extracted'))} | {bool(row.get('audio_extracted'))} | {err} |"
        )
    return "\n".join(lines) + "\n"


def _report_suffix(*, stage: str | None, account: str | None, dry_run: bool) -> str:
    parts = [stage or "all"]
    if account:
        parts.append(account)
    if dry_run:
        parts.append("dry_run")
    return "_".join(_slug(part) for part in parts if part)


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("._-") or "all"


def _apple_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
