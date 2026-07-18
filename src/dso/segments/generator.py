from __future__ import annotations

import json
from pathlib import Path

from dso.db.session import connect, fetch_all, fetch_one, insert_row
from dso.features.audio import energy_between, extract_audio_features
from dso.features.timeline import build_timeline_signals
from dso.media.ffmpeg import bounded_window
from dso.media.ingest import get_video
from dso.utils import new_id, read_json, utc_now
from dso.versions import SEGMENTER_VERSION, STANDARD_CANDIDATE_VERSION


HOOK_KEYWORDS = [
    "导师",
    "评委",
    "老师",
    "点评",
    "没想到",
    "第一次",
    "最后",
    "直接",
    "竟然",
    "为什么",
    "突破",
    "淘汰",
    "晋级",
    "改编",
]
SUSPENSE_KEYWORDS = [
    "淘汰",
    "晋级",
    "待定",
    "排名",
    "票数",
    "结果",
    "选择",
    "PK",
    "pk",
    "对决",
    "赛制",
    "悬念",
    "复活",
    "争议",
]
STORY_KEYWORDS = [
    "故事",
    "经历",
    "一路",
    "小时候",
    "家人",
    "妈妈",
    "父亲",
    "写给",
    "压力",
    "梦想",
    "坚持",
    "原创",
    "练习",
]
CHORUS_KEYWORDS = [
    "副歌",
    "高音",
    "转调",
    "和声",
    "rap",
    "RAP",
    "爆发",
    "高潮",
    "开口跪",
    "长音",
    "哭腔",
    "升key",
    "真假音",
    "solo",
    "drop",
    "炸场",
    "炸場",
    "炸裂",
]
REACTION_KEYWORDS = [
    "全场",
    "观众",
    "现场",
    "掌声",
    "欢呼",
    "尖叫",
    "起立",
    "站起来",
    "拍手",
    "泪目",
    "哭了",
    "哽咽",
    "导师反应",
    "评委反应",
    "反应",
]
SPONSOR_KEYWORDS = [
    "合作伙伴",
    "合作夥伴",
    "超级合作伙伴",
    "超級合作夥伴",
    "提醒您",
    "提醒你",
    "销量第一",
    "銷量第一",
    "怕上火",
    "广告",
    "廣告",
    "VIP",
    "vivo",
    "王老吉",
    "白雀羚",
    "欧丽薇兰",
    "欧利威兰",
    "動感地帶",
    "动感地带",
    "芒果卡",
    "盲國卡",
    "盲国卡",
    "歌手听我唱",
    "歌手聽我唱",
    "合唱官",
    "扫码",
    "掃碼",
    "直播间",
    "直播間",
    "直拍",
    "QQ音乐",
    "QQ音樂",
    "网易云",
    "網易雲",
    "汽水音乐",
    "汽水音樂",
    "酷狗",
    "酷我",
    "酸酸乳",
    "蒙牛",
    "猛流酸酸乳",
    "官方帐号",
    "官方帳號",
    "参与互动",
    "參與互動",
    "登录",
    "登入",
    "收听",
    "收聽",
]
EMOTION_KEYWORDS = {
    "遗憾": ["遗憾", "错过", "再见", "离开", "眼泪"],
    "热血": ["坚持", "燃", "突破", "炸", "爆发"],
    "治愈": ["温柔", "拥抱", "释怀", "陪伴", "回家"],
    "悬念": ["淘汰", "晋级", "待定", "票数", "悬念", "选择"],
}
FIRST_STAGE_KEYWORDS = ["第一次", "首次", "首個", "首个", "第一个舞台", "第一個舞台", "緊張", "紧张"]
CHOICE_KEYWORDS = ["选择", "選擇", "选歌", "選歌", "代表作", "代表", "原唱", "版本"]
RESULT_KEYWORDS = ["排名", "胜负", "勝負", "结果", "結果", "晋级", "晉級", "淘汰", "待定", "票数", "票數", "继续", "繼續"]
DREAM_KEYWORDS = ["梦想", "夢想", "最初", "一路", "经历", "經歷", "珍惜", "人生", "自己", "小时候", "小時候"]
MENTOR_KEYWORDS = ["导师", "導師", "评委", "評委", "老师", "老師", "点评", "點評", "建议", "建議", "评价", "評價"]
ARRANGEMENT_KEYWORDS = ["改编", "改編", "和声", "合声", "合聲", "合唱", "声部", "聲部", "solo", "SOLO"]
SONG_MEMORY_KEYWORDS = ["发行", "發行", "21年", "二十一年", "二十年", "很多年", "经典", "經典", "回忆", "回憶"]
AUDIENCE_DETAIL_KEYWORDS = ["观众", "觀眾", "全场", "全場", "现场", "現場", "掌声", "掌聲", "欢呼", "歡呼", "尖叫", "起立", "泪目", "淚目"]
FAREWELL_KEYWORDS = ["遗憾", "遺憾", "错过", "錯過", "再见", "再見", "离开", "離開", "告别", "告別", "最后一次", "最後一次"]
TEARS_KEYWORDS = ["眼泪", "眼淚", "泪目", "淚目", "哭了", "流泪", "流淚", "哽咽", "感动", "感動"]
PRESSURE_KEYWORDS = ["压力", "壓力", "彩排", "赛制", "賽制", "赛值", "賽值", "竞演", "競演", "试炼", "試練"]
BREAKTHROUGH_KEYWORDS = ["突破", "炸场", "炸場", "炸裂", "高燃", "燃心", "爆发", "爆發"]
AFTERGLOW_KEYWORDS = ["谢谢", "謝謝", "记忆", "記憶", "回忆", "回憶", "传唱", "傳唱", "珍藏"]
COLLAB_STYLE_KEYWORDS = ["合作", "味道", "呈现", "呈現", "编曲", "編曲", "版本", "风格", "風格"]
OST_STORY_KEYWORDS = ["电视剧", "電視劇", "主题曲", "主題曲", "片尾曲", "插曲"]


