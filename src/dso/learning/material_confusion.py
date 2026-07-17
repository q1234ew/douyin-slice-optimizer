from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from dso.db.session import connect, fetch_all
from dso.learning.material_calibration import material_gold_annotation_index
from dso.learning.material_taxonomy import (
    MATERIAL_FORM_LABELS_ZH,
    MATERIAL_FORM_TYPES,
    canonical_material_type,
    material_form_options,
    material_taxonomy_derivation,
    material_type_taxonomy_relation,
)
from dso.learning.multimodal_validation import _build_asset_index
from dso.learning.qwen_omni import qwen_omni_shadow_cache_index, refresh_omni_shadow_for_row
from dso.utils import utc_now
from dso.versions import DOUYIN_HISTORY_VERSION


MATERIAL_CONFUSION_QUEUE_VERSION = "material_confusion_queue.v2"
CROSS_DOMAIN_CONFUSION_PAIR = "cross_domain_material"
CROSS_DOMAIN_CONFUSION_LABEL_ZH = "跨领域/形态分歧"
MATERIAL_CONFUSION_PAIRS: dict[str, dict[str, Any]] = {
    "reaction_vocal_teaching": {
        "label_zh": "Reaction / 声乐教学",
        "left": "reaction",
        "right": "vocal_teaching",
        "left_cues": ["reaction", "锐评", "点评", "解析", "分析", "带你看", "看完", "听完", "逐帧"],
        "right_cues": ["教学", "声乐", "唱法", "怎么唱", "如何唱", "发声", "气息", "练声", "技巧", "混声", "音准"],
    },
    "reaction_compilation": {
        "label_zh": "Reaction / 合集盘点",
        "left": "reaction",
        "right": "compilation",
        "left_cues": ["reaction", "锐评", "点评", "解析", "分析", "看完", "听完", "逐帧"],
        "right_cues": ["盘点", "合集", "top", "排名", "汇总", "一口气", "全场", "名场面", "上篇", "下篇"],
    },
    "compilation_entertainment_news": {
        "label_zh": "合集盘点 / 娱乐资讯",
        "left": "compilation",
        "right": "entertainment_news",
        "left_cues": ["盘点", "合集", "top", "排名", "汇总", "一口气", "名场面", "上篇", "下篇"],
        "right_cues": ["资讯", "热点", "事件", "回应", "官宣", "争议", "爆料", "消息", "近况", "引发", "预测"],
    },
    "behind_the_scenes_performance": {
        "label_zh": "幕后花絮 / 舞台演唱",
        "left": "behind_the_scenes",
        "right": "performance_clip",
        "left_cues": ["幕后", "后台", "花絮", "采访", "排练", "彩排", "准备", "感言", "坦言", "没想到"],
        "right_cues": ["现场", "舞台", "演唱", "演唱会", "直拍", "live", "合唱", "清唱"],
    },
    "performance_program_context": {
        "label_zh": "舞台演唱 / 节目语境",
        "left": "performance_clip",
        "right": "program_context",
        "left_cues": ["现场", "舞台", "演唱", "演唱会", "直拍", "live", "合唱", "清唱"],
        "right_cues": ["节目", "歌手2026", "天赐的声音", "乘风2026", "音综", "赛段", "第期", "排名"],
    },
}


