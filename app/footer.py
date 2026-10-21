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

import datetime
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
    "session": {"name": "会话名", "desc": "人工命名或自动生成", "sample": "fix-footer"},
    "time": {"name": "时间", "desc": "本机时间 HH:MM", "sample": "14:32"},
    "cost": {"name": "花费", "desc": "本会话费用", "sample": "¥0.42"},
}

TAG_ORDER = list(TAGS)

SEPARATOR_NAMES = {"bar": "竖线", "space": "空格", "slash": "斜杠", "dot": "圆点"}

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

RATE_DEFAULT = 7.2          # 美元 → 人民币估算汇率，可在 footer.json 里改 cny_per_usd
CACHE_TTL_MS = 5 * 60 * 1000


def _rate(value) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return RATE_DEFAULT
    return rate if 1.0 <= rate <= 20.0 else RATE_DEFAULT


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
        "cny_per_usd": _rate(config.get("cny_per_usd")),
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


SLOTS: list[tuple[str, str]] = [
    ("default", "通用默认"),
    ("pi", "Pi"),
    ("claude", "Claude Code"),
    ("codex", "Codex"),
]


def slot_config(full: dict, slot: str | None) -> dict:
    """取某一槽位（通用 / 某个工具）实际生效的那份配置，不含 agents 表。"""
    full = normalize(full)
    slot = (slot or "default").strip() or "default"
    override = (full.get("agents") or {}).get(slot)
    if slot == "default" or not override:
        return {"enabled": full["enabled"], "separator": full["separator"],
                "lines": [list(line) for line in full["lines"]]}
    return {"enabled": full["enabled"], "separator": override["separator"],
            "lines": [list(line) for line in override["lines"]]}


