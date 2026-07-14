from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path


FIELDS_MARKER = "    analysis_prompt: list[str] = []\n"
HELPER_MARKER = "def _clip_analysis_prompt(payload: ClipAnalyzeRequest) -> str:\n"


def patch_service(path: Path) -> Path:
    source = path.read_text(encoding="utf-8")
    if FIELDS_MARKER not in source:
        needle = "    return_audio: bool = False\n\n\nclass LoadModelRequest"
        replacement = (
            "    return_audio: bool = False\n"
            "    prompt_profile: str = \"\"\n"
            "    analysis_prompt: list[str] = []\n"
            "    semantic_schema: dict[str, Any] = {}\n"
            "    max_new_tokens: int = 0\n\n\n"
            "class LoadModelRequest"
        )
        if needle not in source:
            raise RuntimeError("ClipAnalyzeRequest marker not found")
        source = source.replace(needle, replacement, 1)

    old_prompt_start = "    prompt = (\n        \"你是短视频语义校准助手。请根据标题、转写文本、标签和时长，\""
    old_prompt_end = "            ensure_ascii=False,\n        )\n    )\n    content: list[dict[str, Any]] = []"
    if "    prompt = _clip_analysis_prompt(payload)\n" not in source:
        start = source.find(old_prompt_start)
        end = source.find(old_prompt_end, start)
        if start < 0 or end < 0:
            raise RuntimeError("Omni prompt block marker not found")
        end += len(old_prompt_end)
        source = source[:start] + "    prompt = _clip_analysis_prompt(payload)\n    content: list[dict[str, Any]] = []" + source[end:]

    old_tokens = '    max_new_tokens = int(os.getenv("DSO_OMNI_MEDIA_MAX_NEW_TOKENS", "96" if video_path else "256"))'
    new_tokens = (
        '    configured_tokens = int(os.getenv("DSO_OMNI_MEDIA_MAX_NEW_TOKENS", "96" if video_path else "256"))\n'
        "    max_new_tokens = max(64, min(384, int(payload.max_new_tokens or configured_tokens)))"
    )
    if old_tokens in source:
        source = source.replace(old_tokens, new_tokens, 1)

    if HELPER_MARKER not in source:
        marker = "\ndef _omni_multimodal_inputs(processor: Any, messages: list[dict[str, Any]], text: str) -> tuple[Any, dict[str, Any]]:\n"
        helper = r'''

def _clip_analysis_prompt(payload: ClipAnalyzeRequest) -> str:
    input_payload = {
        "title": payload.title,
        "transcript": payload.transcript,
        "tags": payload.tags,
        "duration_seconds": payload.duration_seconds,
        "entity_type": payload.entity_type,
        "semantic_schema": payload.semantic_schema,
    }
    if payload.prompt_profile == "material_evidence_d10b" and payload.analysis_prompt:
        return "\n".join(payload.analysis_prompt) + "\n\n输入：" + json.dumps(input_payload, ensure_ascii=False)
    return (
        "你是短视频语义校准助手。请根据标题、转写文本、标签和时长，"
        "只输出 JSON，不要输出解释。字段包括 content_category, hook_type, "
        "slice_structure, artist_names, song_title, tags, risk_flags, advice。"
        "\n\n输入：" + json.dumps(input_payload, ensure_ascii=False)
    )
'''
        if marker not in source:
            raise RuntimeError("Omni helper insertion marker not found")
        source = source.replace(marker, helper + marker, 1)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(path.suffix + f".pre_d10b_{stamp}.bak")
    shutil.copy2(path, backup)
    path.write_text(source, encoding="utf-8")
    compile(source, str(path), "exec")
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch the deployed Qwen Omni service for D10-B evidence prompts.")
    parser.add_argument("--app", default="/home/aidev/dso_multimodal_model_service/app.py")
    args = parser.parse_args()
    path = Path(args.app).expanduser()
    backup = patch_service(path)
    print(f"patched={path}")
    print(f"backup={backup}")


if __name__ == "__main__":
    main()