def material_confusion_queue(
    account_id: str | None = None,
    *,
    dataset_id: str | None = None,
    confusion_pair: str | None = None,
    limit: int = 80,
    local_media_only: bool = True,
    include_reviewed: bool = False,
) -> dict:
    cap = max(1, min(100, int(limit or 80)))
    pair_key = str(confusion_pair or "").strip()
    allowed_pairs = [*MATERIAL_CONFUSION_PAIRS, CROSS_DOMAIN_CONFUSION_PAIR]
    if pair_key and pair_key not in allowed_pairs:
        raise ValueError(f"confusion_pair must be one of: {', '.join(allowed_pairs)}")
    rows = _historical_confusion_rows(account_id=account_id, dataset_id=dataset_id)
    omni_index = qwen_omni_shadow_cache_index()
    annotations = material_gold_annotation_index(confirmed_only=False)
    asset_index = _build_asset_index()
    candidates: list[dict] = []
    reviewed_excluded = 0
    no_local_media_excluded = 0
    no_omni_excluded = 0
    for row in rows:
        sample_id = str(row.get("id") or "")
        annotation = annotations.get(sample_id) or {}
        if annotation.get("review_status") == "confirmed" and not include_reviewed:
            reviewed_excluded += 1
            continue
        platform_item_id = str(row.get("platform_item_id") or "")
        omni = omni_index.get(sample_id) or omni_index.get(platform_item_id) or {}
        if not omni:
            no_omni_excluded += 1
            continue
        omni = refresh_omni_shadow_for_row(omni, row)
        assets = _asset_contract(asset_index.get(platform_item_id) or {})
        if local_media_only and not assets["video"]:
            no_local_media_excluded += 1
            continue
        candidate = _confusion_candidate(row, omni=omni, assets=assets, annotation=annotation)
        if not candidate:
            continue
        if pair_key and candidate["confusion_pair"] != pair_key:
            continue
        candidates.append(candidate)

    selected = _balanced_confusion_selection(candidates, limit=cap, prioritize_reviewed=include_reviewed)
    pair_counts = Counter(str(item.get("confusion_pair") or "unknown") for item in selected)
    account_counts = Counter(str(item.get("account_id") or "unknown") for item in selected)
    ready_count = sum(1 for item in selected if (item.get("assets") or {}).get("ready_for_evidence"))
    known_confusions = _known_gold_confusion_summary(rows, omni_index=omni_index, annotations=annotations)
    gold_scope = _gold_queue_coverage_summary(
        rows,
        annotations=annotations,
        selected=selected,
        eligible_sample_ids={str(item.get("sample_id") or "") for item in candidates} if pair_key else None,
    )
    return {
        "contract_version": DOUYIN_HISTORY_VERSION,
        "queue_version": MATERIAL_CONFUSION_QUEUE_VERSION,
        "status": "ready" if selected else "empty",
        "mode": "material_confusion_targeted_gold",
        "account_id": account_id or "all",
        "dataset_id": dataset_id or "all",
        "confusion_pair": pair_key or "all",
        "include_reviewed": bool(include_reviewed),
        "count": len(selected),
        "total_candidates": len(candidates),
        "taxonomy": {
            "material_form_options": material_form_options(),
            "non_form_values": ["program_context"],
            "detail_fields": ["highlight_signal"],
            "program_context_is_separate": True,
            "source_labels_are_rewritten": False,
        },
        "confusion_pairs": [
            {
                "key": key,
                "label_zh": value["label_zh"],
                "left": value["left"],
                "right": value["right"],
            }
            for key, value in MATERIAL_CONFUSION_PAIRS.items()
        ]
        + [
            {
                "key": CROSS_DOMAIN_CONFUSION_PAIR,
                "label_zh": CROSS_DOMAIN_CONFUSION_LABEL_ZH,
                "left": "dynamic_observed_label",
                "right": "dynamic_omni_label",
                "dynamic": True,
            }
        ],
        "batch_summary": {
            "selected_count": len(selected),
            "candidate_count": len(candidates),
            "pair_counts": dict(pair_counts),
            "account_count": len(account_counts),
            "max_account_count": max(account_counts.values(), default=0),
            "local_media_ready_count": ready_count,
            "local_media_ready_rate": round(ready_count / max(1, len(selected)), 4),
            "reviewed_excluded_count": reviewed_excluded,
            "no_omni_excluded_count": no_omni_excluded,
            "no_local_media_excluded_count": no_local_media_excluded,
            "known_gold_confusions": known_confusions,
            "gold_queue_includes_reviewed": bool(include_reviewed),
            "gold_coverage_scope": "confusion_pair" if pair_key else "account_dataset",
            **gold_scope,
        },
        "samples": selected,
        "recommended_next_action": "review_pair_evidence_then_run_confusion_resolver_shadow",
        "writes_main_semantic_labels": False,
        "rewrites_existing_gold": False,
        "production_weight": False,
        "generated_at": utc_now(),
    }


