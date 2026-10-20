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
  - registry 的 platform_dirs 声明同一目录在两套系统下的位置
    （如 ~/.config/opencode ↔ %APPDATA%/opencode），导入时按目标系统改写；
  - 导出时可选 target_os，提前把包内相对路径写成目标系统格式；
  - 来自另一种系统的绝对路径无法映射时跳过并告警，绝不写出垃圾路径。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from . import archive, categories, providers, registry
from .paths import relative_target
from .safefs import ensure_private_dir

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
        return p.resolve().relative_to(HOME.resolve()).as_posix()
    except ValueError:
        return None


def _os_key(os_name: str | None) -> str:
    """把展示用系统名折叠成两套路径风格。"""
    return "windows" if os_name == "Windows" else "posix"


_WIN_HOME_VARS = {
    "APPDATA": "AppData/Roaming",
    "LOCALAPPDATA": "AppData/Local",
    "USERPROFILE": "",
}


def _decl_rel_home(raw: str) -> str | None:
    """把 registry 里的路径声明转成相对 home 的 posix 片段。

    ~/x → x、%APPDATA%/x → AppData/Roaming/x、%USERPROFILE%/x → x。
    字面盘符路径只去掉盘符（C:\\x\\y → x/y）；注册表应优先用 ~ 或 %VAR% 写法。
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().replace("\\", "/")
    s = re.sub(r"^%([^%]+)%", lambda m: _WIN_HOME_VARS.get(m.group(1).upper(), ""), s)
    s = re.sub(r"^[A-Za-z]:(?=/|$)", "", s)
    s = s.replace("~", "", 1) if s.startswith("~") else s
    s = s.lstrip("/")
    return s or None


def _platform_dirs(agent: dict, manifest: dict | None = None) -> list[dict]:
    dirs = agent.get("platform_dirs")
    if not dirs and manifest:
        dirs = manifest.get("platform_dirs")
    return [d for d in dirs or [] if isinstance(d, dict)]


def _map_platform_rel(dirs: list[dict], rel_home: str,
                      from_os: str | None, to_os: str | None) -> str:
    """把相对 home 的路径从 from_os 的目录布局换到 to_os 的布局。"""
    if not rel_home or not from_os or not to_os or from_os == to_os:
        return rel_home
    src_key, dst_key = _os_key(from_os), _os_key(to_os)
    for d in dirs:
        src = _decl_rel_home(d.get(src_key, ""))
        dst = _decl_rel_home(d.get(dst_key, ""))
        if not src or not dst:
            continue
        if rel_home == src or rel_home.startswith(src + "/"):
            return dst + rel_home[len(src):]
    return rel_home


def _content_path_pairs(agent: dict, manifest: dict) -> list[tuple[str, str]]:
    """源机器配置目录绝对路径 -> 本机等价绝对路径（用于替换文件正文）。"""
    src_home = manifest.get("source_home")
    from_os = manifest.get("source_os")
    to_os = _os_name()
    if not src_home or not from_os or from_os == to_os:
        return []
    src_sep = "\\" if from_os == "Windows" else "/"
    dst_sep = "\\" if to_os == "Windows" else "/"
    pairs: list[tuple[str, str]] = []
    for d in _platform_dirs(agent, manifest):
        src = _decl_rel_home(d.get(_os_key(from_os), ""))
        dst = _decl_rel_home(d.get(_os_key(to_os), ""))
        if src and dst:
            pairs.append((src_home.rstrip("/\\") + src_sep + src.replace("/", src_sep),
                          str(HOME).rstrip("/\\") + dst_sep + dst.replace("/", dst_sep)))
    return pairs


def _arc_name(cat: str, idx: int, name: str) -> str:
    safe = name.replace("\\", "/").strip("/") or "file"
    return f"{cat}/{idx:04d}_{Path(safe).name}" if cat != "patches" else f"patches/{safe}"


def _manifest_agent(manifest: dict) -> dict:
    agent_id = manifest.get("agent_id")
    agent = registry.get_agent(agent_id) if isinstance(agent_id, str) else None
    if not agent:
        raise ValueError(f"未知工具或缺少 agent_id: {agent_id!r}")
    return agent


def _registry_paths(agent: dict) -> list[str]:
    paths: list[str] = []
    for key in ("config_dirs", "session_dirs"):
        for value in agent.get(key) or []:
            if isinstance(value, str):
                paths.append(value)
    for entries in (agent.get("categories") or {}).values():
        for entry in entries or []:
            if isinstance(entry, str):
                paths.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
                paths.append(entry["path"])
    return paths


def allowed_import_roots(agent: dict) -> list[Path]:
    """Return resolved directories/files this registry entry may import into."""
    home = HOME.resolve()
    roots: set[Path] = set()
    for raw in _registry_paths(agent):
        try:
            candidate = categories.expand(raw).resolve()
            if candidate == home or not candidate.is_relative_to(home):
                continue
            roots.add(candidate)
        except (OSError, RuntimeError, ValueError):
            continue
    return sorted(roots, key=str)


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        info = zf.getinfo("manifest.json")
    except KeyError as exc:
        raise ValueError("缺少 manifest.json") from exc
    if info.file_size > 4 * 1024 * 1024:
        raise ValueError("manifest.json 过大")
    try:
        manifest = json.loads(zf.read(info))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest.json 不是有效 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 必须是对象")
    return manifest


def validate_aac(path: Path) -> dict:
    archive.inspect_zip(path, archive.AAC_LIMITS)
    with zipfile.ZipFile(path) as zf:
        manifest = _read_manifest(zf)
    _manifest_agent(manifest)
    return manifest


def export_bundle(agent_id: str, selected: list[str], conv_days: int | None = None,
                  target_os: str | None = None, password: str | None = None) -> Path:
    """把选中的分类打包成 .aac，返回文件路径。

    conv_days：只打包最近 N 天内修改过的会话文件（None = 全部）。
    target_os：可选，提前把包内相对路径写成目标系统的布局
               （macOS / Windows / linux，None = 跟随本机）。
    password：可选，提供则整包 AES-256-GCM 加密。
    """
    agent = registry.get_agent(agent_id)
    if not agent:
        raise ValueError(f"unknown agent: {agent_id}")
    detail = categories.agent_detail(agent)
    cats = detail["categories"]
    patches = detail["patches"] or {}
    conv_cutoff = (time.time() - conv_days * 86400) if conv_days else None
    local_os = _os_name()
    pdirs = agent.get("platform_dirs") or []

    def out_rel(src: Path) -> str | None:
        rel = _rel_home(src)
        return _map_platform_rel(pdirs, rel, local_os, target_os) if rel else None

    manifest: dict = {
        "format": FORMAT_VERSION,
        "agent_id": agent_id,
        "agent_name": agent["name"],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_version": detail.get("agent_version"),
        "source_os": local_os,              # 导出端系统（macOS / Windows / linux）
        "source_home": str(HOME),           # 导出端 home 绝对路径，导入时用于内容路径映射
        "path_platform": target_os or local_os,   # rel_home 使用的路径布局
        "platform_dirs": pdirs,             # 跨系统目录等价关系快照
        "conv_days": conv_days,             # 会话时间范围筛选（None = 全部）
        "categories": {},
    }

    ensure_private_dir(EXPORTS_DIR)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = EXPORTS_DIR / f"{agent_id}-{stamp}{'-encrypted' if password else ''}.aac"
    staging = out.with_name(out.name + ".part")

    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as zf:
        for cat in selected:
            if cat == "providers":
                payload = providers.export_payload(agent_id)
                if payload and payload.get("items"):
                    arc = f"providers/{agent_id}.json"
                    zf.writestr(arc, json.dumps(payload, ensure_ascii=False, indent=2))
                    manifest["providers"] = {"arc": arc, "count": len(payload["items"])}
                continue
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
                        data = json.loads(src.read_text(encoding="utf-8"))
                        payload = {k: data[k] for k in f["json_keys"] if k in data}
                    except Exception:
                        continue
                    arc = f"{cat}/{i:04d}_extracted.json"
                    zf.writestr(arc, json.dumps(payload, ensure_ascii=False, indent=2))
                    entries.append({"arc": arc, "target": str(src),
                                    "rel_home": out_rel(src),
                                    "json_keys": f["json_keys"]})
                else:
                    arc = _arc_name(cat, i, _rel_home(src) or src.name)
                    zf.write(src, arc)
                    entries.append({"arc": arc, "target": str(src),
                                    "rel_home": out_rel(src)})
            if entries:
                manifest["categories"][cat] = {"entries": entries}

        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    try:
        if password:
            from . import crypto
            staging.write_bytes(crypto.encrypt(staging.read_bytes(), password))
        os.replace(staging, out)
    finally:
        staging.unlink(missing_ok=True)
    return out


def _materialize(path: Path, password: str | None) -> tuple[Path, Path | None]:
    """加密包自动解密到临时 zip；返回 (可读路径, 待清理临时文件或 None)。"""
    from . import crypto
    data = path.read_bytes()
    if not crypto.is_encrypted(data):
        return path, None
    if not password:
        raise ValueError("ENCRYPTED:该配置包已加密，请输入密码")
    plain = crypto.decrypt(data, password)
    ensure_private_dir(IMPORTS_DIR)
    fd, name = tempfile.mkstemp(suffix=".aac", dir=IMPORTS_DIR)
    with os.fdopen(fd, "wb") as fh:
        fh.write(plain)
    return Path(name), Path(name)


def import_preview(path: Path, password: str | None = None) -> dict:
    zip_path, tmp = _materialize(path, password)
    try:
        return _import_preview(zip_path)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def _import_preview(path: Path) -> dict:
    archive.inspect_zip(path, archive.AAC_LIMITS)
    with zipfile.ZipFile(path) as zf:
        manifest = _read_manifest(zf)
    agent = _manifest_agent(manifest)
    roots = allowed_import_roots(agent)
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
            pdir = categories.package_dir(agent)
            targets = [relative_target(pdir, e.get("rel")) if pdir else None
                       for e in info["entries"]]
            ow = sum(t is not None and t.is_file() for t in targets)
            sk = sum(t is None for t in targets)
            conflicts[cat] = {"overwrite": ow, "new": len(targets) - ow - sk,
                              "skip": sk}
            continue
        ow = nw = sk = 0
        for e in info["entries"]:
            if e.get("rel_home"):
                e = dict(e, rel_home=_remap_entry_rel(agent, manifest, e["rel_home"]))
            target = _resolve_target(e, roots)
            if target is None:
                sk += 1
            elif target.is_file():
                ow += 1     # 覆盖（json_keys 为合并）；导入前都会先备份 .bak-aac-*
            else:
                nw += 1
        conflicts[cat] = {"overwrite": ow, "new": nw, "skip": sk}
    if manifest.get("providers"):
        conflicts["providers"] = {
            "overwrite": int(manifest["providers"].get("count", 0)), "new": 0, "skip": 0}
    return {"manifest": manifest, "warnings": warnings, "conflicts": conflicts}


def _resolve_target(entry: dict, allowed_roots: list[Path]) -> Path | None:
    """Confine import targets to HOME and this agent's registered paths."""
    if entry.get("rel_home") is not None:
        target = relative_target(HOME, entry["rel_home"])
    else:
        target_value = entry.get("target")
        if not isinstance(target_value, str) or not target_value or "\0" in target_value:
            return None
        try:
            path = Path(target_value)
            if not path.is_absolute():
                return None
            relative = path.relative_to(HOME).as_posix()
            target = relative_target(HOME, relative)
        except (OSError, RuntimeError, ValueError):
            return None
    if target is None:
        return None
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if any(resolved == root or root in resolved.parents for root in allowed_roots):
        return target
    return None


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


