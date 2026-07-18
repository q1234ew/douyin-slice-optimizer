from __future__ import annotations

import importlib
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from dso.config import get_settings
from dso.media.ingest import ingest_video
from dso.utils import new_id, utc_now, write_json
from dso.versions import VIDEO_DOWNLOAD_CONTRACT_VERSION


VIDEODL_PROJECT_URL = "https://github.com/CharlesPikachu/videodl"
VIDEODL_VERSION = "0.9.1"
VIDEODL_PACKAGE_SPEC = f"videofetch=={VIDEODL_VERSION}"
VIDEODL_LICENSE = "PolyForm-Noncommercial-1.0.0"
VIDEODL_PROVIDER_SPECS = {
    "tencent": {
        "client": "TencentVideoClient",
        "module": "videodl.modules.sources.tencent",
        "hosts": ("v.qq.com", "wetv.vip", "iflix.com"),
    },
    "youtube": {
        "client": "YouTubeVideoClient",
        "module": "videodl.modules.sources.youtube",
        "hosts": ("youtube.com", "youtu.be"),
    },
}
VIDEODL_MAX_ITEMS = 20
VIDEODL_MAX_THREADS = 8
VIDEODL_YOUTUBE_MAX_HEIGHT = 720
VIDEODL_PROJECT_TEMP_PATH = Path("tmp") / "video_downloads"
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


class VideoDownloadError(RuntimeError):
    """Base error for the optional remote video downloader."""


class VideoDownloadPolicyError(VideoDownloadError):
    """Raised when a request violates the downloader safety contract."""


class VideoDownloadUnavailableError(VideoDownloadError):
    """Raised when the optional videodl runtime is not installed or loadable."""