def generate_segments(video_id: str, top_k: int = 30) -> list[dict]:
    video = get_video(video_id)
    if video.get("input_mode") == "precut":
        raise ValueError(
            "precut source keeps one immutable full-duration candidate; "
            "process it through the precut batch workflow"
        )
    transcript = _load_transcript(video)
    audio = extract_audio_features(video_id)
    duration = float(video["duration_seconds"])
    timeline = build_timeline_signals(
        video_id,
        video.get("file_path") or "",
        audio.get("frames") or [],
        transcript,
    )

    candidates = _from_transcript(video_id, transcript, audio["frames"], duration)
    candidates.extend(_from_audio_peaks(video_id, audio["peaks"], audio["frames"], duration, transcript))
    candidates.extend(
        _from_timeline_onsets(
            video_id,
            timeline.get("audio_onsets") or [],
            audio.get("frames") or [],
            duration,
            transcript,
        )
    )
    candidates = [
        _refine_candidate_boundaries(item, transcript, audio.get("frames") or [], timeline, duration)
        for item in candidates
    ]
    ranked = _dedupe_and_rank(candidates)[:top_k]

    with connect() as conn:
        conn.execute("DELETE FROM candidate_segments WHERE source_video_id = ?", [video_id])
        for item in ranked:
            insert_row(conn, "candidate_segments", item)
        conn.commit()
        rows = fetch_all(
            conn,
            "SELECT * FROM candidate_segments WHERE source_video_id = ?",
            [video_id],
        )
        by_id = {row["id"]: row for row in rows}
        result = [by_id[item["id"]] for item in ranked if item["id"] in by_id]
        for row in result:
            row["segmenter_version"] = SEGMENTER_VERSION
        return result


def _load_transcript(video: dict) -> list[dict]:
    path = video.get("transcript_path")
    if not path:
        return []
    data = read_json(Path(path), default={}) or {}
    return list(data.get("segments") or [])


def _from_transcript(video_id: str, transcript: list[dict], frames: list[dict], duration: float) -> list[dict]:
    if not transcript:
        return []
    candidates: list[dict] = []
    candidates.extend(_event_arc_candidates(video_id, transcript, frames, duration))

    window_size = 4
    for index in range(0, len(transcript), 2):
        group = transcript[index : index + window_size]
        if not group:
            continue
        text = " ".join(str(seg.get("text") or "") for seg in group).strip()
        rough_energy = energy_between(frames, float(group[0]["start"]), float(group[-1]["end"]))
        flags = _cue_flags(text, rough_energy)
        start_pad = 5.0 if flags["context"] or flags["story"] or flags["suspense"] else 2.0
        if flags["reaction"] and not (flags["context"] or flags["story"] or flags["suspense"]):
            start_pad = 8.0
        end_pad = 8.0 if flags["music"] or flags["reaction"] else 6.0
        start = max(0.0, float(group[0]["start"]) - start_pad)
        end = min(duration, float(group[-1]["end"]) + end_pad)
        min_duration = 18.0 if any(flags.values()) else 14.0
        max_duration = 75.0 if flags["context"] or flags["story"] or flags["suspense"] else 60.0
        if end - start < min_duration:
            start, end = bounded_window((start + end) / 2, min_duration, duration)
        if end - start > max_duration:
            end = min(duration, start + max_duration)
        energy = energy_between(frames, start, end)
        candidates.append(_candidate_row(video_id, start, end, text, energy, generation_source="transcript_window"))
    return candidates


