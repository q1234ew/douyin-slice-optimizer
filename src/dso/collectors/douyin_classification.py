from __future__ import annotations

import html
import re
from typing import Any

from dso.learning.semantic_labels import normalize_semantic_labels
from dso.versions import SEMANTIC_FEATURE_VERSION


KNOWN_ARTISTS = [
    "万妮达",
    "约翰·传奇",
    "John Legend",
    "艾略特",
    "Elliot James Reay",
    "侯明昊",
    "刘惜君",
    "张远",
    "窦靖童",
    "尤长靖",
    "胡彦斌",
    "齐豫",
    "单依纯",
    "蔡徐坤",
    "林俊杰",
    "周杰伦",
    "黄子弘凡",
    "王赫野",
    "陶喆",
    "檀健次",
    "汪苏泷",
    "黄霄雲",
    "黄家驹",
    "韩红",
    "黄绮珊",
    "王心凌",
    "谭维维",
    "郁可唯",
    "古巨基",
    "马嘉祺",
    "陈楚生",
    "黄丽玲",
    "A-Lin",
    "李佳薇",
    "Grace Kinstler",
    "Chanté Moore",
    "香缇莫",
    "杨坤",
    "周兴哲",
    "Eric 周兴哲",
    "Stanaj",
    "Jessie J",
    "GAI周延",
    "林志炫",
    "彭佳慧",
    "那英",
    "苏醒",
]


GENERIC_ARTIST_TAGS = {
    "天赐的声音",
    "天赐的声音6",
    "天赐的声音7",
    "歌手2026",
    "歌手2025",
    "歌手排名",
    "歌手歌单",
    "歌手第六期歌单",
    "歌手彩排音源",
    "歌手小放送",
    "音乐现场",
    "音乐就要这么玩",
    "声乐教学",
    "唱歌技巧",
    "会唱先会听",
    "学唱歌",
    "乐评计划",
    "音乐客厅乐评团",
    "抖音乐评新势力",
    "影娱热点团",
    "影娱漫谈编辑部",
    "dou来聊影视",
    "青年创作者成长计划",
    "reaction",
    "Reaction",
    "翻唱",
    "说唱",
    "清唱",
    "无声卡清唱",
    "无乐不欢",
    "热点",
    "共创",
    "婚礼转场",
    "宝妈",
}


GENERIC_ARTIST_HINTS = [
    "教学",
    "技巧",
    "计划",
    "热点",
    "影娱",
    "编辑部",
    "乐评",
    "有歌",
    "浙江卫视",
    "抖音",
    "音乐客厅",
    "成长计划",
    "reaction",
    "Reaction",
    "翻唱",
    "说唱",
    "合唱",
    "清唱",
    "无声卡",
    "婚礼",
    "转场",
    "舞台赏析",
    "声乐",
    "唱歌",
    "影视",
    "话题",
    "排名",
    "歌单",
    "节目",
    "老师",
    "分享官",
    "娱评",
    "乐子人",
]


