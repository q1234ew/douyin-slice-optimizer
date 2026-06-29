from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from dso.accounts import account_display_name
from dso.config import ensure_data_dirs
from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.utils import new_id, read_json, utc_now, write_json
from dso.versions import HISTORY_CALIBRATION_VERSION, MEMORY_BANK_VERSION


TEXT_EMBEDDING_MODEL = "hashing_text_v1"
TEXT_VECTOR_DIM = 128


def build_text_memory_bank(account_id: str | None = None, *, force: bool = False) -> dict:
    rows = _candidate_rows(account_id=account_id)
    created = 0
    reused = 0
    for row in rows:
        existing = _embedding_record(row["id"])
        if existing and Path(existing["vector_path"]).is_file() and _embedding_fresh(existing, row) and not force:
            reused += 1
            continue
        vector = text_embedding(segment_memory_text(row))
        _store_embedding(row["id"], vector)
        created += 1
    return {
        "contract_version": MEMORY_BANK_VERSION,
        "status": "ready" if rows else "empty",
        "account_id": account_id or "all",
        "model_name": TEXT_EMBEDDING_MODEL,
        "vector_dim": TEXT_VECTOR_DIM,
        "created": created,
        "reused": reused,
        "total_candidates": len(rows),
    }


def calibrate_segment_history(segment_id: str, *, account_id: str | None = None, limit: int = 8) -> dict:
    target = _candidate_row(segment_id)
    if not target:
        raise KeyError(f"segment not found: {segment_id}")
    context = _candidate_context(segment_id) or {}
    account_id = account_id if account_id is not None else (context.get("account_id") or _candidate_account(segment_id))
    target_vector = ensure_text_embedding(segment_id)
    historical_rows, match_scope, fallback_reason = _historical_sample_rows_for_segment(account_id, context)
    if historical_rows:
        return _calibrate_with_historical_samples(
            segment_id,
            account_id=account_id,
            target=target,
            target_vector=target_vector,
            samples=historical_rows,
            match_scope=match_scope,
            fallback_reason=fallback_reason,
            limit=limit,
        )
    return _calibrate_with_training_samples(segment_id, account_id=account_id, target_vector=target_vector, limit=limit)


def _calibrate_with_training_samples(
    segment_id: str,
    *,
    account_id: str | None,
    target_vector: list[float],
    limit: int,
) -> dict:
    samples = _training_sample_rows(account_id=account_id, exclude_segment_id=segment_id)
    rewards = [float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0) for sample in samples]
    high_threshold, low_threshold = _relative_thresholds(rewards)
    matches = []
    for sample in samples:
        vector = ensure_text_embedding(sample["candidate_segment_id"])
        similarity = cosine_similarity(target_vector, vector)
        reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0)
        matches.append(
            {
                "candidate_segment_id": segment_id,
                "matched_segment_id": sample["candidate_segment_id"],
                "training_sample_id": sample["id"],
                "similarity": round(similarity, 4),
                "reward_proxy": float(sample.get("reward_proxy") or 0),
                "normalized_reward": reward,
                "sample_source": sample.get("sample_source") or "",
                "label_window": sample.get("label_window") or "",
                "account_id": sample.get("account_id") or "",
                "account_display_name": account_display_name(sample.get("account_id") or ""),
                "match_type": _match_type(reward, high_threshold=high_threshold, low_threshold=low_threshold),
                "music_slice_type": sample.get("music_slice_type") or "unknown",
            }
        )
    matches.sort(key=lambda row: (row["similarity"], row["normalized_reward"]), reverse=True)
    selected = matches[: max(1, int(limit or 8))]
    _record_history_matches(segment_id, selected)
    high = [row for row in selected if row["match_type"] == "high"]
    low = [row for row in selected if row["match_type"] == "low"]
    similar_high = max((row["similarity"] * row["normalized_reward"] for row in high), default=0.0)
    similar_low = max((row["similarity"] * (100.0 - row["normalized_reward"]) for row in low), default=0.0)
    uncertainty = _history_uncertainty(len(samples), selected)
    if not selected:
        status = "insufficient_history"
    elif len(samples) < 3:
        status = "low_confidence"
    else:
        status = "ready"
    return {
        "contract_version": HISTORY_CALIBRATION_VERSION,
        "status": status,
        "segment_id": segment_id,
        "account_id": account_id or "all",
        "model_name": TEXT_EMBEDDING_MODEL,
        "history_source": "training_samples",
        "match_scope": "account" if account_id else "all",
        "sample_count": len(samples),
        "matched_count": len(selected),
        "similar_high_perf_score": round(similar_high, 4),
        "similar_low_perf_risk": round(similar_low, 4),
        "history_uncertainty": uncertainty,
        "matches": selected,
    }


