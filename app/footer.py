"""状态栏 tag 化：一份定义、三套渲染（pi / claude / codex 共用）。

为什么单独做：原来 pi 的 footer 把「模型 · 内存 · CPU · 命中率 · 字速 · tokens · 缓存」
全写死在代码里，想删一段、换个分隔符都得改源码，删错了就整行不见。
这里把每一段变成一个 **tag**，顺序、是否显示、分隔符都进配置，
渲染层只负责按配置拼字符串 —— 所以预览和真机显示是同一套逻辑。

配置文件：~/.aac/footer.json（跟着 .aac 包同步）
  {
    "version": 1,
    "enabled": true,
    "separator": "│",
    "lines": [["model", "thinking"], ["mem", "cpu", "cache", "speed"],
              ["tokens", "cache_time"]],
    "agents": {"claude": {...}}     # 可选：单个工具覆盖上面这套
  }
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

CONFIG_DIR = Path(os.path.expanduser("~")) / ".aac"
CONFIG_FILE = CONFIG_DIR / "footer.json"
MAX_LINES = 3
MAX_TAGS_PER_LINE = 8

SEPARATORS = {
    "bar": "│",
    "space": " ",
    "slash": "/",
    "dot": "·",
}
DEFAULT_SEPARATOR = "bar"

# tag 定义：id -> (中文名, 说明, 示例值生产者)
TAGS: dict[str, dict] = {
    "model": {"name": "模型名称", "desc": "当前用的模型", "sample": "deepseek-v4.1-flash"},
    "thinking": {"name": "思考强度", "desc": "如 [high] / [low]", "sample": "[high]"},
    "mem": {"name": "内存", "desc": "内存占用百分比", "sample": "内存 62%"},
    "cpu": {"name": "CPU", "desc": "CPU 占用百分比", "sample": "CPU 37%"},
    "cache": {"name": "缓存命中率", "desc": "同心圆 + 百分比", "sample": "⊙ 99%"},
    "speed": {"name": "输出速度", "desc": "字/秒", "sample": "76.5 字/秒"},
    "tokens": {"name": "已用 tokens", "desc": "本会话累计", "sample": "已用 tokens：88,528"},
    "cache_time": {"name": "缓存状态", "desc": "倒计时或已过期", "sample": "⏳ 缓存已过期"},
    "cwd": {"name": "当前目录", "desc": "工作目录（~ 缩写）", "sample": "~/dev/proj"},
    "git": {"name": "Git 分支", "desc": "当前分支名", "sample": " main"},
    "session": {"name": "会话名", "desc": "人工命名或自动生成", "sample": "会话 fix-footer"},
    "time": {"name": "时间", "desc": "本机时间 HH:MM", "sample": "14:32"},
    "cost": {"name": "花费", "desc": "本会话费用", "sample": "¥0.42"},
}

TAG_ORDER = list(TAGS)

PRESETS: dict[str, dict] = {
    "perf": {
        "name": "性能三行（当前效果）",
        "desc": "模型 + 内存 CPU 命中率 字速 + tokens 缓存",
        "config": {"separator": "bar",
                   "lines": [["model", "thinking"], ["mem", "cpu", "cache", "speed"],
                             ["tokens", "cache_time"]]},
    },
    "minimal": {
        "name": "极简一行",
        "desc": "只留模型和已用 tokens",
        "config": {"separator": "bar", "lines": [["model", "tokens"]]},
    },
    "full": {
        "name": "全量信息",
        "desc": "加目录、git 分支、会话名、时间、花费",
        "config": {"separator": "dot",
                   "lines": [["model", "thinking", "git", "cwd"],
                             ["mem", "cpu", "cache", "speed"],
                             ["tokens", "cache_time", "cost", "time"]]},
    },
}

DEFAULT_CONFIG = {"version": 1, "enabled": True,
                  "separator": "bar", "lines": list(PRESETS["perf"]["config"]["lines"]),
                  "agents": {}}


def _clean_lines(lines) -> list[list[str]]:
    """收缩成合法结构：只留认识的 tag，去空行，限行数。"""
    out: list[list[str]] = []
    if not isinstance(lines, list):
        return list(DEFAULT_CONFIG["lines"])
    for line in lines:
        if not isinstance(line, list):
            continue
        tags = []
        for tag in line:
            if isinstance(tag, str) and tag in TAGS and tag not in tags:
                tags.append(tag)
        if tags:
            out.append(tags[:MAX_TAGS_PER_LINE])
        if len(out) >= MAX_LINES:
            break
    return out or list(DEFAULT_CONFIG["lines"])


def normalize(config) -> dict:
    """把任意形状（前端乱传 / 老版本配置）收敛成合法配置，绝不抛异常。"""
    config = config if isinstance(config, dict) else {}
    sep = config.get("separator")
    if sep not in SEPARATORS:
        sep = DEFAULT_SEPARATOR
    agents_in = config.get("agents")
    agents: dict[str, dict] = {}
    if isinstance(agents_in, dict):
        for key, value in agents_in.items():
            if isinstance(key, str) and isinstance(value, dict):
                agents[key] = {
                    "separator": value.get("separator") if value.get("separator") in SEPARATORS else sep,
                    "lines": _clean_lines(value.get("lines")),
                }
    return {
        "version": 1,
        "enabled": bool(config.get("enabled", True)),
        "separator": sep,
        "lines": _clean_lines(config.get("lines")),
        "agents": agents,
    }


def config_for(agent_id: str | None = None) -> dict:
    """取配置；agent_id 有覆盖就用覆盖，否则用全局默认。"""
    base = load_config()
    override = (base.get("agents") or {}).get(agent_id) if agent_id else None
    if not override:
        return base
    return {**base, **override}


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 配置损坏也要能回到默认模板
        data = {}
    return normalize(data)


def save_config(config) -> dict:
    clean = normalize(config)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.part")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_FILE)
    return clean


def sample_stats() -> dict:
    """预览用的样例数据：形状与真实 stats 一致，数字看着像真的。"""
    return {
        "model": "deepseek-v4.1-flash",
        "thinking": "high",
        "mem": 62,
        "cpu": 37,
        "cache_hit": 99,
        "speed": 76.5,
        "tokens": 88528,
        "cache_left_ms": -1,
        "cwd": "~/dev/proj",
        "git": "main",
        "session": "会话 fix-footer",
        "time": "14:32",
        "cost": 0.42,
    }


def render_tag(tag: str, stats: dict) -> str:
    """单个 tag → 文本。数据缺失就返回空串，由调用方决定怎么省略。"""
    if tag == "model":
        value = stats.get("model") or ""
        return str(value)
    if tag == "thinking":
        value = stats.get("thinking")
        return f"[{value}]" if value else ""
    if tag == "mem":
        value = stats.get("mem")
        return f"内存 {int(value)}%" if value is not None else ""
    if tag == "cpu":
        value = stats.get("cpu")
        return f"CPU {int(value)}%" if value is not None else ""
    if tag == "cache":
        value = stats.get("cache_hit")
        return f"⊙ {int(value)}%" if value is not None else ""
    if tag == "speed":
        value = stats.get("speed")
        return f"{float(value):.1f} 字/秒" if value is not None else "— 字/秒"
    if tag == "tokens":
        value = stats.get("tokens")
        return f"已用 tokens：{int(value):,}" if value is not None else "已用 tokens：—"
    if tag == "cache_time":
        left = stats.get("cache_left_ms")
        if left is None:
            return "⏳ 无缓存"
        if left <= 0:
            return "⏳ 缓存已过期"
        seconds = int(left // 1000)
        return f"⏳ 缓存 {seconds // 60}m{seconds % 60:02d}s"
    if tag == "cwd":
        return str(stats.get("cwd") or "")
    if tag == "git":
        value = stats.get("git")
        return f"⎇ {value}" if value else ""
    if tag == "session":
        value = stats.get("session")
        return f"会话 {value}" if value else ""
    if tag == "time":
        return str(stats.get("time") or time.strftime("%H:%M"))
    if tag == "cost":
        value = stats.get("cost")
        return f"¥{float(value):.2f}" if value is not None else ""
    return ""


def render_lines(config: dict | None = None, stats: dict | None = None,
                 agent_id: str | None = None) -> list[str]:
    """按配置拼出每一行。行内空 tag 自动跳过，不会留下空分隔符。"""
    cfg = normalize(config) if config is not None else config_for(agent_id)
    if not cfg.get("enabled", True):
        return []
    data = stats if isinstance(stats, dict) else sample_stats()
    joiner = f" {SEPARATORS[cfg['separator']]} "
    lines: list[str] = []
    for line in cfg["lines"]:
        parts = [text for text in (render_tag(tag, data) for tag in line) if text]
        if parts:
            lines.append(joiner.join(parts))
    return lines


def render_text(config: dict | None = None, stats: dict | None = None,
                agent_id: str | None = None) -> str:
    return "\n".join(render_lines(config, stats, agent_id))


def catalog(agent_id: str | None = None) -> dict:
    """给设置页用的完整信息：tag 列表、分隔符、预设、当前配置、预览。"""
    cfg = config_for(agent_id)
    return {
        "tags": [{"id": tag, **info, "preview": render_tag(tag, sample_stats())}
                 for tag, info in TAGS.items()],
        "separators": [{"id": key, "label": value, "bytes": value}
                       for key, value in SEPARATORS.items()],
        "presets": [{"id": key, **value} for key, value in PRESETS.items()],
        "config": cfg,
        "preview": render_lines(cfg),
        "config_path": str(CONFIG_FILE),
        "exists": CONFIG_FILE.is_file(),
    }