def classify_published_work(
    *,
    title: Any = "",
    tags: Any = None,
    aweme_id: Any = "",
    visible_count: Any = "",
    account_id: str | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Classify already-published Douyin works for research-level aggregation."""
    existing = existing or {}
    tag_items = _split_tags(tags)
    text = _clean_text(" ".join([str(title or ""), *tag_items]))
    tag_names = [tag.lstrip("#") for tag in tag_items]
    song_title = _text(existing.get("song_title")) or _extract_song_title(text)
    artist_names = _text(existing.get("artist_names")) or "|".join(_extract_artist_names(tag_names, song_title, text))
    content_category = _text(existing.get("content_category")) or _content_category(text, song_title)
    hook_type = _text(existing.get("hook_type")) or _hook_type(text, _split_pipe(artist_names))
    program_name = _text(existing.get("program_name")) or _program_name(text, account_id)
    slice_structure = _text(existing.get("slice_structure")) or _slice_structure(text)
    commercial_intent = _text(existing.get("commercial_intent")) or _commercial_intent(text)
    rights_risk = _text(existing.get("rights_risk")) or "unknown"
    confidence = _text(existing.get("classification_confidence")) or _classification_confidence(
        _text(aweme_id),
        _text(visible_count),
        tag_items,
        content_category=content_category,
        hook_type=hook_type,
        slice_structure=slice_structure,
        artist_names=artist_names,
        song_title=song_title,
    )
    semantic = normalize_semantic_labels(
        {
            "content_category": content_category,
            "hook_type": hook_type,
            "slice_structure": slice_structure,
        }
    )
    return {
        "content_category": semantic["content_category"],
        "program_name": program_name,
        "artist_names": artist_names,
        "song_title": song_title,
        "hook_type": semantic["hook_type"],
        "slice_structure": semantic["slice_structure"],
        "semantic_unknown_reason": semantic["semantic_unknown_reason"],
        "commercial_intent": commercial_intent,
        "rights_risk": rights_risk,
        "classification_confidence": confidence,
        "semantic_feature_version": SEMANTIC_FEATURE_VERSION,
    }


def _program_name(text: str, account_id: str | None) -> str:
    if "天赐的声音" in text or account_id == "tianci":
        return "天赐的声音"
    if "歌手2026" in text or account_id == "geshou2026":
        return "歌手2026"
    if "歌手2025" in text:
        return "歌手2025"
    return ""


def _extract_song_title(text: str) -> str:
    match = re.search(r"《([^》]{1,40})》", text)
    return _clean_text(match.group(1)) if match else ""


def _extract_artist_names(tag_names: list[str], song_title: str = "", text: str = "") -> list[str]:
    artists = []
    for known in KNOWN_ARTISTS:
        if known in text:
            artists.append(_normalize_artist_name(known))
    for mention in re.findall(r"@([^#@\n\r]{2,60})", text):
        artists.append(_normalize_artist_name(mention))
    for tag in tag_names:
        clean = _clean_text(tag)
        if not clean or _is_generic_artist_token(clean):
            continue
        if song_title and clean == song_title:
            continue
        if 2 <= len(clean) <= 8:
            artists.append(_normalize_artist_name(clean))
    return _unique(artist for artist in artists if artist and not _is_generic_artist_token(artist))


def _normalize_artist_name(value: str) -> str:
    clean = _clean_text(value).strip("@#")
    if not clean or clean.startswith("歌手"):
        return ""
    clean = re.split(
        r"\s+(?:与|和|及|以及|作为|选择|发布|表示|回应|演绎|带来|唱响|用|一曲|帮唱嘉宾|特邀|加入|止步|邀您|直言)\s*",
        clean,
    )[0].strip()
    if "万妮达" in clean or "Vinida" in clean:
        return "万妮达"
    if "John Legend" in clean or clean.startswith("John Le") or "约翰·传奇" in clean:
        return "约翰·传奇"
    if "Elliot James Reay" in clean or "艾略特" in clean:
        return "艾略特"
    if "齐豫" in clean or "齊豫" in clean or "Chyi" in clean:
        return "齐豫"
    for known in ["侯明昊", "刘惜君", "张远", "窦靖童", "尤长靖", "胡彦斌"]:
        if known in clean:
            return known
    clean = re.split(r"[，,。！!？?：:、|/《#@]", clean)[0].strip()
    clean = re.sub(
        r"\s+(选择|作为|歌声|全程|超绝|带来|唱|说|没想到|情绪|律动|深情|状态|互动|特邀|场外|邀您).*$",
        "",
        clean,
    ).strip()
    replacements = {
        "萬妮达Vinida": "万妮达",
        "万妮达Vinida": "万妮达",
        "张远Bird": "张远",
        "齊豫Chyi,Yu": "齐豫",
        "John Legend": "约翰·传奇",
        "Elliot James Reay": "艾略特",
    }
    clean = replacements.get(clean, clean)
    clean = re.sub(r"\s+(选择|歌声|《|用|全程|超绝|带来|唱)", "", clean).strip()
    if len(clean) < 2 or _is_generic_artist_token(clean):
        return ""
    return replacements.get(clean, clean)


def _content_category(text: str, song_title: str) -> str:
    if any(word in text for word in ["幕后", "花絮", "排练", "采访"]):
        return "behind_the_scenes"
    if any(word in text for word in ["世界杯", "足球", "球赛", "球迷", "裁判", "C罗", "姆巴佩", "哈兰德"]):
        return "sports_entertainment"
    if any(word in text for word in ["AI", "ai", "ChatGPT", "二创", "漫剧", "课件"]):
        return "creative_ai"
    if any(word in text for word in ["美食", "粽子", "端午", "龙舟", "吃喝玩乐", "水上乐园", "乐园", "酱油"]):
        return "lifestyle"
    if any(word in text for word in ["电影", "MV", "电视剧", "短剧", "昨夜将至", "芭比"]):
        return "drama_film"
    if any(word in text for word in ["高考", "成长", "女性力量", "治愈", "勇气", "家庭"]):
        return "life_emotion"
    if any(
        word in text
        for word in [
            "综艺",
            "奔跑吧",
            "爸爸当家",
            "乘风",
            "浪姐",
            "时代少年团",
            "时团",
            "男团",
            "韩娱",
            "偶像",
            "cp",
            "CP",
            "CORTIS",
            "cortis",
            "riize",
            "top登陆少年",
            "snh48",
            "SNH48",
        ]
    ):
        return "entertainment_news"
    if any(word in text for word in ["搞笑", "抽象", "整活", "笑得", "绷住", "萌娃"]):
        return "humor_entertainment"
    if any(word in text for word in ["点评", "导师", "评价", "晋级", "淘汰"]):
        return "judge_comment"
    if any(word in text.lower() for word in ["reaction", "反应", "观众", "全场"]):
        return "reaction"
    if any(word in text for word in ["合集", "盘点", "混剪"]):
        return "compilation"
    if any(word in text for word in ["声乐", "唱歌技巧", "解析", "教学", "分析"]):
        return "commentary"
    if song_title or any(word in text for word in ["清唱", "无伴奏", "合唱", "副歌", "唱", "舞台"]):
        return "performance_clip"
    if "天赐的声音" in text or "歌手2025" in text or "歌手2026" in text:
        return "music_variety"
    return "unknown"


def _hook_type(text: str, artist_names: list[str]) -> str:
    lower = text.lower()
    if any(word in text for word in ["搞笑", "抽象", "整活", "绷住", "笑得", "萌晕"]):
        return "funny"
    if any(word in text for word in ["AI", "ai", "ChatGPT", "二创", "漫剧", "课件"]):
        return "remix_creation"
    if any(word in text for word in ["世界杯", "足球", "端午", "高考", "热点", "出分"]):
        return "topical_hook"
    if any(word in text for word in ["高音", "爆发", "力量", "唱功", "直击", "炸场"]):
        return "high_note"
    if "reaction" in lower:
        return "reaction"
    if _has_pairing_hook(text, artist_names):
        return "celebrity_pairing"
    if any(word in text for word in ["遗憾", "想见你", "有我呢", "靠近", "心底", "泪", "爱情故事"]):
        return "emotional_story"
    if any(word in text for word in ["成长", "勇气", "治愈", "真诚", "女性力量"]):
        return "emotional_story"
    if any(word in text for word in ["搞笑", "一台戏", "岳云鹏"]):
        return "funny"
    if "副歌" in text:
        return "chorus"
    if any(word in text for word in ["导师", "点评", "评价"]):
        return "judge_comment"
    return "unknown"


def _slice_structure(text: str) -> str:
    lower = text.lower()
    if any(word in text for word in ["清唱", "无伴奏"]):
        return "pure_highlight"
    if any(word in lower for word in ["reaction", "反应", "全场"]):
        return "reaction_first"
    if any(word in text for word in ["副歌", "爆发", "直击", "听不够", "高音"]):
        return "pure_highlight"
    if any(word in text for word in ["导师", "点评", "晋级", "淘汰"]):
        return "setup_to_payoff"
    return "unknown"


def _has_pairing_hook(text: str, artist_names: list[str]) -> bool:
    strong_terms = ["合唱", "共创", "搭配", "声线", "默契", "相配", "帮唱", "对唱", "合作", "同台", "联手", "合体"]
    if any(word in text for word in strong_terms):
        return True
    if len(artist_names) < 2:
        return False
    relation_terms = ["与", "和", "及", "CP", "cp", "feat", "Feat", "ft.", "FT."]
    return any(term in text for term in relation_terms)


def _commercial_intent(text: str) -> str:
    if any(word in text for word in ["购买", "下单", "直播间", "同款", "链接"]):
        return "ecommerce"
    if any(word in text for word in ["预约", "开播", "会员", "正片"]):
        return "soft_promo"
    return "none"


def _classification_confidence(
    aweme_id: str,
    visible_count: str,
    tags: list[str],
    *,
    content_category: str,
    hook_type: str,
    slice_structure: str,
    artist_names: str,
    song_title: str,
) -> str:
    evidence_count = sum(
        1
        for value in [
            content_category != "unknown",
            hook_type != "unknown",
            slice_structure != "unknown",
            bool(_text(artist_names)),
            bool(_text(song_title)),
        ]
        if value
    )
    if aweme_id and tags and evidence_count >= 3:
        return "high"
    if aweme_id or tags or visible_count or evidence_count >= 1:
        return "medium"
    return "low"


def _is_generic_artist_token(value: str) -> bool:
    clean = _clean_text(value).strip("#@ ")
    if not clean:
        return True
    if clean in GENERIC_ARTIST_TAGS:
        return True
    lowered = clean.lower()
    if lowered in {"reaction", "live", "cover", "rap"}:
        return True
    if re.fullmatch(r".*(?:第[一二三四五六七八九十0-9]+期|20\d{2}|第\d+季).*", clean):
        return True
    return any(hint in clean for hint in GENERIC_ARTIST_HINTS)


def _split_tags(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [_clean_text(item) for item in value if _clean_text(item)]
    text = _clean_text(value)
    if not text:
        return []
    if "|" in text:
        return [_clean_text(item) for item in text.split("|") if _clean_text(item)]
    tags = re.findall(r"#[^#\s]+", text)
    return tags or [_clean_text(item) for item in re.split(r"[,，、;；\s]+", text) if _clean_text(item)]


def _split_pipe(value: Any) -> list[str]:
    return [_clean_text(item) for item in str(value or "").split("|") if _clean_text(item)]


def _text(value: Any) -> str:
    return _clean_text(value)


def _clean_text(value: Any) -> str:
    decoded = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", decoded).strip()


def _unique(values: Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = _clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
