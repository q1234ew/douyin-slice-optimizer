from __future__ import annotations

import json
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover - exercised only when requests is unavailable.
    requests = None  # type: ignore[assignment]

from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one
from dso.learning.semantic_labels import SEMANTIC_ENUMS, normalize_semantic_field, semantic_label_catalog
from dso.learning.slice_structure_evaluator import evaluate_slice_structure_row
from dso.learning.multimodal_validation import _build_asset_index, _prepare_row
from dso.media.ffmpeg import probe_video, require_binary
from dso.utils import read_json, run_cmd, utc_now, write_json
from dso.versions import QWEN_OMNI_VERSION


QWEN_OMNI_MODEL = "Qwen/Qwen2.5-Omni-7B-GPTQ-Int4"
DEFAULT_OMNI_SERVICE_URL = "http://192.168.31.143:8001"
DEFAULT_MAX_CLIP_SECONDS = 15.0
GPTQ_INT4_15S_MIN_VRAM_GB = 11.64
GPTQ_INT4_30S_MIN_VRAM_GB = 17.43
BF16_15S_MIN_VRAM_GB = 31.11
VRAM_SAFETY_MULTIPLIER = 1.2
OMNI_SEMANTIC_SCHEMA_VERSION = "qwen_omni_semantic_schema.v3"
OMNI_SEMANTIC_NORMALIZER_VERSION = "qwen_omni_semantic_normalizer.v3"
OMNI_CORE_FIELDS = ("content_category", "hook_type", "slice_structure")
OMNI_ENTITY_FIELDS = ("artist_names", "song_title", "tags")
OMNI_AUX_FIELDS = ("domain_category", "material_type", "program_context", "presentation_style")
OMNI_UNKNOWN_VALUES = {"", "unknown", "none", "null", "其他", "其它", "未知", "historical_sample", "video"}

OMNI_DOMAIN_CATEGORIES: dict[str, str] = {
    "unknown": "未知",
    "music_variety": "音乐/音综领域",
    "entertainment": "泛娱乐领域",
    "drama_film": "影视剧情领域",
    "lifestyle": "生活方式领域",
    "sports_entertainment": "体育娱乐领域",
    "creative_ai": "AI 创作领域",
    "commercial": "商业带货领域",
}

OMNI_MATERIAL_TYPES: dict[str, str] = {
    "unknown": "未知",
    "performance_clip": "舞台/演唱片段",
    "performance_highlight": "舞台高光",
    "reaction": "反应/Reaction",
    "commentary": "解读评论",
    "judge_comment": "评委点评",
    "compilation": "合集盘点",
    "vocal_teaching": "声乐教学",
    "program_context": "节目语境",
    "behind_the_scenes": "幕后花絮",
    "entertainment_news": "娱乐资讯",
    "humor_entertainment": "幽默娱乐",
    "drama_film": "影视剧情",
    "life_emotion": "生活情感",
    "lifestyle": "生活方式",
    "creative_ai": "AI 创作",
    "commercial": "商业带货",
}

OMNI_PRESENTATION_STYLES: dict[str, str] = {
    "unknown": "未知",
    "direct_cam": "直拍",
    "analysis": "解析",
    "reaction_review": "Reaction/复盘",
    "listicle": "盘点",
    "vocal_lesson": "教学",
    "a_cappella": "清唱",
    "program_clip": "节目片段",
    "behind_scene": "幕后",
}

OMNI_PROGRAM_CONTEXTS = {
    "天赐的声音",
    "歌手2025",
    "歌手2026",
    "乘风2026",
    "乘风",
    "国乐无双",
    "声生不息",
    "有歌第二季",
    "魔力歌先生",
    "超燃青春的合唱",
}

OMNI_ANNOTATION_FIELD_GUIDES: dict[str, dict[str, Any]] = {
    "content_category": {
        "label_zh": "内容类别",
        "short_label_zh": "类别",
        "description_zh": "判断这条样本主要属于哪类内容主题，例如舞台片段、Reaction、娱乐资讯或合集盘点。",
        "annotation_hint_zh": "优先看标题、画面主体、话题标签和视频实际内容；不确定时保留 unknown，不要为了覆盖率硬填。",
    },
    "hook_type": {
        "label_zh": "开头钩子",
        "short_label_zh": "Hook",
        "description_zh": "判断吸引用户停留的第一触发点，例如高音爆点、艺人组合、情绪故事或专业解读。",
        "annotation_hint_zh": "重点看开头 1-3 秒和标题最强调的卖点；如果只是普通舞台而无明确钩子，选 unknown。",
    },
    "slice_structure": {
        "label_zh": "切片结构",
        "short_label_zh": "结构",
        "description_zh": "判断短视频的叙事组织方式，例如高潮先行、铺垫到爆点、反应先行或线性叙事。",
        "annotation_hint_zh": "需要结合视频顺序判断；只有标题信息不足时不要强行推断结构。",
    },
    "artist_names": {
        "label_zh": "艺人/人物",
        "short_label_zh": "艺人",
        "description_zh": "记录样本中明确出现或被标题可靠提到的歌手、艺人、评委或主要人物。",
        "annotation_hint_zh": "多个名称用逗号分隔；不要把账号名、泛称或不确定人物写入。",
    },
    "song_title": {
        "label_zh": "歌曲名",
        "short_label_zh": "歌名",
        "description_zh": "记录样本中明确出现的歌曲、舞台曲目或被讨论的作品名。",
        "annotation_hint_zh": "没有可靠歌名时留空或 unknown；不要把节目名误填成歌曲名。",
    },
    "tags": {
        "label_zh": "证据标签",
        "short_label_zh": "标签",
        "description_zh": "记录能解释判断依据的短标签，例如高音、直拍、声乐教学、节目名或热点事件。",
        "annotation_hint_zh": "只保留有证据的关键词，多个标签用逗号分隔。",
    },
    "domain_category": {
        "label_zh": "领域分类",
        "short_label_zh": "领域",
        "description_zh": "判断内容所属业务领域，例如音乐综艺、泛娱乐、影视剧情或生活方式。",
        "annotation_hint_zh": "这是“内容所在领域”，不是素材形态；音乐综艺里的点评、Reaction、直拍都应优先归到 music_variety。",
    },
    "material_type": {
        "label_zh": "素材形态",
        "short_label_zh": "形态",
        "description_zh": "判断这条视频素材的呈现形态，例如舞台演唱片段、Reaction、声乐教学、解读评论或合集盘点。",
        "annotation_hint_zh": "这是“这条素材长什么样/怎么表达”，不要和领域分类混用；同属音乐综艺时也要区分演唱、点评、教学、盘点。",
    },
    "program_context": {
        "label_zh": "节目语境",
        "short_label_zh": "节目",
        "description_zh": "记录可可靠识别的节目、赛段或上下文，例如天赐的声音、歌手2026、乘风2026。",
        "annotation_hint_zh": "只有标题、画面、字幕或标签能明确支持时填写；不确定时保留 unknown。",
    },
    "presentation_style": {
        "label_zh": "呈现方式",
        "short_label_zh": "呈现",
        "description_zh": "判断视频表层表达方式，例如直拍、解析、Reaction复盘、盘点、教学、清唱或节目片段。",
        "annotation_hint_zh": "这是比素材形态更细的呈现手法，用来解释为什么同一素材形态会有不同表现。",
    },
    "material_label_verified": {
        "label_zh": "形态标注确认",
        "short_label_zh": "确认",
        "description_zh": "标记本条 material gold set 是否已经人工确认，不等同于主语义标签 manual_verified。",
        "annotation_hint_zh": "只有人工保存过 material gold set 后才能设为 true；系统推断结果不得自动冒充人工确认。",
    },
}


def omni_annotation_field_guides(fields: list[str] | tuple[str, ...] | None = None) -> dict[str, dict[str, Any]]:
    selected = list(fields or OMNI_ANNOTATION_FIELD_GUIDES.keys())
    result: dict[str, dict[str, Any]] = {}
    for field in selected:
        guide = dict(OMNI_ANNOTATION_FIELD_GUIDES.get(field) or {})
        if not guide:
            continue
        guide["field"] = field
        values = _annotation_allowed_values(field)
        if values:
            guide["allowed_values"] = values
        result[field] = guide
    return result


def _annotation_allowed_values(field: str) -> list[dict[str, str]]:
    if field in SEMANTIC_ENUMS:
        return [{"value": value, "label_zh": label} for value, label in SEMANTIC_ENUMS[field].items()]
    if field == "domain_category":
        return [{"value": value, "label_zh": label} for value, label in OMNI_DOMAIN_CATEGORIES.items()]
    if field == "material_type":
        return [{"value": value, "label_zh": label} for value, label in OMNI_MATERIAL_TYPES.items()]
    if field == "presentation_style":
        return [{"value": value, "label_zh": label} for value, label in OMNI_PRESENTATION_STYLES.items()]
    return []

OMNI_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "content_category": {
        "entertainment": "entertainment_news",
        "娱乐": "entertainment_news",
        "celebrity": "entertainment_news",
        "celebrity_pairing": "entertainment_news",
        "明星": "entertainment_news",
        "艺人": "entertainment_news",
        "综艺": "music_variety",
        "音乐类": "music_variety",
        "音乐": "music_variety",
        "音乐现场": "performance_clip",
        "音乐教学": "commentary",
        "影视": "drama_film",
        "影视娱乐": "drama_film",
        "emotion": "life_emotion",
        "healthcare": "lifestyle",
    },
    "hook_type": {
        "lyric": "emotional_story",
        "歌词": "emotional_story",
        "金句": "emotional_story",
        "celebrity": "celebrity_pairing",
        "明星": "celebrity_pairing",
        "艺人": "celebrity_pairing",
        "music": "music_burst",
        "音乐": "music_burst",
        "entertainment": "topical_hook",
        "娱乐": "topical_hook",
        "highlight": "music_burst",
        "高光": "music_burst",
        "interview": "expert_comment",
        "访谈": "expert_comment",
        "音乐分析": "expert_comment",
        "音乐解析": "expert_comment",
        "音乐评论": "expert_comment",
        "音乐专业解析": "expert_comment",
        "歌手分析": "expert_comment",
        "评论与分析": "expert_comment",
        "教学": "expert_comment",
        "challenge": "topical_hook",
        "话题讨论": "topical_hook",
        "问题提出": "topical_hook",
        "event": "topical_hook",
        "emotional": "emotional_story",
        "emotion": "emotional_story",
        "梦幻联动": "celebrity_pairing",
        "live_performance": "music_burst",
        "现场表演": "music_burst",
        "music_performance": "music_burst",
        "音乐现场": "music_burst",
    },
    "slice_structure": {
        "highlight": "pure_highlight",
        "高光": "pure_highlight",
        "上部分": "context_first",
        "introduction_performance_reaction": "setup_to_payoff",
        "introduction-performance-commentary": "context_first",
        "single": "pure_highlight",
        "单个片段": "pure_highlight",
        "单曲": "pure_highlight",
        "single_slice": "pure_highlight",
        "单人表演": "pure_highlight",
        "introduction": "context_first",
        "问题提出": "context_first",
        "评论与分析": "context_first",
        "分析": "context_first",
        "分析与评论": "context_first",
        "interview": "context_first",
        "reaction": "reaction_first",
    },
}

OMNI_CONTEXT_FIRST_VALUES: dict[str, set[str]] = {
    "content_category": {
        "entertainment",
        "娱乐",
        "celebrity",
        "celebrity_pairing",
        "明星",
        "艺人",
        "historical_sample",
        "video",
    },
    "hook_type": {
        "highlight",
        "高光",
        "lyric",
        "歌词",
        "金句",
        "celebrity",
        "明星",
        "艺人",
        "music",
        "音乐",
        "entertainment",
        "娱乐",
        "event",
        "interview",
        "访谈",
    },
}


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