def _remap_entry_rel(agent: dict, manifest: dict, rel_home: str) -> str:
    """导入时把相对 home 路径先换目录布局，再处理 Claude 编码目录。"""
    if not rel_home:
        return rel_home
    rel = _map_platform_rel(
        _platform_dirs(agent, manifest), rel_home,
        manifest.get("path_platform") or manifest.get("source_os"), _os_name())
    return _map_rel_home(manifest.get("agent_id"), rel, manifest.get("source_home"))


def _replace_path_prefix(text: str, src: str, dst: str, target_os: str) -> str:
    """替换绝对路径前缀，并把紧随其后的路径分隔符规范成目标系统。"""
    if not src or not dst:
        return text
    variants = [(src, False)]
    if "\\" in src:
        variants.append((src.replace("\\", "\\\\"), True))
    for value, escaped in variants:
        target = dst.replace("\\", "\\\\") if escaped else dst
        pattern = re.escape(value) + r"([A-Za-z0-9_.\-/\\]*)"

        def repl(match: re.Match) -> str:
            tail = match.group(1)
            if target_os == "Windows":
                tail = tail.replace("/", "\\\\" if escaped else "\\")
            else:
                if escaped:
                    tail = tail.replace("\\\\", "\0").replace("\\", "/").replace("\0", "/")
                else:
                    tail = tail.replace("\\", "/")
            return target + tail

        text = re.sub(pattern, repl, text)
    return text


