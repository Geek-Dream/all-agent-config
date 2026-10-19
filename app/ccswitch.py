"""从 cc-switch 的 SQLite 数据库只读读取供应商配置，供「选择性导入」使用。

- 只读打开（mode=ro），绝不修改 cc-switch 的任何数据；
- 把各 app_type 的 settings_config 映射成 AAC 的供应商条目；
- gemini 的自定义端点需写 ~/.gemini/.env，受 R3 约束，标记为不可导入。
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from pathlib import Path

from . import providers, registry

DEFAULT_DB = Path.home() / ".cc-switch" / "cc-switch.db"

APP_TO_AGENT = {
    "claude": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
    "pi": "pi",
    "openclaw": "openclaw",
    "gemini": "gemini-cli",
}


def _agent_supports(agent_id: str) -> bool:
    agent = registry.get_agent(agent_id) if agent_id else None
    return bool(agent and providers.supported(agent))


def _first_model(models: object) -> str:
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict):
                v = m.get("id") or m.get("name")
                if isinstance(v, str) and v:
                    return v
    if isinstance(models, dict):
        return next((k for k in models if isinstance(k, str)), "")
    return ""


def _parse_codex(conf: str) -> tuple[str | None, dict]:
    try:
        import tomllib
        data = tomllib.loads(conf or "")
    except Exception:  # noqa: BLE001
        return None, {}
    pid = data.get("model_provider") if isinstance(data.get("model_provider"), str) else None
    provs = data.get("model_providers") if isinstance(data.get("model_providers"), dict) else {}
    entry = provs.get(pid) if pid else None
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry["_model"] = data.get("model", "")
    return pid, entry


def _map(app_type: str, name: str, cfg: str | None, is_current: object) -> dict:
    out: dict = {
        "app_type": app_type,
        "agent_id": APP_TO_AGENT.get(app_type),
        "name": name or "",
        "current": bool(is_current),
        "base_url": "",
        "api_key": "",
        "model": "",
        "small_model": "",
        "extra": {},
        "native_id": None,
        "supported": False,
        "note": "",
    }
    try:
        d = json.loads(cfg) if cfg else {}
    except Exception:  # noqa: BLE001
        d = {}
    d = d if isinstance(d, dict) else {}

    if app_type == "claude":
        env = d.get("env") if isinstance(d.get("env"), dict) else {}
        token, key = env.get("ANTHROPIC_AUTH_TOKEN"), env.get("ANTHROPIC_API_KEY")
        out["base_url"] = env.get("ANTHROPIC_BASE_URL", "")
        out["api_key"] = token or key or ""
        out["model"] = env.get("ANTHROPIC_MODEL", "")
        out["small_model"] = env.get("ANTHROPIC_SMALL_FAST_MODEL", "")
        out["extra"] = {"auth_field": "ANTHROPIC_API_KEY" if (key and not token) else "ANTHROPIC_AUTH_TOKEN"}
    elif app_type == "codex":
        pid, entry = _parse_codex(d.get("config") if isinstance(d.get("config"), str) else "")
        auth = d.get("auth") if isinstance(d.get("auth"), dict) else {}
        out["native_id"] = pid
        out["base_url"] = entry.get("base_url", "") or ""
        out["api_key"] = entry.get("experimental_bearer_token") or auth.get("OPENAI_API_KEY") or ""
        out["model"] = entry.get("_model", "")
        extra = {"wire_api": entry.get("wire_api") or "responses"}
        if entry.get("requires_openai_auth"):
            extra["requires_openai_auth"] = True
        out["extra"] = extra
    elif app_type == "opencode":
        opts = d.get("options") if isinstance(d.get("options"), dict) else {}
        out["base_url"] = opts.get("baseURL", "")
        out["api_key"] = opts.get("apiKey", "")
        out["model"] = _first_model(d.get("models"))
        if d.get("npm"):
            out["extra"] = {"npm": d.get("npm")}
    elif app_type == "pi":
        out["base_url"] = d.get("baseUrl", "")
        out["api_key"] = d.get("apiKey", "")
        out["model"] = _first_model(d.get("models"))
        if d.get("api"):
            out["extra"] = {"pi_api": d.get("api")}
    elif app_type == "openclaw":
        out["base_url"] = d.get("baseUrl", "")
        out["api_key"] = d.get("apiKey", "")
        out["model"] = _first_model(d.get("models"))
        if d.get("api"):
            out["extra"] = {"openclaw_api": d.get("api")}
    elif app_type == "gemini":
        env = d.get("env") if isinstance(d.get("env"), dict) else {}
        out["base_url"] = env.get("GOOGLE_GEMINI_BASE_URL", "") or env.get("GEMINI_BASE_URL", "")
        out["api_key"] = env.get("GEMINI_API_KEY", "") or env.get("GOOGLE_API_KEY", "")
        out["model"] = env.get("GEMINI_MODEL", "")

    if app_type == "gemini":
        out["note"] = "Gemini 自定义端点需写 ~/.gemini/.env，受安全约束（R3），暂不支持导入"
    elif not out["agent_id"]:
        out["note"] = "无对应工具"
    elif not _agent_supports(out["agent_id"]):
        out["note"] = "该工具暂不支持供应商管理"
    else:
        out["supported"] = True
    return out


def _sqlite_uri(path: Path) -> str:
    # SQLite 的 URI 文件名要求正斜杠并对特殊字符做百分号编码：Windows 的
    # 反斜杠路径与含空格的用户名（C:\Users\John Doe\...）否则会解析失败。
    quoted = urllib.parse.quote(path.as_posix(), safe="/:")
    return f"file:{quoted}?mode=ro"


def scan(db_path: str | None = None) -> dict:
    path = Path(os.path.expanduser(str(db_path or DEFAULT_DB)))
    result = {"db": str(path), "exists": path.is_file(), "entries": [], "error": None}
    if not path.is_file():
        return result
    con = None
    try:
        con = sqlite3.connect(_sqlite_uri(path), uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT app_type, name, is_current, settings_config FROM providers "
            "ORDER BY app_type, sort_index, name"
        ).fetchall()
        for r in rows:
            result["entries"].append(
                _map(r["app_type"], r["name"], r["settings_config"], r["is_current"]))
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        if con is not None:
            con.close()
    return result


def import_selected(db_path: str | None, selected: list[dict]) -> dict:
    """按 (app_type, name) 选择把 cc-switch 里的条目导入 AAC，返回各工具导入数量。"""
    wanted = {(str(s.get("app_type")), str(s.get("name"))) for s in (selected or [])
              if isinstance(s, dict)}
    if not wanted:
        return {"imported": 0, "agents": {}}
    found = scan(db_path)
    if not found["exists"]:
        raise ValueError(f"未找到 cc-switch 数据库：{found['db']}")
    if found["error"]:
        raise ValueError(f"读取 cc-switch 数据库失败：{found['error']}")

    by_agent: dict[str, list[dict]] = {}
    for e in found["entries"]:
        if (e["app_type"], e["name"]) not in wanted:
            continue
        if not e["supported"] or not e["agent_id"]:
            continue
        by_agent.setdefault(e["agent_id"], []).append(e)

    agents_result: dict[str, int] = {}
    total = 0
    for agent_id, entries in by_agent.items():
        n = providers.merge_entries(agent_id, entries)
        agents_result[agent_id] = n
        total += n
    return {"imported": total, "agents": agents_result}
