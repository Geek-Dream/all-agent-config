"""配置分类扫描 + 程序级修改（补丁）检测。

- 分类内容扫描：按 agents.yaml 里每个工具的 categories 定义统计各类别文件
- 补丁检测：对 npm 安装的工具，下载同版本官方包，逐文件比对 sha256，
  找出用户对程序本体的修改（汉化补丁、进度条魔改等）和新增文件
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import archive

CATEGORY_ORDER = [
    "system", "hooks", "skills", "mcp", "theme", "custom",
    "providers", "patches", "conversations",
]
CATEGORY_LABELS = {
    "system": "系统配置",
    "hooks": "Hooks 钩子",
    "skills": "Skills 技能",
    "mcp": "MCP 服务器",
    "theme": "主题 / 美化",
    "custom": "自定义个人配置",
    "providers": "供应商配置（API 端点 / 密钥）",
    "patches": "程序级修改（补丁）",
    "conversations": "对话历史",
}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 跳过大的二进制


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


_WIN_ENV = re.compile(r"%([^%]+)%")


def expand(p: str) -> Path:
    """展开路径：Unix ~ 与 $VAR/${VAR}，外加 Windows %VAR% 写法（跨平台注册表共用）。"""
    p = _WIN_ENV.sub(lambda m: os.environ.get(m.group(1), m.group(0)), p)
    return Path(os.path.expandvars(p)).expanduser()


SKIP_DIR_NAMES = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".tmp", ".cache"}


def iter_files(base: Path):
    """产出 base 下所有真实文件（自动跳过 node_modules/.git 等目录）。"""
    if base.is_file() and not base.is_symlink():
        yield base
    elif base.is_dir():
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            if SKIP_DIR_NAMES.intersection(f.relative_to(base).parts[:-1]):
                continue
            yield f


def scan_categories(agent: dict) -> dict:
    """返回每个分类的文件清单与体积（patches 除外，单独走 detect_patches）。"""
    raw = agent.get("categories") or {}
    result: dict[str, dict] = {}
    for cat in CATEGORY_ORDER:
        if cat in ("patches", "providers", "conversations"):
            continue
        files: list[dict] = []
        size = 0
        for e in raw.get(cat) or []:
            entry = {"path": e} if isinstance(e, str) else dict(e)
            p = expand(entry["path"])
            if entry.get("json_keys"):
                if p.is_file():
                    sz = p.stat().st_size
                    files.append({"path": str(p), "json_keys": entry["json_keys"], "size": sz})
                    size += sz
            else:
                for f in iter_files(p):
                    sz = f.stat().st_size
                    files.append({"path": str(f), "size": sz})
                    size += sz
        result[cat] = {"files": files, "size": size}
    # 对话历史（带修改时间，导出时可按时间范围筛选）
    files, size = [], 0
    for d in agent.get("session_dirs") or []:
        for f in iter_files(expand(d)):
            st = f.stat()
            files.append({"path": str(f), "size": st.st_size, "mtime": int(st.st_mtime)})
            size += st.st_size
    result["conversations"] = {"files": files, "size": size}
    return result


# ---------------------------------------------------------------- 补丁检测

def npm_root() -> Path | None:
    if not shutil.which("npm"):
        return None
    out = _run(["npm", "root", "-g"], timeout=15).strip().splitlines()
    for line in reversed(out):
        line = line.strip()
        if line and os.path.isdir(line):
            return Path(line)
    return None


def package_dir(agent: dict) -> Path | None:
    """定位工具的安装目录。

    不依赖当前 npm 的 prefix（机器上可能有多个 node 环境），
    而是通过 PATH 中的命令解析符号链接，向上找到 package.json 名字匹配的目录。
    """
    pkg = (agent.get("sources") or {}).get("npm")
    cmd = shutil.which(agent.get("command", ""))
    if not pkg or not cmd:
        return None
    p = Path(cmd).resolve()
    for d in [p, *p.parents]:
        pj = d / "package.json"
        if pj.is_file():
            try:
                if json.loads(pj.read_text()).get("name") == pkg:
                    return d
            except Exception:
                continue
    return None


def installed_pkg_version(pdir: Path) -> str | None:
    try:
        return json.loads((pdir / "package.json").read_text()).get("version")
    except Exception:
        return None


def _tarball_url(pkg: str, version: str) -> str | None:
    out = _run(["npm", "view", f"{pkg}@{version}", "dist.tarball"], timeout=30)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("http"):
            return line
    return None


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    archive.extract_tar(tar, dest, archive.DEFAULT_LIMITS)


def detect_patches(agent: dict) -> dict | None:
    """与同版本官方包逐文件比对，返回被修改 / 新增的程序文件。"""
    pkg = (agent.get("sources") or {}).get("npm")
    pdir = package_dir(agent)
    if not pkg or not pdir:
        return None
    version = installed_pkg_version(pdir)
    if not version:
        return None
    url = _tarball_url(pkg, version)
    if not url:
        return {"pkg": pkg, "pkg_version": version, "modified": [], "added": [],
                "error": "无法获取官方包下载地址"}
    try:
        with tempfile.TemporaryDirectory() as td:
            tgz = Path(td) / "pkg.tgz"
            urllib.request.urlretrieve(url, tgz)  # noqa: S310
            with tarfile.open(tgz, "r:gz") as tar:
                _safe_extract(tar, Path(td))
            pristine = Path(td) / "package"
            if not pristine.is_dir():
                return None

            pristine_files = {f.relative_to(pristine): f for f in iter_files(pristine)}
            modified, added = [], []
            for rel, pf in pristine_files.items():
                if pf.stat().st_size > MAX_FILE_SIZE:
                    continue
                inst = pdir / rel
                if inst.is_file() and not inst.is_symlink():
                    if inst.stat().st_size <= MAX_FILE_SIZE and _sha256(inst) != _sha256(pf):
                        modified.append(str(rel))
            inst_files = {f.relative_to(pdir) for f in iter_files(pdir)}
            for rel in inst_files:
                if (pdir / rel).stat().st_size <= MAX_FILE_SIZE and rel not in pristine_files:
                    if rel.parts and rel.parts[0] in ("node_modules", ".bin"):
                        continue
                    added.append(str(rel))
            return {"pkg": pkg, "pkg_version": version,
                    "modified": sorted(modified), "added": sorted(added)}
    except Exception as e:  # noqa: BLE001
        return {"pkg": pkg, "pkg_version": version, "modified": [], "added": [],
                "error": str(e)}


def agent_detail(agent: dict) -> dict:
    from . import detect
    return {
        "agent": {"id": agent["id"], "name": agent["name"]},
        "agent_version": detect.current_version(agent),
        "categories": scan_categories(agent),
        "patches": detect_patches(agent),
    }
