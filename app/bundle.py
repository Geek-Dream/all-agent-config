""".aac 配置包导出 / 导入。

包结构（zip）：
  manifest.json                 格式版本、工具信息、各类别条目与目标路径、补丁文件哈希
  <category>/...                各分类文件（相互独立，导入时可逐个开关）
  patches/...                   程序级修改文件（保持包内相对路径）

设计原则：纯文件操作，不依赖任何 AI；导入时每个分类独立应用，互不影响。
"""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path

from . import categories, registry

HOME = Path.home()
EXPORTS_DIR = HOME / ".aac" / "exports"
IMPORTS_DIR = HOME / ".aac" / "imports"
FORMAT_VERSION = 1


def _rel_home(p: Path) -> str | None:
    """返回相对 home 的路径（在 home 下才返回，否则 None）。"""
    try:
        return str(p.resolve().relative_to(HOME))
    except ValueError:
        return None


def _arc_name(cat: str, idx: int, name: str) -> str:
    safe = name.replace("\\", "/").strip("/") or "file"
    return f"{cat}/{idx:04d}_{Path(safe).name}" if cat != "patches" else f"patches/{safe}"


def export_bundle(agent_id: str, selected: list[str]) -> Path:
    """把选中的分类打包成 .aac，返回文件路径。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise ValueError(f"unknown agent: {agent_id}")
    detail = categories.agent_detail(agent)
    cats = detail["categories"]
    patches = detail["patches"] or {}

    manifest: dict = {
        "format": FORMAT_VERSION,
        "agent_id": agent_id,
        "agent_name": agent["name"],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_version": detail.get("agent_version"),
        "categories": {},
    }

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORTS_DIR / f"{agent_id}-{time.strftime('%Y%m%d-%H%M%S')}.aac"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for cat in selected:
            if cat == "patches":
                entries = []
                for rel in patches.get("modified", []) + patches.get("added", []):
                    src = categories.package_dir(agent) / rel
                    if not src.is_file():
                        continue
                    arc = _arc_name("patches", 0, rel)
                    zf.write(src, arc)
                    entries.append({"arc": arc, "rel": rel,
                                    "sha256": categories._sha256(src)})
                if entries:
                    manifest["categories"]["patches"] = {
                        "pkg": patches.get("pkg"),
                        "pkg_version": patches.get("pkg_version"),
                        "entries": entries,
                    }
                continue

            info = cats.get(cat)
            if not info or not info["files"]:
                continue
            entries = []
            for i, f in enumerate(info["files"]):
                src = Path(f["path"])
                if not src.is_file():
                    continue
                if f.get("json_keys"):
                    try:
                        data = json.loads(src.read_text())
                        payload = {k: data[k] for k in f["json_keys"] if k in data}
                    except Exception:
                        continue
                    arc = f"{cat}/{i:04d}_extracted.json"
                    zf.writestr(arc, json.dumps(payload, ensure_ascii=False, indent=2))
                    entries.append({"arc": arc, "target": str(src),
                                    "rel_home": _rel_home(src),
                                    "json_keys": f["json_keys"]})
                else:
                    arc = _arc_name(cat, i, _rel_home(src) or src.name)
                    zf.write(src, arc)
                    entries.append({"arc": arc, "target": str(src),
                                    "rel_home": _rel_home(src)})
            if entries:
                manifest["categories"][cat] = {"entries": entries}

        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return out


def import_preview(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    warnings = []
    p = manifest["categories"].get("patches")
    if p:
        agent = registry.get_agent(manifest["agent_id"]) or {}
        pdir = categories.package_dir(agent)
        cur = categories.installed_pkg_version(pdir) if pdir else None
        if cur and cur != p.get("pkg_version"):
            warnings.append(
                f"补丁基于版本 {p.get('pkg_version')} 制作，本机安装的是 {cur}，"
                "应用后可能不兼容，建议先升级到相同版本。")
        if pdir is None:
            warnings.append("本机未找到该工具的安装目录，无法应用程序补丁。")
    return {"manifest": manifest, "warnings": warnings}


def _resolve_target(entry: dict) -> Path | None:
    if entry.get("rel_home"):
        return HOME / entry["rel_home"]
    return Path(entry["target"])  # 非 home 内的路径按原样使用


def _backup(p: Path) -> None:
    if p.is_file():
        bak = p.with_name(p.name + time.strftime(".bak-aac-%Y%m%d-%H%M%S"))
        shutil.copy2(p, bak)


def import_apply(path: Path, selected: list[str], log: list[str]) -> None:
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        applied = []
        for cat in selected:
            info = manifest["categories"].get(cat)
            if not info:
                log.append(f"[跳过] 分类 {cat} 不在包中\n")
                continue
            if cat == "patches":
                agent = registry.get_agent(manifest["agent_id"]) or {}
                pdir = categories.package_dir(agent)
                if not pdir:
                    log.append(f"[失败] patches：本机找不到安装目录\n")
                    continue
                for e in info["entries"]:
                    target = pdir / e["rel"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _backup(target)
                    target.write_bytes(zf.read(e["arc"]))
                log.append(f"[完成] patches：应用 {len(info['entries'])} 个补丁文件\n")
                applied.append(cat)
                continue
            for e in info["entries"]:
                target = _resolve_target(e)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if e.get("json_keys"):
                    payload = json.loads(zf.read(e["arc"]))
                    existing: dict = {}
                    if target.is_file():
                        try:
                            existing = json.loads(target.read_text())
                        except Exception:
                            _backup(target)
                    merged = dict(existing) if isinstance(existing, dict) else {}
                    merged.update(payload)
                    if target.is_file():
                        _backup(target)
                    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
                else:
                    _backup(target)
                    target.write_bytes(zf.read(e["arc"]))
            log.append(f"[完成] {cat}：应用 {len(info['entries'])} 个文件\n")
            applied.append(cat)
        log.append(f"导入结束，共应用 {len(applied)} 个分类：{', '.join(applied)}\n")
