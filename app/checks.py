"""跨系统体检：把「换系统一定跑不起来」的 hook / 脚本提前指出来。

只在导入之后跑一次，结果写进日志和接口返回值。设计原则：
  - 只报警，不改用户文件（改坏了比不改更糟）；
  - 每条问题都带 文件 / 行号 / 命令 / 原因 / 建议，用户能直接照着改；
  - 任何异常吞掉，绝不影响导入本身。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".ps1", ".cmd", ".bat"}
MAX_SCAN_BYTES = 2 * 1024 * 1024        # 单个脚本最多看 2MB
MAX_FILES = 400                          # 最多扫 400 个脚本，防止 skill 目录过大卡住

# (正则, 原因, 建议) —— 命中即记一条
_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"/bin/(zsh|bash|sh)\b|/usr/bin/env\s+(zsh|bash|sh)\b"),
     "依赖 bash/zsh", "Windows 需装 Git Bash，或改写为 PowerShell / .cmd"),
    (re.compile(r"\bosascript\b|\bpbcopy\b|\bpbpaste\b|\bsw_vers\b|\blaunchctl\b"),
     "macOS 专有命令", "Windows 上没有对应命令，需删除或换成等价实现"),
    (re.compile(r"\bbrew\b\s+(install|list|upgrade)"),
     "依赖 Homebrew", "Windows 用 winget / scoop，或手动安装依赖"),
    (re.compile(r"\bsudo\b"),
     "依赖 sudo", "Windows 没有 sudo，需用管理员终端执行"),
    (re.compile(r"[A-Za-z]:\\\\?(Users|Documents and Settings)\\\\"),
     "写死了 Windows 用户目录", "路径应改用 $HOME / %USERPROFILE% 变量"),
]

_USERS_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")


def _line_of(text: str, index: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, index) + 1
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return line_no, text[start:end if end != -1 else len(text)].strip()


def _script_files(agent: dict) -> list[Path]:
    """扫 agent 配置目录里的脚本文件（有限深度、有限数量）。"""
    roots: list[Path] = []
    for raw in agent.get("config_dirs") or []:
        p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if p.is_dir():
            roots.append(p)
    files: list[Path] = []
    for root in roots:
        count = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if d not in ("node_modules", ".git", "sessions", "logs",
                                        "__pycache__", ".venv", "cache")]
            for name in filenames:
                if Path(name).suffix.lower() in SCRIPT_SUFFIXES:
                    files.append(Path(dirpath) / name)
                    count += 1
                    if count > MAX_FILES or len(files) > MAX_FILES:
                        return files
    return files


def _json_hook_commands(path: Path) -> list[tuple[int, str]]:
    """从 settings.json 这类文件里掏出 hooks 里的命令字符串。"""
    import json

    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    found: list[tuple[int, str]] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and key in ("command", "cmd", "run", "shell"):
                    found.append((0, value))
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data)
    return found


def audit_hooks(agent: dict, source_os: str | None = None,
                local_home: str | None = None) -> list[dict]:
    """返回问题清单：[{file, line, command, issue, advice}]。"""
    issues: list[dict] = []
    home_name = Path(local_home or os.path.expanduser("~")).name
    scanned: set[str] = set()

    for path in _script_files(agent):
        if str(path) in scanned:
            continue
        scanned.add(str(path))
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, issue, advice in _RULES:
            for match in pattern.finditer(text):
                line_no, line = _line_of(text, match.start())
                issues.append({"file": str(path), "line": line_no, "command": line,
                               "issue": issue, "advice": advice})
        for match in _USERS_PATH.finditer(text):
            if match.group(1) == home_name:
                continue
            line_no, line = _line_of(text, match.start())
            issues.append({"file": str(path), "line": line_no, "command": line,
                           "issue": f"路径指向别的机器（{match.group(0)}）",
                           "advice": "改用 $HOME / ~ 变量，或改成当前用户路径"})

    # hooks 写在 JSON 配置里的命令
    for raw in agent.get("config_dirs") or []:
        root = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        candidate = root / "settings.json"
        if not candidate.is_file():
            continue
        for _, command in _json_hook_commands(candidate):
            for pattern, issue, advice in _RULES:
                if pattern.search(command):
                    issues.append({"file": str(candidate), "line": 0, "command": command,
                                   "issue": issue, "advice": advice})
    return issues


def format_warnings(issues: list[dict], limit: int = 12) -> list[str]:
    """把问题清单变成日志行：前 N 条细说，其余只报数量。"""
    lines: list[str] = []
    for item in issues[:limit]:
        location = item["file"]
        if item.get("line"):
            location += f":{item['line']}"
        lines.append(f"  ⚠ {item['issue']} —— {location}\n"
                     f"     命令: {item['command'][:160]}\n"
                     f"     建议: {item['advice']}\n")
    if len(issues) > limit:
        lines.append(f"  …… 还有 {len(issues) - limit} 条类似问题，建议逐个手动核对\n")
    return lines