def _remap_text(text: str, src_home: str | None,
                pairs: list[tuple[str, str]] | None = None) -> str:
    """把文本里的源机器绝对路径替换为本机路径（含 JSON 转义形式）。

    pairs 优先替换更长的配置目录（如 .config/opencode），再兜底替换 home。
    目标分隔符按本机系统规范化，避免 Mac→Win 出现 C:\\Users\\x/dev 这种混写。
    """
    if not src_home:
        return text
    target_os = _os_name()
    dst = str(HOME)
    for src, target in sorted(pairs or [], key=lambda p: len(p[0]), reverse=True):
        text = _replace_path_prefix(text, src, target, target_os)
    if src_home != dst:
        text = _replace_path_prefix(text, src_home, dst, target_os)
    return text


_TEXT_EXTS = {".json", ".jsonl", ".toml", ".md", ".txt", ".yaml", ".yml", ".sh",
              ".js", ".mjs", ".py", ".xml", ".ini", ".cfg"}


def _backup(p: Path) -> None:
    if p.is_file():
        suffix = time.strftime(".bak-aac-%Y%m%d-%H%M%S")
        index = 0
        while True:
            bak = p.with_name(p.name + suffix + (f"-{index}" if index else ""))
            try:
                with bak.open("xb") as dst, p.open("rb") as src:
                    shutil.copyfileobj(src, dst)
                shutil.copystat(p, bak)
                return
            except FileExistsError:
                index += 1


