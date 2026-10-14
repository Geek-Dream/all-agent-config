"""已安装检测、当前版本 / 最新版本查询、安装位置（用户/全局）识别。"""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import installer
from .installer import IS_WINDOWS, install_prefix_of

SEMVER_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?")

# 扫描安装位置时检查的常见 npm 全局前缀
if IS_WINDOWS:
    PREFIX_CANDIDATES = [
        Path(os.environ.get("APPDATA", "")) / "npm",          # npm 全局默认前缀
        Path(os.environ.get("LOCALAPPDATA", "")) / "npm",
        Path.home() / ".aac" / "npm",
        Path.home() / ".local",
    ]
else:
    PREFIX_CANDIDATES = [
        Path("/usr/local"),
        Path("/opt/homebrew"),
        Path.home() / ".local",
        Path.home() / ".aac" / "npm",
        Path("/usr"),
    ]


def _has_command(d: Path, command: str) -> bool:
    """目录下是否存在该命令（Windows 的 npm 全局 shim 是 .cmd/.exe，且直接在前缀根目录）。"""
    if IS_WINDOWS:
        return any((d / f).is_file()
                   for f in (command, f"{command}.cmd", f"{command}.exe", f"{command}.bat"))
    return (d / command).is_file()


def _scope_of(prefix: Path) -> str:
    """家目录下的前缀算用户安装，系统目录算全局安装。"""
    try:
        prefix.resolve().relative_to(Path.home())
        return "user"
    except ValueError:
        return "global"


def resolve_command(agent: dict) -> str | None:
    """找到该工具实际可用的二进制路径：先 PATH，再注册表声明的 bin_paths。"""
    cmd = shutil.which(agent["command"])
    if cmd:
        return cmd
    names = ([agent["command"], f"{agent['command']}.cmd", f"{agent['command']}.exe"]
             if IS_WINDOWS else [agent["command"]])
    for d in agent.get("bin_paths") or []:
        for name in names:
            p = Path(d).expanduser() / name
            if p.is_file():
                return str(p)
    apps_root = Path.home() / ".aac" / "apps"      # 压缩包安装位置
    if apps_root.is_dir():
        for d in sorted(apps_root.glob("*/bin")):
            for name in names:
                p = d / name
                if p.is_file():
                    return str(p)
    return None


def find_installs(command: str, bin_paths: list[str] | None = None) -> list[dict]:
    """在 PATH、注册表 bin_paths 和所有常见前缀里找该命令的安装位置。

    返回 [{"prefix", "scope", "primary", "needs_sudo"}]，第一项为 PATH 里实际生效的安装。
    """
    found: list[dict] = []
    seen: set[str] = set()

    def add(prefix: Path, primary: bool) -> None:
        key = str(prefix)
        if key in seen:
            return
        seen.add(key)
        needs_sudo = (not IS_WINDOWS) and not os.access(
            prefix / ("Scripts" if IS_WINDOWS else "bin"), os.W_OK)
        found.append({"prefix": key, "scope": _scope_of(prefix),
                      "primary": primary, "needs_sudo": needs_sudo})

    if shutil.which(command):
        prefix = install_prefix_of(command)
        if prefix is None:
            # 兜底：unix 是 …/prefix/bin/cmd，Windows 是 …/prefix/cmd(.cmd)
            bin_dir = Path(shutil.which(command)).resolve().parent
            prefix = bin_dir if IS_WINDOWS else bin_dir.parent
        add(prefix, primary=True)
    for d in bin_paths or []:                       # 注册表声明的额外安装位置（如 ~/.kimi-code/bin）
        dp = Path(d).expanduser()
        if _has_command(dp, command):
            add(dp.parent, primary=not found)
    for p in PREFIX_CANDIDATES:
        bin_dir = p if IS_WINDOWS else p / "bin"    # Windows npm shim 直接在前缀根目录
        if _has_command(bin_dir, command):
            add(p, primary=not found)
    apps_root = Path.home() / ".aac" / "apps"       # 压缩包安装位置
    if apps_root.is_dir():
        for d in sorted(apps_root.glob("*/bin")):
            if _has_command(d, command):
                add(d.parent, primary=not found)
    return found


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def detect_install_method(agent: dict) -> str | None:
    """返回安装方式：npm-global / other / None（未安装）。"""
    npm_pkg = (agent.get("sources") or {}).get("npm")
    if npm_pkg and shutil.which("npm"):
        out = _run(["npm", "ls", "-g", "--depth=0", "--json"], timeout=30)
        try:
            deps = (json.loads(out or "{}")).get("dependencies", {})
            if npm_pkg in deps:
                return "npm-global"
        except json.JSONDecodeError:
            pass
    if resolve_command(agent):
        return "other"
    # PATH 之外的位置（注册表 bin_paths、常见前缀、~/.aac/apps 压缩包安装）
    if find_installs(agent["command"], agent.get("bin_paths")):
        return "other"
    return None