def material_taxonomy_contract() -> dict:
    return {
        "contract_version": MATERIAL_CONFUSION_QUEUE_VERSION,
        "material_form_types": list(MATERIAL_FORM_TYPES),
        "material_form_labels_zh": dict(MATERIAL_FORM_LABELS_ZH),
        "legacy_derivations": {
            "performance_highlight": {
                "canonical_material_type": "performance_clip",
                "highlight_signal": "highlight",
            },
            "judge_comment": {
                "canonical_material_type": "commentary",
                "detail_signal": "judge_comment",
            },
            "program_context": {
                "canonical_material_type": "unknown",
                "program_context_is_separate": True,
            },
        },
        "rewrites_source_labels": False,
    }


def _historical_confusion_rows(*, account_id: str | None, dataset_id: str | None) -> list[dict]:
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
    with connect() as conn:
        return fetch_all(
            conn,
            f"""
            SELECT id, account_id, dataset_id, platform_item_id, platform_url, title, tags,
                   artist_names, song_title, content_category, hook_type, slice_structure,
                   program_name, performance_label, normalized_reward, reward_proxy,
                   classification_confidence, raw_json, published_at
            FROM historical_capture_samples
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(normalized_reward, reward_proxy, 0) DESC, published_at DESC
            """,
            params,
        )