def download_video_resource(
    url: str,
    *,
    account_id: str = "main",
    title: str | None = None,
    output_dir: str | Path | None = None,
    threads: int = 4,
    max_items: int = 1,
    ingest: bool = True,
    dry_run: bool = False,
    acknowledge_noncommercial: bool = False,
) -> dict[str, Any]:
    """Download a clear, authorized video and optionally enter normal ingest.

    The adapter intentionally uses only audited videodl provider paths. It does
    not pass cookies, credentials, proxies, generic-parser fallbacks, or YouTube
    third-party resolver services, and it rejects a selected format when upstream
    marks it as DRM-protected.
    """

    if not acknowledge_noncommercial:
        raise VideoDownloadPolicyError(
            "videodl uses the PolyForm-Noncommercial-1.0.0 license. "
            "For an eligible noncommercial experiment, rerun with "
            "--acknowledge-noncommercial. Commercial use requires separate "
            f"permission from the upstream project: {VIDEODL_PROJECT_URL}"
        )

    normalized_url, source_provider = _validate_url(url)
    threads = _bounded_int("threads", threads, minimum=1, maximum=VIDEODL_MAX_THREADS)
    max_items = _bounded_int("max_items", max_items, minimum=1, maximum=VIDEODL_MAX_ITEMS)
    client_class, upstream_version = _load_videodl_client(source_provider)
    provider_client = str(VIDEODL_PROVIDER_SPECS[source_provider]["client"])

    download_root = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else get_settings().data_dir / VIDEODL_PROJECT_TEMP_PATH
    )
    job_id = new_id("download")
    job_dir = download_root / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    request_overrides = {"timeout": (10, 120)}
    client = client_class(
        auto_set_proxies=False,
        random_update_ua=False,
        enable_parse_curl_cffi=True,
        enable_download_curl_cffi=True,
        max_retries=2,
        maintain_session=True,
        disable_print=True,
        work_dir=str(job_dir),
        default_search_cookies={},
        default_download_cookies={},
        default_parse_cookies={},
    )

    started_at = utc_now()
    started_clock = time.monotonic()
    parsed_infos = list(client.parsefromurl(normalized_url, request_overrides=request_overrides) or [])
    valid_infos = [info for info in parsed_infos if bool(getattr(info, "with_valid_download_url", False))]
    parse_errors = [str(getattr(info, "err_msg", "") or "") for info in parsed_infos]
    parse_errors = [error for error in parse_errors if error]
    if not valid_infos:
        detail = f" Upstream detail: {'; '.join(parse_errors)}" if parse_errors else ""
        raise VideoDownloadError(
            "videodl did not return a downloadable clear-media result for this URL."
            f"{detail} No generic parser or credential fallback was attempted."
        )
    if len(valid_infos) > max_items:
        raise VideoDownloadPolicyError(
            f"URL resolved to {len(valid_infos)} items, above max_items={max_items}. "
            "Use an episode URL or raise --max-items explicitly."
        )

    candidates = [_candidate_summary(info) for info in valid_infos]
    drm_candidates = [candidate for candidate in candidates if candidate["has_drm"] is True]
    if drm_candidates:
        identifiers = ", ".join(str(item.get("identifier") or "unknown") for item in drm_candidates)
        raise VideoDownloadPolicyError(
            f"Refusing DRM-protected videodl result(s): {identifiers}. "
            "This adapter does not decrypt or bypass protected media."
        )

    base_result: dict[str, Any] = {
        "contract_version": VIDEO_DOWNLOAD_CONTRACT_VERSION,
        "status": "parsed" if dry_run else "downloading",
        "job_id": job_id,
        "provider": "videodl",
        "source_provider": source_provider,
        "provider_client": provider_client,
        "provider_version": upstream_version,
        "provider_license": VIDEODL_LICENSE,
        "source_url": normalized_url,
        "output_dir": str(job_dir),
        "started_at": started_at,
        "dry_run": bool(dry_run),
        "ingest_requested": bool(ingest),
        "policy": {
            "noncommercial_acknowledged": True,
            "cookies_used": False,
            "generic_parsers_enabled": False,
            "third_party_parsers_enabled": False,
            "playlist_expansion_enabled": False,
            "drm_allowed": False,
            "youtube_max_height": (
                VIDEODL_YOUTUBE_MAX_HEIGHT if source_provider == "youtube" else None
            ),
        },
        "candidates": candidates,
        "files": [],
        "ingested_videos": [],
    }
    if dry_run:
        base_result["elapsed_seconds"] = round(time.monotonic() - started_clock, 3)
        base_result["completed_at"] = utc_now()
        _write_manifest(job_dir, base_result)
        return base_result

    downloaded_infos = list(
        client.download(
            valid_infos,
            num_threadings=threads,
            request_overrides=request_overrides,
        )
        or []
    )
    downloaded_results = _downloaded_results(downloaded_infos, job_dir=job_dir)
    if not downloaded_results:
        raise VideoDownloadError(
            f"videodl completed without a non-empty media file in {job_dir}. "
            "Inspect network access and FFmpeg availability, then retry."
        )

    ingested_rows: list[dict[str, Any]] = []
    if ingest:
        for index, (info, file_path) in enumerate(downloaded_results, start=1):
            upstream_title = str(getattr(info, "title", "") or file_path.stem)
            ingest_title = title or upstream_title
            if title and len(downloaded_results) > 1:
                ingest_title = f"{title} #{index}"
            ingested_rows.append(ingest_video(file_path, account_id=account_id, title=ingest_title))

    base_result.update(
        {
            "status": "completed",
            "files": [
                {"path": str(path), "name": path.name, "size_bytes": path.stat().st_size}
                for _, path in downloaded_results
            ],
            "ingested_videos": ingested_rows,
            "elapsed_seconds": round(time.monotonic() - started_clock, 3),
            "completed_at": utc_now(),
        }
    )
    _write_manifest(job_dir, base_result)
    return base_result