def current_version(agent: dict) -> str | None:
    binpath = resolve_command(agent)
    if not binpath:
        return None
    args = agent["version_cmd"].split()
    out = _run([binpath, *args[1:]], timeout=15)
    m = SEMVER_RE.search(out)
    return m.group(0) if m else None


def latest_version(agent: dict) -> str | None:
    npm_pkg = (agent.get("sources") or {}).get("npm")
    if npm_pkg and shutil.which("npm"):
        # 注：npm 的警告会写到 stderr，与 stdout 拼接后需逐行匹配，
        # 取第一行形如 semver 的输出版本号
        out = _run(["npm", "view", npm_pkg, "version"], timeout=20)
        for line in out.splitlines():
            line = line.strip()
            if SEMVER_RE.fullmatch(line):
                return line
    brew = (agent.get("sources") or {}).get("brew")
    if brew and shutil.which("brew"):
        out = _run(["brew", "info", "--json=v2", brew.split("/")[-1]], timeout=30)
        try:
            return json.loads(out)[0]["versions"]["stable"]
        except Exception:
            return None
    return None


def version_key(v: str | None) -> tuple:
    """把 semver 转成可比较的元组；无法解析时返回 ()，表示不可比较。"""
    if not v:
        return ()
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else ()


def is_updatable(current: str | None, latest: str | None) -> bool:
    ck, lk = version_key(current), version_key(latest)
    return bool(ck and lk and lk > ck)


def scan_one(agent: dict) -> dict:
    method = detect_install_method(agent)
    installed = method is not None
    cur = current_version(agent) if installed else None
    latest = latest_version(agent) if installed else None
    installs = find_installs(agent["command"], agent.get("bin_paths")) if installed else []
    primary = installs[0] if installs else None
    if method == "other" and primary and (agent.get("sources") or {}).get("npm"):
        method = "npm-global"   # 前缀探测确认是 npm 安装，只是 PATH 里的 npm 不认识它
    needs_sudo = primary["needs_sudo"] if primary else False
    return {
        "id": agent["id"],
        "name": agent["name"],
        "vendor": agent.get("vendor", ""),
        "installed": installed,
        "method": method,
        "current_version": cur,
        "latest_version": latest,
        "updatable": is_updatable(cur, latest),
        "scope": primary["scope"] if primary else None,          # user | global | None
        "install_prefix": primary["prefix"] if primary else None,
        "install_count": len(installs),
        "needs_sudo": needs_sudo,
        "installs": installs,                                    # 所有安装位置（含未使用的副本）
        "auto_install": installer.available_source(agent) is not None,
        "managed": bool(agent.get("managed_files")),       # 可安全卸载程序文件（独立二进制）
        "install_hint": (agent.get("sources") or {}).get("manual"),
        "homepage": agent.get("homepage", ""),
        "notes": agent.get("notes", ""),
        "config_dirs": agent.get("config_dirs", []),
        "session_dirs": agent.get("session_dirs", []),
    }


async def scan_all(agents: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*[asyncio.to_thread(scan_one, a) for a in agents]))