def _confusion_candidate(row: dict, *, omni: dict, assets: dict, annotation: dict) -> dict | None:
    suggestions = omni.get("semantic_suggestions") if isinstance(omni.get("semantic_suggestions"), dict) else {}
    raw_material = str(suggestions.get("material_type") or "unknown").strip().lower()
    derivation = material_taxonomy_derivation(raw_material, program_context=suggestions.get("program_context"))
    existing_category = str(row.get("content_category") or "unknown").strip().lower()
    text = _confusion_text(row, suggestions)
    matches: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
    for key, definition in MATERIAL_CONFUSION_PAIRS.items():
        left = str(definition["left"])
        right = str(definition["right"])
        left_hits = _cue_hits(text, definition["left_cues"])
        right_hits = _cue_hits(text, definition["right_cues"])
        score = 0.0
        score += 34.0 if raw_material in {left, right} else 0.0
        score += 18.0 if existing_category in {left, right} else 0.0
        score += min(18.0, len(left_hits) * 4.5)
        score += min(18.0, len(right_hits) * 4.5)
        score += 14.0 if left_hits and right_hits else 0.0
        score += 8.0 if raw_material and existing_category not in {"", "unknown", raw_material} else 0.0
        if key == "performance_program_context" and (raw_material == "program_context" or suggestions.get("program_context") not in {"", "unknown", None}):
            score += 20.0
        if score < 28.0:
            continue
        matches.append(
            (
                score,
                key,
                {
                    "left_hits": left_hits,
                    "right_hits": right_hits,
                    "left_type": left,
                    "right_type": right,
                },
                definition,
            )
        )
    if not matches:
        observed_material = canonical_material_type(existing_category)
        omni_material = canonical_material_type(raw_material)
        if observed_material and omni_material and observed_material != omni_material:
            left_cues = _material_type_cues(observed_material)
            right_cues = _material_type_cues(omni_material)
            left_hits = _cue_hits(text, left_cues)
            right_hits = _cue_hits(text, right_cues)
            definition = {
                "label_zh": CROSS_DOMAIN_CONFUSION_LABEL_ZH,
                "left": observed_material,
                "right": omni_material,
                "left_cues": left_cues,
                "right_cues": right_cues,
                "dynamic": True,
            }
            score = 42.0 + min(18.0, len(left_hits) * 4.5) + min(18.0, len(right_hits) * 4.5)
            matches.append(
                (
                    score,
                    CROSS_DOMAIN_CONFUSION_PAIR,
                    {
                        "left_hits": left_hits,
                        "right_hits": right_hits,
                        "left_type": observed_material,
                        "right_type": omni_material,
                    },
                    definition,
                )
            )
    if not matches:
        return None
    score, pair_key, evidence, definition = max(matches, key=lambda item: (item[0], item[1]))
    pair_values = [str(definition["left"]), str(definition["right"])]
    material_candidates = [value for value in pair_values if value in MATERIAL_FORM_TYPES and value != "unknown"]
    media_bonus = 12.0 if assets.get("video") else 0.0
    evidence_bonus = 6.0 if assets.get("audio") else 0.0
    evidence_bonus += 6.0 if assets.get("transcript") or assets.get("ocr") else 0.0
    uncertainty_bonus = 8.0 if existing_category not in {"", "unknown", raw_material} else 0.0
    priority = min(100.0, score + media_bonus + evidence_bonus + uncertainty_bonus)
    return {
        "sample_id": row.get("id") or "",
        "platform_item_id": row.get("platform_item_id") or "",
        "account_id": row.get("account_id") or "",
        "dataset_id": row.get("dataset_id") or "",
        "title": row.get("title") or "",
        "platform_url": row.get("platform_url") or "",
        "published_at": row.get("published_at") or "",
        "performance_label": row.get("performance_label") or "",
        "normalized_reward": round(float(row.get("normalized_reward") or row.get("reward_proxy") or 0.0), 4),
        "content_category": existing_category,
        "domain_category": suggestions.get("domain_category") or "unknown",
        "material_type": raw_material,
        "program_context": suggestions.get("program_context") or "unknown",
        "presentation_style": suggestions.get("presentation_style") or "unknown",
        "omni_raw_material_type": raw_material,
        "omni_canonical_material_type": derivation["canonical_material_type"],
        "omni_highlight_signal": derivation["highlight_signal"],
        "omni_program_context": derivation["program_context"],
        "taxonomy_derivation_reason": derivation["derivation_reason"],
        "confusion_pair": pair_key,
        "confusion_pair_label_zh": definition["label_zh"],
        "pair_definition": {
            "left": definition["left"],
            "right": definition["right"],
            "left_cues": list(definition.get("left_cues") or []),
            "right_cues": list(definition.get("right_cues") or []),
            "dynamic": bool(definition.get("dynamic")),
        },
        "candidate_material_types": material_candidates,
        "candidate_material_labels_zh": [
            MATERIAL_FORM_LABELS_ZH.get(value, value)
            for value in material_candidates
        ],
        "candidate_context_fields": ["program_context"] if "program_context" in pair_values else [],
        "cue_evidence": evidence,
        "assets": assets,
        "priority_score": round(priority, 2),
        "queue_reason": (
            "cross_domain_material_disagreement"
            if pair_key == CROSS_DOMAIN_CONFUSION_PAIR
            else "targeted_material_confusion"
        ),
        "recommended_fields": (
            ["material_type", "domain_category"]
            if pair_key == CROSS_DOMAIN_CONFUSION_PAIR
            else ["material_type", "highlight_signal", "program_context"]
        ),
        "annotation": annotation or None,
        "writes_main_semantic_labels": False,
        "production_weight": False,
    }