class QwenOmniClient:
    def __init__(
        self,
        service_url: str | None = None,
        *,
        model_id: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.service_url = (
            service_url
            or os.environ.get("DSO_QWEN_OMNI_SERVICE_URL")
            or os.environ.get("DSO_OMNI_SERVICE_URL")
            or os.environ.get("DSO_EMBEDDING_SERVICE_URL")
            or DEFAULT_OMNI_SERVICE_URL
        ).rstrip("/")
        self.model_id = model_id or os.environ.get("DSO_QWEN_OMNI_MODEL") or QWEN_OMNI_MODEL
        self.timeout_seconds = float(os.environ.get("DSO_QWEN_OMNI_TIMEOUT_SECONDS") or timeout_seconds or 60.0)
        self._requests_session = None
        if requests is not None:
            self._requests_session = requests.Session()
            self._requests_session.trust_env = _env_truthy("DSO_QWEN_OMNI_TRUST_ENV_PROXY")

    def health(self) -> dict:
        try:
            payload = self._json_request("GET", "/health", timeout_seconds=min(self.timeout_seconds, 8.0))
            return _service_health(payload, self.service_url)
        except Exception as exc:
            return {"status": "service_unavailable", "service_url": self.service_url, "error": str(exc)}

    def load(self, *, model_id: str | None = None, max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS) -> dict:
        payload = {
            "model": model_id or self.model_id,
            "model_id": model_id or self.model_id,
            "profile": "low_vram",
            "low_vram": True,
            "return_audio": False,
            "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        }
        try:
            data = self._json_request("POST", "/load", payload, timeout_seconds=max(self.timeout_seconds, 120.0))
            return _service_health(data, self.service_url)
        except Exception as exc:
            return {"status": "service_unavailable", "service_url": self.service_url, "error": str(exc)}

    def analyze_clip(self, payload: dict) -> dict:
        return self._json_request("POST", "/analyze/clip", payload, timeout_seconds=max(self.timeout_seconds, 120.0))

    def analyze_clip_file(self, payload: dict, video_path: str | Path) -> dict:
        return self._multipart_request(
            "/analyze/clip-file",
            payload,
            Path(video_path),
            timeout_seconds=max(self.timeout_seconds, 180.0),
        )

    def _json_request(self, method: str, path: str, payload: dict | None = None, *, timeout_seconds: float | None = None) -> dict:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if shutil.which("curl"):
            return self._curl_json_request(method, path, body, timeout_seconds=timeout_seconds)
        request = urllib.request.Request(
            f"{self.service_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        with urllib.request.urlopen(request, timeout=float(timeout_seconds or self.timeout_seconds)) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw or "{}")

    def _curl_json_request(self, method: str, path: str, body: bytes | None, *, timeout_seconds: float | None = None) -> dict:
        timeout = max(1.0, float(timeout_seconds or self.timeout_seconds))
        command = [
            "curl",
            "-sS",
            "--connect-timeout",
            str(min(3.0, timeout)),
            "--max-time",
            str(timeout),
            "-w",
            "\n%{http_code}",
            "-X",
            method,
        ]
        if body is not None:
            command.extend(["-H", "Content-Type: application/json", "--data-binary", "@-"])
        command.append(f"{self.service_url}{path}")
        result = subprocess.run(command, input=body, capture_output=True, check=False)
        output = result.stdout.decode("utf-8", errors="replace")
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or f"curl_exit_{result.returncode}")
        if "\n" not in output:
            raise RuntimeError("curl_response_missing_status")
        raw, status_code = output.rsplit("\n", 1)
        try:
            code = int(status_code)
        except ValueError as exc:
            raise RuntimeError(f"curl_response_bad_status:{status_code}") from exc
        if code >= 400:
            raise RuntimeError(f"HTTP Error {code}: {raw[:240]}")
        return json.loads(raw or "{}")

    def _multipart_request(self, path: str, payload: dict, video_path: Path, *, timeout_seconds: float | None = None) -> dict:
        if not video_path.is_file():
            raise FileNotFoundError(str(video_path))
        timeout = max(1.0, float(timeout_seconds or self.timeout_seconds))
        metadata = json.dumps(payload or {}, ensure_ascii=False)
        content_type = mimetypes.guess_type(video_path.name)[0] or "application/octet-stream"
        if self._requests_session is not None:
            with video_path.open("rb") as handle:
                response = self._requests_session.post(
                    f"{self.service_url}{path}",
                    data={"metadata": metadata},
                    files={"video": (video_path.name, handle, content_type)},
                    timeout=timeout,
                )
            try:
                response.raise_for_status()
            except Exception as exc:
                raise RuntimeError(f"HTTP Error {response.status_code}: {response.text[:500]}") from exc
            if not response.content:
                return {}
            return response.json()
        boundary = f"----dso-qwen-omni-{hashlib.sha256(os.urandom(16)).hexdigest()[:16]}"
        chunks: list[bytes] = []
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(b'Content-Disposition: form-data; name="metadata"\r\n')
        chunks.append(b"Content-Type: application/json; charset=utf-8\r\n\r\n")
        chunks.append(metadata.encode("utf-8"))
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="video"; filename="{video_path.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(video_path.read_bytes())
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        request = urllib.request.Request(
            f"{self.service_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw or "{}")


def qwen_omni_status(client: QwenOmniClient | None = None) -> dict:
    client = client or QwenOmniClient()
    health = client.health()
    raw = _raw_health(health)
    loaded_model = _loaded_model_id(raw)
    gate = _resource_gate(raw)
    loaded_omni = _is_omni_model(loaded_model)
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": _deployment_status(health, gate, loaded_omni),
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "service_url": getattr(client, "service_url", ""),
        "service_status": health,
        "resource_gate": gate,
        "loaded_model": loaded_model,
        "loaded_omni": loaded_omni,
        "model_switch_required": bool(loaded_model and not loaded_omni),
        "mode": "shadow",
        "limits": {
            "default_max_clip_seconds": DEFAULT_MAX_CLIP_SECONDS,
            "batch_size": 1,
            "return_audio": False,
            "writes_labels": False,
            "production_weight": False,
        },
        "recommendations": _status_recommendations(health, gate, loaded_omni),
        "generated_at": utc_now(),
    }


def analyze_candidate_with_qwen_omni(
    segment_id: str,
    *,
    account_id: str | None = None,
    max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS,
    load_model: bool = False,
    client: QwenOmniClient | None = None,
) -> dict:
    row = _candidate_row(segment_id)
    if not row:
        raise KeyError(f"segment not found: {segment_id}")
    if account_id:
        row["account_id"] = account_id
    client = client or QwenOmniClient()
    duration = _duration_seconds(row)
    if duration > float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS):
        return _skipped_payload(
            entity_type="candidate",
            entity_id=segment_id,
            reason="clip_too_long_for_low_vram",
            duration_seconds=duration,
            max_clip_seconds=max_clip_seconds,
            client=client,
        )
    service_status = client.load(max_clip_seconds=max_clip_seconds) if load_model else client.health()
    if not _service_ready(service_status):
        return _service_unavailable_payload(
            entity_type="candidate",
            entity_id=segment_id,
            service_status=service_status,
            client=client,
        )
    if not _service_loaded_omni(service_status):
        return _model_switch_required_payload(
            entity_type="candidate",
            entity_id=segment_id,
            service_status=service_status,
            client=client,
        )
    payload = _candidate_payload(row, max_clip_seconds=max_clip_seconds, model_id=getattr(client, "model_id", QWEN_OMNI_MODEL))
    raw = client.analyze_clip(payload)
    return _analysis_payload(
        entity_type="candidate",
        entity_id=segment_id,
        row=row,
        raw=raw,
        service_status=service_status,
        client=client,
        max_clip_seconds=max_clip_seconds,
    )


def run_qwen_omni_shadow(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 20,
    max_clip_seconds: float = DEFAULT_MAX_CLIP_SECONDS,
    load_model: bool = False,
    use_media: bool = False,
    allow_windowed_clips: bool = False,
    visual_ready_only: bool = False,
    client: QwenOmniClient | None = None,
) -> dict:
    client = client or QwenOmniClient()
    requested_limit = max(1, int(limit or 20))
    rows = _historical_rows(
        account_id=account_id,
        dataset_id=dataset_id,
        limit=0 if use_media or visual_ready_only else requested_limit,
    )
    asset_index = _build_asset_index() if use_media or visual_ready_only else {}
    if asset_index:
        rows = [_prepare_row(row, asset_index=asset_index) for row in rows]
    if visual_ready_only:
        rows = [
            row
            for row in rows
            if (row.get("assets") or {}).get("ready_for_multimodal")
            and (row.get("assets") or {}).get("video")
        ]
    rows = rows[:requested_limit]
    service_status = client.load(max_clip_seconds=max_clip_seconds) if load_model else client.health()
    if not _service_ready(service_status):
        return {
            "contract_version": QWEN_OMNI_VERSION,
            "status": "service_unavailable",
            "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
            "mode": "shadow",
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "sample_count": len(rows),
            "analyzed_count": 0,
            "skipped_count": 0,
            "query": _shadow_query(
                account_id=account_id,
                dataset_id=dataset_id,
                limit=requested_limit,
                max_clip_seconds=max_clip_seconds,
                use_media=use_media,
                allow_windowed_clips=allow_windowed_clips,
                visual_ready_only=visual_ready_only,
            ),
            "service_status": service_status,
            "recommendations": ["Omni 服务不可用或模型未加载，先检查 /health 与 /load。"],
            "generated_at": utc_now(),
        }
    if not _service_loaded_omni(service_status):
        loaded_model = _loaded_model_id(_raw_health(service_status))
        return {
            "contract_version": QWEN_OMNI_VERSION,
            "status": "model_switch_required",
            "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
            "mode": "shadow",
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "sample_count": len(rows),
            "analyzed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "query": _shadow_query(
                account_id=account_id,
                dataset_id=dataset_id,
                limit=requested_limit,
                max_clip_seconds=max_clip_seconds,
                use_media=use_media,
                allow_windowed_clips=allow_windowed_clips,
                visual_ready_only=visual_ready_only,
            ),
            "loaded_model": loaded_model,
            "model_switch_required": True,
            "service_status": service_status,
            "recommendations": [
                f"当前服务加载的是 {loaded_model or 'unknown'}，不是 Qwen2.5-Omni 低显存模型。",
                "需要在目标服务端重启或修复 /load，使其加载 Qwen/Qwen2.5-Omni-7B-GPTQ-Int4 后再跑 shadow-run。",
            ],
            "generated_at": utc_now(),
        }
    samples = []
    counts: Counter[str] = Counter()
    for row in rows:
        duration = _duration_seconds(row)
        sample_id = str(row.get("id") or row.get("platform_item_id") or "")
        media_context: dict | None = None
        media_file: Path | None = None
        if use_media:
            video_path = _first_asset_path(row, "video")
            if not video_path:
                counts["skipped"] += 1
                samples.append(
                    {
                        "sample_id": sample_id,
                        "status": "skipped",
                        "reason": "media_missing",
                        "duration_seconds": round(duration, 3),
                        "title": row.get("title") or "",
                    }
                )
                continue
            try:
                media_context = _prepare_omni_clip(
                    video_path,
                    row,
                    max_clip_seconds=max_clip_seconds,
                    allow_windowed_clips=allow_windowed_clips,
                )
                media_file = Path(str(media_context.get("clip_path") or video_path))
                duration = float(media_context.get("clip_duration_seconds") or duration or 0.0)
                if media_context.get("windowed_clip"):
                    counts["windowed_clip"] += 1
                if media_context.get("cache_hit"):
                    counts["clip_cache_hit"] += 1
            except Exception as exc:
                counts["failed"] += 1
                samples.append(
                    {
                        "sample_id": sample_id,
                        "status": "failed",
                        "reason": "media_clip_prepare_failed",
                        "error": str(exc),
                        "title": row.get("title") or "",
                    }
                )
                continue
        if duration > float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS) and not (use_media and allow_windowed_clips):
            counts["skipped"] += 1
            samples.append(
                {
                    "sample_id": sample_id,
                    "status": "skipped",
                    "reason": "clip_too_long_for_low_vram",
                    "duration_seconds": round(duration, 3),
                    "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
                    "title": row.get("title") or "",
                }
            )
            continue
        try:
            payload = _historical_payload(row, max_clip_seconds=max_clip_seconds, model_id=getattr(client, "model_id", QWEN_OMNI_MODEL))
            if media_context:
                payload["media_payload"] = media_context
                payload["duration_seconds"] = float(media_context.get("clip_duration_seconds") or payload.get("duration_seconds") or 0.0)
            raw = (
                client.analyze_clip_file(payload, media_file)
                if use_media and media_file is not None
                else client.analyze_clip(payload)
            )
            item = _analysis_payload(
                entity_type="historical_sample",
                entity_id=sample_id,
                row=row,
                raw=raw,
                service_status=service_status,
                client=client,
                max_clip_seconds=max_clip_seconds,
                media_context=media_context,
            )
            counts[item["status"]] += 1
            samples.append(item)
        except Exception as exc:
            counts["failed"] += 1
            samples.append({"sample_id": sample_id, "status": "failed", "error": str(exc), "title": row.get("title") or ""})
    analyzed = sum(count for key, count in counts.items() if key not in {"skipped", "failed", "windowed_clip", "clip_cache_hit"})
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "ready" if analyzed else ("low_confidence" if samples else "empty"),
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "sample_count": len(rows),
        "analyzed_count": analyzed,
        "skipped_count": int(counts.get("skipped", 0)),
        "failed_count": int(counts.get("failed", 0)),
        "media_used": bool(use_media),
        "media_summary": {
            "visual_ready_only": bool(visual_ready_only),
            "allow_windowed_clips": bool(allow_windowed_clips),
            "windowed_clip_count": int(counts.get("windowed_clip", 0)),
            "clip_cache_hit_count": int(counts.get("clip_cache_hit", 0)),
            "clip_cache_root": str(_omni_clip_cache_root()),
        },
        "query": _shadow_query(
            account_id=account_id,
            dataset_id=dataset_id,
            limit=requested_limit,
            max_clip_seconds=max_clip_seconds,
            use_media=use_media,
            allow_windowed_clips=allow_windowed_clips,
            visual_ready_only=visual_ready_only,
        ),
        "status_counts": dict(counts),
        "service_status": service_status,
        "samples": samples[: max(1, int(limit or 20))],
        "recommendations": _shadow_recommendations(counts, len(rows), max_clip_seconds),
        "generated_at": utc_now(),
    }


def run_qwen_omni_media_batch(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    limit: int = 20,
    max_clip_seconds: float = 8.0,
    load_model: bool = False,
    force: bool = False,
    output_path: str | Path | None = None,
    client: QwenOmniClient | None = None,
) -> dict:
    client = client or QwenOmniClient()
    requested_limit = max(1, int(limit or 20))
    rows = _prepared_media_rows(account_id=account_id, dataset_id=dataset_id)[:requested_limit]
    service_status = client.load(max_clip_seconds=max_clip_seconds) if load_model else client.health()
    report_path = Path(output_path) if output_path else _omni_batch_report_path()
    counts: Counter[str] = Counter()
    samples: list[dict] = []
    reused = 0
    created = 0
    failed = 0
    if not _service_ready(service_status):
        report = _media_batch_report(
            status="service_unavailable",
            account_id=account_id,
            dataset_id=dataset_id,
            limit=requested_limit,
            max_clip_seconds=max_clip_seconds,
            force=force,
            service_status=service_status,
            counts=counts,
            samples=[],
            report_path=report_path,
        )
        write_json(report_path, report)
        return report
    if not _service_loaded_omni(service_status):
        counts["model_switch_required"] = len(rows)
        report = _media_batch_report(
            status="model_switch_required",
            account_id=account_id,
            dataset_id=dataset_id,
            limit=requested_limit,
            max_clip_seconds=max_clip_seconds,
            force=force,
            service_status=service_status,
            counts=counts,
            samples=[],
            report_path=report_path,
        )
        write_json(report_path, report)
        return report

    for row in rows:
        sample_id = str(row.get("id") or row.get("platform_item_id") or "")
        try:
            prepared = _prepare_media_sample(row, max_clip_seconds=max_clip_seconds)
            cache_path = _omni_result_cache_path(
                row,
                prepared["media_context"],
                max_clip_seconds=max_clip_seconds,
                model_id=getattr(client, "model_id", QWEN_OMNI_MODEL),
            )
            cached = None if force else read_json(cache_path, None)
            if isinstance(cached, dict) and cached.get("status") in {"model", "ready"}:
                item = _refresh_cached_analysis_item(cached, row=row)
                item["cache_hit"] = True
                item["result_cache_path"] = str(cache_path)
                write_json(cache_path, item)
                reused += 1
                counts["reused"] += 1
            else:
                payload = _historical_payload(row, max_clip_seconds=max_clip_seconds, model_id=getattr(client, "model_id", QWEN_OMNI_MODEL))
                payload["media_payload"] = prepared["media_context"]
                payload["duration_seconds"] = float(prepared["media_context"].get("clip_duration_seconds") or payload.get("duration_seconds") or 0.0)
                raw = client.analyze_clip_file(payload, prepared["media_file"])
                item = _analysis_payload(
                    entity_type="historical_sample",
                    entity_id=sample_id,
                    row=row,
                    raw=raw,
                    service_status=service_status,
                    client=client,
                    max_clip_seconds=max_clip_seconds,
                    media_context=prepared["media_context"],
                )
                item["cache_hit"] = False
                item["result_cache_path"] = str(cache_path)
                write_json(cache_path, item)
                created += 1
                counts[item.get("status") or "model"] += 1
            samples.append(_compact_media_batch_sample(item))
            _write_incremental_batch_report(
                report_path,
                account_id=account_id,
                dataset_id=dataset_id,
                requested_limit=requested_limit,
                max_clip_seconds=max_clip_seconds,
                force=force,
                service_status=service_status,
                counts=counts,
                samples=samples,
                created=created,
                reused=reused,
                failed=failed,
            )
        except Exception as exc:
            failed += 1
            counts["failed"] += 1
            samples.append(
                {
                    "sample_id": sample_id,
                    "status": "failed",
                    "title": row.get("title") or "",
                    "error": str(exc),
                    "generated_at": utc_now(),
                }
            )
            _write_incremental_batch_report(
                report_path,
                account_id=account_id,
                dataset_id=dataset_id,
                requested_limit=requested_limit,
                max_clip_seconds=max_clip_seconds,
                force=force,
                service_status=service_status,
                counts=counts,
                samples=samples,
                created=created,
                reused=reused,
                failed=failed,
            )
    status = "ready" if created or reused else ("low_confidence" if samples else "empty")
    report = _media_batch_report(
        status=status,
        account_id=account_id,
        dataset_id=dataset_id,
        limit=requested_limit,
        max_clip_seconds=max_clip_seconds,
        force=force,
        service_status=service_status,
        counts=counts,
        samples=samples,
        report_path=report_path,
        created=created,
        reused=reused,
        failed=failed,
    )
    write_json(report_path, report)
    return report


def qwen_omni_shadow_cache_index() -> dict[str, dict]:
    root = _omni_result_cache_root()
    if not root.is_dir():
        return {}
    indexed: dict[str, dict] = {}
    for path in sorted(root.glob("*.json")):
        item = read_json(path, None)
        if not isinstance(item, dict) or item.get("status") not in {"model", "ready"}:
            continue
        sample_id = str(item.get("entity_id") or item.get("sample_id") or "").strip()
        if not sample_id:
            continue
        compact = _compact_omni_cache_item(item, path=path)
        previous = indexed.get(sample_id)
        if not previous or str(compact.get("generated_at") or "") >= str(previous.get("generated_at") or ""):
            indexed[sample_id] = compact
    return indexed


def _compact_omni_cache_item(item: dict, *, path: Path) -> dict:
    raw_values = (
        item.get("raw_semantic_suggestions")
        if isinstance(item.get("raw_semantic_suggestions"), dict)
        else item.get("semantic_suggestions")
        if isinstance(item.get("semantic_suggestions"), dict)
        else {}
    )
    suggestions = item.get("semantic_suggestions") if isinstance(item.get("semantic_suggestions"), dict) else {}
    quality = item.get("semantic_quality") if isinstance(item.get("semantic_quality"), dict) else {}
    return {
        "sample_id": item.get("entity_id") or item.get("sample_id") or "",
        "status": item.get("status") or "",
        "cache_path": str(path),
        "semantic_suggestions": suggestions,
        "raw_semantic_suggestions": raw_values,
        "semantic_quality": quality,
        "ranker_usable_fields": quality.get("ranker_usable_fields") if isinstance(quality.get("ranker_usable_fields"), list) else [],
        "ranker_usable_count": int(quality.get("ranker_usable_count") or 0),
        "normalization_version": quality.get("normalization_version") or "",
        "writes_labels": False,
        "production_weight": False,
        "generated_at": item.get("generated_at") or "",
    }


def refresh_omni_shadow_for_row(omni: dict, row: dict) -> dict:
    """Re-normalize a compact cached Omni item with the current row context."""
    if not isinstance(omni, dict) or not omni:
        return {}
    raw_values = (
        omni.get("raw_semantic_suggestions")
        if isinstance(omni.get("raw_semantic_suggestions"), dict)
        else omni.get("semantic_suggestions")
        if isinstance(omni.get("semantic_suggestions"), dict)
        else {}
    )
    if not raw_values:
        return dict(omni)
    suggestions, quality = _normalize_omni_semantic_suggestions(raw_values, row=row)
    refreshed = dict(omni)
    refreshed["semantic_suggestions"] = suggestions
    refreshed["semantic_quality"] = quality
    refreshed["ranker_usable_fields"] = quality.get("ranker_usable_fields") if isinstance(quality.get("ranker_usable_fields"), list) else []
    refreshed["ranker_usable_count"] = int(quality.get("ranker_usable_count") or 0)
    refreshed["normalization_version"] = quality.get("normalization_version") or ""
    refreshed["writes_labels"] = False
    refreshed["production_weight"] = False
    return refreshed


