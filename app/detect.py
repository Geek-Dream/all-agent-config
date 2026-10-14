"""已安装检测、当前版本 / 最新版本查询。"""

import asyncio
import json
import re
import shutil
import subprocess

SEMVER_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?")


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
    if shutil.which(agent["command"]):
        return "other"
    return None


def current_version(agent: dict) -> str | None:
    out = _run(agent["version_cmd"].split(), timeout=15)
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
    return {
        "id": agent["id"],
        "name": agent["name"],
        "vendor": agent.get("vendor", ""),
        "installed": installed,
        "method": method,
        "current_version": cur,
        "latest_version": latest,
        "updatable": is_updatable(cur, latest),
        "auto_install": "npm" in (agent.get("sources") or {}),
        "install_hint": (agent.get("sources") or {}).get("manual"),
        "notes": agent.get("notes", ""),
        "config_dirs": agent.get("config_dirs", []),
        "session_dirs": agent.get("session_dirs", []),
    }


async def scan_all(agents: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*[asyncio.to_thread(scan_one, a) for a in agents]))