def save_slot(full: dict, slot: str | None, config) -> dict:
    """只改一个槽位，别的工具的自定义原样留着。

    通用槽位改的是全局默认；某个工具槽位存进 agents.<id>，等于"这个工具
    用自己的一套"，没设过的工具继续跟着通用默认走。
    """
    full = normalize(full)
    clean = normalize(config)
    slot = (slot or "default").strip() or "default"
    if slot == "default":
        full["enabled"] = clean["enabled"]
        full["separator"] = clean["separator"]
        full["lines"] = clean["lines"]
        return save_config(full)
    full.setdefault("agents", {})[slot] = {"separator": clean["separator"],
                                           "lines": clean["lines"]}
    return save_config(full)


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
        "cwd": "proj",
        "git": "main",
        "session": "fix-footer",
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
        # 拿不到速度就整段不出（claude 状态栏给不了 token 生成耗时）
        return f"{float(value):.1f} 字/秒" if value is not None else ""
    if tag == "tokens":
        value = stats.get("tokens")
        return f"已用 tokens：{int(value):,}" if value is not None else ""
    if tag == "cache_time":
        left = stats.get("cache_left_ms")
        if left is None:
            return ""             # 没缓存数据就别占地方，不编“无缓存”
        if left <= 0:
            return "⏳ 缓存已过期"
        seconds = int(left // 1000)
        return f"⏳ 缓存 {seconds // 60}m{seconds % 60:02d}s"
    if tag == "cwd":
        value = stats.get("cwd")
        return f"📂 {short_path(str(value))}" if value else ""
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
        if value is None:
            return ""
        # provider 没配单价时算出来是 0，这时候不能说"花了一分钱"——如实写未计价。
        # 有值时加 ≈：这是按汇率估算的，不是账单金额。
        return f"≈¥{float(value):.2f}" if float(value) > 0 else "¥— 未计价"
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
    """给设置页用的完整信息：tag 列表、分隔符、预设、当前配置、预览。

    `editing` 是当前正在编辑的那个槽位（通用 / pi / claude / codex）真正
    生效的内容；`overrides` 告诉界面哪些工具已经单独设过。
    """
    full = load_config()
    slot = (agent_id or "default").strip() or "default"
    cfg = slot_config(full, slot)
    return {
        "slots": [{"id": key, "name": name} for key, name in SLOTS],
        "slot": slot,
        "overrides": sorted((full.get("agents") or {}).keys()),
        "editing": cfg,
        "tags": [{"id": tag, **info, "preview": render_tag(tag, sample_stats())}
                 for tag, info in TAGS.items()],
        "separators": [{"id": key, "label": value, "name": SEPARATOR_NAMES.get(key, ""),
                        "bytes": value}
                       for key, value in SEPARATORS.items()],
        "presets": [{"id": key, **value} for key, value in PRESETS.items()],
        "config": cfg,
        "preview": render_lines(cfg),
        "config_path": str(CONFIG_FILE),
        "exists": CONFIG_FILE.is_file(),
    }


# ---------------------------------------------------------------- 跨工具渲染

def stats_from_claude_payload(payload: dict) -> dict:
    """Claude Code 通过 stdin 传给 statusLine 命令的 JSON → 我们的 stats 形状。

    没有的字段留空（渲染时会跳过），绝不编造数字：这里显示的每个值都来自
    真实数据或本机采样。
    """
    payload = payload if isinstance(payload, dict) else {}
    model = (payload.get("model") or {}) if isinstance(payload.get("model"), dict) else {}
    workspace = (payload.get("workspace") or {}) if isinstance(payload.get("workspace"), dict) else {}
    cost = (payload.get("cost") or {}) if isinstance(payload.get("cost"), dict) else {}
    cwd = workspace.get("current_dir") or payload.get("cwd") or ""
    stats = {
        "model": model.get("display_name") or model.get("id") or "",
        "thinking": str((payload.get("effort") or {}).get("level") or "")
        if isinstance(payload.get("effort"), dict) else "",
        "cwd": short_path(str(cwd)),
        "git": _git_branch(str(cwd)),
        "session": str(payload.get("session_id") or "")[:8],
        "time": time.strftime("%H:%M"),
        "cost": None,
        "tokens": None,
        "speed": None,
        "cache_hit": None,
        "cache_left_ms": None,
        "mem": None,
        "cpu": None,
    }
    try:
        total = cost.get("total_cost_usd")
        if isinstance(total, (int, float)):
            stats["cost"] = float(total) * _rate(config_for("claude").get("cny_per_usd"))
    except Exception:  # noqa: BLE001
        pass
    mem = mem_percent()
    if mem is not None:
        stats["mem"] = mem
    usage = _last_usage_from_transcript(payload.get("transcript_path"))
    if usage:
        stats["tokens"] = usage.get("tokens")
        stats["cache_hit"] = usage.get("cache_hit")
        stats["cache_left_ms"] = usage.get("cache_left_ms")
    return stats


def mem_percent() -> float | None:
    """内存占用百分比。macOS 用 vm_stat，Linux 用 /proc/meminfo，读不到就返回 None。

    不用「1 - 空闲率」的粗暴算法：macOS 会故意把空闲页压到极低，
    os.freemem 那条路永远显示 99%，看着像机器要炸了其实没事。
    """
    import subprocess
    import sys

    if sys.platform == "darwin":
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=1).stdout
        except Exception:  # noqa: BLE001
            return None
        import re

        page = re.search(r"page size of (\d+) bytes", out)
        if not page:
            return None
        page_size = int(page.group(1))

        def pages(label: str) -> int:
            found = re.search(rf"^{label}:\s+(\d+)\.", out, re.MULTILINE)
            return int(found.group(1)) if found else 0

        used = pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")
        try:
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError):
            return None
        if total <= 0 or used <= 0:
            return None
        return max(0.0, min(100.0, used * page_size / total * 100))
    if sys.platform.startswith("linux"):
        try:
            info = Path("/proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            return None
        values = {}
        for line in info.splitlines():
            key, _, rest = line.partition(":")
            values[key.strip()] = rest.strip().split(" ")[0]
        try:
            total = int(values.get("MemTotal", "0"))
            available = int(values.get("MemAvailable", "0"))
        except ValueError:
            return None
        if total <= 0:
            return None
        return max(0.0, min(100.0, (total - available) / total * 100))
    return None  # Windows 没顺手的方式，宁可不显示也不瞎猜


def short_path(value: str) -> str:
    """只保留最后一级目录名：状态栏宽度有限，整条路径没意义。"""
    cleaned = value.rstrip("/\\")
    return os.path.basename(cleaned) or cleaned


def _git_branch(cwd: str) -> str:
    import subprocess

    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=1)
    except Exception:  # noqa: BLE001
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _last_usage_from_transcript(path) -> dict | None:
    """从 claude 的 transcript（jsonl）里捞最近一条有真实用量的助手消息。

    这是拿到「已用 tokens / 缓存命中」的唯一途径：statusLine 的 JSON 里没有。
    全部为 0（走第三方代理时很常见）就当没有数据返回 None，
    由渲染层整段省略——不显示 "?"，也不显示 0。
    """
    if not path:
        return None
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-200:]
    except OSError:
        return None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        usage = (message or {}).get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        try:
            input_tokens = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens")
                              or usage.get("cache_write_input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        total = input_tokens + cache_read + cache_write + output_tokens
        if total <= 0:
            continue                      # 代理返回空用量：不是 0 花费，是没有数据
        window = input_tokens + cache_read
        left = None
        if cache_read or cache_write:
            stamp = entry.get("timestamp")
            if isinstance(stamp, str):
                try:
                    when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    age = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()
                    left = int(CACHE_TTL_MS - age * 1000)
                except (ValueError, TypeError):
                    left = None
            else:
                left = None
        return {"tokens": total,
                "cache_hit": (cache_read / window * 100) if window else None,
                "cache_left_ms": left}
    return None


def render_from_stdin(agent_id: str | None = None) -> str:
    """给 claude 的 statusLine 命令用：stdin 读 JSON，stdout 输出状态栏。"""
    import sys

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 输入不是 JSON 也要给出一行可用状态栏
        payload = {}
    return render_text(stats=stats_from_claude_payload(payload), agent_id=agent_id)


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="状态栏渲染（给 claude 的 statusLine 用）")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读 claude 的 JSON")
    parser.add_argument("--agent", default=None, help="按哪个工具的覆盖配置渲染")
    parser.add_argument("--print-config", action="store_true", help="打印当前配置与预览")
    args = parser.parse_args(argv)
    if args.print_config:
        sys.stdout.write(render_text(agent_id=args.agent) + "\n")
        sys.stdout.write(f"配置：{CONFIG_FILE}\n")
        return 0
    sys.stdout.write(render_from_stdin(args.agent) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