def _event_arc_candidates(video_id: str, transcript: list[dict], frames: list[dict], duration: float) -> list[dict]:
    flags_by_index = [_segment_flags(seg, frames) for seg in transcript]
    candidates: list[dict] = []
    for index, flags in enumerate(flags_by_index):
        has_program_setup = flags["context"] or flags["story"] or flags["suspense"]
        if has_program_setup:
            music_index = index if flags["music"] else _find_forward(
                transcript, flags_by_index, index, lambda item: item["music"], 55.0
            )
            reaction_index = None
            if music_index is not None:
                reaction_index = music_index if flags_by_index[music_index]["reaction"] else _find_forward(
                    transcript, flags_by_index, music_index, lambda item: item["reaction"], 28.0
                )
            end_index = reaction_index if reaction_index is not None else music_index
            if end_index is None:
                end_index = min(len(transcript) - 1, index + 3)
            candidates.append(
                _candidate_from_indices(
                    video_id,
                    transcript,
                    frames,
                    duration,
                    index,
                    end_index,
                    generation_source="event_arc_setup",
                )
            )
            continue

        if flags["music"]:
            setup_index = _find_backward(
                transcript,
                flags_by_index,
                index,
                lambda item: item["context"] or item["story"] or item["suspense"],
                40.0,
            )
            reaction_index = _find_forward(transcript, flags_by_index, index, lambda item: item["reaction"], 24.0)
            start_index = setup_index if setup_index is not None else max(0, index - 1)
            end_index = reaction_index if reaction_index is not None else min(len(transcript) - 1, index + 2)
            candidates.append(
                _candidate_from_indices(
                    video_id,
                    transcript,
                    frames,
                    duration,
                    start_index,
                    end_index,
                    generation_source="event_arc_music",
                )
            )
            continue

        if flags["reaction"]:
            music_index = _find_backward(transcript, flags_by_index, index, lambda item: item["music"], 28.0)
            if music_index is None:
                continue
            setup_index = _find_backward(
                transcript,
                flags_by_index,
                music_index,
                lambda item: item["context"] or item["story"] or item["suspense"],
                42.0,
            )
            start_index = setup_index if setup_index is not None else max(0, music_index - 1)
            candidates.append(
                _candidate_from_indices(
                    video_id,
                    transcript,
                    frames,
                    duration,
                    start_index,
                    index,
                    generation_source="event_arc_reaction",
                )
            )
    return candidates


def _candidate_from_indices(
    video_id: str,
    transcript: list[dict],
    frames: list[dict],
    duration: float,
    start_index: int,
    end_index: int,
    generation_source: str = "event_arc",
) -> dict:
    group = transcript[start_index : end_index + 1]
    text = " ".join(str(seg.get("text") or "") for seg in group).strip()
    energy = energy_between(frames, float(group[0]["start"]), float(group[-1]["end"]))
    flags = _cue_flags(text, energy)
    start_pad = 5.0 if flags["context"] or flags["story"] or flags["suspense"] else 3.0
    end_pad = 8.0 if flags["reaction"] else 10.0 if flags["music"] else 6.0
    start = max(0.0, float(group[0]["start"]) - start_pad)
    end = min(duration, float(group[-1]["end"]) + end_pad)
    if end - start < 20:
        start, end = bounded_window((start + end) / 2, 20.0, duration)
    if end - start > 75:
        end = min(duration, start + 75.0)
    energy = energy_between(frames, start, end)
    return _candidate_row(video_id, start, end, text, energy, generation_source=generation_source)


def _from_audio_peaks(
    video_id: str,
    peaks: list[dict],
    frames: list[dict],
    duration: float,
    transcript: list[dict] | None = None,
) -> list[dict]:
    candidates: list[dict] = []
    for peak in peaks[:20]:
        peak_time = float(peak["time"])
        start, end = bounded_window(peak_time, 32.0, duration)
        text = "音乐/舞台高能候选片段"
        if transcript:
            start, end, text = _audio_peak_window(peak_time, transcript, start, end, duration)
        energy = energy_between(frames, start, end)
        candidates.append(_candidate_row(video_id, start, end, text, energy, generation_source="audio_peak"))
    return candidates


def _audio_peak_window(
    peak_time: float,
    transcript: list[dict],
    start: float,
    end: float,
    duration: float,
) -> tuple[float, float, str]:
    flags_by_index = [_segment_flags(seg, []) for seg in transcript]
    anchor_index = _segment_index_at(transcript, peak_time)
    if anchor_index is None:
        anchor_index = min(range(len(transcript)), key=lambda idx: abs(float(transcript[idx]["start"]) - peak_time))

    setup_index = _find_backward(
        transcript,
        flags_by_index,
        anchor_index,
        lambda item: item["context"] or item["story"] or item["suspense"],
        42.0,
    )
    reaction_index = _find_forward(transcript, flags_by_index, anchor_index, lambda item: item["reaction"], 26.0)
    if setup_index is not None:
        start = max(0.0, float(transcript[setup_index]["start"]) - 4.0)
    if reaction_index is not None:
        end = min(duration, float(transcript[reaction_index]["end"]) + 6.0)
    if end - start < 24:
        start, end = bounded_window(peak_time, 24.0, duration)
    if end - start > 68:
        end = min(duration, start + 68.0)
    text = _transcript_text_between(transcript, start, end) or "音乐/舞台高能候选片段"
    return start, end, text


def _from_timeline_onsets(
    video_id: str,
    onsets: list[dict],
    frames: list[dict],
    duration: float,
    transcript: list[dict],
) -> list[dict]:
    candidates: list[dict] = []
    for onset in onsets[:16]:
        timestamp = float(onset.get("time") or 0.0)
        start, end = bounded_window(timestamp + 4.0, 26.0, duration)
        text = _transcript_text_between(transcript, start, end)
        if not text:
            continue
        energy = energy_between(frames, start, end)
        candidates.append(_candidate_row(video_id, start, end, text, energy, generation_source="audio_onset"))
    return candidates