def _load_videodl_client(source_provider: str) -> tuple[type[Any], str]:
    spec = VIDEODL_PROVIDER_SPECS.get(source_provider)
    if spec is None:
        raise VideoDownloadPolicyError(f"Unsupported video provider: {source_provider!r}.")
    try:
        package = importlib.import_module("videodl")
        module = importlib.import_module(str(spec["module"]))
        client_class = getattr(module, str(spec["client"]))
    except Exception as exc:
        raise VideoDownloadUnavailableError(
            "The optional videodl runtime is unavailable. Install it with "
            f"python3 -m pip install -e '.[videodl]' (package {VIDEODL_PACKAGE_SPEC}). "
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc
    upstream_version = str(getattr(package, "__version__", "unknown") or "unknown")
    if upstream_version != VIDEODL_VERSION:
        raise VideoDownloadUnavailableError(
            f"Installed videofetch version {upstream_version!r} is not the audited "
            f"version {VIDEODL_VERSION!r}. Install {VIDEODL_PACKAGE_SPEC} before retrying."
        )
    if source_provider == "youtube":
        client_class = _build_audited_youtube_client(client_class)
    return client_class, upstream_version


def _load_videodl_tencent_client() -> tuple[type[Any], str]:
    """Compatibility wrapper retained for direct callers of the first adapter."""

    return _load_videodl_client("tencent")


def _build_audited_youtube_client(base_client_class: type[Any]) -> type[Any]:
    try:
        youtube_utils = importlib.import_module("videodl.modules.utils.youtubeutils")
        data_utils = importlib.import_module("videodl.modules.utils")
        youtube_class = getattr(youtube_utils, "YouTube")
        video_info_class = getattr(data_utils, "VideoInfo")
        legalize_string = getattr(data_utils, "legalizestring")
    except Exception as exc:
        raise VideoDownloadUnavailableError(
            "The audited YouTube portion of videodl could not be loaded. "
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc

    class AuditedYouTubeVideoClient(base_client_class):
        """YouTube client restricted to videodl's direct YouTube API utility."""

        def parsefromurl(
            self,
            url: str,
            request_overrides: dict[str, Any] | None = None,
        ) -> list[Any]:
            del request_overrides  # The direct utility does not use requests kwargs.
            parsed = urlsplit(url)
            video_id = (parse_qs(parsed.query, keep_blank_values=True).get("v") or [""])[0]
            info = video_info_class(source=self.source)
            try:
                youtube = youtube_class(video_id=video_id)
                raw_data = youtube.vid_info
                streams = list(youtube.streams.all())
                video_stream, audio_stream = _select_youtube_streams(streams)
                if video_stream is None:
                    raise ValueError("no compatible clear MP4 stream at or below 720p")

                title = legalize_string(
                    youtube.title,
                    replace_null_string=f"YouTube-{video_id}",
                ).removesuffix(".")
                selection = _youtube_stream_selection(video_stream, audio_stream)
                if isinstance(raw_data, dict):
                    raw_data = dict(raw_data)
                    raw_data["_dso_selection"] = selection

                source_dir = Path(self.work_dir) / self.source
                update: dict[str, Any] = {
                    "raw_data": raw_data if isinstance(raw_data, dict) else {},
                    "title": title,
                    "identifier": video_id,
                    "cover_url": _youtube_cover_url(raw_data),
                    "download_url": str(video_stream.url),
                    "ext": "mp4",
                    "save_path": str(source_dir / f"{title}.mp4"),
                    "default_download_headers": self.default_download_headers,
                    "dso_selected_format": selection,
                }
                if audio_stream is not None:
                    update.update(
                        {
                            "audio_download_url": str(audio_stream.url),
                            "audio_ext": "m4a",
                            "audio_save_path": str(source_dir / f"{title}.audio.m4a"),
                            "default_audio_download_headers": self.default_download_headers,
                        }
                    )
                info.update(update)
            except Exception as exc:
                info.update(
                    {
                        "err_msg": (
                            f"{self.source}.parsefromurl >>> {url} "
                            f"(Error: {type(exc).__name__}: {exc})"
                        )
                    }
                )
            return [info]

    AuditedYouTubeVideoClient.__name__ = str(
        VIDEODL_PROVIDER_SPECS["youtube"]["client"]
    )
    return AuditedYouTubeVideoClient


def _select_youtube_streams(streams: list[Any]) -> tuple[Any | None, Any | None]:
    video_candidates = [
        stream
        for stream in streams
        if bool(getattr(stream, "includesvideotrack", False))
        and not bool(getattr(stream, "includesaudiotrack", False))
        and str(getattr(stream, "subtype", "")).lower() == "mp4"
        and 0 < _resolution_height(getattr(stream, "resolution", None))
        <= VIDEODL_YOUTUBE_MAX_HEIGHT
        and not bool(getattr(stream, "issabr", False))
        and str(getattr(stream, "url", "")).startswith("https://")
    ]
    if video_candidates:
        video_stream = max(
            video_candidates,
            key=lambda stream: (
                _resolution_height(getattr(stream, "resolution", None)),
                str(getattr(stream, "video_codec", "")).startswith("avc1"),
                int(getattr(stream, "fps", 0) or 0),
                int(getattr(stream, "bitrate", 0) or 0),
            ),
        )
        audio_candidates = [
            stream
            for stream in streams
            if bool(getattr(stream, "includesaudiotrack", False))
            and not bool(getattr(stream, "includesvideotrack", False))
            and str(getattr(stream, "subtype", "")).lower() == "mp4"
            and not bool(getattr(stream, "is_drc", False))
            and not bool(getattr(stream, "issabr", False))
            and str(getattr(stream, "url", "")).startswith("https://")
        ]
        audio_stream = (
            max(
                audio_candidates,
                key=lambda stream: (
                    bool(getattr(stream, "is_default_audio_track", False)),
                    int(getattr(stream, "bitrate", 0) or 0),
                ),
            )
            if audio_candidates
            else None
        )
        if audio_stream is not None:
            return video_stream, audio_stream

    progressive_candidates = [
        stream
        for stream in streams
        if bool(getattr(stream, "includesvideotrack", False))
        and bool(getattr(stream, "includesaudiotrack", False))
        and str(getattr(stream, "subtype", "")).lower() == "mp4"
        and 0 < _resolution_height(getattr(stream, "resolution", None))
        <= VIDEODL_YOUTUBE_MAX_HEIGHT
        and not bool(getattr(stream, "issabr", False))
        and str(getattr(stream, "url", "")).startswith("https://")
    ]
    if not progressive_candidates:
        return None, None
    return (
        max(
            progressive_candidates,
            key=lambda stream: (
                _resolution_height(getattr(stream, "resolution", None)),
                int(getattr(stream, "fps", 0) or 0),
                int(getattr(stream, "bitrate", 0) or 0),
            ),
        ),
        None,
    )


def _resolution_height(resolution: Any) -> int:
    match = re.fullmatch(r"(\d+)p", str(resolution or "").lower())
    return int(match.group(1)) if match else 0


def _youtube_stream_selection(video_stream: Any, audio_stream: Any | None) -> dict[str, Any]:
    duration_ms = _safe_stream_attr(video_stream, "durationMs")
    try:
        duration_seconds = round(float(duration_ms) / 1000, 3)
    except (TypeError, ValueError):
        duration_seconds = None
    return {
        "resolution": str(getattr(video_stream, "resolution", "") or ""),
        "duration_seconds": duration_seconds,
        "video_itag": getattr(video_stream, "itag", None),
        "video_codec": str(getattr(video_stream, "video_codec", "") or ""),
        "video_fps": getattr(video_stream, "fps", None),
        "video_size_bytes": _safe_stream_attr(video_stream, "filesize"),
        "audio_itag": getattr(audio_stream, "itag", None) if audio_stream is not None else None,
        "audio_codec": (
            str(getattr(audio_stream, "audio_codec", "") or "")
            if audio_stream is not None
            else None
        ),
        "audio_bitrate": getattr(audio_stream, "bitrate", None) if audio_stream is not None else None,
        "audio_size_bytes": (
            _safe_stream_attr(audio_stream, "filesize") if audio_stream is not None else None
        ),
        "separate_audio": audio_stream is not None,
    }


def _safe_stream_attr(stream: Any, name: str) -> Any:
    try:
        return getattr(stream, name, None)
    except Exception:
        return None


def _youtube_cover_url(raw_data: Any) -> str | None:
    try:
        thumbnails = raw_data["videoDetails"]["thumbnail"]["thumbnails"]
        return str(thumbnails[-1]["url"]) if thumbnails else None
    except (KeyError, IndexError, TypeError):
        return None


def _validate_url(url: str) -> tuple[str, str]:
    candidate = str(url or "").strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise VideoDownloadPolicyError("Only absolute HTTPS video URLs are accepted.")
    if parsed.username or parsed.password:
        raise VideoDownloadPolicyError("Credentials embedded in video URLs are not accepted.")
    host = parsed.hostname.lower().rstrip(".")
    source_provider = next(
        (
            provider
            for provider, spec in VIDEODL_PROVIDER_SPECS.items()
            if any(_host_matches(host, allowed) for allowed in spec["hosts"])
        ),
        None,
    )
    if source_provider is None:
        allowed_hosts = sorted(
            {str(host) for spec in VIDEODL_PROVIDER_SPECS.values() for host in spec["hosts"]}
        )
        raise VideoDownloadPolicyError(
            "This audited adapter supports only these video hosts: "
            + ", ".join(allowed_hosts)
        )
    if source_provider == "youtube":
        return _normalize_youtube_url(parsed, host), source_provider
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, "")), source_provider


