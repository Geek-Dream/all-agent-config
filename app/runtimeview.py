"""只读运行配置：展示模型、Provider 和认证状态，绝不返回密钥内容。"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .categories import expand


def _path_label(path: Path) -> str:
    try:
        return str(path).replace(str(Path.home()), "~")
    except Exception:
        return str(path)


def _item(
    key: str,
    name: str,
    *,
    desc: str = "",
    meta: str = "",
    enabled: bool = True,
) -> dict:
    return {
        "id": f"runtime|{key}",
        "name": name,
        "desc": desc,
        "meta": meta,
        "enabled": enabled,
        "toggleable": False,
    }


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _first_json(paths: list[str]) -> tuple[Path | None, dict | None]:
    for raw in paths:
        path = expand(raw)
        data = _load_json(path)
        if data is not None:
            return path, data
    return None, None


def _safe_endpoint(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            return "自定义接口地址"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    except ValueError:
        return "自定义接口地址"


def _option(options: dict, *names: str):
    for name in names:
        if name in options:
            return options[name]
    return None


def _key_status(options: dict) -> str:
    key = _option(options, "apiKey", "api_key")
    if key is None:
        env_name = _option(options, "apiKeyEnv", "api_key_env")
        if isinstance(env_name, str) and env_name:
            return f"API Key 来自环境变量 {env_name}（值已隐藏）"
        return "API Key 未在配置文件中声明"
    if not isinstance(key, str) or not key.strip():
        return "API Key 未配置"
    if key.startswith("{env:") and key.endswith("}"):
        name = key[5:-1].strip()
        return f"API Key 来自环境变量 {name}（值已隐藏）" if name else "API Key 使用环境变量（值已隐藏）"
    return "API Key 已配置（值已隐藏）"


def _credential_type(value) -> str:
    if isinstance(value, dict):
        kind = value.get("type") or value.get("authType")
        if isinstance(kind, str) and kind:
            if kind.lower() == "api":
                return "API"
            return kind.upper() if kind.lower() == "oauth" else kind
    return "API"


def _opencode_items(agent: dict) -> list[dict]:
    runtime = agent.get("runtime") or {}
    config_path, config = _first_json(runtime.get("config_paths") or [])
    auth_path, auth = _first_json(runtime.get("auth_paths") or [])

    items: list[dict] = []
    if config is None:
        items.append(_item(
            "opencode-config",
            "OpenCode 配置",
            meta="未找到可读取的 opencode.json",
            enabled=False,
        ))
    else:
        model = config.get("model")
        model = model if isinstance(model, str) and model.strip() else ""
        items.append(_item(
            "opencode-model",
            model or "默认模型",
            desc=f"默认模型 · {_path_label(config_path) if config_path else 'OpenCode 配置'}",
            meta="" if model else "未设置默认模型",
            enabled=bool(model),
        ))

        providers = config.get("provider")
        if not isinstance(providers, dict):
            providers = {}
        current_provider = model.split("/", 1)[0] if "/" in model else ""
        for name, provider in sorted(providers.items()):
            if not isinstance(provider, dict):
                continue
            options = provider.get("options")
            options = options if isinstance(options, dict) else {}
            endpoint = _safe_endpoint(_option(
                options, "baseURL", "baseUrl", "base_url", "url"))
            models = provider.get("models")
            model_names = sorted(models) if isinstance(models, dict) else []
            meta = [value for value in (
                endpoint,
                f"{len(model_names)} 个模型: {'、'.join(model_names)}" if model_names else "",
                _key_status(options),
            ) if value]
            items.append(_item(
                f"opencode-provider-{name}",
                str(name),
                desc="Provider · 当前使用" if str(name) == current_provider else "Provider",
                meta=" · ".join(meta),
            ))

    if auth is None:
        items.append(_item(
            "opencode-auth",
            "API 凭据",
            meta="未检测到 auth.json",
            enabled=False,
        ))
    elif not auth:
        items.append(_item(
            "opencode-auth",
            "API 凭据",
            meta="已检测到认证缓存，但没有凭据",
            enabled=False,
        ))
    else:
        for name, value in sorted(auth.items()):
            items.append(_item(
                f"opencode-credential-{name}",
                str(name),
                desc=f"{_credential_type(value)} 凭据 · 值已隐藏",
                meta=_path_label(auth_path) if auth_path else "认证缓存",
            ))
    return items


def items_for_agent(agent: dict) -> list[dict]:
    runtime = agent.get("runtime")
    if not isinstance(runtime, dict):
        return []
    if runtime.get("type") == "opencode":
        return _opencode_items(agent)
    return []
