"""供应商配置中心：保存多套 API 端点 / 密钥，并写回各工具自己的配置。

设计原则：
- 密钥只存本机 ~/.aac/providers.json（0600），API 返回一律脱敏；
- 切换时只改连接字段（地址 / 密钥 / 模型），插件、Hooks、MCP 和其他设置原样保留；
- 每次写回前自动备份被修改的配置文件为 .bak-aac-*；
- 纯文件操作，不依赖 AI。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import registry
from .categories import expand
from .safefs import atomic_write_text

STORE = Path.home() / ".aac" / "providers.json"
STORE_VERSION = 1

_MAX_NAME = 60
_MAX_URL = 500
_MAX_KEY = 4096
_MAX_MODEL = 200
_MAX_MODELS = 500


# ---------------------------------------------------------------- 存储

def _load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", STORE_VERSION)
    agents = data.get("agents")
    data["agents"] = agents if isinstance(agents, dict) else {}
    active = data.get("active")
    data["active"] = active if isinstance(active, dict) else {}
    return data


def _save(data: dict) -> None:
    atomic_write_text(STORE, json.dumps(data, ensure_ascii=False, indent=2), mode=0o600)


def _items(data: dict, agent_id: str) -> list[dict]:
    items = data["agents"].get(agent_id)
    if not isinstance(items, list):
        items = []
    data["agents"][agent_id] = [i for i in items if isinstance(i, dict)]
    return data["agents"][agent_id]


def _public(item: dict) -> dict:
    key = item.get("api_key") if isinstance(item.get("api_key"), str) else ""
    out = {k: v for k, v in item.items() if k != "api_key"}
    out["api_key_set"] = bool(key)
    out["api_key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else ("已设置" if key else "")
    return out


def _find(items: list[dict], provider_id: str) -> dict | None:
    for item in items:
        if item.get("id") == provider_id:
            return item
    return None


def _slug(value: str, fallback: str = "provider") -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value or "").strip("-").lower()
    return slug[:40] or fallback


def _clean(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


# ---------------------------------------------------------------- 配置写回

def _target(spec: dict, raw: str) -> Path:
    return expand(str(spec.get("target") or raw))


def _backup(path: Path) -> None:
    if not path.is_file():
        return
    suffix = time.strftime(".bak-aac-%Y%m%d-%H%M%S")
    index = 0
    while True:
        bak = path.with_name(path.name + suffix + (f"-{index}" if index else ""))
        try:
            with bak.open("xb") as dst, path.open("rb") as src:
                shutil.copyfileobj(src, dst)
            shutil.copystat(path, bak)
            return
        except FileExistsError:
            index += 1


def _write_text(path: Path, text: str) -> None:
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _set_toml_top(text: str, key: str, value: str) -> str:
    line = f"{key} = {_toml_quote(value)}"
    lines = text.splitlines()
    head_end = len(lines)
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("["):
            head_end = i
            break
    for i in range(head_end):
        if re.match(rf"^{re.escape(key)}\s*=", lines[i]):
            lines[i] = line
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    lines.insert(head_end, line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _set_toml_table(text: str, table: str, block: str) -> str:
    pattern = re.compile(rf"(?ms)^\[{re.escape(table)}\]\s*\n.*?(?=^\[|\Z)")
    if pattern.search(text):
        return pattern.sub(block.rstrip() + "\n\n", text, count=1)
    return text.rstrip() + "\n\n" + block.rstrip() + "\n"


def _apply_claude(agent: dict, provider: dict) -> list[str]:
    spec = agent.get("provider") or {}
    target = _target(spec, "~/.claude/settings.json")
    data = _read_json(target)
    env = data.get("env")
    env = env if isinstance(env, dict) else {}
    data["env"] = env

    extra = provider.get("extra") or {}
    auth_field = extra.get("auth_field") if isinstance(extra, dict) else None
    if auth_field not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        auth_field = "ANTHROPIC_AUTH_TOKEN"

    if provider.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = provider["base_url"]
    if provider.get("api_key"):
        env[auth_field] = provider["api_key"]
    if provider.get("model"):
        env["ANTHROPIC_MODEL"] = provider["model"]
    if provider.get("small_model"):
        env["ANTHROPIC_SMALL_FAST_MODEL"] = provider["small_model"]

    _write_text(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return [str(target)]


def _apply_codex(agent: dict, provider: dict) -> list[str]:
    spec = agent.get("provider") or {}
    target = _target(spec, "~/.codex/config.toml")
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    pid = _slug(provider.get("native_id") or provider.get("id") or provider.get("name"))
    model = provider.get("model") or ""

    text = _set_toml_top(text, "model_provider", pid)
    if model:
        text = _set_toml_top(text, "model", model)

    extra = provider.get("extra") or {}
    wire_api = extra.get("wire_api") if isinstance(extra, dict) else None
    if wire_api not in ("responses", "chat"):
        wire_api = "responses"
    requires_auth = bool(extra.get("requires_openai_auth")) if isinstance(extra, dict) else False
    block = (
        f"[model_providers.{pid}]\n"
        f"name = {_toml_quote(provider.get('name') or pid)}\n"
        f"base_url = {_toml_quote(provider.get('base_url') or '')}\n"
        f"wire_api = {_toml_quote(wire_api)}\n"
    )
    if provider.get("api_key"):
        block += f"experimental_bearer_token = {_toml_quote(provider['api_key'])}\n"
    if requires_auth:
        block += "requires_openai_auth = true\n"
    text = _set_toml_table(text, f"model_providers.{pid}", block)

    try:
        import tomllib
        tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"生成的 config.toml 无法解析，已取消写入：{exc}") from exc
    _write_text(target, text)
    return [str(target)]


def _opencode_config_path(agent: dict) -> Path:
    for raw in ("~/.config/opencode/opencode.json", "%APPDATA%/opencode/opencode.json"):
        path = expand(raw)
        if path.is_file():
            return path
    return expand("%APPDATA%/opencode/opencode.json" if os.name == "nt"
                 else "~/.config/opencode/opencode.json")


def _apply_opencode(agent: dict, provider: dict) -> list[str]:
    target = _opencode_config_path(agent)
    data = _read_json(target)
    providers = data.get("provider")
    providers = providers if isinstance(providers, dict) else {}
    data["provider"] = providers

    pid = _slug(provider.get("native_id") or provider.get("id") or provider.get("name"))
    entry = providers.get(pid)
    entry = entry if isinstance(entry, dict) else {}
    providers[pid] = entry

    extra = provider.get("extra") or {}
    npm = extra.get("npm") if isinstance(extra, dict) else None
    entry["npm"] = npm if isinstance(npm, str) and npm else "@ai-sdk/openai-compatible"

    options = entry.get("options")
    options = options if isinstance(options, dict) else {}
    entry["options"] = options
    if provider.get("base_url"):
        options["baseURL"] = provider["base_url"]
    if provider.get("api_key"):
        options["apiKey"] = provider["api_key"]

    model = provider.get("model") or ""
    if model:
        models = entry.get("models")
        models = models if isinstance(models, dict) else {}
        models.setdefault(model, {"name": model})
        entry["models"] = models
        data["model"] = f"{pid}/{model}"

    _write_text(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return [str(target)]


def _apply_pi(agent: dict, provider: dict) -> list[str]:
    spec = agent.get("provider") or {}
    slug = _slug(provider.get("native_id") or provider.get("id") or provider.get("name"))

    models_path = _pi_models_path(agent)
    models = _read_json(models_path)
    provs = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    models["providers"] = provs
    entry = provs.get(slug)
    entry = entry if isinstance(entry, dict) else {}
    provs[slug] = entry
    if provider.get("base_url"):
        entry["baseUrl"] = provider["base_url"]
    if provider.get("api_key"):
        entry["apiKey"] = provider["api_key"]
    extra = provider.get("extra") or {}
    api = extra.get("pi_api") if isinstance(extra, dict) else None
    if isinstance(api, str) and api:
        entry["api"] = api
    model = provider.get("model") or ""
    if model:
        models_map = entry.get("models")
        models_map = models_map if isinstance(models_map, dict) else {}
        models_map.setdefault(model, {"maxTokens": 8192})
        entry["models"] = models_map
    _write_text(models_path, json.dumps(models, ensure_ascii=False, indent=2) + "\n")

    settings_path = _target(spec, "~/.pi/agent/settings.json")
    settings = _read_json(settings_path)
    settings["defaultProvider"] = slug
    if model:
        settings["defaultModel"] = model
    _write_text(settings_path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return [str(models_path), str(settings_path)]


def _openclaw_path(agent: dict) -> Path:
    return _target(agent.get("provider") or {}, "~/.openclaw/openclaw.json")


def _apply_openclaw(agent: dict, provider: dict) -> list[str]:
    target = _openclaw_path(agent)
    data = _read_json(target)
    root = data.get("models") if isinstance(data.get("models"), dict) else {}
    data["models"] = root
    provs = root.get("providers") if isinstance(root.get("providers"), dict) else {}
    root["providers"] = provs

    pid = _slug(provider.get("native_id") or provider.get("id") or provider.get("name"))
    entry = provs.get(pid) if isinstance(provs.get(pid), dict) else {}
    provs[pid] = entry
    if provider.get("base_url"):
        entry["baseUrl"] = provider["base_url"]
    if provider.get("api_key"):
        entry["apiKey"] = provider["api_key"]
    extra = provider.get("extra") or {}
    api = extra.get("openclaw_api") if isinstance(extra, dict) else None
    if isinstance(api, str) and api:
        entry["api"] = api

    model = provider.get("model") or ""
    models = entry.get("models") if isinstance(entry.get("models"), list) else []
    if model and not any(isinstance(m, dict) and m.get("id") == model for m in models):
        models = models + [{"id": model, "name": model}]
    if models:
        entry["models"] = models

    agents_root = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    data["agents"] = agents_root
    defaults = agents_root.get("defaults") if isinstance(agents_root.get("defaults"), dict) else {}
    agents_root["defaults"] = defaults
    dm = defaults.get("model") if isinstance(defaults.get("model"), dict) else {}
    defaults["model"] = dm
    dm["primary"] = f"{pid}/{model}" if model else pid

    _write_text(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return [str(target)]


ADAPTERS = {
    "claude-env": _apply_claude,
    "codex-toml": _apply_codex,
    "opencode-json": _apply_opencode,
    "pi-json": _apply_pi,
    "openclaw-json": _apply_openclaw,
}


# ---------------------------------------------------------------- 当前状态

def _matches_native(item: dict, native_id: str | None) -> bool:
    if not native_id:
        return False
    if item.get("native_id") == native_id:
        return True
    return native_id in (_slug(item.get("id") or ""), _slug(item.get("name") or ""))


def _current(agent: dict, items: list[dict]) -> str | None:
    adapter = (agent.get("provider") or {}).get("adapter")
    if adapter == "claude-env":
        data = _read_json(_target(agent.get("provider") or {}, "~/.claude/settings.json"))
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        base = env.get("ANTHROPIC_BASE_URL")
        for item in items:
            if item.get("base_url") and item.get("base_url") == base:
                return item.get("id")
    elif adapter == "codex-toml":
        path = _target(agent.get("provider") or {}, "~/.codex/config.toml")
        if path.is_file():
            try:
                import tomllib
                pid = tomllib.loads(path.read_text(encoding="utf-8")).get("model_provider")
            except Exception:  # noqa: BLE001
                pid = None
            for item in items:
                if _matches_native(item, pid):
                    return item.get("id")
    elif adapter == "opencode-json":
        data = _read_json(_opencode_config_path(agent))
        prefix = str(data.get("model") or "").split("/", 1)[0] or None
        for item in items:
            if _matches_native(item, prefix):
                return item.get("id")
    elif adapter == "pi-json":
        data = _read_json(_target(agent.get("provider") or {}, "~/.pi/agent/settings.json"))
        pid = data.get("defaultProvider")
        for item in items:
            if _matches_native(item, pid):
                return item.get("id")
    elif adapter == "openclaw-json":
        data = _read_json(_openclaw_path(agent))
        primary = _openclaw_primary(data)
        pid = primary.split("/", 1)[0] if "/" in primary else (primary or None)
        for item in items:
            if _matches_native(item, pid):
                return item.get("id")
    return None


# ---------------------------------------------------------------- 扫描现有配置

# 每个扫描器返回 (全部供应商信息, 当前使用中的 native_id)，供面板一次性列出所有配置。

def _scan_claude(agent: dict) -> tuple[list[dict], str | None]:
    data = _read_json(_target(agent.get("provider") or {}, "~/.claude/settings.json"))
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    key = env.get("ANTHROPIC_API_KEY")
    info = {
        "native_id": None,
        "name": None,
        "base_url": env.get("ANTHROPIC_BASE_URL"),
        "api_key": token or key,
        "model": env.get("ANTHROPIC_MODEL"),
        "small_model": env.get("ANTHROPIC_SMALL_FAST_MODEL"),
        "extra": {"auth_field": "ANTHROPIC_API_KEY" if key and not token else "ANTHROPIC_AUTH_TOKEN"},
    }
    return ([info], None) if any((info["base_url"], info["api_key"], info["model"])) else ([], None)


def _scan_codex(agent: dict) -> tuple[list[dict], str | None]:
    path = _target(agent.get("provider") or {}, "~/.codex/config.toml")
    if not path.is_file():
        return [], None
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return [], None
    active = data.get("model_provider") if isinstance(data.get("model_provider"), str) else None
    provs = data.get("model_providers") if isinstance(data.get("model_providers"), dict) else {}
    out: list[dict] = []
    for pid, entry in provs.items():
        entry = entry if isinstance(entry, dict) else {}
        extra: dict = {"wire_api": entry.get("wire_api") or "responses"}
        if entry.get("requires_openai_auth"):
            extra["requires_openai_auth"] = True
        out.append({
            "native_id": pid,
            "name": entry.get("name"),
            "base_url": entry.get("base_url"),
            "api_key": entry.get("experimental_bearer_token"),
            "model": data.get("model") if pid == active else "",
            "small_model": "",
            "extra": extra,
        })
    return out, active


def _pi_models_path(agent: dict) -> Path:
    spec = agent.get("provider") or {}
    return expand(str(spec.get("models") or "~/.pi/agent/models.json"))


def _scan_pi(agent: dict) -> tuple[list[dict], str | None]:
    spec = agent.get("provider") or {}
    settings = _read_json(_target(spec, "~/.pi/agent/settings.json"))
    active = settings.get("defaultProvider") if isinstance(settings.get("defaultProvider"), str) else None
    model = settings.get("defaultModel")
    models = _read_json(_pi_models_path(agent))
    provs = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    out: list[dict] = []
    for pid, entry in provs.items():
        entry = entry if isinstance(entry, dict) else {}
        out.append({
            "native_id": pid,
            "name": pid,
            "base_url": entry.get("baseUrl"),
            "api_key": entry.get("apiKey"),
            "model": model if pid == active else "",
            "small_model": "",
            "extra": {"pi_api": entry.get("api")} if entry.get("api") else {},
        })
    return out, active


def _scan_opencode(agent: dict) -> tuple[list[dict], str | None]:
    data = _read_json(_opencode_config_path(agent))
    model = data.get("model") if isinstance(data.get("model"), str) else ""
    active = model.split("/", 1)[0] if "/" in model else None
    active_model = model.split("/", 1)[1] if "/" in model else ""
    provs = data.get("provider") if isinstance(data.get("provider"), dict) else {}
    out: list[dict] = []
    for pid, entry in provs.items():
        entry = entry if isinstance(entry, dict) else {}
        options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
        npm = entry.get("npm")
        out.append({
            "native_id": pid,
            "name": entry.get("name"),
            "base_url": options.get("baseURL"),
            "api_key": options.get("apiKey"),
            "model": active_model if pid == active else "",
            "small_model": "",
            "extra": {"npm": npm} if isinstance(npm, str) and npm else {},
        })
    return out, active


def _openclaw_primary(data: dict) -> str:
    dm = ((data.get("agents") or {}).get("defaults") or {}).get("model") or {}
    primary = dm.get("primary") if isinstance(dm, dict) else None
    return primary if isinstance(primary, str) else ""


def _scan_openclaw(agent: dict) -> tuple[list[dict], str | None]:
    data = _read_json(_openclaw_path(agent))
    root = data.get("models") if isinstance(data.get("models"), dict) else {}
    provs = root.get("providers") if isinstance(root.get("providers"), dict) else {}
    primary = _openclaw_primary(data)
    active = primary.split("/", 1)[0] if "/" in primary else (primary or None)
    active_model = primary.split("/", 1)[1] if "/" in primary else ""
    out: list[dict] = []
    for pid, entry in provs.items():
        entry = entry if isinstance(entry, dict) else {}
        model = active_model if pid == active else _first_model(entry.get("models"))
        out.append({
            "native_id": pid,
            "name": pid,
            "base_url": entry.get("baseUrl"),
            "api_key": entry.get("apiKey"),
            "model": model,
            "small_model": "",
            "extra": {"openclaw_api": entry.get("api")} if entry.get("api") else {},
        })
    return out, active


_SCANNERS = {
    "claude-env": _scan_claude,
    "codex-toml": _scan_codex,
    "opencode-json": _scan_opencode,
    "pi-json": _scan_pi,
    "openclaw-json": _scan_openclaw,
}

_EXTRA_KEYS = ("auth_field", "wire_api", "requires_openai_auth", "npm", "pi_api", "openclaw_api")


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


def _auto_name(base_url: str) -> str:
    host = urllib.parse.urlsplit(base_url or "").netloc or base_url or "当前配置"
    return host[:_MAX_NAME]


# ---------------------------------------------------------------- 对外 API

def supported(agent: dict) -> bool:
    spec = agent.get("provider")
    return isinstance(spec, dict) and spec.get("adapter") in ADAPTERS


def detail(agent: dict) -> dict:
    if not supported(agent):
        return {"supported": False, "items": [], "current": None}
    data = _load()
    items = _items(data, agent["id"])
    # 打开面板即自动扫描：把工具原生配置里的全部供应商纳入管理（幂等）
    if _absorb_native(agent, data, items) is not None:
        _save(data)
    spec = agent.get("provider") or {}
    return {
        "supported": True,
        "mode": spec.get("mode") or "switch",
        "adapter": spec.get("adapter"),
        "items": [_public(item) for item in items],
        "current": _current(agent, items),
    }


def save(agent_id: str, payload: dict) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent):
        raise ValueError("该工具暂不支持供应商管理")
    provider_id = _clean(payload.get("id"), 64)
    name = _clean(payload.get("name"), _MAX_NAME)
    if not name:
        raise ValueError("供应商名称不能为空")

    extra = payload.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    if "auth_field" in extra and extra["auth_field"] not in (
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        raise ValueError("认证字段只能是 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY")
    if "wire_api" in extra and extra["wire_api"] not in ("responses", "chat"):
        raise ValueError("wire_api 只能是 responses 或 chat")

    base_url = _clean(payload.get("base_url"), _MAX_URL)
    if base_url and not re.match(r"^https?://", base_url):
        raise ValueError("接口地址必须以 http:// 或 https:// 开头")

    data = _load()
    items = _items(data, agent_id)
    item = _find(items, provider_id) if provider_id else None
    if item is None:
        item = {"id": uuid.uuid4().hex[:8], "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        items.append(item)
    item["name"] = name
    item["base_url"] = base_url
    item["model"] = _clean(payload.get("model"), _MAX_MODEL)
    item["small_model"] = _clean(payload.get("small_model"), _MAX_MODEL)
    item["extra"] = {k: v for k, v in extra.items() if k in _EXTRA_KEYS}
    api_key = payload.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        item["api_key"] = api_key.strip()[:_MAX_KEY]
    _save(data)
    return _public(item)


def delete(agent_id: str, provider_id: str) -> None:
    data = _load()
    items = _items(data, agent_id)
    item = _find(items, provider_id)
    if item is None:
        raise ValueError("供应商不存在")
    items.remove(item)
    if data["active"].get(agent_id) == provider_id:
        data["active"].pop(agent_id, None)
    _save(data)


def activate(agent_id: str, provider_id: str) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent):
        raise ValueError("该工具暂不支持供应商管理")
    data = _load()
    items = _items(data, agent_id)
    item = _find(items, provider_id)
    if item is None:
        raise ValueError("供应商不存在")
    if not item.get("base_url") and not item.get("model"):
        raise ValueError("请先填写接口地址或模型")
    adapter = ADAPTERS[(agent.get("provider") or {}).get("adapter")]
    written = adapter(agent, item)
    data["active"][agent_id] = provider_id
    _save(data)
    return {"ok": True, "written": written, "current": provider_id}


def _absorb_native(agent: dict, data: dict, items: list[dict]) -> dict | None:
    """扫描工具原生配置里的全部供应商并合并进 items，标记使用中的那条。

    返回当前「使用中」的 item（可能已修改），无任何可识别供应商时返回 None。
    调用方负责持久化（_save）。
    """
    scanner = _SCANNERS.get((agent.get("provider") or {}).get("adapter"))
    if scanner is None:
        return None
    infos, active_native = scanner(agent)

    active_item = None
    last_item = None
    for info in infos:
        base_url = _clean(info.get("base_url"), _MAX_URL)
        api_key = info.get("api_key")
        api_key = api_key.strip()[:_MAX_KEY] if isinstance(api_key, str) and api_key.strip() else ""
        model = _clean(info.get("model"), _MAX_MODEL)
        if not any((base_url, api_key, model)):
            continue
        native_id = info.get("native_id")

        item = None
        if native_id:
            item = next((i for i in items if i.get("native_id") == native_id), None)
        if item is None and base_url:
            item = next((i for i in items
                         if (i.get("base_url") or "") == base_url and not i.get("native_id")), None)
        if item is None:
            item = {"id": uuid.uuid4().hex[:8], "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            items.append(item)
            item["name"] = _clean(info.get("name"), _MAX_NAME) or _auto_name(base_url)
            item["base_url"] = base_url
            item["model"] = model
            item["small_model"] = _clean(info.get("small_model"), _MAX_MODEL)
            item["extra"] = {k: v for k, v in (info.get("extra") or {}).items() if k in _EXTRA_KEYS}
            if api_key:
                item["api_key"] = api_key
            if native_id:
                item["native_id"] = native_id
        elif native_id and item.get("native_id") != native_id:
            # 认领：给已有条目补上原生 id，但绝不覆盖用户已编辑的字段
            item["native_id"] = native_id
        last_item = item
        if native_id and native_id == active_native:
            active_item = item

    if active_item is None:
        active_item = last_item
    if active_item is not None:
        data["active"][agent["id"]] = active_item["id"]
    return active_item


def import_current(agent_id: str) -> dict:
    """扫描该工具原生配置里的全部供应商，合并入库并返回「使用中」的那条。"""
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent):
        raise ValueError("该工具暂不支持供应商管理")
    data = _load()
    items = _items(data, agent_id)
    item = _absorb_native(agent, data, items)
    if item is None:
        raise ValueError("当前配置里没有可导入的接口地址 / 密钥 / 模型")
    _save(data)
    return _public(item)


def merge_entries(agent_id: str, entries: list[dict]) -> int:
    """把外部来源（如 cc-switch）的条目按名称去重合并进存储，返回新增数量。"""
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent) or not isinstance(entries, list):
        return 0
    data = _load()
    items = _items(data, agent_id)
    added = 0
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        base_url = _clean(raw.get("base_url"), _MAX_URL)
        model = _clean(raw.get("model"), _MAX_MODEL)
        if not base_url and not model:
            continue
        name = _clean(raw.get("name"), _MAX_NAME) or _auto_name(base_url)
        item = next((i for i in items if i.get("name") == name), None)
        if item is None:
            item = {"id": uuid.uuid4().hex[:8], "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            items.append(item)
            added += 1
        item["name"] = name
        item["base_url"] = base_url
        item["model"] = model
        item["small_model"] = _clean(raw.get("small_model"), _MAX_MODEL)
        item["extra"] = {k: v for k, v in (raw.get("extra") or {}).items() if k in _EXTRA_KEYS}
        if raw.get("native_id"):
            item["native_id"] = raw["native_id"]
        key = raw.get("api_key")
        if isinstance(key, str) and key.strip():
            item["api_key"] = key.strip()[:_MAX_KEY]
    _save(data)
    return added


def reveal(agent_id: str, provider_id: str) -> str:
    """返回某供应商的明文密钥，仅供本机面板「显示 / 复制」用（服务只监听回环地址）。"""
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent):
        raise ValueError("该工具暂不支持供应商管理")
    item = _find(_items(_load(), agent_id), provider_id)
    if item is None:
        raise ValueError("供应商不存在")
    return item.get("api_key") or ""


def _models_request_target(agent: dict, base_url: str, key: object) -> tuple[str, dict]:
    """按工具类型拼出模型列表请求的 URL 与鉴权头（Claude 用 x-api-key，其余用 Bearer）。"""
    adapter = (agent.get("provider") or {}).get("adapter")
    path = "/v1/models" if adapter == "claude-env" else "/models"
    url = base_url.rstrip("/") + path
    headers: dict = {}
    if isinstance(key, str) and key:
        if adapter == "claude-env":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    return url, headers


def _model_ids(obj: object) -> list[str]:
    """从各家 /models 响应里抽出模型 id，兼容 OpenAI / Gemini / 裸数组等形状。"""
    if isinstance(obj, dict):
        for key in ("data", "models"):
            if key in obj:
                return _model_ids(obj[key])
        return []
    if isinstance(obj, list):
        out: list[str] = []
        for it in obj:
            if isinstance(it, str) and it:
                out.append(it)
            elif isinstance(it, dict):
                v = it.get("id") or it.get("name") or it.get("model")
                if isinstance(v, str) and v:
                    out.append(v)
        return out
    return []


def fetch_models(agent_id: str, provider_id: str | None = None,
                 base_url: str = "", api_key: str | None = None) -> dict:
    """拉取供应商的模型列表（GET /models）。已存在的供应商可复用库内密钥。"""
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent):
        raise ValueError("该工具暂不支持供应商管理")
    base_url = (base_url or "").strip()
    key = api_key if isinstance(api_key, str) and api_key.strip() else None
    if key is None and provider_id:
        item = _find(_items(_load(), agent_id), provider_id)
        if item is not None:
            key = item.get("api_key")
            if not base_url:
                base_url = item.get("base_url") or ""
    if not base_url:
        raise ValueError("请先填写接口地址")

    url, headers = _models_request_target(agent, base_url, key)
    req = urllib.request.Request(url, method="GET", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return {"models": [], "detail": f"HTTP {e.code}", "url": url}
    except urllib.error.URLError as e:
        return {"models": [], "detail": f"无法连接：{e.reason}", "url": url}
    except Exception as e:  # noqa: BLE001
        return {"models": [], "detail": str(e), "url": url}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"models": [], "detail": "响应不是 JSON", "url": url}
    models = list(dict.fromkeys(_model_ids(data)))[:_MAX_MODELS]
    return {"models": models, "detail": f"{len(models)} 个模型", "url": url}


def probe(agent_id: str, provider_id: str) -> dict:
    """对供应商接口做一次轻量连通性检查（GET 模型列表），不写任何文件。"""
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent):
        raise ValueError("该工具暂不支持供应商管理")
    item = _find(_items(_load(), agent_id), provider_id)
    if item is None:
        raise ValueError("供应商不存在")
    base_url = item.get("base_url") or ""
    if not base_url:
        raise ValueError("该供应商未填写接口地址，无法测试")

    url, headers = _models_request_target(agent, base_url, item.get("api_key"))
    req = urllib.request.Request(url, method="GET", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            return {"ok": 200 <= resp.status < 300, "status": resp.status,
                    "detail": f"HTTP {resp.status}", "url": url}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "detail": f"HTTP {e.code}", "url": url}
    except urllib.error.URLError as e:
        return {"ok": False, "status": None, "detail": f"无法连接：{e.reason}", "url": url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "detail": str(e), "url": url}


def export_payload(agent_id: str) -> dict | None:
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent):
        return None
    data = _load()
    items = _items(data, agent_id)
    if not items:
        return None
    return {
        "mode": (agent.get("provider") or {}).get("mode") or "switch",
        "current": data["active"].get(agent_id) or _current(agent, items),
        "items": [dict(item) for item in items],
    }


def import_payload(agent_id: str, payload: dict) -> int:
    """把包里的供应商合并进本机存储（同名去重），返回新增数量。"""
    agent = registry.get_agent(agent_id)
    if not agent or not supported(agent) or not isinstance(payload, dict):
        return 0
    incoming = payload.get("items")
    if not isinstance(incoming, list):
        return 0
    data = _load()
    items = _items(data, agent_id)
    added = 0
    for raw in incoming:
        if not isinstance(raw, dict) or not _clean(raw.get("name"), _MAX_NAME):
            continue
        name = _clean(raw.get("name"), _MAX_NAME)
        existing = next((i for i in items if i.get("name") == name), None)
        target = existing if existing is not None else {
            "id": uuid.uuid4().hex[:8], "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        target["name"] = name
        target["base_url"] = _clean(raw.get("base_url"), _MAX_URL)
        target["model"] = _clean(raw.get("model"), _MAX_MODEL)
        target["small_model"] = _clean(raw.get("small_model"), _MAX_MODEL)
        target["extra"] = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        if isinstance(raw.get("api_key"), str) and raw["api_key"].strip():
            target["api_key"] = raw["api_key"].strip()[:_MAX_KEY]
        if existing is None:
            items.append(target)
            added += 1
    _save(data)
    return added