def _refine_candidate_boundaries(
    candidate: dict,
    transcript: list[dict],
    frames: list[dict],
    timeline: dict,
    duration: float,
) -> dict:
    original_start = float(candidate.get("start_time") or 0.0)
    original_end = float(candidate.get("end_time") or original_start)
    start_anchor = _best_boundary_anchor(original_start, "start", transcript, timeline)
    end_anchor = _best_boundary_anchor(original_end, "end", transcript, timeline)
    start = float(start_anchor.get("time") if start_anchor else original_start)
    end = float(end_anchor.get("time") if end_anchor else original_end)
    if end - start < 12.0:
        start, end = original_start, original_end
        start_anchor = None
        end_anchor = None
    start = max(0.0, min(start, max(0.0, duration - 1.0)))
    end = min(duration, max(start + 1.0, end))
    text = _transcript_text_between(transcript, start, end) or str(candidate.get("transcript") or "")
    refined = _candidate_row(
        str(candidate.get("source_video_id") or ""),
        start,
        end,
        text,
        energy_between(frames, start, end),
        generation_source=_generation_source(candidate),
    )
    refined["id"] = candidate.get("id") or refined["id"]
    evidence = {
        "version": "semantic_signal_snap.v1",
        "generation_source": _generation_source(candidate),
        "original_range": [round(original_start, 3), round(original_end, 3)],
        "refined_range": [round(start, 3), round(end, 3)],
        "start_anchor": start_anchor or {},
        "end_anchor": end_anchor or {},
        "scene_change_count": _count_timeline_events(timeline.get("scene_changes") or [], start, end),
        "audio_onset_count": _count_timeline_events(timeline.get("audio_onsets") or [], start, end),
        "silence_count": sum(
            1
            for item in timeline.get("silences") or []
            if start <= float(item.get("start") or 0.0) <= end
        ),
    }
    anchor_count = int(bool(start_anchor)) + int(bool(end_anchor))
    refined["generation_signals_json"] = json.dumps(evidence, ensure_ascii=False)
    refined["boundary_strategy"] = "semantic_signal_snap.v1"
    refined["boundary_confidence"] = round(min(0.92, 0.52 + anchor_count * 0.18), 3)
    return refined


def _best_boundary_anchor(
    target: float,
    side: str,
    transcript: list[dict],
    timeline: dict,
    *,
    tolerance: float = 3.5,
) -> dict:
    anchors: list[dict] = []
    transcript_field = "start" if side == "start" else "end"
    transcript_type = "sentence_start" if side == "start" else "sentence_end"
    for item in transcript:
        try:
            anchors.append({"time": float(item.get(transcript_field) or 0.0), "type": transcript_type, "priority": 1.0})
        except (TypeError, ValueError):
            continue
    for silence in timeline.get("silences") or []:
        field = "end" if side == "start" else "start"
        anchors.append({"time": float(silence.get(field) or 0.0), "type": f"silence_{field}", "priority": 1.08})
    for scene in timeline.get("scene_changes") or []:
        anchors.append({"time": float(scene.get("time") or 0.0), "type": "scene_change", "priority": 0.78})
    if side == "start":
        for onset in timeline.get("audio_onsets") or []:
            anchors.append({"time": float(onset.get("time") or 0.0), "type": "audio_onset", "priority": 0.72})
    nearby = [item for item in anchors if abs(float(item["time"]) - target) <= tolerance]
    if not nearby:
        return {}
    best = max(
        nearby,
        key=lambda item: float(item.get("priority") or 0.0) - abs(float(item["time"]) - target) / max(0.1, tolerance),
    )
    return {
        "time": round(float(best["time"]), 3),
        "type": str(best.get("type") or "timeline"),
        "delta_seconds": round(float(best["time"]) - target, 3),
    }


def _generation_source(candidate: dict) -> str:
    value = candidate.get("generation_signals_json")
    if isinstance(value, str):
        try:
            return str((json.loads(value) or {}).get("generation_source") or "legacy")
        except Exception:
            return "legacy"
    return "legacy"


def _count_timeline_events(events: list[dict], start: float, end: float) -> int:
    return sum(1 for item in events if start <= float(item.get("time") or 0.0) <= end)


def _candidate_row(
    video_id: str,
    start: float,
    end: float,
    text: str,
    energy: float,
    *,
    generation_source: str = "legacy",
) -> dict:
    duration = max(0.0, end - start)
    semantics = describe_candidate_content(text, energy, duration)
    now = utc_now()
    return {
        "id": new_id("seg"),
        "source_video_id": video_id,
        "performance_id": None,
        "start_time": round(start, 3),
        "end_time": round(end, 3),
        "duration_seconds": round(duration, 3),
        "transcript": text,
        **semantics,
        "cover_time": round(start + min(duration * 0.45, 15), 3),
        "status": "candidate",
        "generation_signals_json": json.dumps({"generation_source": generation_source}, ensure_ascii=False),
        "boundary_strategy": "unrefined",
        "boundary_confidence": 0.0,
        "candidate_origin": "generated",
        "boundary_locked": 0,
        "source_content_hash": "",
        "import_batch_id": "",
        "candidate_contract_version": STANDARD_CANDIDATE_VERSION,
        "created_at": now,
    }