def _balanced_confusion_selection(
    candidates: list[dict],
    *,
    limit: int,
    prioritize_reviewed: bool = False,
) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        grouped[str(candidate.get("confusion_pair") or "unknown")].append(candidate)
    for items in grouped.values():
        items.sort(
            key=lambda item: _confusion_selection_sort_key(item, prioritize_reviewed=prioritize_reviewed),
            reverse=True,
        )
    pair_keys = [key for key in [*MATERIAL_CONFUSION_PAIRS, CROSS_DOMAIN_CONFUSION_PAIR] if grouped.get(key)]
    selected: list[dict] = []
    seen_groups: set[str] = set()
    account_counts: Counter[str] = Counter()
    per_account_cap = max(3, int(math.ceil(limit / 10)))
    pair_target = max(8, int(math.ceil(limit / max(1, len(pair_keys)))))
    pair_counts: Counter[str] = Counter()

    if prioritize_reviewed:
        reviewed = sorted(
            (
                item
                for item in candidates
                if (item.get("annotation") or {}).get("review_status") == "confirmed"
            ),
            key=lambda item: _confusion_selection_sort_key(item, prioritize_reviewed=True),
            reverse=True,
        )
        for item in reviewed:
            group_key = _stable_confusion_group_key(item)
            if group_key in seen_groups:
                continue
            selected.append(item)
            seen_groups.add(group_key)
            account_counts[str(item.get("account_id") or "unknown")] += 1
            pair_counts[str(item.get("confusion_pair") or "unknown")] += 1
            if len(selected) >= limit:
                return selected

    for relaxed in (False, True):
        made_progress = True
        while made_progress and len(selected) < limit:
            made_progress = False
            for pair_key in pair_keys:
                if not relaxed and pair_counts[pair_key] >= pair_target:
                    continue
                items = grouped[pair_key]
                chosen = None
                for item in items:
                    group_key = _stable_confusion_group_key(item)
                    account = str(item.get("account_id") or "unknown")
                    if group_key in seen_groups:
                        continue
                    if not relaxed and account_counts[account] >= per_account_cap:
                        continue
                    chosen = item
                    break
                if chosen is None:
                    continue
                selected.append(chosen)
                seen_groups.add(_stable_confusion_group_key(chosen))
                account_counts[str(chosen.get("account_id") or "unknown")] += 1
                pair_counts[pair_key] += 1
                made_progress = True
                if len(selected) >= limit:
                    break
    return selected


def _confusion_selection_sort_key(item: dict, *, prioritize_reviewed: bool) -> tuple[Any, ...]:
    annotation = item.get("annotation") if isinstance(item.get("annotation"), dict) else {}
    confirmed = annotation.get("review_status") == "confirmed"
    gold_evaluable = bool(canonical_material_type(annotation.get("material_type"))) if confirmed else False
    return (
        1 if prioritize_reviewed and gold_evaluable else 0,
        1 if prioritize_reviewed and confirmed else 0,
        float(item.get("priority_score") or 0.0),
        float(item.get("normalized_reward") or 0.0),
        str(item.get("sample_id") or ""),
    )


def _material_type_cues(material_type: str) -> list[str]:
    cues: list[str] = []
    for definition in MATERIAL_CONFUSION_PAIRS.values():
        if str(definition.get("left") or "") == material_type:
            cues.extend(str(value) for value in definition.get("left_cues") or [])
        if str(definition.get("right") or "") == material_type:
            cues.extend(str(value) for value in definition.get("right_cues") or [])
    return list(dict.fromkeys(cues))


def _gold_queue_coverage_summary(
    rows: list[dict],
    *,
    annotations: dict[str, dict],
    selected: list[dict],
    eligible_sample_ids: set[str] | None = None,
) -> dict:
    row_index = {str(row.get("id") or ""): row for row in rows}
    grouped: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    raw_evaluable_count = 0
    raw_confirmed_count = 0
    for sample_id, annotation in annotations.items():
        if annotation.get("review_status") != "confirmed":
            continue
        if eligible_sample_ids is not None and str(sample_id) not in eligible_sample_ids:
            continue
        row = row_index.get(str(sample_id))
        if not row:
            continue
        raw_confirmed_count += 1
        if canonical_material_type(annotation.get("material_type")):
            raw_evaluable_count += 1
        grouped[_stable_confusion_group_key(row)].append((str(sample_id), annotation))

    evaluable_groups = {
        group_key
        for group_key, members in grouped.items()
        if any(canonical_material_type(annotation.get("material_type")) for _sample_id, annotation in members)
    }
    selected_group_keys = {
        _stable_confusion_group_key(item)
        for item in selected
        if canonical_material_type(((item.get("annotation") or {}).get("material_type")))
    }
    admitted_groups = evaluable_groups & selected_group_keys
    cross_domain_groups = {
        _stable_confusion_group_key(item)
        for item in selected
        if item.get("confusion_pair") == CROSS_DOMAIN_CONFUSION_PAIR
        and canonical_material_type(((item.get("annotation") or {}).get("material_type")))
    }
    effective_confirmed_count = len(grouped)
    effective_evaluable_count = len(evaluable_groups)
    admitted_count = len(admitted_groups)
    return {
        "gold_confirmed_count": raw_confirmed_count,
        "gold_effective_confirmed_count": effective_confirmed_count,
        "gold_raw_evaluable_count": raw_evaluable_count,
        "gold_evaluable_count": effective_evaluable_count,
        "gold_label_abstention_count": max(0, effective_confirmed_count - effective_evaluable_count),
        "gold_duplicate_collapsed_count": max(0, raw_confirmed_count - effective_confirmed_count),
        "gold_evaluable_duplicate_collapsed_count": max(0, raw_evaluable_count - effective_evaluable_count),
        "gold_admitted_count": admitted_count,
        "gold_outside_queue_count": max(0, effective_evaluable_count - admitted_count),
        "gold_queue_coverage": round(admitted_count / max(1, effective_evaluable_count), 4)
        if effective_evaluable_count
        else 0.0,
        "gold_cross_domain_admitted_count": len(admitted_groups & cross_domain_groups),
    }


