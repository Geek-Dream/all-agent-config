"""npm 包最新版的 engines.node 查询与校验。

每个工具的 Node 下限不写死：安装/更新前读取 npm registry 上最新版的
engines.node（缓存 6 小时，离线时沿用旧值），再和本机 node --version 比较。
只实现 npm semver 常用子集（>= > <= < = ^ ~ * x 空格 AND || OR），
无法解析的范围一律放行，避免误拦。
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_PATH = Path.home() / ".aac" / "engines.json"
TTL_SECONDS = 6 * 60 * 60
TIMEOUT_SECONDS = 4
USER_AGENT = "all-agent-config/0.2"

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_COMPARATOR_RE = re.compile(r"^(\^|~|>=|<=|>|<|=)?\s*(.+)$")

_node_memo: tuple[float, str | None] = (0.0, None)


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    """'22.19.0' / 'v22.19' / '22' → (22, 19, 0)；无法解析返回 None。"""
    if not isinstance(value, str):
        return None
    m = _VERSION_RE.match(value.strip())
    if not m:
        return None
    major, minor, patch = (int(g) if g else 0 for g in m.groups())
    return (major, minor, patch)


def _expand(op: str | None, version: tuple[int, int, int],
            parts: int) -> list[tuple[str, tuple[int, int, int]]] | None:
    major, minor, patch = version
    if op == "^":
        if major > 0:
            return [(">=", version), ("<", (major + 1, 0, 0))]
        if minor > 0:
            return [(">=", version), ("<", (0, minor + 1, 0))]
        return [(">=", version), ("<", (0, 0, patch + 1))]
    if op == "~":
        if parts >= 2:
            return [(">=", version), ("<", (major, minor + 1, 0))]
        return [(">=", version), ("<", (major + 1, 0, 0))]
    if op in (">=", "<=", ">", "<"):
        return [(op, version)]
    if op == "=":
        return [("=", version)]
    if parts >= 3:
        return [("=", version)]
    if parts == 2:
        return [(">=", version), ("<", (major, minor + 1, 0))]
    return [(">=", version), ("<", (major + 1, 0, 0))]


def _compare(have: tuple[int, int, int], op: str,
             want: tuple[int, int, int]) -> bool:
    if op == ">=":
        return have >= want
    if op == "<=":
        return have <= want
    if op == ">":
        return have > want
    if op == "<":
        return have < want
    return have == want


def satisfies(version: str | None, range_text: str | None) -> bool | None:
    """本机版本是否满足 npm range；无法解析返回 None（调用方按放行处理）。"""
    have = parse_version(version)
    if have is None or not isinstance(range_text, str) or not range_text.strip():
        return None
    for group in range_text.split("||"):
        group = group.strip()
        if not group:
            return True
        comparators = group.split()
        predicates: list[tuple[str, tuple[int, int, int]]] = []
        parsed = True
        for token in comparators:
            token = token.strip()
            if token in ("*", "x", "X"):
                continue
            m = _COMPARATOR_RE.match(token)
            if not m:
                parsed = False
                break
            op, raw = m.group(1), m.group(2).strip()
            if raw.lower() in ("x", "*"):
                continue
            vm = _VERSION_RE.match(raw)
            if not vm:
                parsed = False
                break
            parts = len([g for g in vm.groups() if g is not None])
            expanded = _expand(op, parse_version(raw), parts)
            if expanded is None:
                parsed = False
                break
            predicates.extend(expanded)
        if not parsed:
            return None
        if all(_compare(have, op, want) for op, want in predicates):
            return True
    return False


def node_version() -> str | None:
    """本机 node 版本（60 秒内复用），未安装返回 None。"""
    global _node_memo
    now = time.time()
    if now - _node_memo[0] < 60:
        return _node_memo[1]
    path = shutil.which("node")
    version = None
    if path:
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10)
            text = ((out.stdout or "") + (out.stderr or "")).strip()
            if parse_version(text):
                version = text.lstrip("vV")
        except (OSError, subprocess.SubprocessError):
            version = None
    _node_memo = (now, version)
    return version


def load_cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def _fetch_range(package: str) -> str | None:
    """读取 npm registry 上 latest 的 engines.node；失败返回 None。"""
    url = ("https://registry.npmjs.org/"
           + urllib.parse.quote(package, safe="@") + "/latest")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    engines = data.get("engines") if isinstance(data, dict) else None
    value = engines.get("node") if isinstance(engines, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _npm_packages(agents: list[dict]) -> set[str]:
    return {
        (agent.get("sources") or {})["npm"]
        for agent in agents
        if isinstance((agent.get("sources") or {}).get("npm"), str)
    }


def refresh(agents: list[dict]) -> dict:
    """刷新过期缓存；网络失败时保留旧值。返回最新缓存。"""
    cache = load_cache()
    now = time.time()
    changed = False
    for package in sorted(_npm_packages(agents)):
        entry = cache.get(package)
        if isinstance(entry, dict) and now - float(entry.get("at") or 0) < TTL_SECONDS:
            continue
        value = _fetch_range(package)
        if value is None:
            continue
        cache[package] = {"range": value, "at": now}
        changed = True
    if changed:
        save_cache(cache)
    return cache


def stale(agents: list[dict]) -> bool:
    cache = load_cache()
    now = time.time()
    for package in _npm_packages(agents):
        entry = cache.get(package)
        if not isinstance(entry, dict):
            return True
        if now - float(entry.get("at") or 0) >= TTL_SECONDS:
            return True
    return False


def required_range(agent: dict, *, refresh_cache: bool = False) -> str | None:
    package = (agent.get("sources") or {}).get("npm")
    if not isinstance(package, str):
        return None
    cache = load_cache()
    entry = cache.get(package)
    stale_entry = not isinstance(entry, dict) or (
        time.time() - float(entry.get("at") or 0) >= TTL_SECONDS)
    if refresh_cache and stale_entry:
        value = _fetch_range(package)
        if value is not None:
            cache[package] = {"range": value, "at": time.time()}
            save_cache(cache)
            return value
    return entry.get("range") if isinstance(entry, dict) else None


def check(agent: dict, *, refresh_cache: bool = False) -> dict | None:
    """返回 {required, have, ok}；没有 npm 源或没有缓存时返回 None。"""
    required = required_range(agent, refresh_cache=refresh_cache)
    if not required:
        return None
    have = node_version()
    result = satisfies(have, required)
    return {
        "required": required,
        "have": have,
        "ok": True if result is None else result,
    }
