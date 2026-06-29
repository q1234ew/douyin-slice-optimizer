from __future__ import annotations

import json
from pathlib import Path


_INITIAL_STATE_PLACEHOLDER = "__DSO_INITIAL_STATE__"


def render_dashboard(stats: dict, videos: list[dict]) -> str:
    initial_state = json.dumps({"stats": stats, "videos": videos}, ensure_ascii=False).replace("</", "<\\/")
    template = _dashboard_template()
    if _INITIAL_STATE_PLACEHOLDER in template:
        return template.replace(_INITIAL_STATE_PLACEHOLDER, initial_state)
    return template.replace(
        '<div id="app"></div>',
        f'<script id="dso-initial-state" type="application/json">{initial_state}</script>\n    <div id="app"></div>',
    )


def dashboard_static_dir() -> Path:
    return Path(__file__).with_name("static") / "dashboard"


def _dashboard_template() -> str:
    built_index = dashboard_static_dir() / "index.html"
    if built_index.is_file():
        return built_index.read_text(encoding="utf-8")

    source_index = Path(__file__).parents[3] / "frontend" / "index.html"
    if source_index.is_file():
        return source_index.read_text(encoding="utf-8")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="dso-frontend" content="vue3-vite-typescript" />
  <title>Douyin Slice Optimizer</title>
</head>
<body>
  <script id="dso-initial-state" type="application/json">{_INITIAL_STATE_PLACEHOLDER}</script>
  <div id="app"></div>
</body>
</html>
"""
