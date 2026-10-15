"""配置详情与开关：枚举 agent 的 Skills / 扩展 / MCP / Hooks / 配置文件。

开关实现约定：
- 文件 / 目录类：重命名为 <原名>.aacoff 即视为关闭（agent 看不到），改回即开启
- JSON 键类（MCP / Hooks）：把条目从原容器移动到同文件的 "aacDisabled" 命名空间，
  agent 读不到未知键，配置内容不丢失；写回前自动留一份 .aac-bak 备份
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .categories import expand

OFF_SUFFIX = ".aacoff"
DISABLED_NS = "aacDisabled"

# 核心配置：只展示，不提供开关（关掉会导致工具无法正常工作）
CORE_FILES = {"settings.json", "config.toml", "opencode.json",
              "models.json", "keybindings.json", "ide.json"}


# custom 里这些名字的目录视为"条目集合"（agents/commands/extensions/prompts/plugins），
# 子项逐个给开关；其余目录（如下载的整个市场仓库）整体展示，不给开关
CONTAINER_DIR_NAMES = {"agent", "agents", "commands", "extensions", "prompts", "plugins"}


def _entry_path(e) -> Path:
    return expand(e if isinstance(e, str) else e["path"])


def _off_variant(p: Path) -> Path:
    return p.with_name(p.name + OFF_SUFFIX)


def _fmt_size(n: float) -> str:
    u = ["B", "KB", "MB", "GB"]
    i = 0
    while n >= 1024 and i < len(u) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {u[i]}" if i == 0 else f"{n:.1f} {u[i]}"


def _skill_desc(d: Path) -> str:
    """从 SKILL.md front matter 里提取 description。"""
    f = d / "SKILL.md"
    if not f.is_file():
        return ""
    try:
        text = f.read_text(errors="ignore")[:4000]
    except Exception:
        return ""
    if text.startswith("---"):
        end = text.find("---", 3)
        head = text[3:end if end > 0 else len(text)]
        for line in head.splitlines():
            if line.strip().startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                return desc[:120]
    return ""


def _fs_children(d: Path, with_desc: bool) -> list[dict]:
    """目录下的子项（含被关闭的 *.aacoff），每个子项一个开关。"""
    items: list[dict] = []
    if not d.is_dir():
        return items
    for child in sorted(d.iterdir(), key=lambda p: p.name):
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        enabled = not child.name.endswith(OFF_SUFFIX)
        base = child if enabled else d / child.name[: -len(OFF_SUFFIX)]
        meta, desc = "", ""
        if child.is_dir():
            try:
                n = sum(1 for _ in child.iterdir())
                meta = f"目录 · {n} 项"
            except Exception:
                meta = "目录"
            if with_desc:
                desc = _skill_desc(child)
        else:
            try:
                meta = _fmt_size(child.stat().st_size)
            except Exception:
                pass
        items.append({
            "id": f"fs|{base}",
            "name": base.name,
            "desc": desc,
            "meta": meta,
            "enabled": enabled,
            "toggleable": True,
        })
    return items


def _json_items(agent: dict, cat_key: str) -> list[dict]:
    """JSON 键类配置项（MCP 服务器 / Hooks 事件），含已关闭（aacDisabled 下）的。"""
    items: list[dict] = []
    for e in (agent.get("categories") or {}).get(cat_key) or []:
        if isinstance(e, str):
            continue
        keys = e.get("json_keys") or []
        f = expand(e["path"])
        if not f.is_file() or f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        for c in [k for k in keys if isinstance(data.get(k), dict)]:
            on = data.get(c) or {}
            off = (data.get(DISABLED_NS) or {}).get(c) or {}
            for name, val in sorted(on.items()):
                items.append(_json_item(f, c, name, val, True))
            for name, val in sorted(off.items()):
                items.append(_json_item(f, c, name, val, False))
    return items


def _toml_items(agent: dict, cat_key: str) -> list[dict]:
    """TOML 键类配置项（如 Codex config.toml 里的 mcp_servers）。

    只展示、不给开关：TOML 没有官方写库，改写会丢注释和格式，风险大于收益。
    """
    import tomllib

    items: list[dict] = []
    for e in (agent.get("categories") or {}).get(cat_key) or []:
        if isinstance(e, str):
            continue
        keys = e.get("toml_keys") or []
        f = expand(e["path"])
        if not f.is_file() or f.suffix != ".toml":
            continue
        try:
            data = tomllib.loads(f.read_text())
        except Exception:
            continue
        for c in [k for k in keys if isinstance(data.get(k), dict)]:
            for name, val in sorted((data.get(c) or {}).items()):
                it = _json_item(f, c, name, val, True)
                it["id"] = f"toml|{f}|{c}|{name}"
                it["toggleable"] = False
                it["meta"] = (it["meta"] + " · 通用配置" if it["meta"] else "通用配置")
                items.append(it)
    return items


def _json_item(f: Path, container: str, name: str, val, enabled: bool) -> dict:
    meta = ""
    if isinstance(val, dict):
        meta = str(val.get("command") or val.get("url") or val.get("type") or "")
        args = val.get("args")
        if meta and isinstance(args, list) and args:
            meta += " " + " ".join(str(a) for a in args[:3])
        if not meta:
            meta = f"{len(val)} 个键"
    elif isinstance(val, list):
        meta = f"{len(val)} 条规则"
    return {"id": f"json|{f}|{container}|{name}", "name": name,
            "desc": "", "meta": meta[:120], "enabled": enabled, "toggleable": True}


def _file_items(agent: dict) -> list[dict]:
    """单个配置文件（system / custom / theme 中非目录条目），核心文件不给开关。"""
    seen: set[str] = set()
    items: list[dict] = []
    cats = agent.get("categories") or {}
    for cat in ("system", "custom", "theme"):
        for e in cats.get(cat) or []:
            p = _entry_path(e)
            if p.is_dir() or _off_variant(p).is_dir():
                continue  # 目录归扩展组
            if str(p) in seen:
                continue
            seen.add(str(p))
            exists = p.is_file()
            off = _off_variant(p)
            disabled_exists = off.is_file()
            if not exists and not disabled_exists:
                meta, enabled, toggleable = "未创建", False, False
            else:
                enabled = exists
                toggleable = p.name not in CORE_FILES
                try:
                    meta = _fmt_size((p if exists else off).stat().st_size)
                except Exception:
                    meta = ""
            items.append({
                "id": f"fs|{p}",
                "name": p.name,
                "desc": str(p).replace(str(Path.home()), "~"),
                "meta": meta + ("" if toggleable or not exists else " · 核心配置"),
                "enabled": enabled,
                "toggleable": toggleable,
            })
    return items


def list_config(agent: dict) -> dict:
    cats = agent.get("categories") or {}

    skills: list[dict] = []
    for e in cats.get("skills") or []:
        skills += _fs_children(_entry_path(e), with_desc=True)

    exts: list[dict] = []
    extra_dirs: list[dict] = []
    for e in cats.get("custom") or []:
        p = _entry_path(e)
        d = p if p.is_dir() else _off_variant(p)
        if not d.is_dir():
            continue
        base = d.name[:-len(OFF_SUFFIX)] if d.name.endswith(OFF_SUFFIX) else d.name
        if base in CONTAINER_DIR_NAMES:
            exts += _fs_children(d, with_desc=False)
        else:
            # 非条目集合目录（如克隆下来的扩展市场仓库）：整体展示，不给开关
            try:
                n = sum(1 for _ in d.iterdir())
            except Exception:
                n = 0
            extra_dirs.append({
                "id": f"fs|{p}",
                "name": d.name,
                "desc": str(p).replace(str(Path.home()), "~"),
                "meta": f"目录 · {n} 项 · 整体配置",
                "enabled": d is p,
                "toggleable": False,
            })

    return {
        "agent": {"id": agent["id"], "name": agent["name"]},
        "groups": [
            {"key": "skills", "label": "Skills 技能", "items": skills},
            {"key": "ext", "label": "扩展 / 命令 / 子代理", "items": exts},
            {"key": "mcp", "label": "MCP 服务器",
             "items": _json_items(agent, "mcp") + _toml_items(agent, "mcp")},
            {"key": "hooks", "label": "Hooks 钩子", "items": _json_items(agent, "hooks")},
            {"key": "files", "label": "配置文件", "items": _file_items(agent) + extra_dirs},
        ],
    }


def _check_safe(p: Path) -> None:
    rp = p.resolve() if p.exists() else Path(str(p))
    try:
        rp.relative_to(Path.home())
    except ValueError:
        raise PermissionError(f"仅允许操作用户目录下的文件: {p}")


def toggle(item_id: str, enabled: bool) -> str:
    kind, *rest = item_id.split("|")

    if kind == "fs":
        p = Path(rest[0])
        _check_safe(p)
        off = _off_variant(p)
        if enabled:
            if off.exists():
                off.rename(p)
                return f"已开启 {p.name}"
            return "已是开启状态"
        if p.exists():
            p.rename(off)
            return f"已关闭 {p.name}"
        return "已是关闭状态"

    if kind == "json":
        f = Path(rest[0])
        container, name = rest[1], rest[2]
        _check_safe(f)
        if not f.is_file():
            raise FileNotFoundError(str(f))
        data = json.loads(f.read_text())
        on = data.setdefault(container, {})
        off = data.setdefault(DISABLED_NS, {}).setdefault(container, {})
        changed = False
        if enabled and name in off:
            on[name] = off.pop(name)
            changed = True
        elif not enabled and name in on:
            off[name] = on.pop(name)
            changed = True
        if changed:
            if not any(data.get(DISABLED_NS, {}).values()):
                data.pop(DISABLED_NS, None)
            try:
                shutil.copy2(f, f.with_name(f.name + ".aac-bak"))
                f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            except PermissionError:
                raise PermissionError(
                    f"没有写入权限：{f}（文件可能属 root 所有，可在终端执行 "
                    f"sudo chown $(id -un) {f} 修复）")
        return f"已{'开启' if enabled else '关闭'} {name}"

    raise ValueError(f"unknown item kind: {kind}")