def describe_candidate_content(text: str, energy: float, duration: float) -> dict:
    """Return the shared semantic fields used by both candidate entry modes."""
    flags = _cue_flags(text, energy)
    has_context = flags["context"] or flags["story"] or flags["suspense"]
    has_chorus = flags["music"] or energy > 0.72
    has_reaction = flags["reaction"]
    emotion = _detect_emotion(text)
    slice_type = _slice_type(text, has_context, has_chorus, has_reaction, emotion, flags, duration)
    structure = _structure_label(text, flags, has_context, has_chorus, has_reaction, emotion)
    musical_moment = _musical_moment(text, flags, has_chorus, energy, emotion)
    context = _program_context(text, flags, has_context, has_reaction)
    comment_trigger = _comment_trigger(text, emotion, has_chorus, has_context, has_reaction, flags)
    if _sponsor_noise_score(text) >= 2:
        context = "疑似品牌/广告口播密集，建议只作为上下文补充"
        comment_trigger = "广告口播占比较高，需人工确认是否适合独立切片"
    summary = f"{structure}；{musical_moment}；{context}；{comment_trigger}"
    return {
        "summary": summary,
        "primary_topic": "音乐综艺",
        "song_section_type": "climax_candidate" if has_chorus else "context_or_build",
        "music_slice_type": slice_type,
        "emotion_type": emotion,
        "short_video_structure": structure,
        "musical_moment": musical_moment,
        "program_context": context,
        "comment_trigger": comment_trigger,
    }


def _detect_emotion(text: str) -> str:
    if _contains_any(text, FAREWELL_KEYWORDS):
        return "遗憾"
    if _contains_any(text, RESULT_KEYWORDS):
        return "悬念"
    if _contains_any(text, FIRST_STAGE_KEYWORDS + ["压力", "壓力"]):
        return "紧张"
    if _contains_any(text, TEARS_KEYWORDS):
        return "感动"
    if _contains_any(text, DREAM_KEYWORDS + SONG_MEMORY_KEYWORDS + AFTERGLOW_KEYWORDS + OST_STORY_KEYWORDS):
        return "共鸣"
    if _contains_any(text, ["温柔", "溫柔", "拥抱", "擁抱", "释怀", "釋懷", "陪伴", "回家"]):
        return "治愈"
    if _contains_any(text, ["坚持", "堅持", "燃", "突破", "炸", "爆发", "爆發", "高燃"]):
        return "热血"
    if _contains_any(text, AUDIENCE_DETAIL_KEYWORDS):
        return "现场情绪"
    for label, words in EMOTION_KEYWORDS.items():
        if any(word in text for word in words):
            return label
    return "舞台表现"


def _slice_type(
    text: str,
    has_context: bool,
    has_chorus: bool,
    has_reaction: bool,
    emotion: str,
    flags: dict[str, bool],
    duration: float,
) -> str:
    if has_context and has_chorus and has_reaction:
        return "综艺叙事爆点闭环型"
    if _contains_any(text, MENTOR_KEYWORDS):
        return "导师评价型" if not has_chorus else "导师评价到舞台回应型"
    if _contains_any(text, FIRST_STAGE_KEYWORDS):
        return "首秀选择型"
    if _contains_any(text, PRESSURE_KEYWORDS):
        return "竞演压力型"
    if _contains_any(text, AFTERGLOW_KEYWORDS):
        return "舞台余韵型"
    if _contains_any(text, COLLAB_STYLE_KEYWORDS):
        return "合作改编动机型"
    if _contains_any(text, OST_STORY_KEYWORDS):
        return "影视主题曲故事型"
    if _contains_any(text, BREAKTHROUGH_KEYWORDS):
        if _contains_any(text, RESULT_KEYWORDS):
            return "竞演突破悬念型"
        if _contains_any(text, SONG_MEMORY_KEYWORDS + DREAM_KEYWORDS):
            return "人生突破共鸣型"
        if _contains_any(text, AUDIENCE_DETAIL_KEYWORDS + ["炸场", "炸場", "炸裂"]):
            return "炸场名场面型"
        return "舞台突破型"
    if flags["suspense"] and has_chorus:
        return "赛制悬念到音乐爆点型"
    if _contains_any(text, RESULT_KEYWORDS):
        return "赛制悬念型"
    if flags["story"] and has_chorus:
        return "歌手故事到音乐爆点型"
    if _contains_any(text, SONG_MEMORY_KEYWORDS):
        return "歌曲记忆共鸣型"
    if _contains_any(text, DREAM_KEYWORDS):
        return "歌词共鸣型"
    if _contains_any(text, ARRANGEMENT_KEYWORDS):
        return "改编记忆点型"
    if _contains_any(text, AUDIENCE_DETAIL_KEYWORDS):
        return "现场反应型"
    if flags["story"]:
        return "歌手故事铺垫型"
    if has_context and has_chorus:
        return "节目叙事到音乐爆点型"
    if has_chorus and duration <= 30:
        return "直入听觉爆点型"
    if emotion in {"遗憾", "治愈", "感动", "共鸣"}:
        return "歌词共鸣型"
    if has_context:
        return "节目叙事型"
    return "铺垫到高潮型"