def _host_matches(host: str, allowed: Any) -> bool:
    allowed_host = str(allowed)
    return host == allowed_host or host.endswith(f".{allowed_host}")


def _normalize_youtube_url(parsed: Any, host: str) -> str:
    path_parts = [part for part in parsed.path.split("/") if part]
    if _host_matches(host, "youtu.be"):
        video_id = path_parts[0] if path_parts else ""
    elif parsed.path.rstrip("/") == "/watch":
        video_id = (parse_qs(parsed.query, keep_blank_values=True).get("v") or [""])[0]
    elif len(path_parts) >= 2 and path_parts[0] in {"embed", "shorts", "live"}:
        video_id = path_parts[1]
    else:
        raise VideoDownloadPolicyError(
            "YouTube input must be a single watch, youtu.be, shorts, live, or embed video URL; "
            "playlist and channel URLs are not accepted."
        )
    if not YOUTUBE_VIDEO_ID_PATTERN.fullmatch(video_id):
        raise VideoDownloadPolicyError("The YouTube URL does not contain a valid single-video ID.")
    return urlunsplit(("https", "www.youtube.com", "/watch", urlencode({"v": video_id}), ""))


def _bounded_int(name: str, value: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise VideoDownloadPolicyError(f"{name} must be an integer.") from exc
    if parsed < minimum or parsed > maximum:
        raise VideoDownloadPolicyError(f"{name} must be between {minimum} and {maximum}.")
    return parsed


def _candidate_summary(info: Any) -> dict[str, Any]:
    raw_data = getattr(info, "raw_data", {})
    return {
        "identifier": str(getattr(info, "identifier", "") or ""),
        "title": str(getattr(info, "title", "") or ""),
        "source": str(getattr(info, "source", "") or ""),
        "extension": str(getattr(info, "ext", "") or ""),
        "has_drm": _selected_format_has_drm(info, raw_data),
        "selected_format": getattr(info, "dso_selected_format", None),
    }


def _selected_format_has_drm(info: Any, raw_data: Any) -> bool | None:
    selected_url = str(getattr(info, "download_url", "") or "")
    formats = raw_data.get("formats") if isinstance(raw_data, dict) else None
    if isinstance(formats, list):
        selected_matches = [
            item
            for item in formats
            if isinstance(item, dict) and str(item.get("url") or "") == selected_url
        ]
        if selected_matches:
            return any(bool(item.get("has_drm")) for item in selected_matches)
        if any(bool(item.get("has_drm")) for item in formats if isinstance(item, dict)):
            return True
    explicit_values = _find_key_values(raw_data, "has_drm")
    if explicit_values:
        return any(bool(value) for value in explicit_values)
    return None


def _find_key_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if child_key == key:
                found.append(child_value)
            else:
                found.extend(_find_key_values(child_value, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_key_values(child, key))
    return found


def _downloaded_results(downloaded_infos: list[Any], *, job_dir: Path) -> list[tuple[Any, Path]]:
    files: list[tuple[Any, Path]] = []
    resolved_job_dir = job_dir.resolve()
    for info in downloaded_infos:
        path = Path(str(getattr(info, "save_path", "") or "")).expanduser().resolve()
        if (
            path.is_relative_to(resolved_job_dir)
            and path.is_file()
            and path.stat().st_size > 0
        ):
            files.append((info, path))
    return files


def _write_manifest(job_dir: Path, payload: dict[str, Any]) -> None:
    manifest_path = job_dir / "dso-download-manifest.json"
    payload["manifest_path"] = str(manifest_path)
    write_json(manifest_path, payload)
