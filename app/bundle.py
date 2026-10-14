""".aac 配置包导出 / 导入。

包结构（zip）：
  manifest.json                 格式版本、工具信息、各类别条目与目标路径、补丁文件哈希
  <category>/...                各分类文件（相互独立，导入时可逐个开关）
  patches/...                   程序级修改文件（保持包内相对路径）

设计原则：纯文件操作，不依赖任何 AI；导入时每个分类独立应用，互不影响。

跨平台（Mac ↔ Win）：
  - rel_home 一律用 posix 形式（正斜杠）存储，导入端按本机分隔符还原；
  - manifest 记录 source_os / source_home，导入时把配置文件内容里的源机器
    home 绝对路径（含 JSON 转义形式）替换为本机 home；
  - 来自另一种系统的绝对路径无法映射时跳过并告警，绝不写出垃圾路径。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

from . import categories, registry

HOME = Path.home()
EXPORTS_DIR = HOME / ".aac" / "exports"
IMPORTS_DIR = HOME / ".aac" / "imports"
FORMAT_VERSION = 2
IS_WINDOWS = os.name == "nt"

_OS_NAMES = {"darwin": "macOS", "win32": "Windows", "cygwin": "Windows"}


def _os_name() -> str:
    return _OS_NAMES.get(sys.platform, sys.platform)


def _rel_home(p: Path) -> str | None:
    """返回相对 home 的 posix 路径（正斜杠，跨平台通用；在 home 下才返回，否则 None）。"""
    try:
        return p.resolve().relative_to(HOME).as_posix()
    except ValueError:
        return None


def _arc_name(cat: str, idx: int, name: str) -> str:
    safe = name.replace("\\", "/").strip("/") or "file"
    return f"{cat}/{idx:04d}_{Path(safe).name}" if cat != "patches" else f"patches/{safe}"


def export_bundle(agent_id: str, selected: list[str], conv_days: int | None = None) -> Path:
    """把选中的分类打包成 .aac，返回文件路径。

    conv_days：只打包最近 N 天内修改过的会话文件（None = 全部）。
    """
    agent = registry.get_agent(agent_id)
    if not agent:
        raise ValueError(f"unknown agent: {agent_id}")
    detail = categories.agent_detail(agent)
    cats = detail["categories"]
    patches = detail["patches"] or {}
    conv_cutoff = (time.time() - conv_days * 86400) if conv_days else None

    manifest: dict = {
        "format": FORMAT_VERSION,
        "agent_id": agent_id,
        "agent_name": agent["name"],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_version": detail.get("agent_version"),
        "source_os": _os_name(),            # 导出端系统（macOS / Windows / linux）
        "source_home": str(HOME),           # 导出端 home 绝对路径，导入时用于内容路径映射
        "conv_days": conv_days,             # 会话时间范围筛选（None = 全部）
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
                if cat == "conversations" and conv_cutoff and f.get("mtime", 0) < conv_cutoff:
                    continue   # 时间范围之外的会话不打包
                if f["size"] > categories.MAX_FILE_SIZE:
                    # 超大文件（如数百 MB 的会话日志）不进包，记录在清单里
                    manifest.setdefault("skipped", []).append(
                        {"path": str(src), "size": f["size"], "reason": "超过 100MB"})
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
    for s in manifest.get("skipped") or []:
        warnings.append(f"未打包：{s['path']}（{s['size'] // 1024 // 1024}MB，{s['reason']}）")
    src_os = manifest.get("source_os")
    if src_os and src_os != _os_name():
        warnings.append(
            f"配置包来自 {src_os}，本机是 {_os_name()}：文件会自动落到本机用户目录，"
            "配置内容里的绝对路径也会自动映射为本机路径。")
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
    # ---- 冲突检测：逐分类统计 将覆盖（含合并）/ 新增 / 跳过 ----
    src_home = manifest.get("source_home")
    conflicts: dict[str, dict] = {}
    for cat, info in manifest["categories"].items():
        if cat == "patches":
            agent = registry.get_agent(manifest["agent_id"]) or {}
            pdir = categories.package_dir(agent)
            ow = sum(1 for e in info["entries"]
                     if pdir and (pdir.joinpath(*e["rel"].replace("\\", "/").split("/"))).is_file())
            conflicts[cat] = {"overwrite": ow, "new": len(info["entries"]) - ow,
                              "skip": 0 if pdir else len(info["entries"])}
            continue
        ow = nw = sk = 0
        for e in info["entries"]:
            if e.get("rel_home"):
                e = dict(e, rel_home=_map_rel_home(manifest["agent_id"], e["rel_home"], src_home))
            target = _resolve_target(e)
            if target is None:
                sk += 1
            elif target.is_file():
                ow += 1     # 覆盖（json_keys 为合并）；导入前都会先备份 .bak-aac-*
            else:
                nw += 1
        conflicts[cat] = {"overwrite": ow, "new": nw, "skip": sk}
    return {"manifest": manifest, "warnings": warnings, "conflicts": conflicts}


_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _resolve_target(entry: dict) -> Path | None:
    """解析导入目标路径。rel_home（posix）按本机 home 还原；
    非 home 内的绝对路径若来自另一种系统则返回 None（跳过，不写垃圾路径）。"""
    if entry.get("rel_home"):
        return HOME.joinpath(*entry["rel_home"].split("/"))
    target = entry.get("target") or ""
    if IS_WINDOWS and (target.startswith("/") or target.startswith("~")):
        return None
    if not IS_WINDOWS and _WIN_ABS_RE.match(target):
        return None
    return Path(target)


def _claude_enc(p: str) -> str:
    """Claude Code 的项目目录编码：/Users/wl/dev → -Users-wl-dev。"""
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def _map_rel_home(agent_id: str, rel_home: str, src_home: str | None) -> str:
    """跨机导入时改写 rel_home 里的机器相关片段。

    目前处理 Claude Code：会话按 项目路径编码 的目录存放
    （.claude/projects/<编码后的项目路径>/xxx.jsonl），换机后把
    源 home 编码前缀换成本机编码，同名校出的项目就能接上历史。
    """
    if agent_id != "claude-code" or not src_home or src_home == str(HOME):
        return rel_home
    parts = rel_home.split("/")
    if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "projects":
        enc_src = _claude_enc(src_home)
        if parts[2].startswith(enc_src):
            parts[2] = _claude_enc(str(HOME)) + parts[2][len(enc_src):]
            return "/".join(parts)
    return rel_home


def _remap_text(text: str, src_home: str | None) -> str:
    """把文本里的源机器 home 绝对路径替换为本机 home（含 JSON 转义形式）。"""
    if not src_home or src_home == str(HOME):
        return text
    dst = str(HOME)
    for v in {src_home, src_home.replace("\\", "/"), src_home.replace("/", "\\")}:
        if v and v != dst:
            text = text.replace(v, dst)
    esc_src, esc_dst = src_home.replace("\\", "\\\\"), dst.replace("\\", "\\\\")
    if esc_src != src_home and esc_src != esc_dst:
        text = text.replace(esc_src, esc_dst)
    return text


_TEXT_EXTS = {".json", ".jsonl", ".toml", ".md", ".txt", ".yaml", ".yml", ".sh",
              ".js", ".mjs", ".py", ".xml", ".ini", ".cfg"}


def _backup(p: Path) -> None:
    if p.is_file():
        bak = p.with_name(p.name + time.strftime(".bak-aac-%Y%m%d-%H%M%S"))
        shutil.copy2(p, bak)


def import_apply(path: Path, selected: list[str], log: list[str]) -> None:
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        src_home = manifest.get("source_home")
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
                    target = pdir.joinpath(*e["rel"].replace("\\", "/").split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _backup(target)
                    data = zf.read(e["arc"])
                    if target.suffix.lower() in _TEXT_EXTS:
                        try:
                            data = _remap_text(data.decode("utf-8"), src_home).encode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    target.write_bytes(data)
                log.append(f"[完成] patches：应用 {len(info['entries'])} 个补丁文件\n")
                applied.append(cat)
                continue
            skipped = 0
            for e in info["entries"]:
                if e.get("rel_home"):
                    e = dict(e, rel_home=_map_rel_home(
                        manifest["agent_id"], e["rel_home"], src_home))
                target = _resolve_target(e)
                if target is None:
                    skipped += 1
                    log.append(f"[跳过] {e.get('target')} 是其他系统的绝对路径，无法映射到本机\n")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if e.get("json_keys"):
                    payload = json.loads(_remap_text(
                        zf.read(e["arc"]).decode("utf-8", "replace"), src_home))
                    existing: dict = {}
                    if target.is_file():
                        try:
                            existing = json.loads(target.read_text(encoding="utf-8"))
                        except Exception:
                            _backup(target)
                    merged = dict(existing) if isinstance(existing, dict) else {}
                    merged.update(payload)
                    if target.is_file():
                        _backup(target)
                    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
                else:
                    _backup(target)
                    data = zf.read(e["arc"])
                    if target.suffix.lower() in _TEXT_EXTS:
                        try:
                            data = _remap_text(data.decode("utf-8"), src_home).encode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    target.write_bytes(data)
            log.append(f"[完成] {cat}：应用 {len(info['entries']) - skipped} 个文件"
                       + (f"（跳过 {skipped} 个跨平台路径）" if skipped else "") + "\n")
            applied.append(cat)
        if src_home and src_home != str(HOME):
            log.append(f"已把配置内容中的路径 {src_home} 映射为 {HOME}\n")
        log.append(f"导入结束，共应用 {len(applied)} 个分类：{', '.join(applied)}\n")
