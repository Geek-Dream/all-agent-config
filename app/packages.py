"""引用包自愈：配置导入后，把 settings 里提到的扩展包真的装回来。

背景：`.aac` 里只带配置和本地目录扩展，`node_modules/` 与 `.git/` 一律不打包。
所以换机导入后，`settings.json` 里那串 `npm:xxx` / `git:xxx` 只是名字，
扩展本体并不存在 —— 表现就是「状态栏没了、插件没反应」。

顺序（越靠前越优先，全失败才报缺）：
  1. `pi update --extensions`  —— pi 自己的人 reconciler，最可靠
  2. 对还缺的 npm 包跑 `npm install -g <pkg>` —— 有些包只能靠 npm
  3. 仍然缺 → 逐条告诉用户缺什么、该敲哪条命令

所有命令都是 argv 列表、无 shell；找不到可执行文件时只记一句，不抛异常。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

PI_HOME = Path(os.path.expanduser("~")) / ".pi" / "agent"
NPM_ROOT = PI_HOME / "npm" / "node_modules"
GIT_ROOT = PI_HOME / "git"
CMD_TIMEOUT = 900
NPM_TIMEOUT = 600


def _log(log: list[str] | None, text: str) -> None:
    if log is not None:
        log.append(text)


def _run(cmd: list[str], log: list[str] | None, timeout: int) -> tuple[int, str]:
    """跑一条命令，返回 (rc, 输出尾部)。异常一律转成 rc=-1。"""
    _log(log, f"  $ {' '.join(cmd)}\n")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except FileNotFoundError:
        return -1, f"找不到命令：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, f"超时（{timeout}s）：{' '.join(cmd)}"
    except OSError as e:
        return -1, f"执行失败：{e}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = "\n".join(out.splitlines()[-6:])
    if tail:
        _log(log, "".join(f"    {ln}\n" for ln in tail.splitlines()))
    return proc.returncode, tail


def pi_settings_path() -> Path:
    return PI_HOME / "settings.json"


def referenced_packages(agent: dict) -> list[dict]:
    """从本地 settings.json 里读出被引用的包：[{source, kind, name, raw}]。"""
    if agent.get("id") != "pi":
        return []
    try:
        data = json.loads(pi_settings_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    packages = data.get("packages")
    if not isinstance(packages, list):
        return []
    out: list[dict] = []
    for item in packages:
        raw = item.get("source") if isinstance(item, dict) else item
        if not isinstance(raw, str) or not raw.strip():
            continue          # 本地目录扩展是真实文件，跟着包走了，不用重装
        source = raw.strip()
        if source.startswith("npm:"):
            kind, name = "npm", source[4:].strip()
        elif source.startswith("git:"):
            kind, name = "git", source[4:].strip()
        else:
            continue          # 其余形式（本地绝对路径等）不处理
        out.append({"source": source, "kind": kind, "name": name, "raw": raw})
    return out


def _git_rel(name: str) -> Path:
    """git:https://host/a/b(.git) → <git_root>/host/a/b"""
    text = name
    if "://" in text:                      # git:https://host/... → host/...
        text = text.split("://", 1)[1]
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    # pi 的实际布局会把内层（镜像）协议的 "https:" 原样当一个目录名，
    # 例如 git/ghfast.top/https:/github.com/obra/superpowers —— 不要动它。
    return GIT_ROOT.joinpath(*[part for part in text.split("/") if part])


def installed_path(pkg: dict) -> Path | None:
    if pkg["kind"] == "npm":
        return NPM_ROOT / pkg["name"]
    return _git_rel(pkg["name"])


def is_installed(pkg: dict) -> bool:
    path = installed_path(pkg)
    return bool(path) and path.exists()


def missing_packages(agent: dict) -> tuple[list[dict], list[dict]]:
    pkgs = referenced_packages(agent)
    return pkgs, [p for p in pkgs if not is_installed(p)]


def reconcile(agent: dict, log: list[str] | None = None) -> dict:
    """把缺的引用包补上；返回 {checked, ok, installed, missing, notes}。"""
    result: dict = {"checked": 0, "ok": [], "installed": [], "missing": [], "notes": []}
    pkgs = referenced_packages(agent)
    result["checked"] = len(pkgs)
    if not pkgs:
        return result

    missing = [p for p in pkgs if not is_installed(p)]
    result["ok"] = [p["source"] for p in pkgs if p not in missing]
    if not missing:
        _log(log, "[引用包] 全部就位，无需处理\n")
        return result
    _log(log, f"[引用包] 缺 {len(missing)} 个："
              + ", ".join(p["source"] for p in missing) + "\n")

    # 第 1 步：pi 自己的 reconciler
    if shutil.which("pi"):
        _log(log, "[引用包] 尝试 1/2：pi update --extensions\n")
        rc, _ = _run(["pi", "update", "--extensions"], log, CMD_TIMEOUT)
        if rc == 0:
            result["installed"] = [p["source"] for p in missing if is_installed(p)]
    else:
        result["notes"].append("未检测到 pi 命令，跳过 pi 自动补装")

    # 第 2 步：还缺的 npm 包直接用 npm 装
    still_missing = [p for p in missing if not is_installed(p)]
    npm_missing = [p for p in still_missing if p["kind"] == "npm"]
    if npm_missing and shutil.which("npm"):
        _log(log, f"[引用包] 尝试 2/2：npm install -g（{len(npm_missing)} 个）\n")
        for pkg in npm_missing:
            _run(["npm", "install", "-g", pkg["name"]], log, NPM_TIMEOUT)
            if is_installed(pkg):
                result["installed"].append(pkg["source"])

    # 第 3 步：兜底清单
    still_missing = [p for p in pkgs if not is_installed(p)]
    for pkg in still_missing:
        result["missing"].append(pkg["source"])
    if still_missing:
        _log(log, f"[引用包] 仍有 {len(still_missing)} 个没装上，需要手动安装：\n")
        for pkg in still_missing:
            _log(log, f"  ✗ {pkg['source']}\n"
                      f"    手动命令：pi install {pkg['source']}\n"
                      f"    缺失影响：对应的插件 / 状态栏不会出现\n")
    result["missing"] = [p["source"] for p in still_missing]
    result["ok"] = [p["source"] for p in pkgs if p not in still_missing]
    return result