def _calibrate_with_historical_samples(
    segment_id: str,
    *,
    account_id: str | None,
    target: dict,
    target_vector: list[float],
    samples: list[dict],
    match_scope: str,
    fallback_reason: str,
    limit: int,
) -> dict:
    rewards = [float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0) for sample in samples]
    high_threshold, low_threshold = _relative_thresholds(rewards)
    target_text = segment_memory_text(target)
    matches = []
    for sample in samples:
        sample_text = _historical_sample_text(sample)
        vector = text_embedding(sample_text)
        similarity = cosine_similarity(target_vector, vector)
        keyword_overlap = _token_overlap(target_text, sample_text)
        blended = max(similarity, keyword_overlap)
        reward = float(sample.get("normalized_reward") or sample.get("reward_proxy") or 0)
        matches.append(
            {
                "candidate_segment_id": segment_id,
                "matched_segment_id": None,
                "training_sample_id": None,
                "historical_sample_id": sample.get("id") or "",
                "matched_sample_id": sample.get("id") or "",
                "matched_platform_item_id": sample.get("platform_item_id") or "",
                "platform_item_id": sample.get("platform_item_id") or "",
                "account_id": sample.get("account_id") or "",
                "account_display_name": account_display_name(sample.get("account_id") or ""),
                "dataset_id": sample.get("dataset_id") or "",
                "title": sample.get("title") or "",
                "url": sample.get("platform_url") or "",
                "similarity": round(blended, 4),
                "embedding_similarity": round(similarity, 4),
                "keyword_overlap": round(keyword_overlap, 4),
                "reward_proxy": float(sample.get("reward_proxy") or 0),
                "normalized_reward": reward,
                "sample_source": sample.get("source_kind") or "historical_capture",
                "label_window": sample.get("metric_window") or "lifetime/current_visible",
                "performance_label": sample.get("performance_label") or "",
                "match_type": sample.get("performance_label") or _match_type(reward, high_threshold=high_threshold, low_threshold=low_threshold),
                "music_slice_type": sample.get("content_category") or sample.get("hook_type") or "unknown",
                "content_category": sample.get("content_category") or "",
                "hook_type": sample.get("hook_type") or "",
                "slice_structure": sample.get("slice_structure") or "",
                "artist_names": sample.get("artist_names") or "",
                "song_title": sample.get("song_title") or "",
            }
        )
    matches.sort(key=lambda row: (row["similarity"], row["normalized_reward"]), reverse=True)
    selected = matches[: max(1, int(limit or 8))]
    _record_history_matches(segment_id, selected)
    high = [row for row in selected if row["match_type"] == "high"]
    low = [row for row in selected if row["match_type"] == "low"]
    similar_high = max((row["similarity"] * row["normalized_reward"] for row in high), default=0.0)
    similar_low = max((row["similarity"] * (100.0 - row["normalized_reward"]) for row in low), default=0.0)
    uncertainty = _history_uncertainty(len(samples), selected)
    status = "ready"
    if len(samples) < 50 or match_scope != "account":
        status = "low_confidence"
    if not selected:
        status = "insufficient_history"
    return {
        "contract_version": HISTORY_CALIBRATION_VERSION,
        "status": status,
        "segment_id": segment_id,
        "account_id": account_id or "all",
        "model_name": TEXT_EMBEDDING_MODEL,
        "history_source": "historical_capture_samples",
        "match_scope": match_scope,
        "fallback_reason": fallback_reason,
        "sample_count": len(samples),
        "matched_count": len(selected),
        "similar_high_perf_score": round(similar_high, 4),
        "similar_low_perf_risk": round(similar_low, 4),
        "history_uncertainty": uncertainty,
        "matches": selected,
    }


def ensure_text_embedding(segment_id: str) -> list[float]:
    record = _embedding_record(segment_id)
    row = _candidate_row(segment_id)
    if not row:
        raise KeyError(f"segment not found: {segment_id}")
    if record and Path(record["vector_path"]).is_file():
        data = read_json(Path(record["vector_path"]), default={}) or {}
        vector = data.get("vector")
        if isinstance(vector, list) and len(vector) == TEXT_VECTOR_DIM and data.get("content_hash") == _content_hash(row):
            return [float(value) for value in vector]
    vector = text_embedding(segment_memory_text(row))
    _store_embedding(segment_id, vector)
    return vector