def _structure_label(
    text: str,
    flags: dict[str, bool],
    has_context: bool,
    has_chorus: bool,
    has_reaction: bool,
    emotion: str,
) -> str:
    if has_context and has_chorus and has_reaction:
        return "节目上下文(导师评价/赛制悬念/歌手故事) -> 音乐爆点 -> 现场反应"
    if _contains_any(text, MENTOR_KEYWORDS):
        return "导师评价 -> 音乐爆点 -> 现场反应" if has_chorus else "导师评价 -> 专业判断 -> 评论触发"
    if _contains_any(text, FIRST_STAGE_KEYWORDS):
        return "首秀/选歌铺垫 -> 舞台状态 -> 期待悬念"
    if _contains_any(text, PRESSURE_KEYWORDS):
        return "赛前压力/竞演背景 -> 舞台表现 -> 结果预期"
    if _contains_any(text, AFTERGLOW_KEYWORDS):
        return "谢幕/记忆触发 -> 舞台余韵 -> 共鸣讨论"
    if _contains_any(text, COLLAB_STYLE_KEYWORDS):
        return "合作动机/风格选择 -> 舞台呈现 -> 评论判断"
    if _contains_any(text, OST_STORY_KEYWORDS):
        return "影视主题曲记忆 -> 歌曲故事 -> 舞台期待"
    if _contains_any(text, BREAKTHROUGH_KEYWORDS):
        if _contains_any(text, RESULT_KEYWORDS):
            return "竞演进程 -> 突破表现 -> 去留悬念"
        if _contains_any(text, SONG_MEMORY_KEYWORDS + DREAM_KEYWORDS):
            return "人生回望 -> 突破表达 -> 共鸣讨论"
        if _contains_any(text, AUDIENCE_DETAIL_KEYWORDS + ["炸场", "炸場", "炸裂"]):
            return "炸场反馈 -> 舞台爆发 -> 评论扩散"
        return "突破预期 -> 舞台爆发 -> 观众讨论"
    if flags["suspense"] or _contains_any(text, RESULT_KEYWORDS):
        return "赛制悬念 -> 音乐表现 -> 胜负结果" if has_chorus else "赛制/选择信息 -> 选手状态 -> 讨论悬念"
    if _contains_any(text, SONG_MEMORY_KEYWORDS):
        return "歌曲记忆点 -> 舞台演绎 -> 情绪共鸣"
    if _contains_any(text, DREAM_KEYWORDS):
        return "歌手经历/歌词共鸣 -> 舞台特写 -> 评论触发"
    if _contains_any(text, ARRANGEMENT_KEYWORDS):
        return "改编/和声细节 -> 听觉记忆点 -> 专业讨论"
    if _contains_any(text, AUDIENCE_DETAIL_KEYWORDS):
        return "现场反应 -> 舞台回放 -> 评论触发"
    if flags["story"]:
        return "歌手故事 -> 情绪铺垫 -> 舞台期待"
    if has_context and has_chorus:
        return "节目上下文 -> 歌曲爆点 -> 现场反应"
    if has_chorus and has_reaction:
        return "音乐爆点 -> 现场反应 -> 评论触发"
    if has_chorus:
        return "听觉爆点 -> 情绪延展 -> 评论触发"
    if emotion in {"遗憾", "治愈", "感动", "共鸣"}:
        return "歌词共鸣 -> 舞台特写 -> 情绪记忆点"
    return "铺垫信息 -> 舞台表现 -> 结果/反应"


def _musical_moment(text: str, flags: dict[str, bool], has_chorus: bool, energy: float, emotion: str) -> str:
    tags = []
    for label, words in [
        ("副歌", ["副歌"]),
        ("高音", ["高音", "长音"]),
        ("转调/升key", ["转调", "升key"]),
        ("说唱/节奏", ["rap", "RAP", "节奏", "drop"]),
        ("改编记忆点", ["改编", "改編", "solo", "SOLO"]),
        ("和声/合唱", ["和声", "合声", "合聲", "合唱", "声部", "聲部"]),
    ]:
        if _contains_any(text, words):
            tags.append(label)
    for label, words in [
        ("首秀选歌铺垫", FIRST_STAGE_KEYWORDS + CHOICE_KEYWORDS),
        ("竞演压力/赛前状态", PRESSURE_KEYWORDS),
        ("突破/炸场记忆点", BREAKTHROUGH_KEYWORDS),
        ("谢幕/回忆共鸣", AFTERGLOW_KEYWORDS),
        ("合作风格/呈现动机", COLLAB_STYLE_KEYWORDS),
        ("OST/歌曲故事", OST_STORY_KEYWORDS),
        ("歌手故事/人物铺垫", STORY_KEYWORDS),
        ("歌曲记忆/时代感", SONG_MEMORY_KEYWORDS),
        ("歌词共鸣/人生感", DREAM_KEYWORDS),
        ("胜负结果/赛制点", RESULT_KEYWORDS),
        ("专业评价/导师判断", MENTOR_KEYWORDS),
        ("现场反应/泪点", AUDIENCE_DETAIL_KEYWORDS + TEARS_KEYWORDS),
    ]:
        if _contains_any(text, words) and label not in tags:
            tags.append(label)
    if not tags and (has_chorus or energy > 0.72):
        tags.append("强节奏/能量峰值")
    if tags:
        suffix = "音乐爆点候选" if has_chorus or energy > 0.72 or flags["music"] else "候选"
        return f"{'/'.join(tags[:3])}{suffix}"
    if emotion in {"遗憾", "治愈", "感动", "共鸣"}:
        return f"{emotion}情绪铺垫候选"
    return "歌曲铺垫/情绪段候选"


