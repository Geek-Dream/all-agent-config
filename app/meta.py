"""配置项的标签与备注存储（~/.aac/meta.json）。

结构：{agent_id: {item_id: {"tags": ["标签1", "标签2"], "note": "备注说明"}}}
item_id 与 configview 的条目 id 一致（fs|路径 或 json|文件|容器|名称）。
旧版单标签字段 "tag" 在读取时自动迁移为 tags[0]。
"""

from __future__ import annotations

import json
from pathlib import Path

META_FILE = Path.home() / ".aac" / "meta.json"
MAX_TAG = 18        # 单个标签约 6 个中文字符
MAX_TAGS = 8        # 每个条目最多 8 个标签
MAX_NOTE = 500


def _load() -> dict:
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _norm_tags(tags) -> list:
    out: list[str] = []
    for t in tags or []:
        t = str(t).strip()[:MAX_TAG]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def _norm_entry(entry: dict) -> dict:
    entry = dict(entry or {})
    if "tags" not in entry and entry.get("tag"):
        entry["tags"] = [entry["tag"]]
    entry.pop("tag", None)
    entry["tags"] = _norm_tags(entry.get("tags"))
    return entry


def get_agent(agent_id: str) -> dict:
    return {k: _norm_entry(v) for k, v in _load().get(agent_id, {}).items()}


def set_item(agent_id: str, item_id: str,
             tags: list | None = None, note: str | None = None) -> dict:
    data = _load()
    entry = data.setdefault(agent_id, {}).setdefault(item_id, {})
    if tags is not None:
        entry["tags"] = _norm_tags(tags)
        entry.pop("tag", None)
    if note is not None:
        entry["note"] = note.strip()[:MAX_NOTE]
    if not entry.get("tags") and not entry.get("tag") and not entry.get("note"):
        data[agent_id].pop(item_id, None)
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return _norm_entry(entry)