def _candidate_row(segment_id: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT c.*, v.account_id, v.title AS video_title, v.file_path
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            WHERE c.id = ?
            """,
            [segment_id],
        )


def _historical_rows(account_id: str | None, dataset_id: str | None, limit: int) -> list[dict]:
    clauses = ["COALESCE(platform_item_id, '') != ''"]
    params: list[Any] = []
    account = str(account_id or "").strip()
    dataset = str(dataset_id or "").strip()
    if account and account.lower() != "all":
        clauses.append("account_id = ?")
        params.append(account)
    if dataset and dataset.lower() != "all":
        clauses.append("dataset_id = ?")
        params.append(dataset)
    query = f"""
        SELECT *
        FROM historical_capture_samples
        WHERE {' AND '.join(clauses)}
        ORDER BY
          CASE performance_label WHEN 'high' THEN 0 WHEN 'low' THEN 1 ELSE 2 END,
          COALESCE(normalized_reward, reward_proxy, 0) DESC,
          updated_at DESC
    """
    if int(limit or 0) > 0:
        query += " LIMIT ?"
        params.append(max(1, int(limit or 20)))
    with connect() as conn:
        return fetch_all(conn, query, params)


def _prepared_media_rows(account_id: str | None, dataset_id: str | None) -> list[dict]:
    rows = _historical_rows(account_id=account_id, dataset_id=dataset_id, limit=0)
    asset_index = _build_asset_index()
    prepared = [_prepare_row(row, asset_index=asset_index) for row in rows]
    return [
        row
        for row in prepared
        if (row.get("assets") or {}).get("ready_for_multimodal")
        and (row.get("assets") or {}).get("video")
    ]


def _candidate_payload(row: dict, *, max_clip_seconds: float, model_id: str) -> dict:
    return {
        "model": model_id,
        "mode": "shadow",
        "return_audio": False,
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "entity_type": "candidate",
        "segment_id": row.get("id") or "",
        "account_id": row.get("account_id") or "",
        "title": row.get("video_title") or row.get("summary") or "",
        "transcript": row.get("transcript") or row.get("summary") or "",
        "tags": _tags(
            [
                row.get("primary_topic"),
                row.get("music_slice_type"),
                row.get("emotion_type"),
                row.get("short_video_structure"),
                row.get("musical_moment"),
                row.get("program_context"),
            ]
        ),
        "duration_seconds": _duration_seconds(row),
        "semantic_schema": _omni_semantic_schema(),
        "analysis_prompt": _omni_analysis_prompt("candidate"),
    }


def _historical_payload(row: dict, *, max_clip_seconds: float, model_id: str) -> dict:
    return {
        "model": model_id,
        "mode": "shadow",
        "return_audio": False,
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "entity_type": "historical_sample",
        "sample_id": row.get("id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "",
        "title": row.get("title") or "",
        "transcript": row.get("description") or row.get("title") or "",
        "tags": _tags([row.get("tags"), row.get("artist_names"), row.get("song_title"), row.get("hook_type"), row.get("slice_structure")]),
        "duration_seconds": _duration_seconds(row),
        "semantic_schema": _omni_semantic_schema(),
        "analysis_prompt": _omni_analysis_prompt("historical_sample"),
    }


def _omni_semantic_schema() -> dict:
    catalog = semantic_label_catalog()
    return {
        "schema_version": OMNI_SEMANTIC_SCHEMA_VERSION,
        "output_object": "semantic_suggestions",
        "required_fields": [*OMNI_CORE_FIELDS, *OMNI_ENTITY_FIELDS],
        "optional_fields": [*OMNI_AUX_FIELDS],
        "allowed_values": {
            field: [item["value"] for item in catalog.get(field, [])]
            for field in OMNI_CORE_FIELDS
        },
        "auxiliary_allowed_values": {
            "domain_category": list(OMNI_DOMAIN_CATEGORIES),
            "material_type": list(OMNI_MATERIAL_TYPES),
            "presentation_style": list(OMNI_PRESENTATION_STYLES),
        },
        "auxiliary_fields": {
            "domain_category": "Business domain, e.g. music_variety or entertainment. This separates domain from material type.",
            "material_type": "Content form, e.g. performance_clip, commentary, reaction, compilation, or vocal_teaching.",
            "program_context": "Recognized show/program name when reliable, otherwise unknown.",
            "presentation_style": "Surface format such as direct_cam, analysis, reaction_review, listicle, vocal_lesson, a_cappella, or program_clip.",
        },
        "annotation_field_guides": omni_annotation_field_guides([*OMNI_CORE_FIELDS, *OMNI_ENTITY_FIELDS, *OMNI_AUX_FIELDS]),
        "field_descriptions_zh": {
            field: guide["description_zh"]
            for field, guide in omni_annotation_field_guides([*OMNI_CORE_FIELDS, *OMNI_ENTITY_FIELDS, *OMNI_AUX_FIELDS]).items()
        },
        "entity_fields": {
            "artist_names": "string or list of artist/person names; use unknown when not visible or audible.",
            "song_title": "string; use unknown when no reliable song title is present.",
            "tags": "list of concise evidence tags from visible text, audio, title, or platform metadata.",
        },
        "confidence_fields": ["classification_confidence", "field_confidence", "evidence"],
        "unknown_policy": "Use unknown instead of inventing labels outside allowed_values.",
        "writes_labels": False,
        "production_weight": False,
    }


def _omni_analysis_prompt(entity_type: str) -> list[str]:
    values = {
        field: ", ".join(value for value in SEMANTIC_ENUMS[field] if value != "unknown")
        for field in OMNI_CORE_FIELDS
    }
    return [
        f"Analyze this {entity_type} in shadow mode only. Do not predict views or viral probability.",
        "Return JSON with semantic_suggestions, field_confidence, evidence, advice, and risk_flags.",
        f"content_category must be one of: {values['content_category']}, unknown.",
        f"hook_type must be one of: {values['hook_type']}, unknown.",
        f"slice_structure must be one of: {values['slice_structure']}, unknown.",
        f"domain_category should separate the business domain and must be one of: {', '.join(value for value in OMNI_DOMAIN_CATEGORIES if value != 'unknown')}, unknown.",
        f"material_type should describe the content form and must be one of: {', '.join(value for value in OMNI_MATERIAL_TYPES if value != 'unknown')}, unknown.",
        f"presentation_style must be one of: {', '.join(value for value in OMNI_PRESENTATION_STYLES if value != 'unknown')}, unknown.",
        "For music_variety content, use domain_category=music_variety and put performance_clip/reaction/commentary/compilation/vocal_teaching in material_type instead of overloading content_category.",
        "If the clip window is too short to judge narrative structure, set slice_structure to unknown and explain why.",
        "Do not output free-form category names such as music, entertainment, single, lyric, or historical_sample.",
    ]


def _prepare_media_sample(row: dict, *, max_clip_seconds: float) -> dict:
    video_path = _first_asset_path(row, "video")
    if not video_path:
        raise FileNotFoundError("media_missing")
    media_context = _prepare_omni_clip(
        video_path,
        row,
        max_clip_seconds=max_clip_seconds,
        allow_windowed_clips=True,
    )
    media_file = Path(str(media_context.get("clip_path") or video_path))
    if not media_file.is_file():
        raise FileNotFoundError(str(media_file))
    return {"media_context": media_context, "media_file": media_file}


def _omni_result_cache_root() -> Path:
    return ensure_data_dirs().cache_dir / "qwen_omni_results" / "historical_sample"


def _omni_result_cache_path(row: dict, media_context: dict, *, max_clip_seconds: float, model_id: str) -> Path:
    clip_path = Path(str(media_context.get("clip_path") or ""))
    source_path = Path(str(media_context.get("source_path") or ""))
    parts = [
        str(row.get("id") or ""),
        str(row.get("platform_item_id") or ""),
        str(model_id or QWEN_OMNI_MODEL),
        f"{float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS):.3f}",
        str(clip_path),
        str(clip_path.stat().st_size if clip_path.is_file() else 0),
        str(int(clip_path.stat().st_mtime) if clip_path.is_file() else 0),
        str(source_path),
    ]
    source_hash = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    sample_id = _safe_file_part(str(row.get("id") or row.get("platform_item_id") or "sample"))
    return _omni_result_cache_root() / f"{sample_id}_{source_hash}.json"


def _omni_batch_report_path() -> Path:
    stamp = utc_now().replace(":", "").replace("-", "").replace(".", "_")
    return ensure_data_dirs().root / "outputs" / "qwen_omni_shadow" / f"media_batch_{stamp}.json"


def _compact_media_batch_sample(item: dict) -> dict:
    media = item.get("media_payload") if isinstance(item.get("media_payload"), dict) else {}
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    raw_media = raw.get("media_payload") if isinstance(raw.get("media_payload"), dict) else {}
    semantic = item.get("semantic_suggestions") if isinstance(item.get("semantic_suggestions"), dict) else {}
    raw_semantic = item.get("raw_semantic_suggestions") if isinstance(item.get("raw_semantic_suggestions"), dict) else {}
    semantic_quality = item.get("semantic_quality") if isinstance(item.get("semantic_quality"), dict) else {}
    return {
        "sample_id": item.get("entity_id") or item.get("sample_id") or "",
        "status": item.get("status") or "",
        "title": item.get("title") or "",
        "cache_hit": bool(item.get("cache_hit")),
        "result_cache_path": item.get("result_cache_path") or "",
        "semantic_suggestions": semantic,
        "raw_semantic_suggestions": raw_semantic,
        "semantic_quality": semantic_quality,
        "advice": item.get("advice") or "",
        "risk_flags": item.get("risk_flags") if isinstance(item.get("risk_flags"), list) else [],
        "media": {
            "clip_path": media.get("clip_path") or "",
            "source_path": media.get("source_path") or "",
            "clip_duration_seconds": media.get("clip_duration_seconds"),
            "source_duration_seconds": media.get("source_duration_seconds"),
            "windowed_clip": bool(media.get("windowed_clip")),
            "has_audio": bool(media.get("has_audio")),
            "audio_source": media.get("audio_source") or "",
            "active_window": media.get("active_window") or "",
            "multi_window_policy": media.get("multi_window_policy") or media.get("window_policy") or "",
            "planned_window_count": int(media.get("planned_window_count") or 0),
            "window_plan": media.get("window_plan") if isinstance(media.get("window_plan"), list) else [],
            "use_audio_in_video": bool(raw_media.get("use_audio_in_video")),
            "media_bytes": raw.get("media_bytes") or 0,
        },
        "writes_labels": False,
        "production_weight": False,
        "generated_at": item.get("generated_at") or utc_now(),
    }


def _refresh_cached_analysis_item(item: dict, *, row: dict) -> dict:
    refreshed = dict(item)
    raw_values = (
        refreshed.get("raw_semantic_suggestions")
        if isinstance(refreshed.get("raw_semantic_suggestions"), dict)
        else refreshed.get("semantic_suggestions")
        if isinstance(refreshed.get("semantic_suggestions"), dict)
        else {}
    )
    normalized, quality = _normalize_omni_semantic_suggestions(raw_values, row=row)
    refreshed["semantic_suggestions"] = normalized
    refreshed["raw_semantic_suggestions"] = dict(raw_values)
    refreshed["semantic_quality"] = quality
    refreshed["writes_labels"] = False
    refreshed["production_weight"] = False
    return refreshed


def _media_batch_report(
    *,
    status: str,
    account_id: str | None,
    dataset_id: str | None,
    limit: int,
    max_clip_seconds: float,
    force: bool,
    service_status: dict,
    counts: Counter[str],
    samples: list[dict],
    report_path: Path,
    created: int = 0,
    reused: int = 0,
    failed: int = 0,
) -> dict:
    audio_ready = sum(1 for item in samples if ((item.get("media") or {}).get("has_audio")))
    audio_used = sum(1 for item in samples if ((item.get("media") or {}).get("use_audio_in_video")))
    planned_multi_window = sum(1 for item in samples if int((item.get("media") or {}).get("planned_window_count") or 0) > 1)
    audio_sources = Counter(str((item.get("media") or {}).get("audio_source") or "unknown") for item in samples)
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": status,
        "model": QWEN_OMNI_MODEL,
        "mode": "shadow_media_batch",
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "query": {
            "account_id": account_id or "all",
            "dataset_id": dataset_id or "all",
            "limit": int(limit or 0),
            "max_clip_seconds": float(max_clip_seconds or 0.0),
            "force": bool(force),
            "use_media": True,
            "allow_windowed_clips": True,
            "visual_ready_only": True,
        },
        "sample_count": len(samples),
        "created": int(created),
        "reused": int(reused),
        "failed": int(failed),
        "status_counts": dict(counts),
        "media_summary": {
            "audio_ready_count": audio_ready,
            "audio_used_count": audio_used,
            "audio_used_rate": round(audio_used / max(1, audio_ready), 4),
            "audio_source_counts": dict(audio_sources),
            "multi_window_planned_count": planned_multi_window,
            "windowed_clip_count": sum(1 for item in samples if ((item.get("media") or {}).get("windowed_clip"))),
            "result_cache_root": str(_omni_result_cache_root()),
            "clip_cache_root": str(_omni_clip_cache_root()),
        },
        "semantic_summary": _omni_batch_semantic_summary(samples),
        "service_status": service_status,
        "report_path": str(report_path),
        "samples": samples,
        "writes_labels": False,
        "production_weight": False,
        "recommendations": _media_batch_recommendations(samples, failed),
        "generated_at": utc_now(),
    }


def _write_incremental_batch_report(
    report_path: Path,
    *,
    account_id: str | None,
    dataset_id: str | None,
    requested_limit: int,
    max_clip_seconds: float,
    force: bool,
    service_status: dict,
    counts: Counter[str],
    samples: list[dict],
    created: int,
    reused: int,
    failed: int,
) -> None:
    status = "running" if samples else "empty"
    write_json(
        report_path,
        _media_batch_report(
            status=status,
            account_id=account_id,
            dataset_id=dataset_id,
            limit=requested_limit,
            max_clip_seconds=max_clip_seconds,
            force=force,
            service_status=service_status,
            counts=counts,
            samples=samples,
            report_path=report_path,
            created=created,
            reused=reused,
            failed=failed,
        ),
    )


def _omni_batch_semantic_summary(samples: list[dict]) -> dict:
    ready = [item for item in samples if item.get("status") in {"model", "ready"}]
    coverage = {}
    for field in OMNI_CORE_FIELDS:
        known = sum(1 for item in ready if ((item.get("semantic_suggestions") or {}).get(field) not in {"", "unknown", None}))
        usable = sum(
            1
            for item in ready
            if ((item.get("semantic_quality") or {}).get("field_quality") or {}).get(field, {}).get("usable_for_ranker")
        )
        coverage[field] = {
            "known_count": known,
            "known_rate": round(known / max(1, len(ready)), 4),
            "ranker_usable_count": usable,
            "ranker_usable_rate": round(usable / max(1, len(ready)), 4),
        }
    all_three = sum(
        1
        for item in ready
        if all(((item.get("semantic_suggestions") or {}).get(field) not in {"", "unknown", None}) for field in OMNI_CORE_FIELDS)
    )
    return {
        "schema_version": OMNI_SEMANTIC_SCHEMA_VERSION,
        "normalization_version": OMNI_SEMANTIC_NORMALIZER_VERSION,
        "ready_count": len(ready),
        "all_core_known_count": all_three,
        "all_core_known_rate": round(all_three / max(1, len(ready)), 4),
        "coverage": coverage,
    }


def _media_batch_recommendations(samples: list[dict], failed: int) -> list[str]:
    recs = ["Omni 真媒体批处理仍为 shadow mode，只能作为校准建议，不写 manual_verified 或生产排序权重。"]
    if failed:
        recs.append("存在失败样本，下一轮优先查看 error 并用 force=false 断点重跑。")
    audio_ready = sum(1 for item in samples if ((item.get("media") or {}).get("has_audio")))
    audio_used = sum(1 for item in samples if ((item.get("media") or {}).get("use_audio_in_video")))
    if audio_ready and audio_used < audio_ready:
        recs.append("部分有音轨样本未进入音频输入，检查远端 ffmpeg PATH 和服务启动脚本。")
    if len(samples) >= 20:
        recs.append("建议先抽查 20-50 条输出质量，再决定是否夜间跑 600 条全量。")
    else:
        recs.append("当前样本量适合 smoke/抽样验证，下一步可扩大到 20-50 条。")
    return recs


def _shadow_query(
    *,
    account_id: str | None,
    dataset_id: str | None,
    limit: int,
    max_clip_seconds: float,
    use_media: bool,
    allow_windowed_clips: bool,
    visual_ready_only: bool,
) -> dict:
    return {
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "limit": int(limit or 0),
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "use_media": bool(use_media),
        "allow_windowed_clips": bool(allow_windowed_clips),
        "visual_ready_only": bool(visual_ready_only),
    }


def _first_asset_path(row: dict, key: str) -> Path | None:
    paths = ((row.get("assets") or {}).get("paths") or {}) if isinstance(row.get("assets"), dict) else {}
    for value in paths.get(key) or []:
        path = Path(str(value)).expanduser()
        if path.is_file():
            return path
    return None


def _omni_audio_source(probe: dict) -> str:
    return "embedded_audio" if probe.get("audio_streams") else "missing_audio"


def _omni_window_plan(source_duration: float, clip_limit: float) -> list[dict[str, Any]]:
    duration = max(0.0, float(source_duration or 0.0))
    limit = max(1.0, float(clip_limit or DEFAULT_MAX_CLIP_SECONDS))
    if duration <= 0:
        return [
            {
                "window": "hook",
                "start_seconds": 0.0,
                "end_seconds": round(limit, 3),
                "duration_seconds": round(limit, 3),
                "status": "active",
            }
        ]
    windows = [
        {
            "window": "hook",
            "start_seconds": 0.0,
            "end_seconds": round(min(duration, limit), 3),
            "duration_seconds": round(min(duration, limit), 3),
            "status": "active",
        }
    ]
    if duration > limit * 1.6:
        middle_start = max(0.0, min(duration - limit, duration * 0.45))
        windows.append(
            {
                "window": "middle",
                "start_seconds": round(middle_start, 3),
                "end_seconds": round(min(duration, middle_start + limit), 3),
                "duration_seconds": round(min(limit, max(0.0, duration - middle_start)), 3),
                "status": "planned",
            }
        )
    if duration > limit * 2.2:
        payoff_start = max(0.0, min(duration - limit, duration * 0.78))
        if all(abs(payoff_start - float(item.get("start_seconds") or 0.0)) > 1.0 for item in windows):
            windows.append(
                {
                    "window": "payoff",
                    "start_seconds": round(payoff_start, 3),
                    "end_seconds": round(min(duration, payoff_start + limit), 3),
                    "duration_seconds": round(min(limit, max(0.0, duration - payoff_start)), 3),
                    "status": "planned",
                }
            )
    return windows


def _omni_media_window_metadata(probe: dict, source_duration: float, clip_limit: float) -> dict[str, Any]:
    plan = _omni_window_plan(source_duration, clip_limit)
    return {
        "active_window": "hook",
        "audio_source": _omni_audio_source(probe),
        "multi_window_policy": "hook_active_middle_payoff_planned",
        "window_plan": plan,
        "planned_window_count": len(plan),
    }


def _prepare_omni_clip(
    video_path: Path,
    row: dict,
    *,
    max_clip_seconds: float,
    allow_windowed_clips: bool,
) -> dict:
    video_path = Path(video_path).expanduser()
    if not video_path.is_file():
        raise FileNotFoundError(str(video_path))
    probe = {}
    try:
        probe = probe_video(video_path)
    except Exception:
        probe = {}
    source_duration = float(probe.get("duration_seconds") or _duration_seconds(row) or 0.0)
    clip_limit = max(1.0, float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS))
    media_metadata = _omni_media_window_metadata(probe, source_duration, clip_limit)
    needs_window = source_duration <= 0 or source_duration > clip_limit
    if needs_window and not allow_windowed_clips:
        return {
            "clip_path": str(video_path),
            "source_path": str(video_path),
            "source_duration_seconds": round(source_duration, 3),
            "clip_duration_seconds": round(source_duration, 3),
            "window_start_seconds": 0.0,
            "window_end_seconds": round(source_duration, 3),
            "windowed_clip": False,
            "normalized_clip": False,
            "has_audio": bool(probe.get("audio_streams")),
            **media_metadata,
        }
    if not allow_windowed_clips and source_duration <= clip_limit:
        return {
            "clip_path": str(video_path),
            "source_path": str(video_path),
            "source_duration_seconds": round(source_duration, 3),
            "clip_duration_seconds": round(source_duration, 3),
            "window_start_seconds": 0.0,
            "window_end_seconds": round(source_duration, 3),
            "windowed_clip": False,
            "normalized_clip": False,
            "has_audio": bool(probe.get("audio_streams")),
            **media_metadata,
        }
    window_start = 0.0
    window_duration = min(clip_limit, source_duration if source_duration > 0 else clip_limit)
    output = _omni_clip_cache_path(video_path, row, window_start=window_start, window_duration=window_duration)
    cache_hit = output.is_file() and output.stat().st_size > 0
    if not cache_hit:
        _transcode_omni_window(video_path, output, start_seconds=window_start, duration_seconds=window_duration)
    return {
        "clip_path": str(output),
        "source_path": str(video_path),
        "source_duration_seconds": round(source_duration, 3),
        "clip_duration_seconds": round(window_duration, 3),
        "window_start_seconds": round(window_start, 3),
        "window_end_seconds": round(window_start + window_duration, 3),
        "windowed_clip": bool(source_duration > clip_limit),
        "normalized_clip": True,
        "cache_hit": bool(cache_hit),
        "has_audio": bool(probe.get("audio_streams")),
        "window_policy": f"first_{int(round(window_duration))}s_hook_window",
        **media_metadata,
    }


def _omni_clip_cache_root() -> Path:
    return ensure_data_dirs().cache_dir / "qwen_omni_clips" / "historical_sample"


def _omni_clip_cache_path(video_path: Path, row: dict, *, window_start: float, window_duration: float) -> Path:
    stat = video_path.stat()
    source_hash = hashlib.sha256(
        "|".join(
            [
                str(video_path.resolve()),
                str(stat.st_size),
                str(int(stat.st_mtime)),
                f"{window_start:.3f}",
                f"{window_duration:.3f}",
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    sample_id = _safe_file_part(str(row.get("id") or row.get("platform_item_id") or video_path.stem))
    return _omni_clip_cache_root() / f"{sample_id}_{source_hash}_s{window_start:.0f}_d{window_duration:.0f}.mp4"


def _safe_file_part(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or "").strip())
    return text[:80] or "sample"


def _transcode_omni_window(video_path: Path, output_path: Path, *, start_seconds: float, duration_seconds: float) -> None:
    require_binary("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start_seconds):.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{max(0.1, duration_seconds):.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        "fps=8,scale='min(640,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "32",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        run_cmd(command)
    except Exception:
        fallback = command.copy()
        codec_index = fallback.index("libx264")
        fallback[codec_index] = "mpeg4"
        for option in ["-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p"]:
            if option in fallback:
                fallback.remove(option)
        run_cmd(fallback)


def _analysis_payload(
    *,
    entity_type: str,
    entity_id: str,
    row: dict,
    raw: dict,
    service_status: dict,
    client: QwenOmniClient,
    max_clip_seconds: float,
    media_context: dict | None = None,
) -> dict:
    status = str(raw.get("status") or raw.get("decision") or "ready")
    raw_suggestions = _raw_semantic_suggestions(raw)
    suggestions, semantic_quality = _normalize_omni_semantic_suggestions(raw_suggestions, row=row)
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": status,
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": row.get("title") or row.get("video_title") or "",
        "duration_seconds": round(_duration_seconds(row), 3),
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "service_status": service_status,
        "media_used": bool(media_context or raw.get("media_used")),
        "media_payload": media_context or raw.get("media_payload") or {},
        "semantic_suggestions": suggestions,
        "raw_semantic_suggestions": raw_suggestions,
        "semantic_quality": semantic_quality,
        "scores": raw.get("scores") if isinstance(raw.get("scores"), dict) else {},
        "advice": raw.get("advice") or raw.get("recommendation") or "recommend_review",
        "risk_flags": raw.get("risk_flags") if isinstance(raw.get("risk_flags"), list) else [],
        "raw": raw,
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _semantic_suggestions(raw: dict) -> dict:
    suggestions, _quality = _normalize_omni_semantic_suggestions(_raw_semantic_suggestions(raw), row={})
    return suggestions


def _raw_semantic_suggestions(raw: dict) -> dict:
    candidates = [raw.get("semantic_suggestions"), raw.get("semantic"), raw.get("labels"), raw]
    fields = [*OMNI_CORE_FIELDS, *OMNI_ENTITY_FIELDS, *OMNI_AUX_FIELDS]
    result = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for field in fields:
            if field in candidate and field not in result:
                result[field] = candidate.get(field)
    return result


def _normalize_omni_semantic_suggestions(raw_values: dict, *, row: dict) -> tuple[dict, dict]:
    normalized: dict[str, Any] = {}
    field_quality: dict[str, dict] = {}
    for field in ("content_category", "hook_type"):
        value, quality = _normalize_omni_core_field(field, raw_values.get(field), row=row)
        normalized[field] = value
        field_quality[field] = quality
    slice_value, slice_quality = _normalize_omni_slice_structure(raw_values.get("slice_structure"), row=row, raw_values=raw_values)
    normalized["slice_structure"] = slice_value
    field_quality["slice_structure"] = slice_quality
    domain_value, domain_quality = _normalize_omni_domain_category(raw_values.get("domain_category"), row=row, content_category=normalized["content_category"])
    normalized["domain_category"] = domain_value
    field_quality["domain_category"] = domain_quality
    material_value, material_quality = _normalize_omni_material_type(raw_values.get("material_type"), row=row, content_category=normalized["content_category"])
    normalized["material_type"] = material_value
    field_quality["material_type"] = material_quality
    program_value, program_quality = _normalize_omni_program_context(raw_values.get("program_context"), row=row)
    normalized["program_context"] = program_value
    field_quality["program_context"] = program_quality
    style_value, style_quality = _normalize_omni_presentation_style(raw_values.get("presentation_style"), row=row, material_type=material_value)
    normalized["presentation_style"] = style_value
    field_quality["presentation_style"] = style_quality
    for field in OMNI_ENTITY_FIELDS:
        value, quality = _normalize_omni_entity_field(field, raw_values.get(field))
        normalized[field] = value
        field_quality[field] = quality
    unknown_reasons = [
        f"{field}:{quality.get('reason')}"
        for field, quality in field_quality.items()
        if field in OMNI_CORE_FIELDS and normalized.get(field) == "unknown" and quality.get("reason")
    ]
    core_known = sum(1 for field in OMNI_CORE_FIELDS if normalized.get(field) != "unknown")
    ranker_usable = [
        field
        for field in OMNI_CORE_FIELDS
        if field_quality.get(field, {}).get("usable_for_ranker")
    ]
    normalized["semantic_unknown_reason"] = "|".join(unknown_reasons)
    quality = {
        "schema_version": OMNI_SEMANTIC_SCHEMA_VERSION,
        "normalization_version": OMNI_SEMANTIC_NORMALIZER_VERSION,
        "core_known_count": core_known,
        "core_known_rate": round(core_known / max(1, len(OMNI_CORE_FIELDS)), 4),
        "auxiliary_known_fields": [
            field
            for field in OMNI_AUX_FIELDS
            if str(normalized.get(field) or "").strip().lower() not in {"", "unknown", "none", "null"}
        ],
        "ranker_usable_fields": ranker_usable,
        "ranker_usable_count": len(ranker_usable),
        "field_quality": field_quality,
        "writes_labels": False,
        "production_weight": False,
    }
    return normalized, quality


def _normalize_omni_core_field(field: str, raw_value: Any, *, row: dict) -> tuple[str, dict]:
    raw_text = _field_text(raw_value)
    if _is_unknown_text(raw_text):
        inferred = _infer_core_field_from_context(field, row=row, raw_value=raw_text)
        if inferred != "unknown":
            confidence = "medium"
            return inferred, _field_quality(
                field,
                raw_text,
                inferred,
                source="context_inference",
                confidence=confidence,
                reason="raw_missing_context_rescue",
                usable_for_ranker=_omni_shadow_ranker_usable(
                    field,
                    source="context_inference",
                    confidence=confidence,
                    normalized_value=inferred,
                    reason="raw_missing_context_rescue",
                ),
            )
        return "unknown", _field_quality(field, raw_text, "unknown", source="unknown", confidence="low", reason="missing_or_unknown")
    if _omni_key(raw_text) in (OMNI_CONTEXT_FIRST_VALUES.get(field) or set()) or raw_text in (OMNI_CONTEXT_FIRST_VALUES.get(field) or set()):
        inferred = _infer_core_field_from_context(field, row=row, raw_value=raw_text)
        if inferred != "unknown":
            confidence = "medium"
            reason = f"context_rescue:{raw_text[:32]}"
            return inferred, _field_quality(
                field,
                raw_text,
                inferred,
                source="context_inference",
                confidence=confidence,
                reason=reason,
                usable_for_ranker=_omni_shadow_ranker_usable(
                    field,
                    source="context_inference",
                    confidence=confidence,
                    normalized_value=inferred,
                    reason=reason,
                ),
            )
    normalized, reason = normalize_semantic_field(field, raw_text)
    if normalized != "unknown":
        source = "enum" if _omni_key(raw_text) == normalized else "semantic_alias"
        confidence = "high" if source == "enum" else "medium"
        return normalized, _field_quality(
            field,
            raw_text,
            normalized,
            source=source,
            confidence=confidence,
            reason=reason or source,
            usable_for_ranker=_omni_shadow_ranker_usable(
                field,
                source=source,
                confidence=confidence,
                normalized_value=normalized,
                reason=reason or source,
            ),
        )
    alias = _lookup_omni_alias(field, raw_text)
    if alias and alias in SEMANTIC_ENUMS[field] and alias != "unknown":
        confidence = "medium"
        reason = f"omni_alias:{raw_text}"
        return alias, _field_quality(
            field,
            raw_text,
            alias,
            source="omni_alias",
            confidence=confidence,
            reason=reason,
            usable_for_ranker=_omni_shadow_ranker_usable(
                field,
                source="omni_alias",
                confidence=confidence,
                normalized_value=alias,
                reason=reason,
            ),
        )
    inferred = _infer_core_field_from_context(field, row=row, raw_value=raw_text)
    if inferred != "unknown":
        confidence = "medium"
        reason = f"context_rescue:{raw_text[:32]}"
        return inferred, _field_quality(
            field,
            raw_text,
            inferred,
            source="context_inference",
            confidence=confidence,
            reason=reason,
            usable_for_ranker=_omni_shadow_ranker_usable(
                field,
                source="context_inference",
                confidence=confidence,
                normalized_value=inferred,
                reason=reason,
            ),
        )
    return "unknown", _field_quality(
        field,
        raw_text,
        "unknown",
        source="unmapped",
        confidence="low",
        reason=reason or f"unmapped:{raw_text[:40]}",
    )


def _normalize_omni_domain_category(raw_value: Any, *, row: dict, content_category: str) -> tuple[str, dict]:
    raw_text = _field_text(raw_value)
    normalized, reason = _normalize_aux_enum(raw_text, OMNI_DOMAIN_CATEGORIES, aliases={
        "music": "music_variety",
        "音乐": "music_variety",
        "音综": "music_variety",
        "music_show": "music_variety",
        "entertainment_news": "entertainment",
        "娱乐": "entertainment",
        "celebrity": "entertainment",
        "影视": "drama_film",
        "sports": "sports_entertainment",
        "体育": "sports_entertainment",
        "ai": "creative_ai",
        "ecommerce": "commercial",
    })
    if normalized != "unknown":
        return normalized, _field_quality(
            "domain_category",
            raw_text,
            normalized,
            source="enum" if not reason else "aux_alias",
            confidence="medium",
            reason=reason or "enum",
            usable_for_ranker=False,
        )
    inferred = _infer_domain_category_from_context(row=row, content_category=content_category, raw_value=raw_text)
    confidence = "medium" if inferred != "unknown" else "low"
    return inferred, _field_quality(
        "domain_category",
        raw_text,
        inferred,
        source="context_inference" if inferred != "unknown" else "unknown",
        confidence=confidence,
        reason="content_category_domain_split" if inferred != "unknown" else "missing_or_unknown",
        usable_for_ranker=False,
    )


def _normalize_omni_material_type(raw_value: Any, *, row: dict, content_category: str) -> tuple[str, dict]:
    raw_text = _field_text(raw_value)
    normalized, reason = _normalize_aux_enum(raw_text, OMNI_MATERIAL_TYPES, aliases={
        "stage_clip": "performance_clip",
        "stage": "performance_clip",
        "live": "performance_clip",
        "live_performance": "performance_clip",
        "音乐现场": "performance_clip",
        "直拍": "performance_clip",
        "highlight": "performance_highlight",
        "高光": "performance_highlight",
        "analysis": "commentary",
        "review": "commentary",
        "解析": "commentary",
        "乐评": "commentary",
        "music_commentary": "commentary",
        "vocal_lesson": "vocal_teaching",
        "声乐教学": "vocal_teaching",
        "教学": "vocal_teaching",
        "list": "compilation",
        "盘点": "compilation",
        "合集": "compilation",
        "music_show": "program_context",
        "behind_scene": "behind_the_scenes",
        "behind-scenes": "behind_the_scenes",
    })
    if normalized != "unknown":
        return normalized, _field_quality(
            "material_type",
            raw_text,
            normalized,
            source="enum" if not reason else "aux_alias",
            confidence="medium",
            reason=reason or "enum",
            usable_for_ranker=False,
        )
    inferred = _infer_material_type_from_context(row=row, content_category=content_category, raw_value=raw_text)
    confidence = "medium" if inferred != "unknown" else "low"
    return inferred, _field_quality(
        "material_type",
        raw_text,
        inferred,
        source="context_inference" if inferred != "unknown" else "unknown",
        confidence=confidence,
        reason="music_variety_material_split" if inferred != "unknown" else "missing_or_unknown",
        usable_for_ranker=False,
    )


def _normalize_omni_program_context(raw_value: Any, *, row: dict) -> tuple[str, dict]:
    raw_text = _clean_aux_text(raw_value, max_len=48)
    if raw_text and not _is_unknown_text(raw_text):
        return raw_text, _field_quality(
            "program_context",
            raw_text,
            raw_text,
            source="entity_cleanup",
            confidence="medium",
            reason="program_context_cleaned",
            usable_for_ranker=False,
        )
    inferred = _infer_program_context_from_context(row=row)
    confidence = "medium" if inferred else "low"
    return inferred or "unknown", _field_quality(
        "program_context",
        raw_text,
        inferred or "unknown",
        source="context_inference" if inferred else "unknown",
        confidence=confidence,
        reason="program_context_from_title" if inferred else "missing_or_unknown",
        usable_for_ranker=False,
    )


def _normalize_omni_presentation_style(raw_value: Any, *, row: dict, material_type: str) -> tuple[str, dict]:
    raw_text = _field_text(raw_value)
    normalized, reason = _normalize_aux_enum(raw_text, OMNI_PRESENTATION_STYLES, aliases={
        "direct": "direct_cam",
        "live_direct": "direct_cam",
        "直拍": "direct_cam",
        "analysis_review": "analysis",
        "解析": "analysis",
        "reaction": "reaction_review",
        "盘点": "listicle",
        "list": "listicle",
        "teaching": "vocal_lesson",
        "教学": "vocal_lesson",
        "清唱": "a_cappella",
        "program": "program_clip",
        "节目": "program_clip",
        "behind_the_scenes": "behind_scene",
    })
    if normalized != "unknown":
        return normalized, _field_quality(
            "presentation_style",
            raw_text,
            normalized,
            source="enum" if not reason else "aux_alias",
            confidence="medium",
            reason=reason or "enum",
            usable_for_ranker=False,
        )
    inferred = _infer_presentation_style_from_context(row=row, material_type=material_type, raw_value=raw_text)
    confidence = "medium" if inferred != "unknown" else "low"
    return inferred, _field_quality(
        "presentation_style",
        raw_text,
        inferred,
        source="context_inference" if inferred != "unknown" else "unknown",
        confidence=confidence,
        reason="presentation_style_from_material" if inferred != "unknown" else "missing_or_unknown",
        usable_for_ranker=False,
    )


def _normalize_omni_slice_structure(raw_value: Any, *, row: dict, raw_values: dict) -> tuple[str, dict]:
    raw_candidate, raw_quality = _normalize_omni_core_field("slice_structure", raw_value, row=row)
    eval_row = _slice_evaluator_row(row, raw_values=raw_values, current_structure=raw_candidate)
    evaluation = evaluate_slice_structure_row(eval_row)
    suggested = str(evaluation.get("suggested_structure") or "unknown")
    confidence = float(evaluation.get("confidence_score") or 0.0)
    evidence = evaluation.get("evidence") if isinstance(evaluation.get("evidence"), list) else []
    raw_known = raw_candidate != "unknown"
    suggested_known = suggested != "unknown"
    decision = "unknown"
    final_value = "unknown"
    usable_for_ranker = False
    quality_confidence = "low"
    reason = str(raw_quality.get("reason") or "unknown")
    if raw_known and suggested_known and raw_candidate == suggested:
        final_value = raw_candidate
        decision = "agreement"
        quality_confidence = "high"
        usable_for_ranker = True
        reason = "omni_and_rule_agree"
    elif raw_known and not suggested_known:
        final_value = raw_candidate
        decision = "omni_enum_no_rule"
        quality_confidence = "medium" if raw_quality.get("source") in {"enum", "semantic_alias"} else "low"
        usable_for_ranker = bool(raw_quality.get("usable_for_ranker")) and quality_confidence != "low"
        reason = "rule_no_structure_signal"
    elif not raw_known and suggested_known and confidence >= 32.0:
        final_value = suggested
        decision = "rule_rescue_high_confidence"
        quality_confidence = "medium"
        usable_for_ranker = True
        reason = "rule_rescue_from_title_context"
    elif raw_known and suggested_known and raw_candidate != suggested and confidence >= 42.0:
        final_value = suggested
        decision = "rule_override_high_confidence"
        quality_confidence = "medium"
        usable_for_ranker = True
        reason = "high_confidence_rule_override"
    elif raw_known and suggested_known and raw_candidate != suggested:
        decision = "conflict_review"
        reason = f"omni_rule_conflict:{raw_candidate}!={suggested}"
    elif not raw_known and suggested_known:
        decision = "low_confidence_rule_suggestion"
        reason = "rule_suggestion_below_gate"
    return final_value, {
        **raw_quality,
        "normalized_value": final_value,
        "confidence": quality_confidence,
        "reason": reason,
        "usable_for_ranker": usable_for_ranker,
        "ranker_use_scope": "shadow" if usable_for_ranker else "none",
        "production_weight_eligible": False,
        "gate": {
            "decision": decision,
            "omni_candidate": raw_candidate,
            "rule_suggested": suggested,
            "rule_confidence_score": round(confidence, 4),
            "rule_evidence": evidence[:5],
            "rule_status": evaluation.get("status") or "",
        },
    }


def _omni_shadow_ranker_usable(
    field: str,
    *,
    source: str,
    confidence: str,
    normalized_value: Any,
    reason: str,
) -> bool:
    if normalized_value in {"", "unknown", None}:
        return False
    if confidence not in {"high", "medium"}:
        return False
    if field == "content_category":
        return source in {"enum", "semantic_alias", "omni_alias", "context_inference"}
    if field == "hook_type":
        if source in {"enum", "semantic_alias", "omni_alias", "context_inference"}:
            return True
        return False
    if field == "slice_structure":
        return source == "enum" and confidence == "high"
    return False


def _normalize_omni_entity_field(field: str, raw_value: Any) -> tuple[Any, dict]:
    if field == "tags":
        tags = _normalize_omni_tags(raw_value)
        return tags, {
            "field": field,
            "raw_value": raw_value,
            "normalized_value": tags,
            "source": "entity_cleanup",
            "confidence": "medium" if tags else "low",
            "reason": "tags_cleaned" if tags else "missing_or_unknown",
            "usable_for_ranker": False,
            "ranker_use_scope": "none",
            "production_weight_eligible": False,
        }
    raw_text = _field_text(raw_value)
    if _is_unknown_text(raw_text):
        return "", _field_quality(field, raw_text, "", source="unknown", confidence="low", reason="missing_or_unknown", usable_for_ranker=False)
    cleaned = _clean_omni_entity(field, raw_text)
    confidence = "medium" if cleaned else "low"
    reason = "entity_cleaned" if cleaned == raw_text else "entity_filtered_or_cleaned"
    return cleaned, _field_quality(field, raw_text, cleaned, source="entity_cleanup", confidence=confidence, reason=reason, usable_for_ranker=False)


def _field_quality(
    field: str,
    raw_value: Any,
    normalized_value: Any,
    *,
    source: str,
    confidence: str,
    reason: str,
    usable_for_ranker: bool = False,
) -> dict:
    ranker_usable = bool(usable_for_ranker)
    return {
        "field": field,
        "raw_value": raw_value,
        "normalized_value": normalized_value,
        "source": source,
        "confidence": confidence,
        "reason": reason,
        "usable_for_ranker": ranker_usable,
        "ranker_use_scope": "shadow" if ranker_usable else "none",
        "production_weight_eligible": False,
    }


def _lookup_omni_alias(field: str, raw_value: Any) -> str:
    aliases = OMNI_FIELD_ALIASES.get(field) or {}
    text = _field_text(raw_value)
    return aliases.get(text) or aliases.get(_omni_key(text)) or ""


def _infer_core_field_from_context(field: str, *, row: dict, raw_value: Any) -> str:
    text = _semantic_context_text(row, raw_value=raw_value)
    lower = text.lower()
    if field == "content_category":
        if _contains_any(text, ["声乐", "唱歌技巧", "解析", "分析", "教学", "锐评"]):
            return "commentary"
        if _contains_any(text, ["导师", "点评", "评价"]):
            return "judge_comment"
        if _contains_any(text, ["盘点", "合集", "混剪"]):
            return "compilation"
        if _contains_any(text, ["幕后", "花絮", "排练", "彩排", "采访"]):
            return "behind_the_scenes"
        if _contains_any(text, ["电影", "电视剧", "短剧", "影视", "MV"]):
            return "drama_film"
        if _contains_any(text, ["搞笑", "抽象", "整活", "笑拉", "沙雕"]):
            return "humor_entertainment"
        if _contains_any(text, ["世界杯", "足球", "球赛", "蒙超"]):
            return "sports_entertainment"
        if _contains_any(text, ["天赐的声音", "歌手2025", "歌手2026", "有歌第二季", "乘风", "声生不息"]):
            return "music_variety"
        if _contains_any(text, ["舞台", "清唱", "唱", "合唱", "副歌", "现场live", "直拍"]):
            return "performance_clip"
        if _contains_any(lower, ["reaction", "反应"]) or _contains_any(text, ["观众", "全场"]):
            return "reaction"
        return "unknown"
    if field == "hook_type":
        if _contains_any(text, ["搞笑", "抽象", "整活", "笑拉", "沙雕"]):
            return "funny"
        if _contains_any(text, ["世界杯", "足球", "高考", "热点", "出分", "挑战"]):
            return "topical_hook"
        if _contains_any(text, ["高音", "爆发", "炸场", "开口跪", "封神", "唱功", "强混"]):
            return "high_note"
        if _contains_any(lower, ["reaction"]) or _contains_any(text, ["反应", "全场", "观众", "泪目", "尖叫"]):
            return "reaction"
        if _contains_any(text, ["合唱", "对唱", "合作", "联动", "同台", "帮唱", "梦幻联动"]):
            return "celebrity_pairing"
        if _contains_any(text, ["遗憾", "泪", "爱情", "成长", "治愈", "真诚"]):
            return "emotional_story"
        if _contains_any(text, ["副歌", "大合唱"]):
            return "chorus"
        if _contains_any(text, ["导师", "点评", "评价"]):
            return "judge_comment"
        if _contains_any(text, ["声乐", "解析", "分析", "教学", "锐评", "老师"]):
            return "expert_comment"
    return "unknown"


def _normalize_aux_enum(raw_value: Any, allowed: dict[str, str], *, aliases: dict[str, str] | None = None) -> tuple[str, str]:
    raw_text = _field_text(raw_value)
    if _is_unknown_text(raw_text):
        return "unknown", "missing_or_unknown"
    key = _omni_key(raw_text)
    alias_map = aliases or {}
    mapped = alias_map.get(raw_text) or alias_map.get(key) or key
    if mapped in allowed:
        return mapped, "" if mapped == key else f"aux_alias:{raw_text[:40]}"
    if raw_text in allowed:
        return raw_text, ""
    return "unknown", f"unmapped:{raw_text[:40]}"


def _infer_domain_category_from_context(*, row: dict, content_category: str, raw_value: Any) -> str:
    text = _semantic_context_text(row, raw_value=raw_value)
    category = str(content_category or "").strip().lower()
    music_categories = {
        "music_variety",
        "performance_clip",
        "performance_highlight",
        "judge_comment",
        "reaction",
        "commentary",
        "compilation",
        "behind_the_scenes",
    }
    if category in music_categories or _contains_any(
        text,
        [
            "天赐的声音",
            "歌手2025",
            "歌手2026",
            "乘风",
            "国乐无双",
            "声生不息",
            "演唱",
            "舞台",
            "直拍",
            "声乐",
            "唱歌",
            "歌曲",
            "合唱",
            "音乐",
        ],
    ):
        return "music_variety"
    if category in {"entertainment_news", "humor_entertainment"}:
        return "entertainment"
    if category == "drama_film":
        return "drama_film"
    if category in {"lifestyle", "life_emotion"}:
        return "lifestyle"
    if category == "sports_entertainment":
        return "sports_entertainment"
    if category == "creative_ai":
        return "creative_ai"
    if category == "commercial":
        return "commercial"
    return "unknown"


def _infer_material_type_from_context(*, row: dict, content_category: str, raw_value: Any) -> str:
    text = _semantic_context_text(row, raw_value=raw_value)
    lower = text.lower()
    if _contains_any(text, ["声乐教学", "唱歌技巧", "零基础", "混声", "共鸣", "气息", "换声区", "声乐大课", "学唱歌"]):
        return "vocal_teaching"
    if _contains_any(text, ["盘点", "合集", "混剪", "TOP", "top", "十大", "名场面", "年度"]):
        return "compilation"
    if _contains_any(lower, ["reaction"]) or _contains_any(text, ["如何看", "陪你看", "带你看"]):
        return "reaction"
    if _contains_any(text, ["逐帧解析", "声乐老师解析", "乐评", "锐评", "解析", "分析", "点评", "评价", "评论", "老师", "导师"]):
        return "commentary"
    if _contains_any(text, ["幕后", "花絮", "排练", "彩排", "采访"]):
        return "behind_the_scenes"
    if _contains_any(text, ["黑历史", "内娱", "热点", "争夺战", "瓜", "塌房", "明星"]):
        return "entertainment_news"
    if _contains_any(text, ["搞笑", "抽象", "整活", "笑拉", "沙雕"]):
        return "humor_entertainment"
    if _contains_any(text, ["AI", "ai", "AIGC", "二创"]):
        return "creative_ai"
    if _contains_any(text, ["电影", "电视剧", "影视", "短剧", "MV"]):
        return "drama_film"
    if _contains_any(text, ["直拍", "舞台", "现场", "演唱会", "清唱", "live", "Live", "LIVE", "副歌", "合唱", "开口跪", "高光"]):
        return "performance_clip"
    if _infer_program_context_from_context(row=row):
        return "program_context"
    category = str(content_category or "").strip().lower()
    if category in OMNI_MATERIAL_TYPES and category not in {"unknown", "music_variety"}:
        return category
    return "unknown"


def _infer_program_context_from_context(*, row: dict) -> str:
    text = _semantic_context_text(row, raw_value="")
    for name in sorted(OMNI_PROGRAM_CONTEXTS, key=len, reverse=True):
        if name and name in text:
            return name
    return ""


def _infer_presentation_style_from_context(*, row: dict, material_type: str, raw_value: Any) -> str:
    text = _semantic_context_text(row, raw_value=raw_value)
    lower = text.lower()
    if _contains_any(text, ["直拍", "4k直拍", "饭拍"]):
        return "direct_cam"
    if _contains_any(text, ["清唱", "无声卡清唱"]):
        return "a_cappella"
    if material_type == "vocal_teaching" or _contains_any(text, ["声乐教学", "唱歌技巧", "零基础", "混声", "共鸣"]):
        return "vocal_lesson"
    if material_type == "compilation" or _contains_any(text, ["盘点", "合集", "十大", "TOP", "top"]):
        return "listicle"
    if material_type == "reaction" or _contains_any(lower, ["reaction"]) or _contains_any(text, ["如何看", "陪你看", "带你看"]):
        return "reaction_review"
    if material_type == "commentary" or _contains_any(text, ["解析", "分析", "点评", "评价", "乐评", "锐评"]):
        return "analysis"
    if _contains_any(text, ["幕后", "花絮", "排练", "彩排"]):
        return "behind_scene"
    if _infer_program_context_from_context(row=row):
        return "program_clip"
    return "unknown"


def _clean_aux_text(value: Any, *, max_len: int) -> str:
    text = _field_text(value)
    if _is_unknown_text(text):
        return ""
    text = re.sub(r"^[#@]+", "", text).strip()
    if len(text) > max_len or any(marker in text for marker in ["http://", "https://", "\n"]):
        return ""
    return text


def _slice_evaluator_row(row: dict, *, raw_values: dict, current_structure: str) -> dict:
    return {
        "id": row.get("id") or row.get("platform_item_id") or "",
        "title": _semantic_context_text(row, raw_value=""),
        "tags": _field_text(raw_values.get("tags")),
        "content_category": _field_text(raw_values.get("content_category")),
        "hook_type": _field_text(raw_values.get("hook_type")),
        "artist_names": _field_text(raw_values.get("artist_names")),
        "song_title": _field_text(raw_values.get("song_title")),
        "slice_structure": current_structure,
        "classification_confidence": "",
        "raw_json": {},
    }


def _semantic_context_text(row: dict, *, raw_value: Any) -> str:
    values = [
        row.get("title"),
        row.get("video_title"),
        row.get("summary"),
        row.get("transcript"),
        row.get("description"),
        row.get("tags"),
        row.get("program_name"),
        row.get("primary_topic"),
        row.get("music_slice_type"),
        row.get("emotion_type"),
        row.get("short_video_structure"),
        row.get("musical_moment"),
        row.get("program_context"),
        row.get("artist_names"),
        row.get("song_title"),
        raw_value,
    ]
    return " ".join(_field_text(value) for value in values if _field_text(value))


def _clean_omni_entity(field: str, value: Any) -> str:
    text = _field_text(value).strip()
    if _is_unknown_text(text):
        return ""
    text = re.sub(r"^@+", "", text).strip()
    if "创作的原声" in text:
        return ""
    if field == "song_title" and ("#" in text or "@" in text or len(text) > 40):
        return ""
    if field == "artist_names" and ("#" in text or len(text) > 48):
        return ""
    return text


def _normalize_omni_tags(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_tags = [str(item or "").strip() for item in value]
    else:
        raw_tags = [item.strip() for item in re.split(r"[,，、|/]", _field_text(value)) if item.strip()]
    tags = []
    seen = set()
    for tag in raw_tags:
        if _is_unknown_text(tag):
            continue
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags[:24]


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(item or "").strip() for item in value if str(item or "").strip())
    return str(value or "").strip()


def _is_unknown_text(value: Any) -> bool:
    text = _field_text(value)
    return _omni_key(text) in OMNI_UNKNOWN_VALUES or text in OMNI_UNKNOWN_VALUES


def _omni_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "_", text.lower()).strip("_")


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _skipped_payload(
    *,
    entity_type: str,
    entity_id: str,
    reason: str,
    duration_seconds: float,
    max_clip_seconds: float,
    client: QwenOmniClient,
) -> dict:
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "skipped",
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "reason": reason,
        "duration_seconds": round(float(duration_seconds or 0.0), 3),
        "max_clip_seconds": float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS),
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _service_unavailable_payload(*, entity_type: str, entity_id: str, service_status: dict, client: QwenOmniClient) -> dict:
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "service_unavailable",
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "service_status": service_status,
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _model_switch_required_payload(*, entity_type: str, entity_id: str, service_status: dict, client: QwenOmniClient) -> dict:
    loaded_model = _loaded_model_id(_raw_health(service_status))
    return {
        "contract_version": QWEN_OMNI_VERSION,
        "status": "model_switch_required",
        "model": getattr(client, "model_id", QWEN_OMNI_MODEL),
        "mode": "shadow",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "loaded_model": loaded_model,
        "model_switch_required": True,
        "service_status": service_status,
        "writes_labels": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def _service_health(payload: dict, service_url: str) -> dict:
    status = "ready" if str(payload.get("status") or "").lower() in {"ready", "ok", "model", "loaded"} else str(payload.get("status") or "unknown")
    return {"status": status, "service_url": service_url, "raw": payload}


def _service_ready(status: dict) -> bool:
    return str(status.get("status") or "").lower() in {"ready", "ok", "model", "loaded", "heuristic"}


def _raw_health(status: dict) -> dict:
    return status.get("raw") if isinstance(status.get("raw"), dict) else status


def _service_loaded_omni(status: dict) -> bool:
    return _is_omni_model(_loaded_model_id(_raw_health(status)))


def _resource_gate(raw: dict) -> dict:
    device = _primary_cuda_device(raw)
    total = float(device.get("total_memory_gb") or 0.0) if device else 0.0
    required_15 = round(GPTQ_INT4_15S_MIN_VRAM_GB * VRAM_SAFETY_MULTIPLIER, 2)
    required_30 = round(GPTQ_INT4_30S_MIN_VRAM_GB * VRAM_SAFETY_MULTIPLIER, 2)
    bf16_required = round(BF16_15S_MIN_VRAM_GB * VRAM_SAFETY_MULTIPLIER, 2)
    return {
        "cuda_available": bool(((raw.get("torch") or {}).get("cuda_available"))),
        "device": device,
        "total_memory_gb": round(total, 2),
        "gptq_int4_15s_required_gb_with_margin": required_15,
        "gptq_int4_30s_required_gb_with_margin": required_30,
        "bf16_15s_required_gb_with_margin": bf16_required,
        "supports_gptq_int4_15s": total >= required_15,
        "supports_gptq_int4_30s": total >= required_30,
        "supports_bf16_15s": total >= bf16_required,
        "recommended_max_clip_seconds": 30 if total >= required_30 else 15 if total >= required_15 else 0,
    }


def _primary_cuda_device(raw: dict) -> dict:
    devices = ((raw.get("torch") or {}).get("devices") or []) if isinstance(raw.get("torch"), dict) else []
    return devices[0] if devices and isinstance(devices[0], dict) else {}


def _loaded_model_id(raw: dict) -> str:
    model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    return str(model.get("model_id") or raw.get("model_id") or env.get("model_id") or "")


def _is_omni_model(model_id: str) -> bool:
    normalized = str(model_id or "").lower()
    return "qwen2.5-omni" in normalized and ("gptq" in normalized or "awq" in normalized or "int4" in normalized)


def _deployment_status(health: dict, gate: dict, loaded_omni: bool) -> str:
    if str(health.get("status") or "").lower() == "busy":
        return "busy"
    if not _service_ready(health):
        return "service_unavailable"
    if not gate.get("supports_gptq_int4_15s"):
        return "insufficient_vram"
    return "ready" if loaded_omni else "model_switch_required"


def _status_recommendations(health: dict, gate: dict, loaded_omni: bool) -> list[str]:
    recs = []
    if str(health.get("status") or "").lower() == "busy":
        recs.append("Omni 单并发推理正在执行；当前请求应保持规则排序并稍后重试。")
    elif not _service_ready(health):
        recs.append("先启动远程多模态模型服务，并确认 /health 可访问。")
    if not gate.get("supports_gptq_int4_15s"):
        recs.append("当前显存不足以稳定运行 15 秒 GPTQ-Int4 Omni 低显存实验。")
    elif not gate.get("supports_gptq_int4_30s"):
        recs.append("当前资源只建议跑 15 秒以内短片段，batch_size=1。")
    if not loaded_omni:
        recs.append("当前服务未加载 Omni 低显存模型；需要显式 /load 后再跑 shadow 分析。")
    recs.append("Omni 输出仅作为校准建议，不直接写 manual_verified 或生产排序权重。")
    return recs


def _shadow_recommendations(counts: Counter[str], total: int, max_clip_seconds: float) -> list[str]:
    recs = [f"继续保持 shadow mode，短片段上限 {float(max_clip_seconds or DEFAULT_MAX_CLIP_SECONDS):.0f} 秒。"]
    if counts.get("skipped"):
        recs.append("存在超长样本被跳过；低显存版本优先采样 15 秒以内候选。")
    if not total:
        recs.append("当前筛选下没有历史样本，先确认 account_id/dataset_id。")
    recs.append("对比 Omni 建议与人工校准一致率后，再决定是否进入回测特征。")
    return recs


def _duration_seconds(row: dict) -> float:
    value = row.get("duration_seconds")
    try:
        duration = float(value or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0 and row.get("start_time") is not None and row.get("end_time") is not None:
        try:
            duration = max(0.0, float(row.get("end_time") or 0.0) - float(row.get("start_time") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
    return duration


def _tags(values: list[Any]) -> list[str]:
    tags = []
    for value in values:
        if isinstance(value, list):
            parts = value
        else:
            parts = str(value or "").replace("|", ",").replace("，", ",").split(",")
        for part in parts:
            text = str(part or "").strip(" #")
            if text and text not in tags:
                tags.append(text)
    return tags[:12]