def _program_context(text: str, flags: dict[str, bool], has_context: bool, has_reaction: bool) -> str:
    details = []
    if _contains_any(text, ["导师", "评委", "老师", "点评", "判断"]):
        details.append("导师评价")
    if flags["suspense"]:
        details.append("赛制悬念")
    if flags["story"]:
        details.append("歌手故事")
    if _contains_any(text, ["第一次", "首次", "首個", "首个", "緊張", "紧张", "壓力", "压力"]):
        details.append("首次登台/舞台压力")
    if _contains_any(text, ["選歌", "选歌", "改編", "改编", "原唱", "版本"]):
        details.append("选歌/改编动机")
    if details:
        return f"含{'/'.join(details)}等节目上下文"
    if has_reaction:
        return "含现场反应，可回看前置节目上下文"
    if has_context:
        return "含人物状态/节目钩子等叙事信息"
    return "节目上下文需人工确认"


def _comment_trigger(
    text: str,
    emotion: str,
    has_chorus: bool,
    has_context: bool,
    has_reaction: bool,
    flags: dict[str, bool],
) -> str:
    if _contains_any(text, ["第一次", "首次", "緊張", "紧张", "壓力", "压力"]):
        return "可讨论第一次登台的紧张感是否让后续舞台更有代入感"
    if flags["suspense"] and has_chorus and has_reaction:
        return "可讨论晋级/淘汰结果和现场反应是否配得上这段表现"
    if flags["suspense"] and has_chorus:
        return "可讨论晋级/淘汰结果是否配得上这段表现"
    if flags["story"] and has_chorus:
        return "可讨论歌手故事是否让音乐爆点更有后劲"
    if has_context and has_chorus and has_reaction:
        return "可讨论导师判断、舞台爆点和现场反应是否同频"
    if has_context and has_chorus:
        return "可讨论这段改编/表现是否完成突破"
    if has_reaction:
        return "可讨论现场反应是否构成名场面"
    if emotion == "遗憾":
        return "可讨论这句歌词是否唱出遗憾感"
    if emotion == "热血":
        return "可讨论舞台爆发和歌手突破"
    if has_chorus:
        return "可讨论副歌、高音或改编记忆点"
    return "可讨论节目上下文和舞台表现"


def _dedupe_and_rank(candidates: list[dict]) -> list[dict]:
    selected: list[dict] = []
    seen_texts: set[str] = set()
    for candidate in sorted(candidates, key=_rough_rank, reverse=True):
        if _should_discard_candidate(candidate):
            continue
        fingerprint = _text_fingerprint(candidate.get("transcript") or "")
        if fingerprint and fingerprint in seen_texts:
            continue
        if any(_overlap(candidate, existing) > 0.55 for existing in selected):
            continue
        selected.append(candidate)
        if fingerprint:
            seen_texts.add(fingerprint)
    return selected


def _text_fingerprint(text: str) -> str:
    normalized = "".join(char for char in text.lower() if char.isalnum())
    if len(normalized) < 8:
        return ""
    return normalized[:80]


def _rough_rank(candidate: dict) -> float:
    score = 0.0
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ["transcript", "summary", "program_context", "comment_trigger", "short_video_structure"]
    )
    flags = _cue_flags(text, 0.0)
    has_music = "爆点" in candidate.get("musical_moment", "") or flags["music"]
    has_context = "节目上下文" in candidate.get("short_video_structure", "") or flags["context"] or flags["story"]
    has_reaction = flags["reaction"] or _contains_any(text, AUDIENCE_DETAIL_KEYWORDS + TEARS_KEYWORDS)
    score += 32 if has_context and has_music and flags["reaction"] else 0
    score += 22 if has_context and has_music else 0
    score += 16 if has_music and flags["reaction"] else 0
    score += 10 if flags["suspense"] or flags["story"] else 0
    score += min(18, len(candidate.get("transcript") or "") / 10)
    duration = float(candidate["duration_seconds"])
    score += 14 if 18 <= duration <= 55 else 7 if 12 <= duration <= 75 else -10
    quality = _text_quality_score(candidate.get("transcript") or "")
    score += quality * 10
    sponsor_score = _sponsor_noise_score(text)
    score -= min(48, sponsor_score * 10)
    if sponsor_score >= 2 and not (has_reaction or flags["suspense"]):
        score -= 16
    english_ratio = _english_lyrics_ratio(candidate.get("transcript") or "")
    if english_ratio >= 0.55:
        score -= 26 if not (has_context or has_reaction or flags["suspense"]) else 6
        if duration < 24 and not has_reaction:
            score -= 8
    if _repetitive_noise(candidate.get("transcript") or ""):
        score -= 80
    score -= 12 if quality < 0.35 else 0
    score -= 8 if "需人工确认" in (candidate.get("program_context") or "") else 0
    return score