def import_apply(path: Path, selected: list[str], log: list[str],
                 password: str | None = None) -> None:
    zip_path, tmp = _materialize(path, password)
    try:
        _import_apply(zip_path, selected, log)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def _import_apply(path: Path, selected: list[str], log: list[str]) -> None:
    archive.inspect_zip(path, archive.AAC_LIMITS)
    with zipfile.ZipFile(path) as zf:
        manifest = _read_manifest(zf)
        agent = _manifest_agent(manifest)
        roots = allowed_import_roots(agent)
        src_home = manifest.get("source_home")
        pairs = _content_path_pairs(agent, manifest)
        applied = []
        for cat in selected:
            if cat == "providers":
                info = manifest.get("providers")
                if not info or not info.get("arc"):
                    log.append("[跳过] 分类 providers 不在包中\n")
                    continue
                try:
                    payload = json.loads(zf.read(info["arc"]).decode("utf-8"))
                except Exception as e:  # noqa: BLE001
                    log.append(f"[失败] providers：读取失败 {e}\n")
                    continue
                added = providers.import_payload(manifest["agent_id"], payload)
                log.append(f"[完成] providers：导入 {len(payload.get('items') or [])} 个供应商"
                           f"（新增 {added} 个，同名合并）\n")
                applied.append(cat)
                continue
            info = manifest["categories"].get(cat)
            if not info:
                log.append(f"[跳过] 分类 {cat} 不在包中\n")
                continue
            if cat == "patches":
                pdir = categories.package_dir(agent)
                if not pdir:
                    log.append(f"[失败] patches：本机找不到安装目录\n")
                    continue
                skipped = 0
                for e in info["entries"]:
                    target = relative_target(pdir, e.get("rel"))
                    if target is None:
                        skipped += 1
                        log.append(f"[跳过] 不安全的补丁路径: {e.get('rel')}\n")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _backup(target)
                    data = zf.read(e["arc"])
                    if target.suffix.lower() in _TEXT_EXTS:
                        try:
                            data = _remap_text(data.decode("utf-8"), src_home, pairs).encode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    target.write_bytes(data)
                log.append(f"[完成] patches：应用 {len(info['entries']) - skipped} 个补丁文件"
                           f"（跳过 {skipped} 个不安全路径）\n")
                applied.append(cat)
                continue
            skipped = 0
            for e in info["entries"]:
                if e.get("rel_home"):
                    e = dict(e, rel_home=_remap_entry_rel(agent, manifest, e["rel_home"]))
                target = _resolve_target(e, roots)
                if target is None:
                    skipped += 1
                    log.append(f"[跳过] 不安全或无法映射的目标路径: {e.get('rel_home', e.get('target'))}\n")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if e.get("json_keys"):
                    payload = json.loads(_remap_text(
                        zf.read(e["arc"]).decode("utf-8", "replace"), src_home, pairs))
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
                            data = _remap_text(data.decode("utf-8"), src_home, pairs).encode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    target.write_bytes(data)
            log.append(f"[完成] {cat}：应用 {len(info['entries']) - skipped} 个文件"
                       + (f"（跳过 {skipped} 个不安全或无法映射的路径）" if skipped else "") + "\n")
            applied.append(cat)
        if src_home and src_home != str(HOME):
            log.append(f"已把配置内容中的路径 {src_home} 映射为 {HOME}\n")
        log.append(f"导入结束，共应用 {len(applied)} 个分类：{', '.join(applied)}\n")