def text_embedding(text: str, *, dim: int = TEXT_VECTOR_DIM) -> list[float]:
    vector = [0.0] * dim
    tokens = _tokens(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        weight = 1.0 + min(1.0, len(token) / 8.0)
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 6) for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)))


def segment_memory_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "transcript",
            "summary",
            "music_slice_type",
            "emotion_type",
            "short_video_structure",
            "musical_moment",
            "program_context",
            "comment_trigger",
        ]
    )


def _candidate_rows(account_id: str | None = None) -> list[dict]:
    query = """
        SELECT c.*, v.account_id
        FROM candidate_segments c
        JOIN source_videos v ON v.id = c.source_video_id
    """
    params: list[Any] = []
    if account_id:
        query += " WHERE v.account_id = ?"
        params.append(account_id)
    query += " ORDER BY c.created_at DESC"
    with connect() as conn:
        return fetch_all(conn, query, params)


def _candidate_row(segment_id: str) -> dict | None:
    with connect() as conn:
        return fetch_one(conn, "SELECT * FROM candidate_segments WHERE id = ?", [segment_id])


def _candidate_account(segment_id: str) -> str | None:
    with connect() as conn:
        row = fetch_one(
            conn,
            """
            SELECT v.account_id
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            WHERE c.id = ?
            """,
            [segment_id],
        )
    return row.get("account_id") if row else None


def _candidate_context(segment_id: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT c.*, v.account_id, v.title AS video_title
            FROM candidate_segments c
            JOIN source_videos v ON v.id = c.source_video_id
            WHERE c.id = ?
            """,
            [segment_id],
        )


def _historical_sample_rows_for_segment(account_id: str | None, context: dict) -> tuple[list[dict], str, str]:
    account = (account_id or "").strip()
    if account and account.lower() not in {"all", "main"}:
        rows = _historical_sample_rows(account_id=account)
        if len(rows) >= 50:
            return rows, "account", ""
        if rows:
            fallback = _historical_sample_rows(account_id=None)
            return (fallback or rows), "global_fallback", "target_account_below_50_samples"
    if account and account.lower() == "main":
        rows = _historical_sample_rows(account_id=account)
        if len(rows) >= 50:
            return rows, "account", ""
    rows = _historical_sample_rows(account_id=None)
    if rows:
        return rows, "global", "target_account_missing_or_below_threshold"
    return [], "none", "no_historical_capture_samples"


def _historical_sample_rows(account_id: str | None = None) -> list[dict]:
    clauses = [
        "COALESCE(platform_item_id, '') != ''",
        "(COALESCE(reward_proxy, 0) > 0 OR COALESCE(normalized_reward, 0) > 0 OR COALESCE(likes, 0) > 0 OR COALESCE(comments, 0) > 0 OR COALESCE(favorites, 0) > 0 OR COALESCE(shares, 0) > 0)",
    ]
    params: list[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    with connect() as conn:
        return fetch_all(
            conn,
            f"""
            SELECT *
            FROM historical_capture_samples
            WHERE {' AND '.join(clauses)}
            ORDER BY reward_proxy DESC, updated_at DESC
            """,
            params,
        )


def _training_sample_rows(account_id: str | None = None, exclude_segment_id: str | None = None) -> list[dict]:
    query = """
        SELECT ts.*, c.transcript, c.summary, c.music_slice_type, c.emotion_type,
               c.short_video_structure, c.musical_moment, c.program_context, c.comment_trigger,
               v.account_id
        FROM training_samples ts
        JOIN candidate_segments c ON c.id = ts.candidate_segment_id
        JOIN source_videos v ON v.id = c.source_video_id
        WHERE ts.candidate_segment_id IS NOT NULL
          AND ts.candidate_segment_id != ''
          AND ts.sample_source != 'mock'
    """
    params: list[Any] = []
    if account_id:
        query += " AND v.account_id = ?"
        params.append(account_id)
    if exclude_segment_id:
        query += " AND ts.candidate_segment_id != ?"
        params.append(exclude_segment_id)
    query += " ORDER BY ts.created_at DESC"
    with connect() as conn:
        return fetch_all(conn, query, params)


def _historical_sample_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "title",
            "tags",
            "artist_names",
            "song_title",
            "program_name",
            "content_category",
            "hook_type",
            "slice_structure",
        ]
    )


def _embedding_record(segment_id: str) -> dict | None:
    with connect() as conn:
        return fetch_one(
            conn,
            """
            SELECT * FROM clip_embeddings
            WHERE candidate_segment_id = ? AND embedding_type = 'text' AND model_name = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [segment_id, TEXT_EMBEDDING_MODEL],
        )