def _should_discard_candidate(candidate: dict) -> bool:
    text = str(candidate.get("transcript") or "")
    joined = " ".join(
        str(candidate.get(key) or "")
        for key in ["transcript", "summary", "program_context", "comment_trigger", "short_video_structure"]
    )
    if _repetitive_noise(text):
        return True
    if _sponsor_noise_score(joined) >= 4:
        return True
    if _text_quality_score(text) < 0.22:
        return True
    return False


def _overlap(a: dict, b: dict) -> float:
    start = max(float(a["start_time"]), float(b["start_time"]))
    end = min(float(a["end_time"]), float(b["end_time"]))
    if end <= start:
        return 0.0
    shortest = min(float(a["duration_seconds"]), float(b["duration_seconds"]))
    return (end - start) / shortest if shortest else 0.0


def _segment_flags(segment: dict, frames: list[dict]) -> dict[str, bool]:
    text = str(segment.get("text") or "")
    start = float(segment.get("start") or 0)
    end = float(segment.get("end") or start)
    energy = energy_between(frames, start, end) if frames else 0.0
    return _cue_flags(text, energy)


def _cue_flags(text: str, energy: float = 0.0) -> dict[str, bool]:
    return {
        "context": _contains_any(text, HOOK_KEYWORDS),
        "suspense": _contains_any(text, SUSPENSE_KEYWORDS),
        "story": _contains_any(text, STORY_KEYWORDS),
        "music": _contains_any(text, CHORUS_KEYWORDS) or energy > 0.72 or _english_lyrics_ratio(text) >= 0.55,
        "reaction": _contains_any(text, REACTION_KEYWORDS),
    }


def _contains_any(text: str, words: list[str]) -> bool:
    lower = text.lower()
    return any(word.lower() in lower for word in words)


def _sponsor_noise_score(text: str) -> int:
    lower = text.lower()
    return sum(1 for word in SPONSOR_KEYWORDS if word.lower() in lower)


def _english_lyrics_ratio(text: str) -> float:
    compact = "".join(char for char in text if not char.isspace())
    if len(compact) < 18:
        return 0.0
    ascii_letters = sum(1 for char in compact if char.isascii() and char.isalpha())
    cjk = sum(1 for char in compact if "\u4e00" <= char <= "\u9fff")
    if cjk >= 10:
        return 0.0
    return round(ascii_letters / max(1, len(compact)), 3)


def _repetitive_noise(text: str) -> bool:
    compact = "".join(char for char in text if not char.isspace())
    if len(compact) < 32:
        return False
    most_common = max((compact.count(char) for char in set(compact)), default=0)
    if most_common / len(compact) >= 0.42:
        return True
    for size in (1, 2, 3, 4):
        chunks = [compact[index : index + size] for index in range(0, len(compact) - size + 1, size)]
        if not chunks:
            continue
        repeated = max((chunks.count(chunk) for chunk in set(chunks)), default=0)
        if repeated >= 8 and repeated / len(chunks) >= 0.55:
            return True
    return False


def _text_quality_score(text: str) -> float:
    cleaned = text.strip()
    if not cleaned:
        return 0.0
    if "未转写片段" in cleaned or cleaned == "音乐/舞台高能候选片段":
        return 0.2
    if _repetitive_noise(cleaned):
        return 0.05
    unique_chars = len(set(cleaned))
    density = min(1.0, len(cleaned) / 48)
    diversity = min(1.0, unique_chars / max(1, len(cleaned)) * 3)
    return round(max(0.0, min(1.0, density * 0.65 + diversity * 0.35)), 3)


def _find_forward(
    transcript: list[dict],
    flags_by_index: list[dict[str, bool]],
    start_index: int,
    predicate,
    max_seconds: float,
) -> int | None:
    anchor = float(transcript[start_index].get("start") or 0)
    for index in range(start_index + 1, len(transcript)):
        if float(transcript[index].get("end") or 0) - anchor > max_seconds:
            break
        if predicate(flags_by_index[index]):
            return index
    return None


def _find_backward(
    transcript: list[dict],
    flags_by_index: list[dict[str, bool]],
    start_index: int,
    predicate,
    max_seconds: float,
) -> int | None:
    anchor = float(transcript[start_index].get("end") or 0)
    for index in range(start_index - 1, -1, -1):
        if anchor - float(transcript[index].get("start") or 0) > max_seconds:
            break
        if predicate(flags_by_index[index]):
            return index
    return None


def _segment_index_at(transcript: list[dict], timestamp: float) -> int | None:
    for index, segment in enumerate(transcript):
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        if start <= timestamp <= end:
            return index
    return None


def _transcript_text_between(transcript: list[dict], start: float, end: float) -> str:
    texts = []
    for segment in transcript:
        seg_start = float(segment.get("start") or 0)
        seg_end = float(segment.get("end") or seg_start)
        if seg_end < start or seg_start > end:
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            texts.append(text)
    return " ".join(texts).strip()