def _asset_contract(raw: dict[str, list[str]]) -> dict:
    paths = {key: list(dict.fromkeys(values))[:3] for key, values in raw.items() if values}
    video = bool(paths.get("video"))
    audio = bool(paths.get("audio"))
    transcript = bool(paths.get("transcript"))
    ocr = bool(paths.get("ocr"))
    visual = bool(paths.get("frame") or paths.get("cover"))
    return {
        "video": video,
        "audio": audio,
        "transcript": transcript,
        "ocr": ocr,
        "visual": visual,
        "ready_for_evidence": bool(video and (audio or transcript or ocr or visual)),
        "paths": paths,
    }


def _confusion_text(row: dict, suggestions: dict) -> str:
    values = [
        row.get("title"),
        row.get("tags"),
        row.get("content_category"),
        row.get("hook_type"),
        row.get("slice_structure"),
        row.get("program_name"),
        row.get("artist_names"),
        row.get("song_title"),
        suggestions.get("material_type"),
        suggestions.get("program_context"),
        suggestions.get("presentation_style"),
    ]
    return " ".join(str(value or "").lower() for value in values)


def _cue_hits(text: str, cues: list[str]) -> list[str]:
    return [cue for cue in cues if cue.lower() in text]


def _stable_confusion_group_key(item: dict) -> str:
    account = str(item.get("account_id") or "").strip().lower()
    title = str(item.get("title") or "").strip().lower()
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[@#《》【】\[\]（）()，,。.!！?？:：;；\"'“”‘’、\s]+", "", title)
    title = re.sub(r"\d+", "#", title)[:80]
    identity = title or str(item.get("platform_item_id") or item.get("sample_id") or "")
    return f"{account}:{identity}"


def _known_gold_confusion_summary(rows: list[dict], *, omni_index: dict[str, dict], annotations: dict[str, dict]) -> dict:
    row_index = {str(row.get("id") or ""): row for row in rows}
    relations: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    for sample_id, annotation in annotations.items():
        if annotation.get("review_status") != "confirmed":
            continue
        row = row_index.get(sample_id)
        if not row:
            continue
        omni = omni_index.get(sample_id) or omni_index.get(str(row.get("platform_item_id") or "")) or {}
        if omni:
            omni = refresh_omni_shadow_for_row(omni, row)
        suggestions = omni.get("semantic_suggestions") if isinstance(omni.get("semantic_suggestions"), dict) else {}
        expected = str(annotation.get("material_type") or "unknown")
        predicted = str(suggestions.get("material_type") or "unknown")
        relation = material_type_taxonomy_relation(expected, predicted)
        relations[relation] += 1
        if relation != "mismatch":
            continue
        values = {expected, predicted}
        matched_pair = False
        for key, definition in MATERIAL_CONFUSION_PAIRS.items():
            if values <= {str(definition["left"]), str(definition["right"])}:
                pair_counts[key] += 1
                matched_pair = True
                break
        if not matched_pair:
            pair_counts[CROSS_DOMAIN_CONFUSION_PAIR] += 1
    return {
        "relation_counts": dict(relations),
        "pair_counts": dict(pair_counts),
        "severe_mismatch_count": int(relations.get("mismatch") or 0),
    }