def _store_embedding(segment_id: str, vector: list[float]) -> None:
    settings = ensure_data_dirs()
    row = _candidate_row(segment_id) or {}
    vector_path = settings.cache_dir / "memory" / "text" / f"{segment_id}.json"
    write_json(
        vector_path,
        {
            "contract_version": MEMORY_BANK_VERSION,
            "candidate_segment_id": segment_id,
            "model_name": TEXT_EMBEDDING_MODEL,
            "vector_dim": TEXT_VECTOR_DIM,
            "content_hash": _content_hash(row),
            "vector": vector,
            "created_at": utc_now(),
        },
    )
    with connect() as conn:
        conn.execute(
            "DELETE FROM clip_embeddings WHERE candidate_segment_id = ? AND embedding_type = 'text' AND model_name = ?",
            [segment_id, TEXT_EMBEDDING_MODEL],
        )
        insert_row(
            conn,
            "clip_embeddings",
            {
                "id": new_id("emb"),
                "candidate_segment_id": segment_id,
                "embedding_type": "text",
                "model_name": TEXT_EMBEDDING_MODEL,
                "vector_path": str(vector_path),
                "vector_dim": TEXT_VECTOR_DIM,
                "created_at": utc_now(),
            },
        )
        conn.commit()


def _record_history_matches(segment_id: str, matches: list[dict]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM history_matches WHERE candidate_segment_id = ?", [segment_id])
        for match in matches:
            insert_row(
                conn,
                "history_matches",
                {
                    "id": new_id("hist"),
                    "candidate_segment_id": segment_id,
                    "matched_segment_id": match.get("matched_segment_id"),
                    "training_sample_id": match.get("training_sample_id"),
                    "match_type": match.get("match_type") or "neutral",
                    "similarity": float(match.get("similarity") or 0),
                    "reward_proxy": float(match.get("reward_proxy") or 0),
                    "normalized_reward": float(match.get("normalized_reward") or 0),
                    "sample_source": match.get("sample_source") or "",
                    "model_name": TEXT_EMBEDDING_MODEL,
                    "version": HISTORY_CALIBRATION_VERSION,
                    "created_at": utc_now(),
                },
            )
        conn.commit()


def _embedding_fresh(record: dict, row: dict) -> bool:
    data = read_json(Path(record["vector_path"]), default={}) or {}
    return data.get("content_hash") == _content_hash(row) and data.get("model_name") == TEXT_EMBEDDING_MODEL


def _content_hash(row: dict) -> str:
    return hashlib.sha256(segment_memory_text(row).encode("utf-8")).hexdigest()[:16]


def _relative_thresholds(rewards: list[float]) -> tuple[float, float]:
    if not rewards:
        return 60.0, 40.0
    ordered = sorted(float(value) for value in rewards)
    if len(ordered) == 1:
        return ordered[0], ordered[0]
    return _percentile(ordered, 0.60), _percentile(ordered, 0.40)


def _percentile(ordered: list[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _match_type(reward: float, *, high_threshold: float, low_threshold: float) -> str:
    if reward >= high_threshold:
        return "high"
    if reward <= low_threshold:
        return "low"
    return "neutral"


def _history_uncertainty(sample_count: int, matches: list[dict]) -> float:
    if sample_count <= 0:
        return 1.0
    coverage = min(1.0, sample_count / 10.0)
    best_similarity = max((float(row.get("similarity") or 0) for row in matches), default=0.0)
    return round(max(0.0, min(1.0, 1.0 - 0.55 * coverage - 0.35 * best_similarity)), 4)


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return round(len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens)), 4)


def _tokens(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text.lower()).strip()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", cleaned)
    tokens: list[str] = []
    tokens.extend(words)
    chinese_chars = [word for word in words if len(word) == 1 and "\u4e00" <= word <= "\u9fff"]
    tokens.extend("".join(chinese_chars[index : index + 2]) for index in range(max(0, len(chinese_chars) - 1)))
    tokens.extend("".join(chinese_chars[index : index + 3]) for index in range(max(0, len(chinese_chars) - 2)))
    return [token for token in tokens if token]
