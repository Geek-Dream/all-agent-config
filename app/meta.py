"""配置项的标签与备注存储（~/.aac/meta.json）。

结构：{agent_id: {item_id: {"tag": "≤6字标签", "note": "备注说明"}}}
item_id 与 configview 的条目 id 一致（fs|路径 或 json|文件|容器|名称）。
"""

from __future__ import annotations

import json
from pathlib import Path

META_FILE = Path.home() / ".aac" / "meta.json"
MAX_TAG = 18        # 约 6 个中文字符
MAX_NOTE = 500


def _load() -> dict:
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_agent(agent_id: str) -> dict:
    return _load().get(agent_id, {})


def set_item(agent_id: str, item_id: str,
             tag: str | None = None, note: str | None = None) -> dict:
    data = _load()
    entry = data.setdefault(agent_id, {}).setdefault(item_id, {})
    if tag is not None:
        entry["tag"] = tag.strip()[:MAX_TAG]
    if note is not None:
        entry["note"] = note.strip()[:MAX_NOTE]
    if not entry.get("tag") and not entry.get("note"):
        data[agent_id].pop(item_id, None)
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry
